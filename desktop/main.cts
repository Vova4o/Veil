import { app, BrowserWindow, dialog, ipcMain, Menu, net, protocol, session, shell } from 'electron';
import type { IpcMainInvokeEvent, WebContents } from 'electron';
import path from 'node:path';
import { mkdirSync, readFileSync, existsSync, writeFileSync } from 'node:fs';
import { DesktopBackend } from './backend.cjs';

const APP_URL = 'cmv://app';
app.setName('VEIL');
const profile = process.env.COMFY_META_DESKTOP_PROFILE || path.join(app.getPath('appData'), 'Comfy Meta Viewer');
app.setPath('userData', profile);
// Chromium sizes its HTTP cache at ~1% of free disk space and discards the whole cache
// ("Invalid cache (current) size") once free space shrinks below what it already holds.
// Library images are local files behind the backend, so a fixed modest cache is enough.
app.commandLine.appendSwitch('disk-cache-size', String(256 * 1024 * 1024));
mkdirSync(profile, { recursive: true });
protocol.registerSchemesAsPrivileged([{ scheme: 'cmv', privileges: {
    standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true,
} }]);

let window: BrowserWindow | undefined;
let backend: DesktopBackend | undefined;
let stopping = false;
let starting: Promise<void> | undefined;
let failure = '';
const projectRoot = path.resolve(__dirname, '..');

function isAppUrl(value: string): boolean {
    try { const url = new URL(value); return url.protocol === 'cmv:' && url.host === 'app'; }
    catch { return false; }
}

function trustSender(event: IpcMainInvokeEvent): void {
    if (event.sender !== window?.webContents || event.senderFrame !== event.sender.mainFrame || !isAppUrl(event.senderFrame.url)) {
        throw new Error('Untrusted desktop request');
    }
}

async function openExternal(value: string): Promise<void> {
    const url = new URL(value);
    if (url.protocol === 'https:' || url.protocol === 'http:') await shell.openExternal(url.href);
}

function configureNavigation(contents: WebContents): void {
    contents.on('will-navigate', (event, url) => {
        if (!isAppUrl(url)) { event.preventDefault(); void openExternal(url).catch(() => {}); }
    });
    contents.setWindowOpenHandler(({ url }) => {
        if (isAppUrl(url)) return { action: 'allow', overrideBrowserWindowOptions: {
            autoHideMenuBar: true, backgroundColor: '#111216',
            webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true },
        } };
        void openExternal(url).catch(() => {});
        return { action: 'deny' };
    });
    contents.on('did-create-window', child => configureNavigation(child.webContents));
}

