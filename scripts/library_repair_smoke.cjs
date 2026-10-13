// Actual Viewer/Library UI against an isolated local backend. No inference or user data.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const server = spawn(path.join(root, '.venv/Scripts/python.exe'), ['tests/create_navigation_server.py'], {
    cwd: root, windowsHide: true, env: { ...process.env, PYTHONUNBUFFERED: '1', NO_PROXY: 'localhost,127.0.0.1,::1' },
});
let stderr = '';
server.stderr.on('data', chunk => { stderr += String(chunk); });
let browser;
(async () => {
    try {
        const ready = await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('Fixture startup timeout: ' + stderr)), 15000);
            createInterface({ input: server.stdout }).on('line', line => {
                if (line.startsWith('SMOKE_READY ')) { clearTimeout(timer); resolve(line); }
            });
            server.on('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited: ' + code + stderr)); });
        });
        const [, port, ...parts] = ready.split(' ');
        const origin = 'http://127.0.0.1:' + port;
        browser = await chromium.launch({ channel: 'msedge', headless: true });
        const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        for (const name of ['z.png', 'a.png', 'm.png']) {
            const response = await page.request.post(origin + '/api/upload', { multipart: { files: {
                name, mimeType: 'image/png', buffer: fs.readFileSync(path.join(parts.join(' '), 'reference.png')),
            } } });
            assert.equal(response.status(), 200);
        }
        await page.goto(origin + '/');
        await page.waitForFunction(async () => (await import('/static/js/state.js')).images.length === 3);
        let releaseOld;
        let started;
        const oldStarted = new Promise(resolve => { started = resolve; });
        const held = new Promise(resolve => { releaseOld = resolve; });
        await page.route('**/api/images?**', async route => {
            const response = await route.fetch();
            if (route.request().url().includes('sort_by=name')) { started(); await held; }
            await route.fulfill({ response });
        });
        await page.evaluate(async () => {
            const api = await import('/static/js/api.js');
            const state = await import('/static/js/state.js');
            state.setSortKey('name');
            window.oldLoad = api.loadCollectionImages({ ...state.currentCollection }, { force: true, render: false });
        });
        await oldStarted;
        const freshIds = await page.evaluate(async () => {
            const api = await import('/static/js/api.js');
            const state = await import('/static/js/state.js');
            state.setSortKey('date');
            api.invalidateApiCache();
            await api.loadCollectionImages({ ...state.currentCollection }, { force: true, render: false });
            return state.images.map(img => img.id);
        });
        releaseOld();
        await page.evaluate(() => window.oldLoad);
        assert.deepEqual(await page.evaluate(async () => (await import('/static/js/state.js')).images.map(img => img.id)), freshIds);
        await page.unroute('**/api/images?**');

        const originalCollection = await page.evaluate(async () => ({ ...(await import('/static/js/state.js')).currentCollection }));
        const scan = await page.request.post(origin + '/api/scan', { data: { path: parts.join(' ') } });
        assert.equal(scan.status(), 200);
        const secondSource = (await scan.json()).folder_id;
        let releaseFolder;
        let folderStarted;
        const folderSeen = new Promise(resolve => { folderStarted = resolve; });
        const heldFolder = new Promise(resolve => { releaseFolder = resolve; });
        await page.route('**/api/images?**', async route => {
            const response = await route.fetch();
            if (new URL(route.request().url()).searchParams.get('folder_id') === String(originalCollection.id)) {
                folderStarted(); await heldFolder;
            }
            await route.fulfill({ response });
        });
        await page.evaluate(async collection => {
            const api = await import('/static/js/api.js');
            window.oldFolder = api.loadCollectionImages(collection, { force: true, render: false });
        }, originalCollection);
        await folderSeen;
        const secondIds = await page.evaluate(async id => {
            const api = await import('/static/js/api.js');
            const state = await import('/static/js/state.js');
            await api.loadFolderImages(id, 'Second source', { force: true, render: false });
            return state.images.map(img => img.id);
        }, secondSource);
        assert.equal(secondIds.length, 1);
        releaseFolder();
        await page.evaluate(() => window.oldFolder);
        assert.deepEqual(await page.evaluate(async () => (await import('/static/js/state.js')).images.map(img => img.id)), secondIds);
        assert.equal(await page.evaluate(async () => (await import('/static/js/state.js')).currentCollection.id), secondSource);
        await page.unroute('**/api/images?**');

        let releaseDetail;
        let detailStarted;
        const detailSeen = new Promise(resolve => { detailStarted = resolve; });
        const heldDetail = new Promise(resolve => { releaseDetail = resolve; });
        await page.route('**/api/assets/*', async route => {
            const response = await route.fetch();
            detailStarted();
            await heldDetail;
            await route.fulfill({ response });
        });
        await page.evaluate(async () => {
            const state = await import('/static/js/state.js');
            state.setDetailCache({});
            const lightbox = await import('/static/js/lightbox.js');
            window.oldDetail = lightbox.openLightbox(0, state.images);
        });
        await detailSeen;
        await page.evaluate(async () => (await import('/static/js/lightbox.js')).closeLightbox());
        releaseDetail();
        await page.evaluate(() => window.oldDetail);
        assert.equal(await page.locator('#lightbox').evaluate(el => el.classList.contains('open')), false);
        await page.unroute('**/api/assets/*');

        await page.locator('#btn-repair-library').evaluate(el => el.click());
        await page.locator('.library-repair-dialog[open]').waitFor();
        await page.waitForFunction(() => document.querySelector('.repair-status')?.textContent === 'Library check finished.');
        const repair = (await (await page.request.get(origin + '/api/library/repair')).json()).repair;
        assert.equal(repair.status, 'completed');
        assert.equal(repair.failed, 0);
        assert.equal(repair.total, 4);
        assert.equal(repair.errors.length, 0);
        const after = await page.request.get(origin + '/api/images?per_page=100');
        assert.equal((await after.json()).images.length, 4);
        await page.waitForFunction(() => document.querySelectorAll('.gallery-card img').length > 0
            && [...document.querySelectorAll('.gallery-card img')].every(img => img.complete && img.naturalWidth > 0));
        fs.mkdirSync(path.join(root, 'reports'), { recursive: true });
        await page.screenshot({ path: path.join(root, 'reports/library-repair-viewer-20261014.png') });
        await page.locator('.repair-close').click();
        await page.goto(origin + '/library');
        await page.locator('#btn-repair-library').click();
        await page.waitForFunction(() => document.querySelector('.repair-status')?.textContent === 'Library check finished.');
        await page.screenshot({ path: path.join(root, 'reports/library-repair-library-20261014.png') });
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('.library-repair-dialog').evaluate(el => el.open), false);
        assert.deepEqual(errors, []);
        const report = { sort_race: 'passed', folder_race: 'passed', close_during_metadata: 'passed', viewer_repair: repair,
            library_repair: (await (await page.request.get(origin + '/api/library/repair')).json()).repair,
            keyboard_close: 'passed', page_errors: errors };
        fs.writeFileSync(path.join(root, 'reports/library-repair-20261014.json'), JSON.stringify(report, null, 2));
        console.log(JSON.stringify(report));
    } finally {
        if (browser) await browser.close();
        server.kill();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
