import test from 'node:test';
import assert from 'node:assert/strict';
import { JsonRequestCache, LatestRequest } from '../app/static/js/request-state.js';

function fixture() {
    const pending = [];
    const cache = new JsonRequestCache(() => new Promise(resolve => pending.push(resolve)));
    const resolve = (index, value) => pending[index]({ ok: true, json: async () => value });
    return { cache, pending, resolve };
}

test('old response cannot repopulate an invalidated cache', async () => {
    const { cache, resolve } = fixture();
    const old = cache.fetch('/list');
    cache.invalidate();
    const fresh = cache.fetch('/list');
    resolve(1, { version: 'new' });
    await fresh;
    resolve(0, { version: 'old' });
    await old;
    assert.deepEqual(await cache.fetch('/list'), { version: 'new' });
});

test('forced refresh starts a new request and keeps ownership after older completion', async () => {
    const { cache, pending, resolve } = fixture();
    const old = cache.fetch('/list');
    const fresh = cache.fetch('/list', { force: true });
    assert.equal(pending.length, 2);
    resolve(0, { version: 'old' });
    await old;
    assert.strictEqual(cache.fetch('/list'), fresh);
    resolve(1, { version: 'new' });
    await fresh;
    assert.deepEqual(await cache.fetch('/list'), { version: 'new' });
});

test('ordinary concurrent requests share work; failures remain retryable', async () => {
    const { cache, pending, resolve } = fixture();
    const first = cache.fetch('/list');
    assert.strictEqual(cache.fetch('/list'), first);
    pending[0]({ ok: false, json: async () => ({ error: 'offline' }) });
    await assert.rejects(first, /offline/);
    const retry = cache.fetch('/list');
    resolve(1, { restored: true });
    assert.deepEqual(await retry, { restored: true });
});

test('only the newest list request owns publication', () => {
    const owner = new LatestRequest();
    const old = owner.begin();
    const next = owner.begin();
    assert.equal(owner.owns(old), false);
    assert.equal(owner.owns(next), true);
});
