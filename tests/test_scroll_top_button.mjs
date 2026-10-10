import test from 'node:test';
import assert from 'node:assert/strict';

import {
    nextScrollTopState,
    scrollTopButtonOffsets,
    shouldShowScrollTop,
} from '../app/static/js/components/scroll-top-button.js';

test('button appears only after more than two viewports of scrolling', () => {
    assert.equal(shouldShowScrollTop(0, 600), false);
    assert.equal(shouldShowScrollTop(1200, 600), false);
    assert.equal(shouldShowScrollTop(1201, 600), true);
});

test('an unmeasured scroller never shows the button', () => {
    assert.equal(shouldShowScrollTop(5000, 0), false);
});

test('without a remembered position the button only offers going to the top', () => {
    assert.deepEqual(nextScrollTopState({ scrollTop: 0, viewportHeight: 600 }), { mode: 'hidden', returnTop: null });
    assert.deepEqual(nextScrollTopState({ scrollTop: 3000, viewportHeight: 600 }), { mode: 'top', returnTop: null });
});

test('after jumping up the button offers the way back on the first screens', () => {
    for (const scrollTop of [0, 600, 900]) {
        assert.deepEqual(
            nextScrollTopState({ scrollTop, viewportHeight: 600, returnTop: 5000 }),
            { mode: 'return', returnTop: 5000 },
        );
    }
});

test('scrolling away from the first screens forgets the way back', () => {
    assert.deepEqual(
        nextScrollTopState({ scrollTop: 901, viewportHeight: 600, returnTop: 5000 }),
        { mode: 'hidden', returnTop: null },
    );
    assert.deepEqual(
        nextScrollTopState({ scrollTop: 4400, viewportHeight: 600, returnTop: 5000 }),
        { mode: 'top', returnTop: null },
    );
});

test('an unmeasured scroller never offers the way back', () => {
    assert.deepEqual(
        nextScrollTopState({ scrollTop: 0, viewportHeight: 0, returnTop: 5000 }),
        { mode: 'hidden', returnTop: null },
    );
});

test('button keeps a fixed gap from the visible corner of a full-width anchor', () => {
    assert.deepEqual(
        scrollTopButtonOffsets({ anchorRight: 1280, visibleBottom: 720, viewportWidth: 1280, viewportHeight: 720 }),
        { right: 20, bottom: 20 },
    );
});

test('button moves left of a side panel next to the anchor', () => {
    assert.deepEqual(
        scrollTopButtonOffsets({ anchorRight: 830, visibleBottom: 720, viewportWidth: 1280, viewportHeight: 720 }),
        { right: 470, bottom: 20 },
    );
});

test('anchor edges beyond the viewport are clamped to it', () => {
    assert.deepEqual(
        scrollTopButtonOffsets({ anchorRight: 1400, visibleBottom: 6000, viewportWidth: 1280, viewportHeight: 720 }),
        { right: 20, bottom: 20 },
    );
});
