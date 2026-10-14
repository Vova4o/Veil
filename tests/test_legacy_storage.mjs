import test from 'node:test';
import assert from 'node:assert/strict';

function mockStorage(initial = {}) {
    const map = new Map(Object.entries(initial));
    return {
        get length() { return map.size; },
        key: index => [...map.keys()][index] ?? null,
        getItem: key => (map.has(key) ? map.get(key) : null),
        setItem: (key, value) => map.set(key, String(value)),
        removeItem: key => map.delete(key),
        snapshot: () => Object.fromEntries(map),
    };
}

globalThis.localStorage = mockStorage({ cmv_theme: 'light', cmv_gallery_density: '3', other: 'keep' });
globalThis.sessionStorage = mockStorage({ cmv_state: 'old' });
await import('../app/static/js/legacy-storage.js');

test('legacy-prefixed keys move to the codename prefix', () => {
    assert.deepEqual(globalThis.localStorage.snapshot(), {
        mediastudio_theme: 'light', mediastudio_gallery_density: '3', other: 'keep',
    });
    assert.deepEqual(globalThis.sessionStorage.snapshot(), { mediastudio_state: 'old' });
});

test('an existing new key wins and the legacy key is dropped', () => {
    globalThis.localStorage = mockStorage({ cmv_theme: 'light', mediastudio_theme: 'dark' });
    globalThis.__migrateLegacyStorage();
    assert.deepEqual(globalThis.localStorage.snapshot(), { mediastudio_theme: 'dark' });
});

test('missing storage does not throw', () => {
    globalThis.localStorage = undefined;
    globalThis.sessionStorage = undefined;
    assert.doesNotThrow(() => globalThis.__migrateLegacyStorage());
});
