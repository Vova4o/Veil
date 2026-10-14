from __future__ import annotations

import hashlib
import io
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from app import database as db
from app import job_worker, library_repair, worker
from app.indexing import SourceSnapshot, index_source_directory
from app.main import app
from app.paths import build_runtime_paths


@pytest.fixture
def repair_runtime(tmp_path, monkeypatch):
    previous = db.get_db_path()
    paths = build_runtime_paths({'MEDIASTUDIO_DATA_DIR': str(tmp_path / 'data'),
                                'MEDIASTUDIO_CACHE_DIR': str(tmp_path / 'cache')}, project_root=tmp_path)
    paths.ensure_directories()
    db.set_db_path(paths.database)
    db.init_db()
    for key, value in paths.flask_config().items():
        monkeypatch.setitem(app.config, key, value)
    monkeypatch.setattr(job_worker, 'start_job_worker', lambda: None)
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('a.png', 'b.png'):
        Image.new('RGB', (7, 9), 'green').save(source / name)
    result = index_source_directory(source, thumbnail_dir=paths.thumbnails,
        preview_dir=paths.previews, cutout_dir=paths.cutouts)
    with closing(db.get_conn()) as conn:
        ids = [row['id'] for row in conn.execute('SELECT id FROM images ORDER BY id')]
    yield paths, source, result.folder_id, ids
    db.set_db_path(previous)


def run_check():
    client = app.test_client()
    accepted = client.post('/api/library/repair', json={})
    assert accepted.status_code == 202
    assert job_worker.process_pending_job()
    return client.get('/api/library/repair').json['repair']


def test_repair_preserves_originals_and_virtual_organization(repair_runtime):
    paths, source, folder_id, ids = repair_runtime
    originals = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}
    with closing(db.get_conn()) as conn:
        conn.execute("UPDATE images SET rating=5, is_favorite=1, note='keep me', error='retry' WHERE id=?", (ids[0],))
        album = conn.execute("INSERT INTO albums(name, custom_order) VALUES ('kept',1)").lastrowid
        conn.executemany('INSERT INTO album_images(album_id,image_id,position) VALUES (?,?,?)',
                         [(album, ids[1], 0), (album, ids[0], 1)])
        conn.execute("INSERT INTO tags(name,name_key) VALUES ('kept-tag','kept-tag')")
        conn.execute('INSERT INTO image_tags(image_id,tag_id) VALUES (?,last_insert_rowid())', (ids[0],))
        conn.executemany('INSERT INTO item_positions(scope,image_id,position) VALUES (?,?,?)',
                         [(f'folder:{folder_id}', ids[1], 0), (f'folder:{folder_id}', ids[0], 1)])
        conn.commit()
    result = run_check()
    assert result['status'] == 'completed'
    assert result['repaired'] == 2
    assert result['errors'] == []
    with closing(db.get_conn()) as conn:
        row = conn.execute('SELECT * FROM images WHERE id=?', (ids[0],)).fetchone()
        assert (row['rating'], row['is_favorite'], row['note'], row['error']) == (5, 1, 'keep me', None)
        assert [r['image_id'] for r in conn.execute('SELECT image_id FROM album_images ORDER BY position')] == ids[::-1]
        assert [r['image_id'] for r in conn.execute('SELECT image_id FROM item_positions ORDER BY position')] == ids[::-1]
        assert conn.execute('SELECT COUNT(*) FROM image_tags').fetchone()[0] == 1
    assert originals == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}
    healthy = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths.thumbnails.iterdir()}
    second = run_check()
    assert second['unchanged'] == 2 and second['repaired'] == 0
    assert healthy == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths.thumbnails.iterdir()}


def test_duplicate_submission_and_restart_replay(repair_runtime):
    client = app.test_client()
    first = client.post('/api/library/repair', json={}).json['repair']
    second = client.post('/api/library/repair', json={}).json['repair']
    assert first['id'] == second['id']
    claimed = db.claim_next_job()
    assert claimed
    # Simulate shutdown partway through the durable job.
    library_repair.run_repair(claimed['payload'], stop_requested=lambda: True)
    assert db.reset_stuck_jobs() == 1
    assert job_worker.process_pending_job()
    assert client.get('/api/library/repair').json['repair']['status'] == 'completed'


def test_broken_thumbnail_repaired_but_healthy_metadata_retained(repair_runtime):
    paths, _, _, ids = repair_runtime
    run_check()
    with closing(db.get_conn()) as conn:
        before = conn.execute('SELECT metadata_json FROM images WHERE id=?', (ids[0],)).fetchone()[0]
    (paths.thumbnails / f'{ids[0]}.jpg').write_bytes(b'broken jpeg')
    preview = paths.previews / f'{ids[0]}-cached.jpg'
    preview.write_bytes(b'broken preview')
    with patch.object(worker, 'extract_metadata', side_effect=AssertionError('healthy metadata must be retained')):
        result = run_check()
    assert result['repaired'] == 1 and result['unchanged'] == 1
    assert library_repair._valid_image(paths.thumbnails / f'{ids[0]}.jpg')
    assert not preview.exists()
    with closing(db.get_conn()) as conn:
        assert conn.execute('SELECT metadata_json FROM images WHERE id=?', (ids[0],)).fetchone()[0] == before


