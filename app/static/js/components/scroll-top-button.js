/** Floating Back to top control shared by the Viewer gallery and the Library. */
import { collectionIcon } from './viewer-collections.js';

const SHOW_AFTER_VIEWPORTS = 2;
// After jumping up, the way back is offered while the user stays on the first screens.
const RETURN_ZONE_VIEWPORTS = 1.5;
const EDGE_GAP = 20;
const LABELS = { top: 'Back to top', return: 'Back to previous position' };
const controllers = new WeakMap();

export function shouldShowScrollTop(scrollTop, viewportHeight) {
    return viewportHeight > 0 && scrollTop > viewportHeight * SHOW_AFTER_VIEWPORTS;
}

/**
 * Button mode after a scroll: 'top', 'return' or 'hidden'.
 * Scrolling away from the first screens forgets the remembered position.
 */
export function nextScrollTopState({ scrollTop, viewportHeight, returnTop = null }) {
    if (returnTop !== null && viewportHeight > 0 && scrollTop <= viewportHeight * RETURN_ZONE_VIEWPORTS) {
        return { mode: 'return', returnTop };
    }
    return { mode: shouldShowScrollTop(scrollTop, viewportHeight) ? 'top' : 'hidden', returnTop: null };
}

/** Fixed-position offsets that keep the button inside the visible part of the anchor. */
export function scrollTopButtonOffsets({ anchorRight, visibleBottom, viewportWidth, viewportHeight }) {
    return {
        right: Math.max(0, viewportWidth - Math.min(anchorRight, viewportWidth)) + EDGE_GAP,
        bottom: Math.max(0, viewportHeight - Math.min(visibleBottom, viewportHeight)) + EDGE_GAP,
    };
}

/** Forgets the way back when the scroller shows a different collection, filter or view. */
export function clearScrollReturn(scroller) {
    controllers.get(scroller)?.clearReturn();
}

/**
 * scroller is the scrolling element: document.scrollingElement for the page itself.
 * anchor bounds the content column, so the button never covers side panels.
 */
export function initScrollTopButton({ scroller, anchor = scroller }) {
    if (!scroller || !anchor) return;
    const pageScroll = scroller === document.scrollingElement;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'scroll-top-button';
    button.innerHTML = collectionIcon('arrow-up', 18);
    document.body.append(button);

    const place = () => {
        const viewport = document.documentElement;
        const bounds = anchor.getBoundingClientRect();
        const scrollerBounds = scroller.getBoundingClientRect();
        const { right, bottom } = scrollTopButtonOffsets({
            // Client edges exclude the anchor's own scrollbar.
            anchorRight: bounds.left + anchor.clientLeft + anchor.clientWidth,
            visibleBottom: pageScroll
                ? viewport.clientHeight
                : scrollerBounds.top + scroller.clientTop + scroller.clientHeight,
            viewportWidth: viewport.clientWidth,
            viewportHeight: viewport.clientHeight,
        });
        button.style.right = `${right}px`;
        button.style.bottom = `${bottom}px`;
    };

    let mode = 'hidden';
    let returnTop = null;
    // Scroll events are already dispatched at most once per rendered frame.
    const update = () => {
        const next = nextScrollTopState({ scrollTop: scroller.scrollTop, viewportHeight: scroller.clientHeight, returnTop });
        if (next.mode !== 'hidden' && mode === 'hidden') place();
        ({ mode, returnTop } = next);
        button.classList.toggle('is-visible', mode !== 'hidden');
        button.classList.toggle('is-return', mode === 'return');
        if (mode !== 'hidden') {
            button.title = LABELS[mode];
            button.setAttribute('aria-label', LABELS[mode]);
        }
    };

    const scrollToPosition = top => {
        const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const distance = top - scroller.scrollTop;
        // Jump most of the way: a long smooth scroll would lazy-load every thumbnail it passes.
        if (!reduceMotion && Math.abs(distance) > scroller.clientHeight) {
            scroller.scrollTo({ top: top - Math.sign(distance) * scroller.clientHeight, behavior: 'instant' });
        }
        scroller.scrollTo({ top, behavior: reduceMotion ? 'instant' : 'smooth' });
    };

    (pageScroll ? window : scroller).addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', place);
    new ResizeObserver(place).observe(anchor);

    button.addEventListener('click', () => {
        const goingBack = mode === 'return';
        const target = goingBack ? returnTop : 0;
        returnTop = goingBack ? null : scroller.scrollTop;
        scrollToPosition(target);
        update();
    });

    controllers.set(scroller, {
        clearReturn: () => {
            returnTop = null;
            update();
        },
    });
    place();
    update();
}
