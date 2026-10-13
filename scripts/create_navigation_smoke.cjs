// Exercises the real Create/Library/Viewer UI with a controlled inference fixture.
const { chromium } = require('playwright');
const { spawn, spawnSync } = require('node:child_process');
const { createInterface } = require('node:readline');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const report = { errors: [], checks: [] };
const server = spawn(path.join(root, '.venv', 'Scripts', 'python.exe'), ['tests/create_navigation_server.py'], {
    cwd: root, windowsHide: true, env: { ...process.env, PYTHONUNBUFFERED: '1' },
});
let stderr = '';
server.stderr.on('data', chunk => { stderr += String(chunk); });
let browser;
(async () => {
    try {
        const ready = await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error('Fixture startup timed out: ' + stderr)), 15000);
            createInterface({ input: server.stdout }).on('line', line => {
                if (line.startsWith('SMOKE_READY ')) { clearTimeout(timeout); resolve(line); }
            });
            server.on('exit', code => { clearTimeout(timeout); reject(new Error('Fixture exited ' + code + ': ' + stderr)); });
        });
        const [, port, ...parts] = ready.split(' ');
        const fixtureRoot = parts.join(' ');
        const origin = 'http://127.0.0.1:' + port;
        browser = await chromium.launch({ channel: 'msedge', headless: true });
        const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
        let catalogRequests = 0;
        let releaseCatalog;
        const catalog = { opencode: { label: 'OpenCode', models: [{ id: 'test/model', provider: 'opencode', name: 'Cached model', vision: true, free: true, variants: ['high'] }] },
            codex: { label: 'Codex', models: [{ id: 'cached-codex', name: 'Cached Codex', vision: null, variants: ['high'] }] },
            claude: null, antigravity: null, agents: [] };
        await page.route('**/api/simple/agents*', async route => {
            catalogRequests += 1;
            if (route.request().url().includes('refresh=1')) {
                await new Promise(resolve => { releaseCatalog = resolve; });
                await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Catalog offline' }) });
            } else await route.fulfill({ contentType: 'application/json', body: JSON.stringify(catalog) });
        });
        page.on('pageerror', error => report.errors.push(error.message));
        await page.addInitScript(() => {
            localStorage.setItem('cmv_create_agent', JSON.stringify({ agentKind: 'opencode', model: 'test/model', name: 'Navigation test', vision: true }));
        });
        await page.goto(origin + '/create');
        await page.locator('#agent-line').click();
        await page.locator('[data-native-model="cached-codex"]').waitFor();
        await page.reload();
        await page.locator('#agent-line').click();
        await page.locator('[data-native-model="cached-codex"]').waitFor();
        assert.equal(catalogRequests, 1);
        await page.locator('#agent-refresh').click();
        assert.equal(await page.locator('[data-native-model="cached-codex"]').count(), 1);
        await page.waitForFunction(() => document.querySelector('#agent-refresh').disabled);
        // The cached menu remains usable while a manual refresh waits for the CLI.
        while (!releaseCatalog) await new Promise(resolve => setTimeout(resolve, 10));
        releaseCatalog();
        await page.getByText(/Catalog offline/).waitFor();
        assert.equal(await page.locator('[data-native-model="cached-codex"]').count(), 1);
        report.checks.push('Model cards survive reload without discovery; a slow failed refresh preserves the cached list');
        await page.keyboard.press('Escape');
        await page.locator('[data-mode="reference"]').click();
        await page.locator('#photo-input').setInputFiles(path.join(fixtureRoot, 'reference.png'));
        await page.locator('#prompt').fill('A quiet garden with the reference composition.');
        await page.locator('[data-batch="2"]').click();
        const submitted = page.waitForResponse(r => r.url().endsWith('/api/simple/tasks') && r.request().method() === 'POST');
        await page.locator('#create').click();
        const response = await submitted;
        assert.equal(response.status(), 202);
        const taskId = (await response.json()).task_id;
        await page.locator('.app-switcher a[href="/library"]').click();
        await page.locator('.app-switcher a[href="/"]').click();
        await page.locator('.app-switcher a[href="/create"]').click();
        await page.waitForFunction(() => document.querySelector('#create')?.classList.contains('running'));
        await page.waitForFunction(() => document.querySelector('#ref-main .ref-preview img')?.naturalWidth > 0);
        assert.match(await page.locator('#generating-text').textContent(), /agent/i);
        assert.equal(await page.locator('#prompt').inputValue(), 'A quiet garden with the reference composition.');
        report.checks.push('Agent status, request and reference restored after Create → Library → Viewer → Create');
        await page.request.post(origin + '/__smoke/agent');
        await page.waitForFunction(async () => (await (await fetch('/__smoke/state')).json()).queued === 2);
        await page.locator('.app-switcher a[href="/library"]').click();
        await page.locator('.app-switcher a[href="/create"]').click();
        await page.waitForFunction(() => /Creating/.test(document.querySelector('#generating-text')?.textContent || ''));
        report.checks.push('Running batch status restored after a second navigation');
        await page.locator('.app-switcher a[href="/library"]').click();
        await page.request.post(origin + '/__smoke/generation');
        await page.waitForFunction(async id => (await (await fetch('/api/simple/tasks/' + id)).json()).status === 'completed', taskId);
        await page.locator('.app-switcher a[href="/"]').click();
        await page.waitForFunction(() => document.querySelectorAll('[data-image-id]').length >= 2);
        const viewer = await page.evaluate(async () => (await (await fetch('/api/images')).json()));
        assert.equal(viewer.total, 2);
        report.checks.push('Completed images indexed into the physical output source and shown by Viewer');
        await page.locator('.app-switcher a[href="/create"]').click();
        await page.waitForFunction(() => document.querySelector('#result')?.hidden === false && document.querySelector('#result-img')?.naturalWidth > 0);
        assert.equal(await page.locator('#result-thumbnails .canvas-thumb-btn').count(), 2);
        await page.reload();
        await page.waitForFunction(() => document.querySelector('#result')?.hidden === false && document.querySelector('#result-img')?.naturalWidth > 0);
        assert.equal(await page.locator('#result-thumbnails .canvas-thumb-btn').count(), 2);
        const final = await page.request.get(origin + '/__smoke/state');
        assert.equal((await final.json()).queued, 2);
        report.checks.push('Both results survive navigation and reload; no duplicate generation');
        await page.request.post(origin + '/__smoke/startup', { data: { action: 'hold' } });
        await page.locator('#prompt').fill('Prepare another quiet garden.');
        const coldSubmit = page.waitForResponse(r => r.url().endsWith('/api/simple/tasks') && r.request().method() === 'POST');
        await page.locator('#create').click();
        const coldId = (await (await coldSubmit).json()).task_id;
        await page.waitForFunction(async () => {
            const state = await (await fetch('/__smoke/state')).json();
            return state.agent_started && state.startup_started;
        });
        await page.locator('#launch-title').getByText('Starting ComfyUI', { exact: true }).waitFor();
        await page.request.post(origin + '/__smoke/agent');
        await page.locator('#agent-stream-text').getByText('A quiet garden.', { exact: true }).waitFor();
        await page.locator('#session-open').click();
        await page.locator('#session-list .session-entry').first().locator('.session-prompt').getByText('A quiet garden.', { exact: true }).waitFor();
        assert.equal((await (await page.request.get(origin + '/__smoke/state')).json()).queued, 2);
        await page.locator('#session-close').click();
        await page.reload();
        await page.locator('#agent-stream-text').getByText('A quiet garden.', { exact: true }).waitFor();
        await page.request.post(origin + '/__smoke/startup', { data: { action: 'fail' } });
        await page.locator('#error').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#error-title').textContent(), 'ComfyUI is unavailable or rejected the task');
        await page.locator('#session-open').click();
        const savedPrompt = page.locator('#session-list .session-entry').first();
        await savedPrompt.getByText('Generation failed · prompt saved', { exact: false }).waitFor();
        assert.equal(await savedPrompt.locator('.session-prompt').textContent(), 'A quiet garden.');
        await page.reload();
        await page.locator('#session-open').click();
        assert.equal(await page.locator('#session-list .session-entry').first().locator('.session-prompt').textContent(), 'A quiet garden.');
        assert.equal((await (await page.request.get(origin + '/__smoke/state')).json()).queued, 2);
        assert.equal((await (await page.request.get(origin + '/api/simple/tasks/' + coldId)).json()).status, 'failed');
        await page.locator('#session-close').click();
        report.checks.push('Local startup overlaps the agent; its prompt appears before queueing and survives startup failure and reload without duplication');
        await page.request.post(origin + '/__smoke/agent-error');
        await page.locator('#prompt').fill('Describe the quiet garden again.');
        await page.locator('#create').click();
        await page.locator('#error').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#error-title').textContent(), 'The agent could not prepare the prompt');
        assert.equal(await page.locator('#error-text').textContent(), 'Antigravity returned an invalid structured result.');
        assert.equal(await page.locator('#error-details').evaluate(node => node.hidden || node.open), false);
        await page.locator('#error-details summary').click();
        assert.equal(await page.locator('#error-technical').textContent(), 'schema_version must be a string.');
        assert.equal(await page.locator('#create').isEnabled(), true);
        assert.equal(await page.locator('#generating').isVisible(), false);
        assert.equal((await (await page.request.get(origin + '/__smoke/state')).json()).queued, 2);
        report.checks.push('Agent contract failure exits the waiting state, enables Create, and shows diagnostics only in Details; no inference queued');
        assert.deepEqual(report.errors, []);
        report.ok = true;
    } catch (error) {
        report.ok = false;
        report.failure = String(error);
        report.stderr = stderr.slice(-3000);
        process.exitCode = 1;
    } finally {
        await browser?.close();
        if (process.platform === 'win32' && server.exitCode === null) {
            spawnSync('taskkill', ['/PID', String(server.pid), '/T', '/F'], { windowsHide: true });
        } else server.kill();
        fs.mkdirSync(path.join(root, 'reports'), { recursive: true });
        fs.writeFileSync(path.join(root, 'reports', 'create-navigation-smoke.json'), JSON.stringify(report, null, 2));
        console.log(JSON.stringify(report, null, 2));
    }
})();
