// Compare the real desktop UI and direct HTTP on one backend and library.
const { _electron } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const option = name => args[args.indexOf(name) + 1];
const executable = args.includes('--executable') ? option('--executable') : null;
const profile = fs.mkdtempSync(path.join(ROOT, 'build', 'desktop-perf-'));
const report = { profile, packaged: Boolean(executable), requests: [], navigation: [], gallery: [], errors: [] };
report.cacheReuse = !args.includes('--no-cache-reuse');
let application;

async function selectImages(page) {
    await page.locator('html.app-ready').waitFor({ timeout: 90_000 });
    await page.locator('[data-quick-filter="all"]').click();
    await page.locator('[data-quick-filter="images"]').click();
    await page.locator('#btn-view-gallery').click();
    await page.waitForFunction(async () => (await import('/static/js/state.js')).images.length >= 24);
}

async function navigation(page, mode, indices) {
    await page.bringToFront();
    for (const index of indices) {
        const result = await page.evaluate(async index => {
            const state = await import('/static/js/state.js');
            const lightbox = await import('/static/js/lightbox.js');
            const image = document.querySelector('#lb-img');
            const oldSource = image.getAttribute('src');
            const start = performance.now();
            await lightbox.openLightbox(index, state.images);
            const metaMs = performance.now() - start;
            let firstPaint;
            return await new Promise((resolve, reject) => {
                const deadline = setTimeout(() => reject(new Error('Preview paint timeout')), 30_000);
                function sample() {
                    const source = image.getAttribute('src');
                    if (source && source !== oldSource && image.complete && image.naturalWidth) {
                        firstPaint ??= performance.now() - start;
                        if (source.startsWith('blob:')) {
                            clearTimeout(deadline);
                            resolve({ id: state.images[index].id, metaMs, firstPaintMs: firstPaint,
                                previewPaintMs: performance.now() - start, width: image.naturalWidth, height: image.naturalHeight });
                            return;
                        }
                    }
                    requestAnimationFrame(sample);
                }
                requestAnimationFrame(sample);
            });
        }, index);
        report.navigation.push({ mode, ...result });
        console.log(JSON.stringify({ navigation: { mode, ...result } }));
    }
    await page.keyboard.press('Escape');
}

async function scrollGallery(page, mode) {
    await page.bringToFront();
    await page.keyboard.press('Escape');
    for (let step = 0; step < 12; step++) {
        const result = await page.evaluate(async step => {
            const area = document.querySelector('#content-area');
            const start = performance.now();
            area.scrollTop = step * area.clientHeight * 0.85;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const viewport = area.getBoundingClientRect();
            const visible = [...area.querySelectorAll('.gallery-card img')].filter(image => {
                const rect = image.getBoundingClientRect();
                return rect.bottom > viewport.top && rect.top < viewport.bottom;
            });
            const readyBefore = visible.filter(image => image.complete && image.naturalWidth).length;
            await new Promise(resolve => {
                let finished = false;
                const timeout = setTimeout(() => { finished = true; resolve(); }, 10_000);
                function sample() {
                    if (finished) return;
                    const current = [...area.querySelectorAll('.gallery-card img')].filter(image => {
                        const rect = image.getBoundingClientRect();
                        return rect.bottom > viewport.top && rect.top < viewport.bottom;
                    });
                    if (current.length && current.every(image => image.complete && image.naturalWidth)) {
                        finished = true; clearTimeout(timeout); resolve();
                    } else requestAnimationFrame(sample);
                }
                requestAnimationFrame(sample);
            });
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const painted = [...area.querySelectorAll('.gallery-card img')].filter(image => {
                const rect = image.getBoundingClientRect();
                return rect.bottom > viewport.top && rect.top < viewport.bottom;
            });
            return { step, ms: performance.now() - start, visible: painted.length, readyBefore,
                readyAfter: painted.filter(image => image.complete && image.naturalWidth).length,
                cards: area.querySelectorAll('.gallery-card').length,
                timings: performance.getEntriesByType('resource').filter(resource => resource.name.includes('/api/thumbnail/')).slice(-25)
                    .map(resource => ({ ms: resource.duration, bytes: resource.encodedBodySize })),
            };
        }, step);
        report.gallery.push({ mode, ...result });
        console.log(JSON.stringify({ gallery: { mode, ...result } }));
        assert.equal(result.readyAfter, result.visible, 'Visible gallery thumbnails did not finish loading');
    }
}

