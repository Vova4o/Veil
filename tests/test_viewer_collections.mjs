import test from 'node:test';
import assert from 'node:assert/strict';
import { collectionCount, sourcePresentation } from '../app/static/js/components/viewer-collections.js';

test('mixed media sources count videos and images together', () => {
    const result = collectionCount({ image_count: 3, video_count: 2, asset_count: 5 });
    assert.equal(result.count, 5);
    assert.equal(result.label, '5 items');
    assert.equal(result.description, '3 images, 2 videos');
    assert.equal(collectionCount({ image_count: 0, video_count: 1 }).label, '1 item');
});

test('albums do not mislabel videos as images', () => {
    assert.equal(collectionCount({ asset_count: 1 }, { album: true }).label, '1 item');
    assert.equal(collectionCount({ asset_count: 0 }, { album: true }).count, 0);
});

test('indexing progress uses processed assets when video is included', () => {
    const source = { enabled: true, status: 'processing', image_count: 2, video_count: 2,
        asset_count: 4, processed_count: 2, processed_asset_count: 3 };
    assert.equal(sourcePresentation(source).percent, 75);
    assert.equal(sourcePresentation(source).processing, true);
});

test('unknown totals and inconsistent progress stay within valid bounds', () => {
    assert.equal(sourcePresentation({ status: 'processing', image_count: 0 }).percent, null);
    assert.equal(sourcePresentation({ image_count: 10, processed_count: 15 }).percent, 100);
    assert.equal(sourcePresentation({ image_count: 10, processed_count: -1 }).percent, 0);
    assert.equal(collectionCount({ asset_count: Infinity }).count, 0);
});

test('disabled sources remain disabled even while an old processing status is cached', () => {
    const result = sourcePresentation({ enabled: false, status: 'processing', source_status: 'available' });
    assert.equal(result.label, 'Disabled');
    assert.equal(result.processing, false);
});
