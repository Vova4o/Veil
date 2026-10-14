# Library checks and recovery

Viewer → More actions → **Check and repair library** starts a background check.
Library has the same action in its header (the circular arrow). Closing the status
dialog or changing pages leaves the server job running. Reopening the action
during a check shows its progress instead of starting another job. After a server
restart, the existing durable queue resumes the interrupted check.

The check reconciles enabled physical sources, including configured subfolders,
and checks uploaded originals stored inside VEIL. It retries missing, malformed or
failed embedded metadata; repairs missing or corrupt thumbnails; and removes
corrupt display previews so they can regenerate when opened. Healthy metadata and
preview files are retained. Asset IDs, albums, ratings, favorites, tags, notes,
AI annotations and custom positions remain attached to the existing records.
It never rewrites source files or uploaded-original BLOBs. Missing originals,
offline sources and files changing during the check are reported; the repair
itself does not delete those records. Normal source monitoring retains its
existing reconciliation behavior.

Progress and the latest result are stored in SQLite. The result distinguishes
repaired, already healthy, skipped and failed files, with up to 20 diagnostic
messages. Unexpected job failures use the existing queue's retry/backoff; an
exhausted retry is shown as a failed check. After finishing, Viewer and Library
refresh their current collection. No routine startup runs a full repair.

`POST /api/library/repair` starts or joins the single active repair. An optional
positive `source_id` limits the check to an enabled source. `GET` on the same URL
returns the latest persisted progress/result, or `null` before the first check.
These endpoints are documented in `site/api/openapi.json`.

**Reset index** remains a separate destructive operation: it deletes virtual
organization and DB-only uploaded originals before reindexing saved folders.
It is not the recovery action above.

## Preventing transient display errors

Viewer request caching has explicit generations: a late response cannot refill
an invalidated cache or replace a newer forced response. List loads and pagination
apply only to their current collection, sort/filter selection and request owner.
Folder event snapshots are coalesced and applied sequentially. Metadata loads are
shared only within the current detail-cache generation; late loads cannot reopen
a closed lightbox or replace a newer metadata view. Library's existing abort
controllers additionally guard publication after response parsing.

Sorting by type uses the MIME type already known at indexing; extracting embedded
metadata cannot change that sort key. Other sort keys retain deterministic ID
tie breakers. The worker publishes each image's metadata before decoding its
thumbnail, instead of waiting for a 50-item processing batch. Index mutations and
asset processing share a local lock, and processing rechecks asset ownership.
This is the existing single-process local application model, not a cross-process
worker protocol.

## Verification (2026-10-14)

- `tests/test_request_state.mjs` controls response order, forced refreshes,
  invalidation, request sharing and retry after failure.
- `tests/test_library_repair.py` checks original-byte preservation, virtual
  organization, idempotency, restart replay, uploads, missing/broken originals,
  healthy-cache retention, type-order stability and metadata publication timing.
- `node scripts/library_repair_smoke.cjs` runs real Viewer/Library pages in Edge
  against an isolated backend. It delays sort, folder and metadata responses,
  verifies closing during loading, runs repair on both screens, and checks Escape
  and page errors. Evidence and screenshots go to the ignored `reports/` folder.
- Windows HTTP tests should run with `NO_PROXY=localhost,127.0.0.1,::1` so the
  configured system proxy does not intercept the isolated loopback server.

These checks establish application behavior on test assets. They do not measure
repair time on the user's full library or constitute a packaged Electron test.
