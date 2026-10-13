// Verify the three image modification paths in the actual UI, with isolated user data.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const server = spawn(path.join(root, '.venv', 'Scripts', 'python.exe'), ['tests/create_navigation_server.py'], {
    cwd: root, windowsHide: true, env: { ...process.env, PYTHONUNBUFFERED: '1' },
});
let stderr = '';
server.stderr.on('data', chunk => { stderr += String(chunk); });
let browser;
(async () => {
    try {
        const port = await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('Fixture startup timed out: ' + stderr)), 15000);
            createInterface({ input: server.stdout }).on('line', line => {
                if (line.startsWith('SMOKE_READY ')) { clearTimeout(timer); resolve(line.split(' ')[1]); }
            });
            server.on('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + ': ' + stderr)); });
        });
        browser = await chromium.launch({ channel: 'msedge', headless: true });
        const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.goto('http://127.0.0.1:' + port + '/create');
        await page.locator('[data-mode="reference"]').click();
        assert.match(await page.locator('#mode-hint').textContent(), /agent describes/i);
        assert.equal(await page.locator('#denoise-section').isVisible(), false);
        await page.locator('[data-mode="variation"]').click();
        assert.equal(await page.locator('#denoise-section').isVisible(), true);
        assert.equal(await page.locator('#ratio-section').isVisible(), false);
        assert.match(await page.locator('#mode-hint').textContent(), /noise sets/i);
        await page.locator('[data-mode="edit"]').click();
        await page.locator('[data-edit-kind="text"]').click();
        assert.match(await page.locator('#mode-hint').textContent(), /editing adapter/i);
        assert.equal(await page.locator('#ref-subject').isVisible(), false);
        assert.equal(await page.locator('#denoise-section').isVisible(), false);
        await page.locator('[data-edit-kind="reference"]').click();
        assert.equal(await page.locator('#ref-subject').isVisible(), true);
        assert.match(await page.locator('#mode-hint').textContent(), /first photo supplies the scene/i);
        await page.locator('#prompt').fill('Copy the cup from the second photo.');
        await page.locator('#create').click();
        assert.match(await page.locator('#error-text').textContent(), /photo/i);
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({ ok: true, checks: [
            'From photo keeps its text reconstruction meaning',
            'Variation shows noise and uses a single photo',
            'Text edit uses one photo and explains the editing adapter',
            'Reference edit shows separate scene and replacement blocks',
            'Missing photos block submission; no browser errors',
        ] }, null, 2));
    } finally {
        await browser?.close();
        server.kill();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
