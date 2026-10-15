/**
 * Create sessions: the Session panel works like a chat list.
 *
 * Every Create run belongs to the current session. The server keeps sessions and names
 * them from the first request (a cut of the text first, then a short title from the free
 * OpenCode model). The panel shows the session list or one session's log: each request
 * with its time, the prompt the agent wrote, its notes, provider errors and the images.
 * An image opens in Viewer's lightbox; a deleted one shows a placeholder.
 */
import { openLightbox, closeLightbox, prevLightbox, nextLightbox } from '../lightbox.js';
import { dom } from '../state.js';

const $ = id => document.getElementById(id);
const CURRENT_KEY = 'mediastudio_create_session_id';
const HINTED_KEY = 'mediastudio_create_session_hinted';
const ACTIVE = new Set(['agent', 'queued', 'running']);
const POLL_MS = 2500;
// A fresh name may arrive from the naming model shortly after the first run.
const NAMING_WINDOW_MS = 3 * 60 * 1000;

const MODE_LABELS = { text: 'Prompt', variation: 'Prompt + photo', reference: 'Recreate', edit: 'Edit' };
const STATUS_LABELS = {
    agent: 'The agent is writing the prompt…',
    queued: 'Prompt ready · waiting for the GPU',
    running: 'Creating…',
    completed: '',
    failed: 'Failed',
    cancelled: 'Stopped',
};
const ICONS = {
    list: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M3 12h18M3 18h12"></path></svg>',
    back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"></path></svg>',
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"></rect><path d="M5 15V5a2 2 0 0 1 2-2h10"></path></svg>',
    trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"></path></svg>',
    deleted: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7"></path><circle cx="9" cy="9" r="2"></circle><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"></path><path d="m16 3 5 5M21 3l-5 5"></path></svg>',
    error: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 8v5M12 16h.01"></path></svg>',
};

const state = {
    id: null,
    session: null,       // the server record of the current session, null while it has no runs
    fresh: false,        // a new session this page started; the server has no record of it yet
    runs: [],
    sessions: [],
    view: 'log',
    suggestAfter: 20,
    poll: null,
    loading: null,
    gallery: [],
    onOpenPrompt: null,
};

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function read(key) {
    try { return localStorage.getItem(key); } catch { return null; }
}

function write(key, value) {
    try {
        if (value == null) localStorage.removeItem(key);
        else localStorage.setItem(key, value);
    } catch {}
}

