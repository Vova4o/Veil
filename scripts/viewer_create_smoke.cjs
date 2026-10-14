// Viewer -> Create handoff against the real UI and an isolated backend; no inference.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const server = spawn(path.join(root, '.venv', 'Scripts', 'python.exe'), ['tests/create_navigation_server.py'], {
    cwd: root, windowsHide: true, env: { ...process.env, PYTHONUNBUFFERED: '1', NO_PROXY: 'localhost,127.0.0.1,::1' },
});
let stderr = '';
server.stderr.on('data', chunk => { stderr += String(chunk); });
let browser;
(async () => {
    try {
        const ready = await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('Fixture startup timed out: ' + stderr)), 15000);
            createInterface({ input: server.stdout }).on('line', line => {
                if (line.startsWith('SMOKE_READY ')) { clearTimeout(timer); resolve(line); }
            });
            server.on('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + ': ' + stderr)); });
        });
        const [, port, ...parts] = ready.split(' ');
        const origin = 'http://127.0.0.1:' + port;
        browser = await chromium.launch({ channel: 'msedge', headless: true });
        const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
        const errors = [];
        const inferenceRequests = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('request', request => {
            if (request.method() === 'POST' && /\/api\/simple\/(tasks|agent|generate)/.test(request.url())) {
                inferenceRequests.push(request.url());
            }
        });
        const upload = await page.request.post(origin + '/api/upload', { multipart: { files: {
            name: 'viewer-photo.png', mimeType: 'image/png', buffer: fs.readFileSync(path.join(parts.join(' '), 'reference.png')),
        } } });
        assert.equal(upload.status(), 200);
        const asset = (await upload.json()).assets[0];
        assert.ok(asset.id, JSON.stringify(asset));
        await page.addInitScript(() => localStorage.setItem('mediastudio_create_mode', 'edit'));
        await page.goto(origin + '/');
        await page.locator(`.gallery-card[data-image-id="${asset.id}"]`).click();
        await page.locator('#lightbox.open').waitFor();
        await page.locator('#lb-remix').click();
        await page.waitForURL(origin + '/create*');
        await page.waitForFunction(() => document.querySelector('#ref-main')?.classList.contains('is-filled'));
        assert.equal(await page.locator('[data-mode="variation"]').getAttribute('aria-checked'), 'true');
        assert.match(await page.locator('#ref-main .ref-hint').textContent(), /32×48/);
        assert.equal(await page.locator('#ref-main .ref-preview img').evaluate(img => img.naturalWidth), 32);
        assert.equal(new URL(page.url()).search, '');
        assert.equal(await page.locator('#denoise-section').isVisible(), true);
        assert.equal(await page.locator('#ratio-section').isVisible(), false);
        assert.deepEqual(inferenceRequests, []);
        assert.equal((await page.request.get(origin + '/__smoke/state')).ok(), true);
        assert.equal((await (await page.request.get(origin + '/__smoke/state')).json()).queued, 0);

        // A delayed saved-task restore must finish before the linked photo is attached.
        const queued = await page.request.post(origin + '/api/simple/tasks', { data: {
            task_id: 'remix-restore', mode: 'text', request: 'a quiet garden',
            use_agent: false, prompt: 'a quiet garden', batch_size: 1,
        } });
        assert.equal(queued.status(), 202);
        const taskSource = fs.readFileSync(path.join(root, 'app/static/js/features/simple-mode-v2.js'), 'utf8');
        const taskKey = /const CREATE_TASK_KEY = ['"]([^'"]+)/.exec(taskSource)[1];
        await page.evaluate(key => localStorage.setItem(key, 'remix-restore'), taskKey);
        let releaseRestore;
        const heldRestore = new Promise(resolve => { releaseRestore = resolve; });
        await page.route('**/api/simple/tasks/remix-restore?include_request=1', async route => {
            const response = await route.fetch();
            await heldRestore;
            await route.fulfill({ response });
        });
        const requested = page.waitForRequest(r => r.url().includes('remix-restore?include_request=1'));
        await page.goto(`${origin}/create?photo=${asset.id}&mode=variation`);
        await requested;
        releaseRestore();
        await page.waitForFunction(() => document.querySelector('#ref-main')?.classList.contains('is-filled'));
        assert.equal(await page.locator('[data-mode="variation"]').getAttribute('aria-checked'), 'true');
        assert.match(await page.locator('#ref-main .ref-hint').textContent(), /32×48/);
        const state = await (await page.request.get(origin + '/__smoke/state')).json();
        assert.equal(state.queued, 1, 'restoring and attaching the photo must not duplicate the saved task');
        assert.deepEqual(inferenceRequests, []);
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({ ok: true, checks: [
            'Viewer Remix opens the original photo in Variation despite a saved Edit mode',
            'Photo dimensions, preview, noise control and consumed URL are correct',
            'Attaching a photo starts no agent or image generation',
            'A delayed saved-task restore cannot overwrite the linked photo or duplicate its task',
            'No browser JavaScript errors',
        ] }, null, 2));
    } finally {
        await browser?.close();
        server.kill();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
