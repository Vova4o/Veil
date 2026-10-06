import test from 'node:test';
import assert from 'node:assert/strict';

const elements = new Map();
const navigationItems = [];
globalThis.document = {
    getElementById(id) {
        assert.notEqual(id, 'image-list', 'Viewer must not depend on the removed media strip');
        assert.notEqual(id, 'panel-images', 'Viewer must not reactivate the removed Media panel');
        return elements.get(id) || null;
    },
    querySelectorAll(selector) { return selector === '.source-folder-item, .album-item' ? navigationItems : []; },
};

elements.set('viewer-collection-count', { textContent: '', title: '' });
const state = await import('../app/static/js/state.js');
const sidebar = await import('../app/static/js/features/sidebar.js');

test('an empty filtered collection reports zero despite cached global media', () => {
    state.setSidebarImages(Array.from({ length: 50 }, (_, id) => ({ id })));
    state.setSidebarTotalImages(5706);
    state.setImages([]);
    state.setTotalImages(0);
    sidebar.updateSidebarImageCount();
    assert.equal(elements.get('viewer-collection-count').textContent, '0');
});

test('a small album reports its own size and loaded count', () => {
    state.setImages([{ id: 1 }, { id: 2 }]);
    state.setTotalImages(3);
    sidebar.updateSidebarImageCount();
    const count = elements.get('viewer-collection-count');
    assert.equal(count.textContent, '3');
    assert.match(count.title, /2 \/ 3/);
});

test('refreshing navigation needs no thumbnail container or DOM creation', () => {
    state.setImages([{ id: 1 }]);
    state.setTotalImages(1);
    sidebar.renderSidebar({ reconcile: true });
    assert.equal(elements.get('viewer-collection-count').textContent, '1');
});

test('switching collection clears the previous picker even when source and album IDs overlap', () => {
    const createItem = dataset => {
        const attributes = new Map();
        const classes = new Set();
        const control = {
            setAttribute: (name, value) => attributes.set(name, value),
            removeAttribute: name => attributes.delete(name),
        };
        return { dataset, attributes, classes, ...control,
            classList: { toggle: (name, active) => active ? classes.add(name) : classes.delete(name) },
            querySelector: () => control };
    };
    const source = createItem({ sourceId: '1' });
    const album = createItem({ albumId: '1' });
    navigationItems.push(source, album);
    state.setCurrentCollection({ type: 'folder', id: 1 });
    sidebar.syncCollectionPickers();
    assert.equal(source.attributes.get('aria-current'), 'true');
    assert.equal(album.classes.has('active'), false);
    state.setCurrentCollection({ type: 'album', id: 1 });
    sidebar.syncCollectionPickers();
    assert.equal(source.attributes.has('aria-current'), false);
    assert.equal(source.classes.has('active'), false);
    assert.equal(album.attributes.get('aria-current'), 'true');
    state.setCurrentCollection({ type: 'media' });
    sidebar.syncCollectionPickers();
    assert.equal(album.classes.has('active'), false);
    navigationItems.length = 0;
});
