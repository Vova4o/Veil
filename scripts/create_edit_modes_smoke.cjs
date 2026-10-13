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
        const ready = await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('Fixture startup timed out: ' + stderr)), 15000);
            createInterface({ input: server.stdout }).on('line', line => {
                if (line.startsWith('SMOKE_READY ')) { clearTimeout(timer); resolve(line); }
            });
            server.on('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + ': ' + stderr)); });
        });
        const [, port, ...parts] = ready.split(' ');
        const photo = path.join(parts.join(' '), 'reference.png');
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
        // One edit mode: the photo to edit plus an optional reference, no By text / With a reference switch.
        assert.equal(await page.locator('#edit-kinds, [data-edit-kind]').count(), 0);
        assert.match(await page.locator('#mode-hint').textContent(), /editing adapter.*second photo is optional/i);
        assert.equal(await page.locator('#ref-subject').isVisible(), true);
        assert.equal(await page.locator('#ref-subject .ref-title').textContent(), 'Reference');
        assert.match(await page.locator('#ref-subject .ref-hint').textContent(), /^Optional/);
        assert.equal(await page.locator('#denoise-section').isVisible(), false);
        await page.locator('#prompt').fill('Change the sweater to a red one.');
        await page.locator('#create').click();
        assert.match(await page.locator('#error-text').textContent(), /photo/i);
        await page.locator('#photo-input').setInputFiles(photo);
        await page.locator('#subject-input').setInputFiles(photo);
        await page.waitForFunction(() => document.querySelector('#create-label').textContent === 'Edit with reference');
        assert.match(await page.locator('#mode-hint').textContent(), /bring in from the reference/i);
        await page.locator('[data-remove="subject"]').click();
        await page.waitForFunction(() => document.querySelector('#create-label').textContent === 'Edit photo');
        await page.waitForFunction(() => document.querySelector('#ref-main')?.classList.contains('is-filled'));
        const submitted = page.waitForRequest(r => r.url().endsWith('/api/simple/tasks') && r.method() === 'POST');
        await page.locator('#create').click();
        const sent = (await submitted).postDataJSON();
        assert.equal(sent.mode, 'edit');
        assert.equal(sent.subject_image, null);
        assert.match(sent.reference_image, /^data:image\//);
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({ ok: true, checks: [
            'From photo keeps its text reconstruction meaning',
            'Variation shows noise and uses a single photo',
            'Edit photo shows the photo and an optional reference block, without a kind switch',
            'A missing photo to edit blocks submission',
            'Adding and removing the reference switches the hint and button',
            'An edit without a reference is submitted with one photo; no browser errors',
        ] }, null, 2));
    } finally {
        await browser?.close();
        server.kill();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
