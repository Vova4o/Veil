import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, appendFileSync } from 'node:fs';
import path from 'node:path';

export interface BackendOptions {
    packaged: boolean;
    projectRoot: string;
    resourcesPath: string;
    profile: string;
    onExit?: (message: string) => void;
}

export class DesktopBackend {
    private child?: ChildProcessWithoutNullStreams;
    private stopping = false;
    private stopPromise?: Promise<void>;
    private exited?: Promise<void>;
    readonly token = randomBytes(32).toString('hex');
    private readonly sessionId = randomBytes(16).toString('hex');
    origin = '';
    readonly logPath: string;

    constructor(readonly options: BackendOptions) {
        const logs = path.join(options.profile, 'logs');
        mkdirSync(logs, { recursive: true });
        this.logPath = path.join(logs, 'desktop-backend.log');
    }

    log(message: string): void {
        appendFileSync(this.logPath, `${new Date().toISOString()} ${message}\n`);
    }

    get pid(): number | undefined { return this.child?.pid; }

    async start(): Promise<string> {
        const { packaged, resourcesPath, projectRoot, profile } = this.options;
        const executable = packaged
            ? path.join(resourcesPath, 'backend', `mediastudio-backend${process.platform === 'win32' ? '.exe' : ''}`)
            : process.env.MEDIASTUDIO_PYTHON || path.join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        if (!existsSync(executable)) throw new Error(`Python runtime is missing: ${executable}`);
        const data = process.env.MEDIASTUDIO_DATA_DIR || path.join(profile, 'data');
        const cache = process.env.MEDIASTUDIO_CACHE_DIR || path.join(profile, 'cache');
        // Development data lives in the project; installs from before the codename still use the old folder name.
        const devData = ['.mediastudio_data', '.comfy_meta_uploads'].map(name => path.join(projectRoot, name)).find(existsSync);
        const legacy = process.env.MEDIASTUDIO_IMPORT_DIR || (!packaged ? devData : undefined);
        const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8',
            MEDIASTUDIO_DATA_DIR: data, MEDIASTUDIO_CACHE_DIR: cache,
            MEDIASTUDIO_DESKTOP_TOKEN: this.token, MEDIASTUDIO_DESKTOP_SESSION: this.sessionId, MEDIASTUDIO_DESKTOP: '1' };
        if (legacy) Object.assign(env, { MEDIASTUDIO_IMPORT_DIR: legacy });
        // A packaged backend owns its bundled modules, never a caller's Python environment.
        if (packaged) { delete env.PYTHONHOME; delete env.PYTHONPATH; }
        this.log(`Starting ${packaged ? 'bundled' : 'development'} backend`);
        const child = spawn(executable, packaged ? [] : ['-m', 'app.desktop_server'], {
            cwd: packaged ? profile : projectRoot, env, windowsHide: true,
            stdio: ['pipe', 'pipe', 'pipe'],
        });
        this.child = child;
        this.exited = new Promise(resolve => child.once('close', () => resolve()));
        child.stderr.on('data', data => this.log(String(data).trimEnd()));
        child.stdin.on('error', error => this.log(`Parent pipe: ${error.message}`));
        return new Promise((resolve, reject) => {
            let ready = false;
            const timeout = setTimeout(() => {
                reject(new Error('Python backend startup timed out. See the desktop log.'));
                void this.stop();
            }, 60_000);
            const lines = createInterface({ input: child.stdout });
            lines.on('line', line => {
                if (!line.startsWith('MEDIASTUDIO_DESKTOP_READY ')) { this.log(line); return; }
                try {
                    const info = JSON.parse(line.slice('MEDIASTUDIO_DESKTOP_READY '.length));
                    // Windows venv launchers can keep a parent PID above Python.
                    // A per-launch nonce on our private stdout pipe binds readiness
                    // to this child tree without assuming the server is its root PID.
                    if (info.session !== this.sessionId || !Number.isInteger(info.pid) || info.pid < 1 ||
                        !Number.isInteger(info.port) || info.port < 1 || info.port > 65535) {
                        throw new Error('Invalid backend readiness record');
                    }
                    if (ready || this.stopping) return;
                    ready = true;
                    clearTimeout(timeout);
                    this.origin = `http://127.0.0.1:${info.port}`;
                    this.log(`Backend ready, PID ${info.pid}`);
                    resolve(this.origin);
                } catch (error) {
                    clearTimeout(timeout);
                    reject(error);
                    void this.stop();
                }
            });
            child.once('error', error => { clearTimeout(timeout); reject(error); });
            child.once('close', code => {
                clearTimeout(timeout);
                lines.close();
                this.log(`Backend exited (${code})`);
                const message = `Python backend exited (${code}). See the desktop log.`;
                if (!ready) reject(new Error(message));
                else if (!this.stopping) this.options.onExit?.(message);
            });
        });
    }

    stop(): Promise<void> {
        if (this.stopPromise) return this.stopPromise;
        this.stopping = true;
        this.stopPromise = this.stopOwnedChild();
        return this.stopPromise;
    }

    private async stopOwnedChild(): Promise<void> {
        const child = this.child;
        if (!child || child.exitCode !== null || child.signalCode !== null) return;
        if (!child.stdin.destroyed) child.stdin.end('shutdown\n');
        let timeout: ReturnType<typeof setTimeout> | undefined;
        const timedOut = await Promise.race([
            this.exited!.then(() => false),
            new Promise<boolean>(resolve => { timeout = setTimeout(() => resolve(true), 15_000); }),
        ]);
        if (timeout) clearTimeout(timeout);
        if (timedOut && child.exitCode === null && child.signalCode === null && child.pid) {
            this.log('Graceful shutdown timed out; terminating the owned backend tree');
            if (process.platform === 'win32') {
                await new Promise<void>(resolve => {
                    const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
                    killer.once('close', () => resolve());
                    killer.once('error', () => { child.kill(); resolve(); });
                });
            } else child.kill('SIGKILL');
            await this.exited;
        }
    }
}