def test_missing_original_keeps_annotated_record(repair_runtime):
    _, source, _, ids = repair_runtime
    (source / 'a.png').unlink()
    with closing(db.get_conn()) as conn:
        conn.execute("UPDATE images SET note='keep unavailable' WHERE id=?", (ids[0],))
        conn.commit()
    result = run_check()
    assert result['skipped'] == 1 and result['errors']
    with closing(db.get_conn()) as conn:
        assert conn.execute('SELECT note FROM images WHERE id=?', (ids[0],)).fetchone()[0] == 'keep unavailable'


def test_unreadable_file_is_reported_not_counted_as_repaired(repair_runtime):
    _, source, _, _ = repair_runtime
    (source / 'a.png').write_bytes(b'incomplete file')
    result = run_check()
    assert result['failed'] == 1 and result['repaired'] == 1
    assert result['errors']


def test_api_rejects_invalid_or_disabled_scope(repair_runtime):
    _, _, folder_id, _ = repair_runtime
    client = app.test_client()
    assert client.post('/api/library/repair', json=[]).status_code == 400
    for value in (True, -1, '1', 1.1):
        assert client.post('/api/library/repair', json={'source_id': value}).status_code == 400
    assert client.post('/api/library/repair', json={'source_id': 9999}).status_code == 404
    db.update_source_settings(folder_id, enabled=False)
    assert client.post('/api/library/repair', json={'source_id': folder_id}).status_code == 409


def test_worker_publishes_metadata_before_thumbnail_decode(repair_runtime):
    paths, _, _, ids = repair_runtime
    original_make = worker.make_thumbnail_bytes
    def slow_thumbnail(path: Path):
        with closing(db.get_conn()) as conn:
            assert conn.execute('SELECT metadata_json FROM images WHERE id=?', (ids[0],)).fetchone()[0]
        return original_make(path)
    with patch.object(worker, 'make_thumbnail_bytes', side_effect=slow_thumbnail):
        assert worker.process_asset(ids[0], paths.thumbnails)


def test_truncated_jpeg_with_valid_header_is_not_a_healthy_cache(tmp_path):
    path = tmp_path / 'preview.jpg'
    Image.new('RGB', (64, 64), 'blue').save(path)
    path.write_bytes(path.read_bytes()[:-10])
    assert not library_repair._valid_image(path)


def test_worker_retains_pending_metadata_when_source_goes_offline(repair_runtime):
    paths, source, folder_id, ids = repair_runtime
    source.rename(source.with_name('temporarily-offline'))
    assert not worker.process_asset(ids[0], paths.thumbnails)
    assert db.get_folder_record(folder_id)['source_status'] == 'unavailable'
    with closing(db.get_conn()) as conn:
        row = conn.execute('SELECT metadata_json,error FROM images WHERE id=?', (ids[0],)).fetchone()
    assert row['metadata_json'] is None and row['error'] is None


def test_uploaded_original_is_repaired_without_replacing_blob(repair_runtime):
    paths, source, _, _ = repair_runtime
    original = (source / 'a.png').read_bytes()
    response = app.test_client().post('/api/upload', data={'files': (io.BytesIO(original), 'uploaded.png')})
    assert response.status_code == 200
    asset_id = response.json['assets'][0]['id']
    with closing(db.get_conn()) as conn:
        conn.execute("UPDATE images SET error='retry upload' WHERE id=?", (asset_id,))
        conn.commit()
    (paths.thumbnails / f'{asset_id}.jpg').write_bytes(b'broken upload thumbnail')
    result = run_check()
    assert result['failed'] == 0 and result['repaired'] == 3
    assert db.get_asset_original_data(asset_id) == original


def test_type_order_does_not_wait_for_metadata(repair_runtime):
    paths, source, folder_id, ids = repair_runtime
    Image.new('RGB', (6, 6), 'red').save(source / 'z.jpg')
    index_source_directory(source, thumbnail_dir=paths.thumbnails,
        preview_dir=paths.previews, cutout_dir=paths.cutouts)
    before = [item.id for item in db.get_images_page(folder_id, sort_by='type', sort_dir='asc').images]
    # Populate PNG format first while JPEG is still pending.
    assert worker.process_asset(ids[0], paths.thumbnails)
    during = [item.id for item in db.get_images_page(folder_id, sort_by='type', sort_dir='asc').images]
    assert before == during


def test_changing_source_is_skipped_without_rewriting_cached_state(repair_runtime):
    paths, source, _, _ = repair_runtime
    run_check()
    before = {path: path.read_bytes() for path in paths.thumbnails.iterdir()}
    normal_scan = library_repair.scan_source_directory
    first = normal_scan(source, recursive=False)
    second = SourceSnapshot(source, False, {})
    with patch.object(library_repair, 'scan_source_directory', side_effect=[first, second]):
        result = run_check()
    assert result['skipped'] == 2 and result['repaired'] == 0
    assert 'changing' in result['errors'][0]
    assert before == {path: path.read_bytes() for path in paths.thumbnails.iterdir()}


def test_interrupted_job_failure_has_visible_retry_state(repair_runtime):
    app.test_client().post('/api/library/repair', json={})
    with patch.object(library_repair, 'process_asset', side_effect=OSError('cache temporarily locked')):
        assert job_worker.process_pending_job()
    result = library_repair.get_repair()
    assert result['status'] == 'pending' and result['phase'] == 'retrying'
    assert 'cache temporarily locked' in result['errors'][-1]
