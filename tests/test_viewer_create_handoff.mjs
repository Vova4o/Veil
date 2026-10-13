import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../app/static/js/features/simple-mode-v2.js', import.meta.url), 'utf8');

function harness(search, response = { ok: true, blob: async () => new Blob(['photo'], { type: 'image/png' }) }) {
    const requests = [];
    const photos = [];
    const errors = [];
    const urls = [];
    const context = vm.createContext({
        document: { readyState: 'loading', addEventListener() {} },
        window: { location: { search, pathname: '/create', hash: '#photo' } },
        history: { state: { saved: true }, replaceState: (state, title, url) => urls.push({ state, url }) },
        URLSearchParams, File, Blob,
        fetch: async url => { requests.push(url); return response; },
        capturePhoto: (slot, file) => photos.push({ slot, file }),
        captureError: (title, message) => errors.push({ title, message }),
    });
    vm.runInContext(source, context);
    vm.runInContext(`
        setMode = mode => { state.mode = mode; };
        setPhoto = async (slot, file) => capturePhoto(slot, file);
        error = (title, message) => captureError(title, message);
    `, context);
    return { requests, photos, errors, urls, run: code => vm.runInContext(code, context) };
}

test('Viewer photo links attach the original in Variation without starting inference', async () => {
    const h = harness('?photo=17&mode=variation&keep=yes');
    h.run("state.mode = 'edit'");
    await h.run('openPhotoFromLink()');
    assert.equal(h.run('state.mode'), 'variation');
    assert.deepEqual(h.requests, ['/api/original/17']);
    assert.equal(h.photos[0].slot, 'main');
    assert.equal(h.photos[0].file.type, 'image/png');
    assert.equal(await h.photos[0].file.text(), 'photo');
    assert.deepEqual(h.urls, [{ state: { saved: true }, url: '/create?keep=yes#photo' }]);
    assert.deepEqual(h.errors, []);
});

test('photo links default to Variation regardless of the previously selected mode', async () => {
    for (const mode of ['', '&mode=text', '&mode=constructor']) {
        const h = harness('?photo=2' + mode);
        h.run("state.mode = 'edit'");
        await h.run('openPhotoFromLink()');
        assert.equal(h.run('state.mode'), 'variation');
    }
});

test('invalid asset links leave the current Create session alone', async () => {
    for (const search of ['', '?photo=0', '?photo=-1', '?photo=abc', '?photo=1.5']) {
        const h = harness(search);
        h.run("state.mode = 'edit'");
        await h.run('openPhotoFromLink()');
        assert.equal(h.run('state.mode'), 'edit');
        assert.deepEqual(h.requests, []);
        assert.deepEqual(h.photos, []);
        assert.deepEqual(h.urls, []);
    }
});

test('a removed original reports an actionable error without attaching a broken image', async () => {
    const h = harness('?photo=17&mode=variation', { ok: false, status: 404 });
    await h.run('openPhotoFromLink()');
    assert.deepEqual(h.photos, []);
    assert.match(h.errors[0].message, /HTTP 404/);
});
