import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../app/static/js/features/simple-mode-v2.js', import.meta.url), 'utf8');

function harness() {
    const context = vm.createContext({ document: { readyState: 'loading', addEventListener() {} } });
    vm.runInContext(source, context);
    return code => vm.runInContext(code, context);
}

test('every hidden reveal reads left to right through its greeting', () => {
    const run = harness();
    const phrases = JSON.parse(run('JSON.stringify(HERO_PHRASES)'));
    assert.ok(phrases.length >= 10);
    for (const { text, reveals } of phrases) {
        for (const reveal of reveals) {
            const indices = run(`revealIndices(${JSON.stringify(text)}, ${JSON.stringify(reveal)})`);
            assert.ok(indices, `"${reveal}" is not hidden in "${text}"`);
            assert.equal(indices.length, reveal.replace(/ /g, '').length);
            // Some letters must fall, or there is nothing to reveal.
            assert.ok(indices.length < text.replace(/\s/g, '').length, `"${reveal}" keeps all of "${text}"`);
        }
    }
});

test('reveals keep the letters in order and fail when a letter is missing', () => {
    const run = harness();
    assert.deepEqual([...run(`revealIndices('Show me what you see', 'so wet')`)], [0, 2, 3, 6, 11]);
    assert.equal(run(`revealIndices('One prompt away', 'tease')`), null);
});