function shellPage(): Response {
    const file = path.join(projectRoot, 'desktop', 'shell.html');
    const body = readFileSync(file, 'utf8').replace('<!--STATUS-->', failure
        ? 'The application could not start. Open the log for details, then try again.'
        : 'Starting your library…').replace('<!--ACTIONS-->', failure ? '' : 'hidden');
    return new Response(body, { headers: { 'Content-Type': 'text/html; charset=utf-8',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'" } });
}

async function startBackend(): Promise<void> {
    if (stopping) return;
    if (starting) return starting;
    starting = (async () => {
        failure = '';
        await backend?.stop();
        if (stopping) return;
        backend = new DesktopBackend({ packaged: app.isPackaged, projectRoot,
            resourcesPath: process.resourcesPath, profile, onExit: message => {
                failure = message;
                if (window && !window.isDestroyed()) void window.loadURL(`${APP_URL}/_desktop/shell`);
            } });
        try {
            await backend.start();
            if (!stopping) await window?.loadURL(`${APP_URL}/`);
        } catch (error) {
            failure = String(error);
            backend.log(failure);
            if (!stopping) await window?.loadURL(`${APP_URL}/_desktop/shell`);
        }
    })();
    try { await starting; } finally { starting = undefined; }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
    app.on('second-instance', () => { if (window?.isMinimized()) window.restore(); window?.focus(); });
    app.on('before-quit', event => {
        if (stopping) return;
        event.preventDefault();
        stopping = true;
        window?.destroy();
        void (async () => { await backend?.stop(); app.quit(); })();
    });
    app.on('window-all-closed', () => app.quit());
    app.whenReady().then(async () => {
        const desktopSession = session.defaultSession;
        const appPermission = (permission: string) => permission === 'clipboard-sanitized-write' || permission === 'fullscreen';
        desktopSession.setPermissionRequestHandler((contents, permission, callback, details) => callback(
            appPermission(permission) && details.isMainFrame && isAppUrl(details.requestingUrl) && isAppUrl(contents.getURL()),
        ));
        desktopSession.setPermissionCheckHandler((contents, permission, origin, details) => Boolean(
            appPermission(permission) && contents && details.isMainFrame && isAppUrl(contents.getURL()) &&
            isAppUrl(details.requestingUrl || origin),
        ));
        desktopSession.protocol.handle('cmv', async request => {
            const url = new URL(request.url);
            const initiator = (request as Request & { initiatorOrigin?: string }).initiatorOrigin;
            if (!isAppUrl(request.url) || (initiator && initiator !== APP_URL)) return new Response('Forbidden', { status: 403 });
            if (url.pathname === '/_desktop/shell') return shellPage();
            if (url.pathname === '/_desktop/shell.js') return new Response(readFileSync(path.join(projectRoot, 'desktop', 'shell.js')), { headers: { 'Content-Type': 'text/javascript' } });
            if (!backend?.origin || failure) return new Response('Backend unavailable', { status: 503 });
            const headers = new Headers(request.headers);
            headers.set('X-CMV-Desktop-Token', backend.token);
            // These describe the renderer's cmv origin, not the upstream request.
            // Forwarding Origin makes Chromium enforce HTTP CORS on modules/fonts.
            for (const name of ['host', 'origin', 'referer', 'sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest']) headers.delete(name);
            try {
                const upstream: RequestInit & { duplex: 'half' } = {
                    method: request.method, headers,
                    body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
                    redirect: 'manual', duplex: 'half', signal: request.signal,
                };
                return await net.fetch(`${backend.origin}${url.pathname}${url.search}`, upstream);
            } catch (error) {
                backend.log(`Protocol ${url.pathname}: ${String(error)}`);
                return new Response('Backend unavailable', { status: 503 });
            }
        });
        ipcMain.handle('cmv:choose-folder', async (event, options?: { title?: unknown; defaultPath?: unknown }) => {
            trustSender(event);
            const title = typeof options?.title === 'string' && options.title ? options.title.slice(0, 120) : 'Open source folder';
            const defaultPath = typeof options?.defaultPath === 'string' && options.defaultPath ? options.defaultPath : undefined;
            const result = await dialog.showOpenDialog(window!, { properties: ['openDirectory', 'createDirectory'], title, defaultPath });
            return result.canceled ? null : result.filePaths[0];
        });
        ipcMain.handle('cmv:retry', async event => { trustSender(event); await startBackend(); });
        ipcMain.handle('cmv:open-logs', async event => { trustSender(event); await shell.openPath(path.join(profile, 'logs')); });
        const boundsPath = path.join(profile, 'window.json');
        let bounds = { width: 1440, height: 960 };
        try {
            const saved = JSON.parse(readFileSync(boundsPath, 'utf8'));
            if (Number.isFinite(saved.width) && Number.isFinite(saved.height)) bounds = {
                width: Math.max(900, Math.min(saved.width, 3840)), height: Math.max(640, Math.min(saved.height, 2160)),
            };
        } catch { /* First launch. */ }
        const icon = path.join(projectRoot, 'desktop', 'assets', 'icon.png');
        window = new BrowserWindow({ ...bounds, minWidth: 900, minHeight: 640,
            title: 'VEIL', backgroundColor: '#111216', autoHideMenuBar: true,
            ...(existsSync(icon) ? { icon } : {}),
            webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false,
                contextIsolation: true, sandbox: true, webSecurity: true },
        });
        window.on('close', () => {
            if (window) writeFileSync(boundsPath, JSON.stringify(window.getNormalBounds()));
            // Media popups belong to the application window, not a separate
            // lifetime that can keep Python alive after the library is closed.
            for (const child of BrowserWindow.getAllWindows()) if (child !== window) child.destroy();
        });
        configureNavigation(window.webContents);
        Menu.setApplicationMenu(Menu.buildFromTemplate([
            { label: 'File', submenu: [
                { label: 'Open application data', click: () => { void shell.openPath(path.join(profile, 'data')); } },
                { label: 'Open logs', click: () => { void shell.openPath(path.join(profile, 'logs')); } },
                { type: 'separator' }, { role: 'quit' },
            ] },
            { role: 'editMenu' },
            { label: 'View', submenu: [{ role: 'reload' }, { role: 'toggleDevTools' },
                { type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
            { role: 'windowMenu' },
        ]));
        await window.loadURL(`${APP_URL}/_desktop/shell`);
        await startBackend();
    }).catch(error => { dialog.showErrorBox('VEIL', String(error)); app.quit(); });
}