(async () => {
    const env = { ...process.env, COMFY_META_DESKTOP_PROFILE: profile,
        COMFY_META_DATA_DIR: path.join(profile, 'data'), COMFY_META_CACHE_DIR: path.join(profile, 'cache'),
        COMFY_META_IMPORT_DIR: path.join(ROOT, '.comfy_meta_uploads') };
    if (!report.cacheReuse) env.COMFY_META_IMPORT_CACHE_DIR = path.join(profile, 'missing-legacy-cache');
    delete env.ELECTRON_RUN_AS_NODE;
    application = await _electron.launch({ ...(executable ? { executablePath: executable, args: [] } : { args: [ROOT] }), env });
    const desktop = await application.firstWindow();
    desktop.on('pageerror', error => report.errors.push(error.message));
    await desktop.waitForURL('cmv://app/', { timeout: 90_000 });
    // Test-owned main-process instrumentation. The owner token never enters reports.
    await application.evaluate(({ session }) => {
        global.__cmvPerf = {};
        session.defaultSession.webRequest.onBeforeSendHeaders({ urls: ['http://127.0.0.1/*'] }, (details, callback) => {
            const header = Object.entries(details.requestHeaders).find(([key]) => key.toLowerCase() === 'x-cmv-desktop-token');
            if (header) global.__cmvPerf = { origin: new URL(details.url).origin, token: header[1] };
            callback({ requestHeaders: details.requestHeaders });
        });
    });
    await desktop.evaluate(() => fetch('/_desktop/health').then(response => response.json()));
    const connection = await application.evaluate(() => global.__cmvPerf);
    if (!connection.token) throw new Error('Test-owned backend was not captured');
    await selectImages(desktop);
    await scrollGallery(desktop, 'desktop-cold');
    await scrollGallery(desktop, 'desktop-warm');
    const ids = await desktop.evaluate(async () => (await import('/static/js/state.js')).images.slice(0, 24).map(image => image.id));
    // Cold then warm navigation uses the actual displayed lightbox frame.
    await navigation(desktop, 'desktop-cold', [0, 1, 2, 3, 4, 5]);
    await navigation(desktop, 'desktop-warm', [0, 1, 2, 3, 4, 5]);
    for (const kind of ['assets', 'thumbnail', 'preview']) {
        const extraIds = args.includes('--asset-ids') ? option('--asset-ids').split(',').map(Number).filter(id => Number.isInteger(id) && id > 0) : [];
        for (const id of [...ids.slice(0, 6), ...(kind === 'thumbnail' ? extraIds : [])]) {
            const resource = `/api/${kind}/${id}`;
            const direct = await application.evaluate(async ({ net }, resource) => {
                const start = performance.now();
                const response = await net.fetch(`${global.__cmvPerf.origin}${resource}`, { headers: { 'X-CMV-Desktop-Token': global.__cmvPerf.token } });
                const bytes = (await response.arrayBuffer()).byteLength;
                return { ms: performance.now() - start, bytes, status: response.status };
            }, resource);
            const proxy = await desktop.evaluate(async resource => {
                const start = performance.now();
                const response = await fetch(resource, { cache: 'no-cache' });
                const bytes = (await response.arrayBuffer()).byteLength;
                return { ms: performance.now() - start, bytes, status: response.status };
            }, resource);
            report.requests.push({ kind, id, direct, proxy });
            console.log(JSON.stringify(report.requests.at(-1)));
            assert.equal(direct.status, 200);
            assert.equal(proxy.status, 200);
            assert.equal(direct.bytes, proxy.bytes);
        }
    }
    const opened = application.waitForEvent('window');
    await application.evaluate(({ BrowserWindow }) => { global.__cmvHttpWindow = new BrowserWindow({ show: true,
        width: 1440, height: 960, webPreferences: { sandbox: true, nodeIntegration: false, contextIsolation: true, backgroundThrottling: false } });
        void global.__cmvHttpWindow.loadURL('about:blank'); });
    const web = await opened;
    await web.setExtraHTTPHeaders({ 'X-CMV-Desktop-Token': connection.token });
    await web.goto(`${connection.origin}/`);
    web.on('pageerror', error => report.errors.push(error.message));
    await selectImages(web);
    await scrollGallery(web, 'http-warm');
    await navigation(web, 'http-warm', [0, 1, 2, 3, 4, 5]);
    await navigation(web, 'http-cold', [6, 7, 8, 9, 10, 11]);
    await navigation(desktop, 'desktop-now-warm', [6, 7, 8, 9, 10, 11]);
    assert.equal(report.errors.length, 0);
    report.status = 'PASS';
})().catch(error => { report.status = 'FAIL'; report.failure = error.stack; process.exitCode = 1; }).finally(async () => {
    if (application) await application.close();
    fs.writeFileSync(path.join(profile, 'performance.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ profile, status: report.status, failure: report.failure }));
});
