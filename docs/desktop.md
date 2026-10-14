# Electron desktop

The first desktop increment keeps the existing Flask API, Jinja pages, SQLite
library and ComfyUI manager. Electron supplies the native window and directory
dialog; TypeScript owns the Python process lifetime. Windows is the first
acceptance target. macOS/Linux packaging entries are scaffolding, not verified
release support.

## Launch

- `start.bat` launches the built Windows app when available, otherwise the
  development Electron shell.
- `desktop.bat` always launches the development Electron shell from source, so
  a stale `dist/` build never hides local changes. It installs missing Node.js
  and `.venv` dependencies and pauses on errors. `desktop.bat --packaged` runs
  `dist/desktop/win-unpacked/VEIL.exe`.
- `npm run desktop` compiles the TypeScript shell and launches it against the
  project's `.venv` (`COMFY_META_PYTHON` can select a different interpreter).
- `start.bat --web` / `start.sh --web` retain browser development. On Windows,
  the launcher uses the imported desktop data if available. Run one runtime
  against a library at a time.

Development prerequisites: Node.js, `npm ci`, and
`poetry install --no-root --with dev,desktop`.
Create the environment in `.venv` (`poetry config virtualenvs.in-project true --local`)
or point `COMFY_META_PYTHON` at the installed interpreter.

## Runtime and user data

The renderer uses a stable `cmv://app` origin. A standard, secure Electron
protocol proxies Flask pages and requests to an authenticated loopback server
on an OS-assigned port. Streaming responses, multipart uploads and video Range
requests retain their existing API behavior. Browser preferences therefore
survive backend port changes.

Python uses Waitress rather than the Flask development server. It announces
readiness with its PID, assigned port and a per-launch nonce. Electron checks the
nonce on its private child pipe (Windows venv launchers can have a separate PID), bounds startup,
and closes the child over stdin. Parent EOF also shuts down Python. The shared
service context stops watchers, queue/download workers and only the ComfyUI
process owned by the manager. A bounded fallback terminates that backend tree
if graceful shutdown does not finish. An unexpected backend exit shows a retry
screen and a link to local logs.

Node integration is disabled; context isolation and renderer sandboxing stay
enabled. The preload exposes only directory selection, retry and log opening,
with main-frame/sender checks. External HTTP(S) links use the system browser.
Only app-origin main frames receive clipboard-write and fullscreen permission;
clipboard reads and unrelated permissions are denied.
The renderer never receives the backend authentication token.
Opening an original uses a separate sandboxed window and keeps the library open.

Default Windows profile: `%APPDATA%\Comfy Meta Viewer`:

- `data/`: SQLite, configuration and user workflow templates;
- `cache/`: generated thumbnails, previews and cutouts;
- `logs/desktop-backend.log`: backend startup and errors;
- Chromium storage and `window.json`: UI preferences and window size.

`COMFY_META_DESKTOP_PROFILE` selects an isolated profile. Existing
`COMFY_META_DATA_DIR` / `COMFY_META_CACHE_DIR` overrides remain supported.
`COMFY_META_IMPORT_DIR` imports an existing `.comfy_meta_uploads` directory only
into an empty target. Source-mode launch automatically finds the legacy library
in the workspace. `start.bat` also passes that import path to a built app.
The import uses SQLite backup (including committed WAL data), copies configuration
and user templates, checks database integrity, and publishes the staged result
by renaming it. Existing destination data and original sources are preserved.
`desktop-import.json` records provenance. Matching legacy thumbnails and display
previews are reused through read-only lookup in the old `cache/` directory;
`COMFY_META_IMPORT_CACHE_DIR` can select another legacy cache. Both databases are
compared by asset ID, source path, relative path, size, modification time, media
type and content fingerprint. Each request rechecks the current source identity.
Existing desktop caches take precedence. Missing/stale legacy entries generate
normal desktop caches. No bulk cache copy delays startup, and no legacy files
are changed. Removing the old cache remains safe; it only removes acceleration.

## Windows build

`npm run desktop:package` compiles Electron, freezes the backend, bundles FFmpeg
and FFprobe, then creates the Windows NSIS installer. Set
`COMFY_META_MEDIA_TOOLS_DIR` if the two real binaries are not on PATH. The build
resolves Chocolatey shims to their installed FFmpeg binaries when possible.

Outputs:

- `dist/desktop/win-unpacked/VEIL.exe`;
- `dist/desktop/VEIL-1.0.0-Setup.exe`.

The unpacked directory must travel together. The installer includes its Python
runtime and video tools, requires no system Python/Poetry/Node.js, and preserves
application data on uninstall. ComfyUI remains a separately configured runtime.
Signing, auto-update and public release delivery are not enabled in this increment.

## Acceptance

`npm run desktop:smoke -- --video-dir <directory>` launches real Electron against
an isolated imported library. It uploads small videos through the existing UI,
checks generated thumbnails and byte ranges, plays and seeks decoded video,
opens the editor, checks sandbox/loopback authentication, kills its own test
backend, retries, verifies stable browser storage, and checks backend exit.
Closing the library also closes its media windows and stops the owned backend.
It also checks video fullscreen, clipboard permissions, opening an original in its own window and the directory-dialog IPC
with a cancelled dialog stub; this does not claim an automated OS-dialog selection.

Add `--executable <absolute executable path>` to exercise the packaged app with
PATH restricted to Windows system tools. Evidence is written to the isolated
`build/desktop-smoke-*` profile. Live generated ComfyUI jobs, actual third-party
OAuth completion, other operating systems and updates require separate acceptance.

Windows acceptance on 2026-10-08 passed for both development Electron and the
packaged executable: three H.264 MP4s decoded, played and sought, including
portrait and landscape frames. The packaged run had only Windows system tools
on PATH. Full repository checks passed: 550 Python tests with 92 subtests,
52 JavaScript tests, lint, typing and API contracts. The NSIS installer was built;
installer execution on a clean Windows machine remains a separate release check.

## Gallery performance

The gallery page observer follows the actual scroll container and requests the
next page two viewport heights ahead. New image thumbnails shrink before EXIF
transpose to avoid a second full-resolution pixel buffer, use decoder downsampling
for JPEG, encode opaque alpha images as JPEG, and preserve genuine transparency
as PNG. At most two thumbnail decoders run concurrently, including the indexing
worker. Original pixels and full display-preview resolution are unchanged.

`node scripts/desktop_perf.cjs` compares gallery scrolling and lightbox paint
with direct HTTP on the same test-owned server/library. Use `--no-cache-reuse`
for new-cache generation and `--asset-ids <comma-separated IDs>` to include large
originals. Add `--executable <path>` for the packaged app. Reports are stored in
`build/desktop-perf-*/performance.json` without owner authentication tokens.
