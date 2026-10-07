// Real Electron acceptance: protocol, uploads, decoded video, restart, storage, exit.
const { _electron } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const ROOT = path.resolve(__dirname, '..');
const argv = process.argv.slice(2);
const option = name => { const index = argv.indexOf(name); return index >= 0 ? argv[index + 1] : undefined; };
const executablePath = option('--executable');
const videoDirectory = option('--video-dir');
const profile = fs.mkdtempSync(path.join(ROOT, 'build', 'desktop-smoke-'));
const report = { profile, packaged: Boolean(executablePath), videos: [], errors: [], console: [], failedRequests: [] };
let application;

async function waitForLibrary(page) {
    await page.waitForURL('cmv://app/', { timeout: 90_000 });
    await page.locator('html.app-ready').waitFor({ timeout: 90_000 });
}

async function run() {
    const env = { ...process.env, COMFY_META_DESKTOP_PROFILE: profile,
        COMFY_META_DATA_DIR: path.join(profile, 'data'), COMFY_META_CACHE_DIR: path.join(profile, 'cache'),
        COMFY_META_IMPORT_DIR: path.join(ROOT, '.comfy_meta_uploads') };
    delete env.ELECTRON_RUN_AS_NODE;
    if (executablePath) {
        // The installed executable must work without Python, Poetry or FFmpeg on PATH.
        env.PATH = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32');
        delete env.PYTHONPATH;
        delete env.PYTHONHOME;
    }
    application = await _electron.launch({
        ...(executablePath ? { executablePath, args: [] } : { args: [ROOT] }),
        env, timeout: 90_000,
    });
    const page = await application.firstWindow();
    page.on('pageerror', error => report.errors.push(error.message));
    page.on('console', message => { if (['error', 'warning'].includes(message.type())) report.console.push(message.text().slice(0, 1000)); });
    page.on('requestfailed', request => report.failedRequests.push({ url: request.url().split('?')[0], failure: request.failure() }));
    page.on('response', response => { if (response.status() >= 400) report.failedRequests.push({ url: response.url().split('?')[0], status: response.status() }); });
    await waitForLibrary(page);
    report.initial = await page.evaluate(async () => ({
        health: await (await fetch('/_desktop/health')).json(),
        desktopBridge: typeof window.cmvDesktop?.chooseFolder,
        nodeAccess: typeof window.require,
        folders: (await (await fetch('/api/folders')).json()).folders?.length,
    }));
    assert.equal(report.initial.desktopBridge, 'function');
    assert.equal(report.initial.nodeAccess, 'undefined');
    report.clipboardWritePermission = await page.evaluate(async () =>
        (await navigator.permissions.query({ name: 'clipboard-write' })).state);
    assert.equal(report.clipboardWritePermission, 'granted');
    assert.equal(await page.evaluate(async () =>
        (await navigator.permissions.query({ name: 'clipboard-read' })).state), 'denied');
    assert.equal((await fetch(`http://127.0.0.1:${report.initial.health.port}/api/folders`)).status, 403);
    assert(fs.existsSync(path.join(profile, 'data', 'desktop-import.json')));
    report.sse = await page.evaluate(() => new Promise(resolve => {
        const events = new EventSource('/api/folders/events');
        const timeout = setTimeout(() => { events.close(); resolve('timeout'); }, 10_000);
        events.onmessage = () => { clearTimeout(timeout); events.close(); resolve('message'); };
        events.onerror = () => { clearTimeout(timeout); events.close(); resolve('error'); };
    }));
    assert.equal(report.sse, 'message');
    if (videoDirectory) {
        const files = fs.readdirSync(videoDirectory).filter(name => /\.(mp4|webm)$/i.test(name))
            .map(name => path.join(videoDirectory, name)).filter(file => fs.statSync(file).size < 5_000_000).slice(0, 3);
        assert(files.length >= 2, 'Provide at least two small video files for acceptance');
        const uploaded = page.waitForResponse(response => response.url().endsWith('/api/upload') && response.request().method() === 'POST');
        await page.locator('#add-file-input').setInputFiles(files);
        const upload = await (await uploaded).json();
        assert.equal(upload.count, files.length);
        await page.getByText(`${files.length} video file(s) added`, { exact: true }).waitFor();
        await page.locator('[data-quick-filter="all"]').click();
        await page.locator('[data-quick-filter="videos"]').click();
        await page.locator('#btn-view-gallery').click();
        for (const asset of upload.assets) {
            assert.equal(asset.media_type, 'video');
            assert.equal(asset.preview_status, 'ready');
            const range = await page.evaluate(async id => {
                const response = await fetch(`/api/original/${id}`, { headers: { Range: 'bytes=0-31' } });
                return { status: response.status, range: response.headers.get('Content-Range'), bytes: (await response.arrayBuffer()).byteLength };
            }, asset.id);
            assert.equal(range.status, 206);
            assert.equal(range.bytes, 32);
            await page.locator(`[data-image-id="${asset.id}"]`).first().click();
            await page.waitForFunction(id => {
                const video = document.querySelector('#lb-video');
                return video && !video.hidden && video.getAttribute('src')?.startsWith(`/api/original/${id}?`) && video.readyState >= 2;
            }, asset.id, { timeout: 20_000 });
            const decoded = await page.evaluate(async () => {
                const video = document.querySelector('#lb-video');
                video.muted = true;
                video.currentTime = Math.min(1, video.duration / 3);
                await video.play();
                return { width: video.videoWidth, height: video.videoHeight, duration: video.duration, start: video.currentTime };
            });
            await page.waitForFunction(start => document.querySelector('#lb-video').currentTime > start + 0.2, decoded.start);
            assert(decoded.width > 0 && decoded.height > 0);
            report.videos.push({ file: asset.file_name, ...decoded, range });
            if (!report.fullscreen) {
                report.fullscreen = await page.evaluate(async () => {
                    const video = document.querySelector('#lb-video');
                    await video.requestFullscreen();
                    const result = document.fullscreenElement === video;
                    await document.exitFullscreen();
                    return result;
                });
                assert.equal(report.fullscreen, true);
            }
            if (!report.originalWindow) {
                const opened = application.waitForEvent('window');
                await page.locator('#lb-view-original').click();
                const original = await opened;
                await original.waitForURL(`cmv://app/api/original/${asset.id}**`);
                assert.equal(page.url(), 'cmv://app/');
                assert.equal(await original.evaluate(() => typeof window.require), 'undefined');
                report.originalWindow = original.url().split('?')[0];
                await original.close();
            }
            await page.keyboard.press('Escape');
            await page.waitForFunction(() => !document.querySelector('#lightbox').classList.contains('open'));
        }
        await page.waitForFunction(() => getComputedStyle(document.querySelector('#lightbox')).opacity === '0');
        await page.screenshot({ path: path.join(profile, 'gallery.png') });
    }
    // Browser storage must survive a new random backend port and an app restart.
    await page.evaluate(() => localStorage.setItem('cmv_desktop_acceptance', 'persistent'));
    await page.goto('cmv://app/editor');
    await page.locator('#prompt').waitFor();
    await page.locator('#prompt').fill('Desktop acceptance draft');
    report.editor = {
        prompt: await page.locator('#prompt').inputValue(),
        model: await page.locator('#model-selected-name').innerText(),
    };
    // Exercise the native dialog IPC without selecting or changing a source.
    await application.evaluate(({ dialog }) => {
        const show = dialog.showOpenDialog;
        dialog.showOpenDialog = async () => {
            dialog.showOpenDialog = show;
            return { canceled: true, filePaths: [] };
        };
    });
    assert.equal(await page.evaluate(() => window.cmvDesktop.chooseFolder()), null);
    report.folderDialogIpc = 'PASS (cancelled dialog stub)';
    await page.goto('cmv://app/');
    await page.locator('html.app-ready').waitFor();
    // Crash only this test-owned backend; the shell must expose a working retry.
    const oldPid = report.initial.health.pid;
    process.kill(oldPid);
    await page.waitForURL('cmv://app/_desktop/shell', { timeout: 20_000 });
    await page.getByRole('button', { name: 'Try again' }).click();
    await waitForLibrary(page);
    report.restarted = await page.evaluate(async () => ({
        health: await (await fetch('/_desktop/health')).json(),
        storage: localStorage.getItem('cmv_desktop_acceptance'),
    }));
    assert.notEqual(report.restarted.health.pid, oldPid);
    assert.equal(report.restarted.storage, 'persistent');
    const popup = application.waitForEvent('window');
    await page.evaluate(() => { window.open('/_desktop/health', '_blank'); });
    await popup;
    const closed = application.waitForEvent('close', { timeout: 25_000 });
    await application.evaluate(({ BrowserWindow }) => {
        BrowserWindow.getAllWindows().find(window => window.webContents.getURL() === 'cmv://app/').close();
    });
    await closed;
    application = undefined;
    await new Promise(resolve => setTimeout(resolve, 500));
    assert.throws(() => process.kill(report.restarted.health.pid, 0));
    report.windowClose = 'PASS (with an open child window)';
    assert.equal(report.errors.length, 0, JSON.stringify(report.errors));
    report.status = 'PASS';
}

run().catch(async error => {
    report.status = 'FAIL';
    report.failure = error.stack;
    if (application) {
        for (const page of application.windows()) {
            console.error((await page.locator('body').innerText().catch(() => 'No page')).slice(0, 2500));
            report.dom = await page.evaluate(() => ({ url: location.href, classes: document.documentElement.className,
                scripts: [...document.scripts].map(script => script.src), resources: performance.getEntriesByType('resource').map(resource => resource.name) })).catch(() => ({}));
            await page.screenshot({ path: path.join(profile, 'failure.png') }).catch(() => {});
        }
        await application.close().catch(() => {});
    }
    const log = path.join(profile, 'logs', 'desktop-backend.log');
    if (fs.existsSync(log)) console.error(fs.readFileSync(log, 'utf8').slice(-5000));
    process.exitCode = 1;
}).finally(() => {
    fs.writeFileSync(path.join(profile, 'acceptance.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
});