async function api(path, options = {}) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(data.error || `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return data;
}

function newId() {
    return crypto.randomUUID ? crypto.randomUUID() : `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

/** The session the next Create run belongs to. */
export function currentSessionId() {
    return state.id;
}

function isOpen() {
    return !$('session').hidden;
}

// --- dates ---------------------------------------------------------------------------

function dayKey(date) {
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayLabel(date) {
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (dayKey(date) === dayKey(today)) return 'Today';
    if (dayKey(date) === dayKey(yesterday)) return 'Yesterday';
    return date.toLocaleDateString([], { day: 'numeric', month: 'long', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}

function timeLabel(date) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function listGroup(date) {
    const days = Math.floor((startOfDay(new Date()) - startOfDay(date)) / 86400000);
    if (days <= 0) return 'Today';
    if (days === 1) return 'Yesterday';
    if (days < 7) return 'Previous 7 days';
    if (days < 30) return 'Previous 30 days';
    return 'Older';
}

function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function runsLabel(count) {
    return `${count} ${count === 1 ? 'run' : 'runs'}`;
}

// --- loading -------------------------------------------------------------------------

async function loadCurrent() {
    const id = state.id;
    if (state.fresh) return;
    try {
        const data = await api(`/api/simple/sessions/${encodeURIComponent(id)}`);
        if (state.id !== id) return;
        state.session = data.session;
        state.runs = data.runs || [];
        state.suggestAfter = data.suggest_new_after || state.suggestAfter;
    } catch (err) {
        if (state.id !== id) return;
        if (err.status === 404) {
            state.session = null;
            state.runs = [];
        }
    }
}

async function loadList() {
    try {
        const data = await api('/api/simple/sessions');
        state.sessions = data.sessions || [];
        state.suggestAfter = data.suggest_new_after || state.suggestAfter;
    } catch {}
}

/** Reload what the panel shows; calls made while one load runs share it. */
export function refreshSessions() {
    if (state.loading) {
        state.loading.again = true;
        return state.loading.promise;
    }
    const loading = { again: false };
    loading.promise = (async () => {
        // Sequential on purpose: a change that arrives mid-load gets one more pass.
        do {
            loading.again = false;
            await loadCurrent(); // eslint-disable-line no-await-in-loop
            if (isOpen() && state.view === 'list') await loadList(); // eslint-disable-line no-await-in-loop
        } while (loading.again);
    })().finally(() => {
        state.loading = null;
        paint();
        schedulePoll();
    });
    state.loading = loading;
    return loading.promise;
}

function needsPolling() {
    if (state.runs.some(run => ACTIVE.has(run.status))) return true;
    if (state.session?.title_state === 'fallback') {
        const created = Date.parse(state.session.created_at || '');
        return Number.isFinite(created) && Date.now() - created < NAMING_WINDOW_MS;
    }
    return false;
}

function schedulePoll() {
    clearTimeout(state.poll);
    state.poll = null;
    if (!needsPolling()) return;
    state.poll = setTimeout(() => refreshSessions(), POLL_MS);
}

// --- switching -----------------------------------------------------------------------

function selectSession(id, { view = 'log' } = {}) {
    state.id = id;
    state.fresh = false;
    write(CURRENT_KEY, id);
    state.session = state.sessions.find(item => item.id === id) || null;
    state.runs = [];
    state.view = view;
    paint();
    return refreshSessions();
}

/** Start an empty session; it is saved with its first run. */
export function newSession() {
    hideHint();
    state.id = newId();
    state.fresh = true;
    write(CURRENT_KEY, state.id);
    state.session = null;
    state.runs = [];
    state.view = 'log';
    paint();
    schedulePoll();
}

/** A run of the current session was accepted by the server. */
export async function sessionRunStarted() {
    state.fresh = false;
    await refreshSessions();
    maybeHint();
}

// --- the 20-run hint -----------------------------------------------------------------

function hinted() {
    try { return new Set(JSON.parse(read(HINTED_KEY) || '[]')); } catch { return new Set(); }
}

function maybeHint() {
    const count = state.runs.length;
    if (count < state.suggestAfter) return;
    const seen = hinted();
    if (seen.has(state.id)) return;
    seen.add(state.id);
    write(HINTED_KEY, JSON.stringify([...seen].slice(-200)));
    $('session-hint-text').textContent = `This session has ${count} runs. The agent rereads the whole session every time, `
        + 'so a long one gets slower and less precise. A new session starts it fresh; you can also keep going here.';
    $('session-hint').hidden = false;
}

function hideHint() {
    $('session-hint').hidden = true;
}

// --- painting ------------------------------------------------------------------------

function paint() {
    if (!isOpen()) return;
    const list = state.view === 'list';
    $('session-back').innerHTML = list ? ICONS.back : ICONS.list;
    $('session-back').title = list ? 'Back to the session' : 'All sessions';
    $('session-back').setAttribute('aria-label', $('session-back').title);
    $('session-kicker').textContent = list ? 'CREATE' : 'SESSION';
    if ($('session-title-input').hidden) {
        $('session-title').textContent = list ? 'Sessions' : (state.session?.title || 'New session');
    }
    $('session-title').classList.toggle('is-renamable', !list && Boolean(state.session));
    paintBanner();
    if (list) paintList();
    else paintLog();
}

function paintBanner() {
    const show = state.view === 'log' && state.runs.length >= state.suggestAfter;
    $('session-banner').hidden = !show;
    if (show) {
        $('session-banner-text').textContent = `${runsLabel(state.runs.length)} in this session. `
            + 'A new session keeps the agent quick and focused.';
    }
}

function paintList() {
    const body = $('session-body');
    if (!state.sessions.length) {
        body.innerHTML = '<div class="session-empty"><strong>No sessions yet</strong><p>Your first run starts one.</p></div>';
        return;
    }
    const groups = new Map();
    for (const item of state.sessions) {
        const date = new Date(item.updated_at || item.created_at);
        const label = listGroup(date);
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push({ item, date });
    }
    body.innerHTML = [...groups].map(([label, items]) => `<section class="session-group"><h3>${esc(label)}</h3>${items.map(({ item, date }) => `
        <div class="session-row${item.id === state.id ? ' is-current' : ''}" data-session="${esc(item.id)}" role="button" tabindex="0">
            <div class="session-row-copy"><strong>${esc(item.title || 'New session')}</strong>
            <span>${esc(runsLabel(item.runs))} · ${esc(listGroup(date) === 'Today' ? timeLabel(date) : date.toLocaleDateString())}</span></div>
            <button type="button" class="session-row-delete" data-delete="${esc(item.id)}" aria-label="Delete session" title="Delete session (images stay in the library)">${ICONS.trash}</button>
        </div>`).join('')}</section>`).join('');
    body.querySelectorAll('[data-session]').forEach(row => {
        const open = () => selectSession(row.dataset.session);
        row.onclick = event => { if (!event.target.closest('[data-delete]')) open(); };
        row.onkeydown = event => { if (event.key === 'Enter') open(); };
    });
    body.querySelectorAll('[data-delete]').forEach(button => {
        button.onclick = () => deleteSession(button.dataset.delete);
    });
}

function outputTile(output) {
    if (output.deleted) {
        return `<div class="session-tile is-deleted" title="This image was deleted">${ICONS.deleted}<span>Deleted</span></div>`;
    }
    const index = state.gallery.findIndex(item => item.id === output.id);
    return `<button type="button" class="session-tile" data-gallery="${index}" title="${esc(output.file_name || 'Open')}">`
        + `<img src="/api/thumbnail/${output.id}" alt="" loading="lazy"></button>`;
}

function runCard(run) {
    const date = new Date(run.created_at);
    const label = run.mode === 'edit' && run.with_reference ? 'Edit with reference' : MODE_LABELS[run.mode] || 'Run';
    const promptTitle = run.mode === 'edit' ? 'Edit command' : 'Prompt';
    const meta = [run.use_agent ? (run.agent_name || 'Agent') : 'Own prompt', run.edit_profile_label,
        run.mode === 'variation' && run.denoise != null ? `noise ${Number(run.denoise).toFixed(2)}` : '',
        run.backend === 'colab' ? 'Colab' : ''].filter(Boolean).join(' · ');
    const status = STATUS_LABELS[run.status] ?? '';
    const pending = Math.max(0, (run.batch_size || 1) - run.outputs.length);
    const placeholders = ACTIVE.has(run.status) ? '<div class="session-tile is-pending"><span class="session-spinner"></span></div>'.repeat(pending) : '';
    const tiles = run.outputs.map(outputTile).join('') + placeholders;
    const error = run.error ? `<div class="session-error">${ICONS.error}<div><strong>${esc(run.error.error)}</strong>`
        + `${run.error.technical_error ? `<details><summary>Details</summary><pre>${esc(run.error.technical_error)}</pre></details>` : ''}</div></div>` : '';
    return `<article class="session-run" data-task="${esc(run.task_id)}">
        <div class="session-request-bubble"><div class="session-bubble-head"><span class="session-mode">${esc(label)}</span><time datetime="${esc(run.created_at)}">${esc(timeLabel(date))}</time></div>
            <p>${run.request ? esc(run.request) : '<em>No text, the photo only</em>'}</p></div>
        <div class="session-answer">
            <div class="session-answer-head"><span>${esc(meta)}</span>${status ? `<span class="session-status is-${esc(run.status)}">${esc(status)}</span>` : ''}</div>
            ${run.prompt ? `<details class="session-prompt-block"${run.use_agent ? '' : ' open'}><summary>${promptTitle}</summary><p>${esc(run.prompt)}</p>${run.negative_prompt ? `<p class="session-negative">Remove: ${esc(run.negative_prompt)}</p>` : ''}<div class="session-prompt-actions"><button type="button" class="session-link-btn" data-copy="${esc(run.task_id)}">${ICONS.copy}<span>Copy</span></button><button type="button" class="session-link-btn" data-open-prompt="${esc(run.task_id)}">Open prompt</button></div></details>` : ''}
            ${run.note ? `<p class="session-note">${esc(run.note)}</p>` : ''}
            ${error}
            ${tiles ? `<div class="session-tiles">${tiles}</div>` : ''}
        </div>
    </article>`;
}

function paintLog() {
    const body = $('session-body');
    const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 40 || body.dataset.view !== `log:${state.id}`;
    body.dataset.view = `log:${state.id}`;
    if (!state.runs.length) {
        body.innerHTML = '<div class="session-empty"><strong>New session</strong><p>Your next run starts it. The agent names the session after your first request and remembers the earlier runs while you keep working in it.</p></div>';
        state.gallery = [];
        return;
    }
    state.gallery = state.runs.flatMap(run => run.outputs.filter(output => !output.deleted).map(output => ({
        id: output.id,
        file_name: output.file_name,
        media_type: output.media_type || 'image',
        width: output.width,
        height: output.height,
        size: output.width && output.height ? [output.width, output.height] : null,
    })));
    let lastDay = '';
    const html = [];
    for (const run of state.runs) {
        const date = new Date(run.created_at);
        const day = dayKey(date);
        if (day !== lastDay) {
            html.push(`<div class="session-day"><span>${esc(dayLabel(date))}</span></div>`);
            lastDay = day;
        }
        html.push(runCard(run));
    }
    body.innerHTML = html.join('');
    body.querySelectorAll('[data-gallery]').forEach(button => {
        button.onclick = () => openImage(Number(button.dataset.gallery));
    });
    body.querySelectorAll('[data-copy]').forEach(button => {
        button.onclick = async () => {
            const run = state.runs.find(item => item.task_id === button.dataset.copy);
            if (!run) return;
            try {
                await navigator.clipboard.writeText(run.prompt);
                button.classList.add('is-done');
                button.querySelector('span').textContent = 'Copied';
                setTimeout(() => {
                    button.classList.remove('is-done');
                    button.querySelector('span').textContent = 'Copy';
                }, 1400);
            } catch {}
        };
    });
    body.querySelectorAll('[data-open-prompt]').forEach(button => {
        button.onclick = () => {
            const run = state.runs.find(item => item.task_id === button.dataset.openPrompt);
            if (run && state.onOpenPrompt) {
                closeSessions();
                state.onOpenPrompt(run);
            }
        };
    });
    if (stick) body.scrollTop = body.scrollHeight;
}

function openImage(index) {
    if (index < 0 || !state.gallery[index]) return;
    openLightbox(index, state.gallery);
}

// --- actions -------------------------------------------------------------------------

async function deleteSession(id) {
    const item = state.sessions.find(entry => entry.id === id);
    if (!window.confirm(`Delete the session "${item?.title || 'New session'}"? Its images stay in the library.`)) return;
    try {
        await api(`/api/simple/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    } catch {}
    state.sessions = state.sessions.filter(entry => entry.id !== id);
    if (id === state.id) {
        state.id = newId();
        state.fresh = true;
        write(CURRENT_KEY, state.id);
        state.session = null;
        state.runs = [];
    }
    paint();
}

function startRename() {
    if (state.view !== 'log' || !state.session) return;
    const input = $('session-title-input');
    input.value = state.session.title || '';
    input.hidden = false;
    $('session-title').hidden = true;
    input.focus();
    input.select();
}

async function finishRename(save) {
    const input = $('session-title-input');
    if (input.hidden) return;
    const title = input.value.trim();
    input.hidden = true;
    $('session-title').hidden = false;
    if (save && title && state.session && title !== state.session.title) {
        try {
            const data = await api(`/api/simple/sessions/${encodeURIComponent(state.id)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }),
            });
            state.session = { ...state.session, ...data.session };
        } catch {}
    }
    paint();
}

export function openSessions() {
    $('session-backdrop').hidden = false;
    $('session').hidden = false;
    state.view = 'log';
    paint();
    refreshSessions();
}

export function closeSessions() {
    finishRename(false);
    $('session-backdrop').hidden = true;
    $('session').hidden = true;
}

function lightboxOpen() {
    return dom.lightbox?.classList.contains('open');
}

function wireKeys() {
    // Viewer's keyboard module is not loaded in Create; the lightbox needs its own keys here.
    document.addEventListener('keydown', event => {
        if (!lightboxOpen()) return;
        if (event.key === 'Escape') closeLightbox();
        else if (event.key === 'ArrowLeft') prevLightbox();
        else if (event.key === 'ArrowRight') nextLightbox();
        else return;
        event.preventDefault();
        event.stopImmediatePropagation();
    }, true);
}

/** Wire the panel. ``onOpenPrompt(run)`` opens a run's prompt in Create's prompt window. */
export async function initSessions({ onOpenPrompt } = {}) {
    state.onOpenPrompt = onOpenPrompt || null;
    write('mediastudio_create_session', null); // the old browser-only log
    const stored = read(CURRENT_KEY);
    state.id = stored || newId();
    state.fresh = !stored;
    write(CURRENT_KEY, state.id);
    $('session-open').onclick = openSessions;
    $('session-close').onclick = closeSessions;
    $('session-backdrop').onclick = closeSessions;
    $('session-new').onclick = newSession;
    $('session-banner-new').onclick = newSession;
    $('session-hint-new').onclick = () => {
        newSession();
        openSessions();
    };
    $('session-hint-dismiss').onclick = hideHint;
    $('session-back').onclick = async () => {
        if (state.view === 'list') {
            state.view = 'log';
            paint();
            return;
        }
        state.view = 'list';
        paint();
        await loadList();
        paint();
    };
    $('session-title').ondblclick = startRename;
    $('session-title-input').onkeydown = event => {
        if (event.key === 'Enter') finishRename(true);
        if (event.key === 'Escape') {
            event.stopPropagation();
            finishRename(false);
        }
    };
    $('session-title-input').onblur = () => finishRename(true);
    wireKeys();
    const { initLightboxEvents } = await import('../lightbox.js');
    initLightboxEvents({ enableContextMenu: false });
    await refreshSessions();
}
