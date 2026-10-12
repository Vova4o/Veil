const $ = id => document.getElementById(id);
const MAX_PHOTO = 20 * 1024 * 1024;
const TXT2IMG_SECONDS = 50;
const SESSION_LIMIT = 40;
const RATIOS = ['1:1', '3:4', '4:3', '9:16', '16:9'];

const MODE_COPY = {
    text: {
        label: 'From text',
        hint: 'Describe the picture in your own words. The agent turns it into a detailed prompt.',
        placeholder: 'For example: a night café by the sea, warm window light, light fog and wet pavement',
        button: 'Create',
    },
    reference: {
        label: 'From photo',
        hint: 'Add a photo as a reference. The agent describes it and applies your wishes; the photo itself is not changed.',
        placeholder: 'Optional: what to change, for example "the same scene at night in the rain"',
        button: 'Create from photo',
    },
    edit: {
        label: 'Edit photo',
        hint: 'Add a photo and say what to change. The face, light and scene stay as they are.',
        placeholder: 'For example: change her sweater to a black satin camisole',
        button: 'Edit photo',
    },
};

// Titles of the reference blocks per mode; a mode without an entry hides that block.
const REF_COPY = {
    reference: {
        eyebrow: 'PHOTO',
        main: ['Reference photo', 'The agent describes it and applies your text'],
    },
    edit: {
        eyebrow: 'PHOTOS',
        main: ['Photo to edit', 'Face, light and scene stay as they are'],
        subject: ['Reference to bring in', 'Optional: a person, a garment or a tattoo'],
    },
};
const LAYOUT_EASING = 'cubic-bezier(.16,1,.3,1)';

const state = {
    models: [],
    modelId: '',
    health: null,
    mode: 'text',
    ratio: '1:1',
    photoRatio: 'source',
    batch: 1,
    photo: null,
    subject: null,
    slotFocus: 'main',
    refineFrom: null,
    agentStatus: null,
    canvasWidth: null,
    ambient: [],
    ambientDeck: [],
    ambientHistory: [],
    ambientCurrent: null,
    ambientLoadingMore: false,
    ambientRequest: 0,
    ambientPoolRequest: 0,
    ambientPicking: false,
    ambientPickRequest: 0,
    fit: 'cover',
    layer: 0,
    timer: null,
    timerInterval: 0,
    interval: 300000,
    opacity: 44,
    blur: 10,
    sound: false,
    job: null,
    poll: null,
    agentTimer: null,
    agentTick: null,
    agentPhase: 'started',
    agentName: '',
    last: null,
    lastOutputs: [],
    resultRefs: [],
    selectedResultIndex: 0,
    resultRequest: 0,
    lastRun: null,
    dialogRun: null,
    session: [],
    downloads: [],
    downloadPoll: null,
    runtimeConfig: null,
    runtimeStatus: null,
    runtimeBusy: null,
    runtimeStartedAt: 0,
    runtimePoll: null,
    // Settings changed while a ComfyUI started by Create runs: offer a restart.
    runtimeStale: false,
    desktop: null,
    runtimeDraft: null,
};

function catalog() {
    try { return JSON.parse($('simple-model-catalog')?.textContent || '[]'); }
    catch { return []; }
}

function restore() {
    try {
        const mode = localStorage.getItem('cmv_create_mode');
        if (MODE_COPY[mode]) state.mode = mode;
        const ratio = localStorage.getItem('cmv_simple_ratio');
        if (RATIOS.includes(ratio)) state.ratio = ratio;
        const photoRatio = localStorage.getItem('cmv_create_photo_ratio');
        if (photoRatio === 'source' || RATIOS.includes(photoRatio)) state.photoRatio = photoRatio;
        state.batch = Math.max(1, Math.min(4, Number(localStorage.getItem('cmv_simple_batch')) || 1));
        const savedCanvasWidth = Number(localStorage.getItem('cmv_simple_canvas_width'));
        if (Number.isFinite(savedCanvasWidth) && savedCanvasWidth > 0) state.canvasWidth = savedCanvasWidth;

        const rawInterval = localStorage.getItem('cmv_simple_ambient_interval');
        state.interval = rawInterval !== null && !isNaN(Number(rawInterval)) ? Number(rawInterval) : 300000;
        const rawOpacity = localStorage.getItem('cmv_simple_glass_opacity');
        const numOpacity = Number(rawOpacity);
        if (rawOpacity !== null && !isNaN(numOpacity) && numOpacity >= 5 && numOpacity <= 98) state.opacity = numOpacity;
        else if (rawOpacity === 'ultra') state.opacity = 18;
        else if (rawOpacity === 'high') state.opacity = 30;
        else if (rawOpacity === 'solid') state.opacity = 82;
        const rawBlur = localStorage.getItem('cmv_simple_ambient_blur');
        state.blur = rawBlur !== null && !isNaN(Number(rawBlur)) ? Number(rawBlur) : 10;
        state.fit = localStorage.getItem('cmv_simple_ambient_fit') || 'cover';
        state.sound = localStorage.getItem('cmv_simple_sound_alert') === 'true';

        const savedPrompt = localStorage.getItem('cmv_simple_prompt');
        if (savedPrompt !== null) $('prompt').value = savedPrompt;
        const session = JSON.parse(localStorage.getItem('cmv_create_session') || '[]');
        if (Array.isArray(session)) state.session = session.slice(0, SESSION_LIMIT);
        const lastRun = JSON.parse(localStorage.getItem('cmv_create_last_run') || 'null');
        if (lastRun && typeof lastRun.prompt === 'string') state.lastRun = lastRun;
    } catch {}
}

function init() {
    state.models = catalog();
    state.modelId = document.body.dataset.defaultModelId || state.models[0]?.id || '';
    restore();
    applyVisualSettings();
    hydrateAmbient();
    wire();
    wireCanvasResize();
    setMode(state.mode, { keepRefine: false, quiet: true });
    syncBatch();
    resizePrompt();
    $('prompt-clear').hidden = !$('prompt').value.trim();
    restoreLastResult();
    paintSession();
    paintHealth({ status: 'checking' });
    loadHealth();
    loadDownloads();
    loadAgentStatus();
    loadAmbient();
    watchAmbientSources();
    loadRuntimeSummary();
}

// --- modes ---------------------------------------------------------------------------

function setMode(mode, { keepRefine = false, quiet = false } = {}) {
    if (!MODE_COPY[mode]) return;
    const changed = state.mode !== mode;
    state.mode = mode;
    save('cmv_create_mode', mode);
    document.querySelectorAll('[data-mode]').forEach(button => {
        const active = button.dataset.mode === mode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-checked', String(active));
    });
    const copy = MODE_COPY[mode];
    $('mode-hint').textContent = copy.hint;
    $('prompt').placeholder = copy.placeholder;
    $('create-label').textContent = copy.button;
    if (!keepRefine && state.refineFrom && state.refineFrom.mode !== mode) clearRefine();
    animateLayout(paintRefs);
    syncRatio();
    paintAgentLine();
    if (state.health) paintHealth(state.health);
    if (changed && !quiet) hideError();
}

function currentRatio() {
    return state.mode === 'text' ? state.ratio : state.photoRatio;
}

function ratioValue() {
    const ratio = currentRatio();
    if (ratio === 'source') {
        const photo = state.photo;
        return photo?.width && photo?.height ? photo.width / photo.height : 3 / 4;
    }
    const [width, height] = ratio.split(':').map(Number);
    return width / height;
}

function syncRatio() {
    const ratio = currentRatio();
    document.querySelectorAll('[data-ratio]').forEach(button => {
        if (button.dataset.ratio === 'source') button.hidden = state.mode === 'text';
        button.classList.toggle('active', button.dataset.ratio === ratio);
    });
    syncCanvasAspect();
}

function syncBatch() {
    document.querySelectorAll('[data-batch]').forEach(button => {
        button.classList.toggle('active', Number(button.dataset.batch) === state.batch);
    });
}

// --- photos --------------------------------------------------------------------------

function readPhoto(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            const dataUrl = String(reader.result);
            const image = new Image();
            image.onload = () => resolve({
                dataUrl,
                objectUrl: URL.createObjectURL(file),
                name: file.name || 'photo',
                width: image.naturalWidth,
                height: image.naturalHeight,
                size: file.size,
            });
            image.onerror = () => reject(new Error('The file is not a readable image.'));
            image.src = dataUrl;
        };
        reader.onerror = () => reject(new Error('Could not read the file.'));
        reader.readAsDataURL(file);
    });
}

async function setPhoto(slot, file) {
    if (!file || !String(file.type || '').startsWith('image/')) {
        error('Could not add the photo', 'Choose an image file.');
        return false;
    }
    if (file.size > MAX_PHOTO) {
        error('The photo is too large', 'Choose a file smaller than 20 MB.');
        return false;
    }
    try {
        const photo = await readPhoto(file);
        const key = slot === 'subject' ? 'subject' : 'photo';
        if (state[key]?.objectUrl) URL.revokeObjectURL(state[key].objectUrl);
        state[key] = photo;
    } catch (err) {
        error('Could not add the photo', err.message);
        return false;
    }
    hideError();
    paintRefs();
    syncRatio();
    return true;
}

function clearPhoto(slot) {
    const key = slot === 'subject' ? 'subject' : 'photo';
    if (state[key]?.objectUrl) URL.revokeObjectURL(state[key].objectUrl);
    state[key] = null;
    const input = slot === 'subject' ? $('subject-input') : $('photo-input');
    if (input) input.value = '';
    paintRefs();
    syncRatio();
}

function paintRefBlock(slot, photo, copy) {
    const block = $(`ref-${slot}`);
    if (!block) return;
    block.hidden = !copy;
    if (!copy) return;
    block.querySelector('.ref-title').textContent = copy[0];
    block.querySelector('.ref-hint').textContent = photo ? `${photo.name} · ${photo.width}×${photo.height}` : copy[1];
    block.classList.toggle('is-filled', Boolean(photo));
    block.querySelector('.ref-empty').hidden = Boolean(photo);
    block.querySelector('.ref-tools').hidden = !photo;
    const preview = block.querySelector('.ref-preview');
    preview.hidden = !photo;
    if (photo) preview.querySelector('img').src = photo.objectUrl;
}

/** The left panel: one block for the reference mode, two for edits (scene and donor). */
function paintRefs() {
    const layout = $('studio-layout');
    const copy = REF_COPY[state.mode];
    layout.dataset.refs = String(Boolean(copy));
    $('refs-card').hidden = !copy;
    if (copy) {
        $('refs-eyebrow').textContent = copy.eyebrow;
        paintRefBlock('main', state.photo, copy.main);
        paintRefBlock('subject', state.subject, copy.subject);
        $('refs-card').dataset.blocks = copy.subject ? '2' : '1';
    }
    syncCanvasRowHeight();
}

/** Layout changes slide the cards from their old place to the new one (FLIP). */
function animateLayout(mutate) {
    const layout = $('studio-layout');
    const signature = () => `${layout.dataset.view}|${layout.dataset.refs}|${layout.dataset.blocks || ''}`;
    const cards = () => [...layout.children].filter(card => !card.hidden && card.getClientRects().length);
    const before = signature();
    const rects = new Map(cards().map(card => [card, card.getBoundingClientRect()]));
    mutate();
    if (signature() === before || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    for (const card of cards()) {
        const now = card.getBoundingClientRect();
        const old = rects.get(card);
        if (!old) {
            card.animate(
                [{ opacity: 0, transform: 'translateX(28px) scale(.985)' }, { opacity: 1, transform: 'none' }],
                { duration: 420, easing: LAYOUT_EASING },
            );
            continue;
        }
        const dx = old.left - now.left;
        const dy = old.top - now.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
        card.animate(
            [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
            { duration: 460, easing: LAYOUT_EASING },
        );
    }
}

function wirePhotoSlots() {
    $('photo-input').onchange = event => {
        if (event.target.files?.[0]) setPhoto('main', event.target.files[0]);
    };
    $('subject-input').onchange = event => {
        if (event.target.files?.[0]) setPhoto('subject', event.target.files[0]);
    };
    document.querySelectorAll('[data-pick]').forEach(button => {
        button.onclick = () => (button.dataset.pick === 'subject' ? $('subject-input') : $('photo-input')).click();
    });
    document.querySelectorAll('[data-paste]').forEach(button => {
        button.onclick = () => pasteFromClipboard(button.dataset.paste);
    });
    document.querySelectorAll('[data-remove]').forEach(button => {
        button.onclick = () => clearPhoto(button.dataset.remove);
    });
    document.querySelectorAll('[data-view-ref]').forEach(button => {
        button.onclick = () => openViewer({ kind: 'ref', slot: button.dataset.viewRef });
    });
    document.querySelectorAll('[data-url]').forEach(form => {
        const input = form.querySelector('input');
        form.onsubmit = event => {
            event.preventDefault();
            importImageUrl(input.value, form.dataset.url);
        };
        // A pasted link is imported right away.
        input.addEventListener('paste', () => setTimeout(() => {
            if (/^https?:\/\/\S+$/i.test(input.value.trim())) importImageUrl(input.value, form.dataset.url);
        }));
    });
    for (const [element, slot] of [[$('ref-main'), 'main'], [$('ref-subject'), 'subject'], [$('prompt-box'), 'main']]) {
        element.addEventListener('focusin', () => { state.slotFocus = slot; });
        element.addEventListener('pointerdown', () => { state.slotFocus = slot; });
        ['dragenter', 'dragover'].forEach(name => element.addEventListener(name, event => {
            const types = [...(event.dataTransfer?.types || [])];
            if (!types.includes('Files') && !types.includes('text/uri-list')) return;
            event.preventDefault();
            element.classList.add('is-dragging-reference');
        }));
        ['dragleave', 'drop'].forEach(name => element.addEventListener(name, () => {
            element.classList.remove('is-dragging-reference');
        }));
        element.addEventListener('drop', event => {
            const file = [...(event.dataTransfer?.files || [])].find(item => item.type.startsWith('image/'));
            // An image dragged from another browser tab often arrives as a link only.
            const link = (event.dataTransfer?.getData('text/uri-list') || '').split('\n').find(line => /^https?:/i.test(line.trim()));
            if (!file && !link) return;
            event.preventDefault();
            if (file) acceptImage(file, slot);
            else importImageUrl(link.trim(), slot);
        });
    }
    // Images on the clipboard go to the focused slot; text pastes are left alone, except an
    // image link pasted outside the text fields.
    document.addEventListener('paste', event => {
        const item = [...(event.clipboardData?.items || [])].find(entry => entry.kind === 'file' && entry.type.startsWith('image/'));
        const file = item?.getAsFile();
        if (file) {
            event.preventDefault();
            acceptImage(file, state.slotFocus);
            return;
        }
        const typing = event.target instanceof HTMLElement && event.target.closest('input, textarea, [contenteditable="true"]');
        const text = event.clipboardData?.getData('text/plain')?.trim() || '';
        if (!typing && state.mode !== 'text' && /^https?:\/\/\S+$/i.test(text)) {
            event.preventDefault();
            importImageUrl(text, state.slotFocus);
        }
    });
}

/** Download an image from a link on the server (browsers block most foreign images). */
async function importImageUrl(url, slot) {
    const link = String(url || '').trim();
    if (!/^https?:\/\//i.test(link)) {
        error('Not an image link', 'Paste a link that starts with http:// or https://.');
        return;
    }
    const target = slot === 'subject' && state.mode === 'edit' ? 'subject' : 'main';
    const block = $(`ref-${target}`);
    const loading = block?.querySelector('.ref-loading');
    if (loading) loading.hidden = false;
    try {
        const data = await json('/api/simple/fetch-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: link }),
        });
        const blob = await (await fetch(data.data_url)).blob();
        const added = await acceptImage(new File([blob], data.name || 'image', { type: blob.type }), target);
        if (added !== false && block) block.querySelector('.ref-url input').value = '';
    } catch (err) {
        error('Could not add the image from the link', err.message);
    } finally {
        if (loading) loading.hidden = true;
    }
}

function acceptImage(file, slot) {
    if (state.mode === 'text') {
        setMode('reference');
        showNote('The photo is used as a reference for a new picture. Choose "Edit photo" to change the photo itself.');
    }
    const target = slot === 'subject' && state.mode === 'edit' ? 'subject' : 'main';
    return setPhoto(target, file);
}

async function pasteFromClipboard(slot) {
    if (!navigator.clipboard?.read) {
        error('Paste is not available here', 'Press Ctrl+V on the page instead.');
        return;
    }
    try {
        const items = await navigator.clipboard.read();
        const item = items.find(entry => entry.types.some(value => value.startsWith('image/')));
        if (!item) {
            error('No image on the clipboard', 'Copy an image first, then press Paste.');
            return;
        }
        const type = item.types.find(value => value.startsWith('image/'));
        const blob = await item.getType(type);
        await acceptImage(new File([blob], `pasted.${type.split('/')[1] || 'png'}`, { type }), slot);
    } catch (err) {
        error('Could not read the clipboard', err.message || 'The browser refused access.');
    }
}

// --- agent ---------------------------------------------------------------------------

function currentAgent() {
    if (!state.agentStatus) return null;
    return state.mode === 'text' ? state.agentStatus.text : state.agentStatus.photo;
}

async function loadAgentStatus() {
    try {
        state.agentStatus = await json('/api/simple/agent-status');
    } catch {
        state.agentStatus = { available: false, text: null, photo: null, instruction_files: [] };
    }
    paintAgentLine();
    paintInstructionFiles();
}

function paintAgentLine() {
    const line = $('agent-line');
    const text = $('agent-text');
    if (!line || !text) return;
    line.classList.remove('is-missing', 'is-ready');
    if (!state.agentStatus) {
        text.textContent = 'Checking the agent…';
        return;
    }
    const agent = currentAgent();
    if (!agent) {
        line.classList.add('is-missing');
        text.innerHTML = state.mode === 'reference'
            ? 'No agent configured. <a href="/settings/ai">Set one up in Integrations</a>'
            : 'No agent: your text goes to the model as is. <a href="/settings/ai">Integrations</a>';
        return;
    }
    if (state.mode === 'reference' && !agent.sees_images) {
        line.classList.add('is-missing');
        text.innerHTML = `${esc(agent.name)} cannot see photos. <a href="/settings/ai">Choose a vision agent</a>`;
        return;
    }
    line.classList.add('is-ready');
    const sees = state.mode !== 'text' && agent.sees_images ? ' · sees the photo' : '';
    text.textContent = `Agent: ${agent.name}${sees}`;
}

function paintInstructionFiles() {
    const list = $('session-instruction-files');
    if (!list) return;
    const files = state.agentStatus?.instruction_files || [];
    list.innerHTML = files.map(path => {
        const name = String(path).split(/[\\/]/).pop();
        return `<li><button type="button" class="instruction-file" data-path="${esc(path)}" title="${esc(path)} — click to copy the path">${esc(name)}</button></li>`;
    }).join('');
    list.querySelectorAll('[data-path]').forEach(button => {
        button.onclick = async () => {
            try {
                await navigator.clipboard.writeText(button.dataset.path);
                button.classList.add('is-copied');
                setTimeout(() => button.classList.remove('is-copied'), 1200);
            } catch {}
        };
    });
}

const AGENT_PHASES = {
    started: 'The agent is reading the task…',
    thinking: 'The agent is thinking…',
    working: 'The agent is working…',
    writing: 'The agent is writing the prompt…',
};
const AGENT_LOG_LINES = 6;

function startAgentTimer() {
    const started = Date.now();
    clearInterval(state.agentTimer);
    state.agentPhase = 'started';
    state.agentName = currentAgent()?.name || 'Agent';
    const tick = () => {
        const seconds = Math.round((Date.now() - started) / 1000);
        setGenerating(AGENT_PHASES[state.agentPhase] || AGENT_PHASES.started, `${state.agentName} · ${seconds} s`);
        createButton(true, Math.min(7, 2 + seconds / 20), `Agent · ${seconds} s`);
    };
    state.agentTick = tick;
    tick();
    state.agentTimer = setInterval(tick, 1000);
}

function resetAgentStream() {
    $('agent-stream').hidden = true;
    $('agent-stream-log').replaceChildren();
    $('agent-stream-text').hidden = true;
    $('agent-stream-text').textContent = '';
}

/** One server-sent progress event of the agent: phase, a journal line or the prompt text. */
function paintAgentProgress(event) {
    if (event.phase === 'started' && event.agent) state.agentName = event.agent;
    if (AGENT_PHASES[event.phase]) state.agentPhase = event.phase;
    if (event.log) {
        const log = $('agent-stream-log');
        const item = document.createElement('li');
        item.textContent = event.log;
        item.title = event.log;
        log.append(item);
        while (log.children.length > AGENT_LOG_LINES) log.firstElementChild.remove();
        $('agent-stream').hidden = false;
    }
    if (event.text) {
        const text = $('agent-stream-text');
        text.textContent = event.text;
        text.hidden = false;
        $('agent-stream').hidden = false;
    }
    state.agentTick?.();
}

function stopAgentTimer() {
    clearInterval(state.agentTimer);
    state.agentTimer = null;
    state.agentTick = null;
}

// --- health --------------------------------------------------------------------------

function paintHealth(health) {
    state.health = health || {};
    const badge = $('model-health');
    const statusText = $('model-status-text');
    const panel = $('model-health-panel');
    const install = $('model-install');
    const resources = health?.missing_resources || [];
    const nodes = health?.missing_nodes || [];
    const modeBlocked = health?.status === 'ready' && health?.modes_ready?.[state.mode] === false;
    // Offline is a runtime state, not a setup problem: installed files are checked on disk.
    const offline = health?.comfyui_online === false;
    let className = 'is-checking';
    let label = 'Checking';
    let text = 'Checking local components';
    if (state.runtimeBusy) {
        const seconds = Math.round((Date.now() - state.runtimeStartedAt) / 1000);
        className = 'is-starting';
        label = state.runtimeBusy === 'restarting' ? 'Restarting' : 'Starting';
        text = `Starting ComfyUI… ${seconds} s`;
    } else if (health?.status === 'ready' && !modeBlocked) {
        [className, label, text] = offline
            ? ['is-idle', 'Not running', 'Components installed · ComfyUI starts with the first run']
            : ['is-ready', 'Ready', 'Everything is installed'];
    } else if (modeBlocked) {
        className = 'is-missing';
        label = health.restart_required ? 'Restart ComfyUI' : 'Setup needed';
        text = health.restart_required
            ? 'Restart ComfyUI to load the photo-edit nodes'
            : 'Photo editing needs extra ComfyUI nodes';
    } else if (health?.status === 'not_installed') {
        [className, label] = ['is-missing', 'Setup needed'];
        text = `${resources.length} component${resources.length === 1 ? '' : 's'} missing`;
    } else if (health?.status === 'workflow_pending') {
        [className, label, text] = ['is-pending', 'Not ready', 'The recipe is not prepared yet'];
    } else if (health?.status === 'unknown') {
        [className, label, text] = state.runtimeConfig && !state.runtimeConfig.install_path
            ? ['is-checking', 'Not connected', 'ComfyUI Desktop was not found on this computer']
            : ['is-checking', 'Not checked', 'ComfyUI is offline; components will be checked when it starts'];
    }
    badge.className = `model-health-badge ${className}`;
    badge.textContent = label;
    statusText.textContent = text;
    statusText.title = text;
    paintRuntimeAction();
    $('model-health-message').textContent = resources.length || nodes.length
        ? (offline ? 'Missing on disk:' : 'Missing:')
        : '';
    $('model-missing').innerHTML = [
        ...resources.map(item => `<div class="model-missing-row"><span>${esc(item.display_name || item.filename)}</span><small>${esc(item.folder || '')}</small></div>`),
        ...nodes.map(item => `<div class="model-missing-row"><span>${esc(item.display_name || item.id)}</span><small>${item.installed_on_disk ? 'installed, restart ComfyUI' : 'ComfyUI nodes'}</small></div>`),
    ].join('');
    const activeDownloads = state.downloads.some(item => ['queued', 'downloading'].includes(item.status));
    const failedDownloads = state.downloads.some(item => ['failed', 'error'].includes(item.status));
    const canInstall = health?.installable !== false && (health?.status === 'not_installed' || nodes.some(item => !item.installed_on_disk));
    install.hidden = !canInstall;
    install.disabled = activeDownloads;
    install.querySelector('span').textContent = activeDownloads ? 'Downloading…' : 'Download missing';
    panel.hidden = !(resources.length || nodes.length || activeDownloads || failedDownloads);
}

async function loadHealth(force = false) {
    const id = state.modelId;
    if (!id) return;
    try {
        const data = await json(`/api/simple/models/${encodeURIComponent(id)}/status${force ? '?refresh=1' : ''}`);
        paintHealth(data.health || {});
    } catch {
        if (!state.health || state.health.status === 'checking') paintHealth({ status: 'unknown' });
    }
}

function blocking() {
    const health = state.health || {};
    if (health.status === 'not_installed') return 'Download the missing components first.';
    if (health.status === 'workflow_pending') return 'The recipe is not prepared yet.';
    if (health.status === 'ready' && health.modes_ready?.[state.mode] === false) {
        return health.restart_required
            ? 'Restart ComfyUI so it loads the photo-edit nodes.'
            : 'Photo editing needs extra ComfyUI nodes: press "Download missing".';
    }
    return '';
}

// --- create flow ---------------------------------------------------------------------

async function create() {
    if (state.job) return;
    const mode = state.mode;
    const request = $('prompt').value.trim();
    if (mode !== 'text' && !state.photo) {
        return error('Add a photo', 'This mode needs a photo: drop it here, choose a file or paste it with Ctrl+V.');
    }
    if (mode === 'edit' && !request) return error('Nothing to change', 'Describe what to change in the photo.');
    if (mode === 'text' && !request) return error('Nothing to create', 'Describe the picture you want.');
    const why = blocking();
    if (why) return error('Not ready yet', why);
    const agent = currentAgent();
    const useAgent = Boolean(agent) && !(mode === 'reference' && !agent.sees_images);
    if (mode === 'reference' && !useAgent) {
        return error('The photo mode needs an agent that sees photos', 'Add a vision agent on the Integrations page, or use "Edit photo".');
    }
    hideError();
    hideNote();
    const job = { phase: 'agent', cancelled: false };
    state.job = job;
    paintRuntimeAction();
    state.resultRequest += 1; // a late restored result must not cover this run
    canvas('generating');
    $('generation-stop').hidden = false;
    const photo = mode === 'text' ? null : state.photo;
    const subject = mode === 'edit' ? state.subject : null;
    const copy = REF_COPY[mode];
    state.resultRefs = [
        photo && { label: copy.main[0], src: photo.dataUrl },
        subject && { label: copy.subject[0], src: subject.dataUrl },
    ].filter(Boolean);
    const ratio = currentRatio();
    let plan;
    resetAgentStream();
    if (useAgent) {
        startAgentTimer();
        job.controller = new AbortController();
        try {
            plan = await streamJson('/api/simple/agent?stream=1', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                signal: job.controller.signal,
                body: JSON.stringify({
                    profile_id: state.modelId,
                    mode,
                    request,
                    previous_prompt: state.refineFrom?.mode === mode ? state.refineFrom.prompt : null,
                    aspect_ratio: ratio,
                    reference_image: photo?.dataUrl || null,
                    subject_image: subject?.dataUrl || null,
                }),
            }, event => {
                if (state.job === job && !job.cancelled) paintAgentProgress(event);
            });
        } catch (err) {
            stopAgentTimer();
            if (state.job !== job || job.cancelled) return;
            abortJob();
            return error('The agent could not write the prompt', err.message, err.data?.technical_error);
        }
        stopAgentTimer();
    } else {
        plan = {
            positive_prompt: request,
            negative_prompt: '',
            edit_profile: null,
            note: 'No agent is configured, so your text went to the model as is. Set up an agent in Integrations for better results.',
            agent: null,
        };
    }
    if (state.job !== job || job.cancelled) return;
    await runGeneration({
        mode,
        prompt: plan.positive_prompt,
        negative: plan.negative_prompt || '',
        editProfile: plan.edit_profile || null,
        editProfileLabel: plan.edit_profile_label || '',
        seconds: plan.seconds_per_image || null,
        agentName: plan.agent?.name || null,
        userRequest: request,
        note: plan.note || '',
        photo,
        subject,
        ratio,
    }, job);
}

async function runGeneration(plan, job = { phase: 'queue', cancelled: false }) {
    state.job = job;
    paintRuntimeAction();
    job.phase = 'queue';
    canvas('generating');
    $('generation-stop').hidden = false;
    // An offline ComfyUI is started by the generate request itself; say so instead of "sending".
    const coldStart = state.health?.comfyui_online === false;
    setGenerating(coldStart ? 'Starting ComfyUI…' : 'Sending to ComfyUI…',
        coldStart ? 'The first run after a start takes longer' : plan.agentName ? `Prompt by ${plan.agentName}` : '');
    createButton(true, 8, 'Queueing…');
    if (plan.note) showNote(plan.note);
    let data;
    try {
        data = await json('/api/simple/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                profile_id: state.modelId,
                mode: plan.mode,
                prompt: plan.prompt,
                user_request: plan.userRequest || '',
                negative_prompt: plan.mode === 'edit' ? plan.negative || '' : null,
                edit_profile: plan.mode === 'edit' ? plan.editProfile || null : null,
                reference_image: plan.mode === 'text' ? null : plan.photo?.dataUrl || null,
                subject_image: plan.mode === 'edit' ? plan.subject?.dataUrl || null : null,
                aspect_ratio: plan.ratio,
                batch_size: state.batch,
                agent_name: plan.agentName,
            }),
        });
    } catch (err) {
        if (state.job !== job) return;
        abortJob();
        handleGenerateError(err);
        return;
    }
    if (state.job !== job) return;
    if (coldStart) {
        loadHealth(true);
        loadRuntimeSummary();
    }
    plan.editProfile = data.edit_profile || plan.editProfile;
    plan.seconds = data.seconds_per_image || plan.seconds || TXT2IMG_SECONDS;
    plan.width = data.width;
    plan.height = data.height;
    Object.assign(job, {
        phase: 'running',
        plan,
        runIds: data.run_ids || [data.run_id],
        done: new Set(),
        outputs: new Map(),
        failed: [],
        live: new Map(),
        startedAt: Date.now(),
    });
    job.total = job.runIds.length;
    state.lastRun = { ...plan, runIds: job.runIds, time: Date.now() };
    persistLastRun();
    addSessionEntry(state.lastRun);
    clearRefine();
    pollRuns(job);
}

const LIVE_STAGES = {
    queued: 'Waiting in the ComfyUI queue',
    loading: 'Loading the model',
    encoding: 'Reading the prompt',
    decoding: 'Decoding the image',
    saving: 'Saving',
    working: 'Working',
};

/** Seconds per image: measured on finished images of this job, else the profile estimate. */
function secondsPerImage(job) {
    const measured = job.runIds.map(id => job.live.get(id)?.duration_seconds).filter(value => value > 0);
    if (measured.length) return measured.reduce((sum, value) => sum + value, 0) / measured.length;
    return job.plan.seconds || TXT2IMG_SECONDS;
}

/** Progress, stage line and time left from ComfyUI's live events, with the estimate as fallback. */
function runProgress(job) {
    const total = job.total;
    const finished = job.done.size;
    const per = secondsPerImage(job);
    const currentId = job.runIds.find(id => !job.done.has(id));
    const live = currentId ? job.live.get(currentId) : null;
    const queuedAfter = Math.max(0, total - finished - 1);
    let fraction;
    let stage;
    let remaining;
    if (live && live.state !== 'queued' && live.steps) {
        fraction = Math.min(0.98, live.step / live.steps);
        // ComfyUI reports a step once it is done; show the one in progress.
        stage = live.stage === 'sampling' || (live.step > 0 && live.stage === 'working')
            ? `Step ${Math.min(live.step + 1, live.steps)} of ${live.steps}`
            : LIVE_STAGES[live.stage] || '';
        const current = live.eta_seconds ?? Math.max(1, per - (live.elapsed_seconds || 0));
        remaining = current + queuedAfter * per;
    } else {
        const elapsed = (Date.now() - job.startedAt) / 1000;
        fraction = Math.min(0.9, Math.max(0, elapsed - finished * per) / per);
        stage = live ? LIVE_STAGES[live.stage] || '' : '';
        remaining = Math.max(0, per * (total - finished) - Math.max(0, elapsed - finished * per));
    }
    const elapsedNow = live?.elapsed_seconds;
    return {
        percent: Math.round(8 + 90 * Math.min(0.97, (finished + fraction) / total)),
        stage,
        eta: remaining > 0 ? `about ${formatDuration(remaining)} left` : 'almost done',
        elapsed: elapsedNow ? `${formatDuration(elapsedNow)} in` : '',
    };
}

function pollRuns(job) {
    clearInterval(state.poll);
    const tick = async () => {
        if (state.job !== job || job.phase !== 'running') return;
        const pending = job.runIds.filter(id => !job.done.has(id));
        await Promise.all(pending.map(async id => {
            try {
                const data = await json(`/api/simple/runs/${id}`);
                if (data.live) job.live.set(id, data.live);
                if (Array.isArray(data.outputs) && data.outputs.length) job.outputs.set(id, data.outputs);
                if (data.is_complete) {
                    job.done.add(id);
                    if (data.status !== 'completed') job.failed.push(data);
                }
            } catch {}
        }));
        if (state.job !== job) return;
        const outputs = job.runIds.flatMap(id => job.outputs.get(id) || []);
        if (outputs.length && outputs.length !== state.lastOutputs.length) await renderResult(outputs, false);
        const finished = job.done.size;
        const total = job.total;
        const current = Math.min(total, finished + 1);
        const progress = runProgress(job);
        createButton(true, progress.percent, total > 1 ? `${Math.min(finished, total)} of ${total} ready` : `${progress.percent}%`);
        const detail = [job.plan.editProfileLabel, progress.stage, progress.elapsed, progress.eta].filter(Boolean).join(' · ');
        if (!outputs.length) setGenerating(total > 1 ? `Creating image ${current} of ${total}…` : 'Creating the image…', detail);
        paintResultProgress(finished < total ? `Image ${current} of ${total} · ${[progress.stage, progress.eta].filter(Boolean).join(' · ')}` : '');
        if (finished >= total) finishRuns(job, outputs);
    };
    state.poll = setInterval(tick, 1000);
    tick();
}

function finishRuns(job, outputs) {
    clearInterval(state.poll);
    state.poll = null;
    state.job = null;
    paintRuntimeAction();
    createButton(false);
    $('generation-stop').hidden = true;
    paintResultProgress('');
    if (outputs.length) {
        renderResult(outputs, true);
        const durations = job.runIds.map(id => job.live.get(id)?.duration_seconds).filter(value => value > 0);
        updateSessionOutputs(job.runIds, outputs, durations);
        if (job.failed.length) showNote(`${job.failed.length} of ${job.total} images failed. ${runErrorText(job.failed[0])}`);
        return;
    }
    canvas(state.lastOutputs.length ? 'result' : 'idle');
    const failed = job.failed[0];
    if (job.cancelled || failed?.status === 'cancelled') {
        showNote('Generation was stopped.');
        return;
    }
    error('Generation failed', runErrorText(failed)
        || 'The run finished without a picture. ComfyUI may not have processed the task; check its log.');
}

function runErrorText(data) {
    const raw = data?.run?.error || data?.run?.last_error || '';
    if (typeof raw === 'string') return raw;
    if (raw && typeof raw === 'object') {
        const message = raw.message || raw.error || '';
        const technical = raw.technical_message || '';
        const node = raw.class_type ? ` (node: ${raw.class_type})` : '';
        return technical && technical !== message ? `${message}${node}\n${technical}` : `${message}${node}` || JSON.stringify(raw);
    }
    return '';
}

async function stopGeneration() {
    const job = state.job;
    if (!job) return;
    job.cancelled = true;
    if (job.phase === 'agent' || job.phase === 'queue') {
        job.controller?.abort();
        stopAgentTimer();
        abortJob();
        showNote('Stopped. The agent may still finish in the background; its answer is ignored.');
        return;
    }
    $('generation-stop').disabled = true;
    const pending = (job.runIds || []).filter(id => !job.done.has(id));
    await Promise.allSettled(pending.map(id => json(`/api/simple/runs/${id}/cancel`, { method: 'POST' })));
    $('generation-stop').disabled = false;
}

function abortJob() {
    clearInterval(state.poll);
    state.poll = null;
    state.job = null;
    paintRuntimeAction();
    stopAgentTimer();
    createButton(false);
    $('generation-stop').hidden = true;
    canvas(state.lastOutputs.length ? 'result' : 'idle');
}

function handleGenerateError(err) {
    const code = err.data?.code;
    if (err.data?.missing_resources) {
        paintHealth({ ...(state.health || {}), status: 'not_installed', missing_resources: err.data.missing_resources, installable: true });
    }
    if (code === 'restart_required' || code === 'nodes_missing') {
        loadHealth(true);
        return error('Photo editing is not ready', code === 'restart_required'
            ? 'Restart ComfyUI so it loads the new nodes, then try again.'
            : 'Press "Download missing" to install the photo-edit nodes.');
    }
    if (err.status === 502 || err.status === 503 || code === 'comfyui_rejected' || code === 'comfyui_connection_failed') {
        return error('ComfyUI is unavailable or rejected the task',
            err.data?.suggestion || err.message || 'Make sure ComfyUI is running. Configure the address with the gear icon in the header.');
    }
    return error('Could not start generation', err.message);
}

function formatDuration(seconds) {
    const value = Math.round(seconds);
    if (value < 60) return `${value} s`;
    const minutes = Math.floor(value / 60);
    const rest = value % 60;
    return rest >= 10 ? `${minutes} min ${rest} s` : `${minutes} min`;
}

function setGenerating(text, sub = '') {
    $('generating-text').textContent = text;
    $('generating-sub').textContent = sub;
}

function paintResultProgress(text) {
    const node = $('result-progress');
    node.hidden = !text;
    node.textContent = text;
}

// --- results -------------------------------------------------------------------------

function restoreLastResult() {
    const cached = getCachedJson('cmv_simple_last_result');
    const outputs = Array.isArray(cached?.outputs) ? cached.outputs : (cached ? [cached] : []);
    return renderResult(outputs, false);
}

function clearLastResult() {
    state.resultRequest += 1;
    state.last = null;
    state.lastOutputs = [];
    state.selectedResultIndex = 0;
    try { localStorage.removeItem('cmv_simple_last_result'); } catch {}
    try { sessionStorage.removeItem('cmv_simple_last_result'); } catch {}
}

async function renderResult(outputs, persist = false) {
    const request = ++state.resultRequest;
    const loaded = await Promise.all((Array.isArray(outputs) ? outputs : []).map(async output => {
        if (!output || typeof output !== 'object') return null;
        const url = await loadImageUrl(output);
        return url ? { ...output, preview_url: url, thumbnail_url: output.thumbnail_url || url } : null;
    }));
    if (request !== state.resultRequest) return;
    const valid = loaded.filter(Boolean);
    if (!valid.length) {
        clearLastResult();
        if (!state.job) canvas('idle');
        if (persist) error('Result not found', 'Generation finished without an available image.');
        return;
    }
    const keepIndex = state.lastOutputs.length && state.selectedResultIndex < valid.length ? state.selectedResultIndex : 0;
    state.lastOutputs = valid;
    state.selectedResultIndex = keepIndex;
    state.last = valid[keepIndex];
    updateResultView();
    try { sessionStorage.removeItem('cmv_simple_last_result'); } catch {}
    setCachedJson('cmv_simple_last_result', { outputs: valid }, true);
    canvas('result');
    if (persist && state.sound) playSuccessChime();
}

function updateResultView() {
    const outputs = state.lastOutputs || [];
    const index = Math.max(0, Math.min(outputs.length - 1, state.selectedResultIndex || 0));
    state.selectedResultIndex = index;
    const current = outputs[index] || state.last;
    if (!current) return;
    state.last = current;
    const artwork = document.querySelector('.canvas-artwork-wrap');
    if (artwork) {
        artwork.innerHTML = `<img id="result-img" class="canvas-result-img" src="${esc(current.preview_url || current.thumbnail_url)}" alt="Result ${index + 1}" decoding="async">`;
        artwork.onclick = () => openResultFullscreen(state.selectedResultIndex);
    }
    const strip = $('result-thumbnails');
    if (outputs.length > 1) {
        strip.hidden = false;
        strip.innerHTML = outputs.map((output, i) => `<button type="button" class="canvas-thumb-btn${i === index ? ' active' : ''}" data-index="${i}" aria-label="Result ${i + 1}"><img src="${esc(output.thumbnail_url || output.preview_url)}" alt="" class="canvas-thumb-img" decoding="async"></button>`).join('');
        strip.querySelectorAll('.canvas-thumb-btn').forEach(button => {
            button.onclick = event => {
                event.stopPropagation();
                selectResultIndex(Number(button.dataset.index));
            };
        });
    } else {
        strip.hidden = true;
        strip.innerHTML = '';
    }
    for (const id of ['result-refine', 'result-prompt']) {
        const button = $(id);
        if (button) button.disabled = !state.lastRun;
    }
}

function canvas(view) {
    const layout = $('studio-layout');
    animateLayout(() => {
        layout.dataset.view = view;
        $('generating').hidden = view !== 'generating';
        $('result').hidden = view !== 'result';
        $('canvas')?.classList.toggle('has-result', view === 'result');
        paintRefs();
    });
    resizePrompt();
    $('result-compare').hidden = !(view === 'result' && state.resultRefs.length);
    if (view === 'generating' || view === 'result') {
        syncCanvasAspect();
        requestAnimationFrame(() => {
            syncCanvasRowHeight();
            requestAnimationFrame(syncCanvasRowHeight);
        });
    } else {
        layout.style.removeProperty('--studio-split-height');
    }
}

function syncCanvasAspect() {
    const ratio = ratioValue();
    const defaultWidth = Math.round(Math.min(760, Math.max(360, 520 * ratio / (3 / 4))));
    const limits = canvasWidthLimits();
    const width = state.canvasWidth
        ? Math.round(Math.max(limits.min, Math.min(limits.max, state.canvasWidth)))
        : defaultWidth;
    if (state.canvasWidth) state.canvasWidth = width;
    const aspect = `${ratio.toFixed(4)} / 1`;
    $('studio-layout').style.setProperty('--canvas-column-width', `${width}px`);
    document.querySelector('.studio-canvas-card')?.style.setProperty('--canvas-aspect', aspect);
    $('canvas')?.style.setProperty('--canvas-aspect', aspect);
    syncCanvasRowHeight();
}

function syncCanvasRowHeight() {
    const layout = $('studio-layout');
    if (!layout || !['generating', 'result'].includes(layout.dataset.view)) return;
    const controls = document.querySelector('.studio-controls-card');
    const canvasCard = document.querySelector('.studio-canvas-card');
    const height = Math.max(
        Math.ceil(controls?.getBoundingClientRect().height || 0),
        Math.ceil(canvasCard?.getBoundingClientRect().height || 0),
    );
    if (height > 0) layout.style.setProperty('--studio-split-height', `${height}px`);
}

function createButton(running, percent = 0, text = 'Creating…') {
    const button = $('create');
    button.classList.toggle('running', running);
    button.disabled = running;
    $('progress-fill').style.width = `${running ? percent : 0}%`;
    $('progress-text').textContent = running ? text : 'Starting…';
}

function error(title, text, technical) {
    $('error-title').textContent = title;
    $('error-text').textContent = text || '';
    const details = $('error-details');
    details.hidden = !technical;
    $('error-technical').textContent = technical || '';
    $('error').hidden = false;
}

function hideError() { $('error').hidden = true; }

function showNote(text) {
    if (!text) return;
    $('note-text').textContent = text;
    $('note').hidden = false;
}

function hideNote() { $('note').hidden = true; }

// --- result actions ------------------------------------------------------------------

async function editThis() {
    const current = state.last;
    if (!current || state.job) return;
    const url = current.id ? `/api/original/${current.id}` : current.preview_url;
    try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        const blob = await response.blob();
        const file = new File([blob], current.filename || 'result.png', { type: blob.type || 'image/png' });
        setMode('edit');
        clearPhoto('subject');
        if (!(await setPhoto('main', file))) return;
        clearRefine();
        $('prompt').value = '';
        $('prompt').dispatchEvent(new Event('input'));
        $('prompt').focus();
        showNote('This result is now the photo to edit. Describe the next change.');
    } catch (err) {
        error('Could not take this image', err.message);
    }
}

function refine() {
    const run = state.lastRun;
    if (!run || state.job) return;
    if (state.mode !== run.mode) setMode(run.mode);
    state.refineFrom = { prompt: run.prompt, mode: run.mode };
    $('refine-chip').hidden = false;
    $('prompt').value = '';
    $('prompt').dispatchEvent(new Event('input'));
    $('prompt').focus();
}

function clearRefine() {
    state.refineFrom = null;
    $('refine-chip').hidden = true;
}

function downloadResult() {
    const url = state.last?.id ? `/api/original/${state.last.id}` : state.last?.preview_url;
    if (!url) return;
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = state.last.filename || `creation-${Date.now()}.png`;
    anchor.click();
}

// --- prompt dialog -------------------------------------------------------------------

function openPromptDialog(run = state.lastRun) {
    if (!run) return;
    state.dialogRun = run;
    const parts = [
        MODE_COPY[run.mode]?.label,
        run.agentName ? `written by ${run.agentName}` : 'typed by hand',
        run.editProfileLabel ? `edit type: ${run.editProfileLabel}` : '',
    ].filter(Boolean);
    $('prompt-dialog-meta').textContent = parts.join(' · ') + (run.userRequest ? `\nRequest: ${run.userRequest}` : '');
    $('prompt-dialog-label').textContent = run.mode === 'edit' ? 'Edit command' : 'Prompt';
    $('prompt-dialog-text').value = run.prompt || '';
    $('prompt-dialog-negative-wrap').hidden = run.mode !== 'edit';
    $('prompt-dialog-negative').value = run.negative || '';
    $('prompt-backdrop').hidden = false;
    $('prompt-dialog').hidden = false;
    document.body.classList.add('simple-modal-open');
    $('prompt-dialog-text').focus();
}

function closePromptDialog() {
    if ($('prompt-dialog').hidden) return;
    $('prompt-backdrop').hidden = true;
    $('prompt-dialog').hidden = true;
    document.body.classList.remove('simple-modal-open');
}

async function rerunFromDialog() {
    const run = state.dialogRun;
    const prompt = $('prompt-dialog-text').value.trim();
    if (!run || !prompt || state.job) return;
    const photo = run.mode === 'text' ? null : (run.photo || state.photo);
    if (run.mode !== 'text' && !photo) {
        closePromptDialog();
        return error('Add the photo again', 'The photo of this run is no longer loaded. Add it, then open Prompt again.');
    }
    const why = blocking();
    if (why) return error('Not ready yet', why);
    closePromptDialog();
    hideError();
    hideNote();
    if (state.mode !== run.mode) setMode(run.mode);
    await runGeneration({
        ...run,
        prompt,
        negative: $('prompt-dialog-negative').value.trim(),
        agentName: null,
        note: '',
        photo,
        subject: run.mode === 'edit' ? (run.subject || state.subject) : null,
        ratio: run.ratio || currentRatio(),
    });
}

// --- session log ---------------------------------------------------------------------

function sessionRecord(run) {
    return {
        key: (run.runIds || []).join(','),
        time: run.time || Date.now(),
        mode: run.mode,
        userRequest: run.userRequest || '',
        prompt: run.prompt,
        negative: run.negative || '',
        editProfile: run.editProfile || null,
        editProfileLabel: run.editProfileLabel || '',
        agentName: run.agentName || null,
        note: run.note || '',
        ratio: run.ratio,
        outputs: [],
    };
}

function persistLastRun() {
    try { localStorage.setItem('cmv_create_last_run', JSON.stringify(sessionRecord(state.lastRun))); } catch {}
}

function saveSession() {
    try { localStorage.setItem('cmv_create_session', JSON.stringify(state.session.slice(0, SESSION_LIMIT))); } catch {}
}

function addSessionEntry(run) {
    state.session.unshift(sessionRecord(run));
    state.session = state.session.slice(0, SESSION_LIMIT);
    saveSession();
    paintSession();
}

function updateSessionOutputs(runIds, outputs, durations = []) {
    const entry = state.session.find(item => item.key === runIds.join(','));
    if (!entry) return;
    entry.outputs = outputs.map(item => ({ id: item.id, thumbnail_url: item.thumbnail_url }));
    // ComfyUI execution time per image, from execution start to the saved file.
    if (durations.length) entry.seconds = Math.round(durations.reduce((sum, value) => sum + value, 0) / durations.length);
    saveSession();
    paintSession();
}

function paintSession() {
    const count = $('session-count');
    count.hidden = !state.session.length;
    count.textContent = String(state.session.length);
    const list = $('session-list');
    if (!state.session.length) {
        list.innerHTML = '<div class="assistant-message assistant-message-system"><strong>Nothing yet</strong><p>Each run shows up here with the request, the prompt the agent wrote and its notes.</p></div>';
        return;
    }
    list.innerHTML = state.session.map((entry, index) => {
        const time = new Date(entry.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const took = entry.seconds ? `${formatDuration(entry.seconds)} per image` : '';
        const meta = [MODE_COPY[entry.mode]?.label, entry.agentName || 'no agent', entry.editProfileLabel, took].filter(Boolean).join(' · ');
        const thumbs = (entry.outputs || []).map(item => `<img src="${esc(item.thumbnail_url || `/api/thumbnail/${item.id}`)}" alt="" loading="lazy">`).join('');
        return `<article class="session-entry">
            <header><strong>${esc(time)}</strong><span>${esc(meta)}</span></header>
            ${entry.userRequest ? `<p class="session-request">${esc(entry.userRequest)}</p>` : ''}
            <details><summary>${entry.mode === 'edit' ? 'Edit command' : 'Prompt'}</summary><p class="session-prompt">${esc(entry.prompt)}</p>${entry.negative ? `<p class="session-negative">Remove: ${esc(entry.negative)}</p>` : ''}</details>
            ${entry.note ? `<p class="session-note">${esc(entry.note)}</p>` : ''}
            ${thumbs ? `<div class="session-thumbs">${thumbs}</div>` : ''}
            <div class="session-actions"><button type="button" class="download-mini-btn" data-session-open="${index}">Open prompt</button></div>
        </article>`;
    }).join('');
    list.querySelectorAll('[data-session-open]').forEach(button => {
        button.onclick = () => {
            const entry = state.session[Number(button.dataset.sessionOpen)];
            if (!entry) return;
            closeSession();
            openPromptDialog({ ...entry, runIds: [] });
        };
    });
}

function openSession() {
    $('session-backdrop').hidden = false;
    $('session').hidden = false;
}

function closeSession() {
    $('session-backdrop').hidden = true;
    $('session').hidden = true;
}

// --- wiring --------------------------------------------------------------------------

function wire() {
    document.querySelectorAll('[data-mode]').forEach(button => {
        button.onclick = () => setMode(button.dataset.mode);
    });
    document.querySelectorAll('[data-ratio]').forEach(button => {
        button.onclick = () => {
            const ratio = button.dataset.ratio;
            if (state.mode === 'text') {
                state.ratio = ratio;
                save('cmv_simple_ratio', ratio);
            } else {
                state.photoRatio = ratio;
                save('cmv_create_photo_ratio', ratio);
            }
            syncRatio();
        };
    });
    document.querySelectorAll('[data-batch]').forEach(button => {
        button.onclick = () => {
            state.batch = Number(button.dataset.batch);
            save('cmv_simple_batch', state.batch);
            syncBatch();
        };
    });
    wirePhotoSlots();

    $('prompt').oninput = () => {
        resizePrompt();
        $('prompt-clear').hidden = !$('prompt').value.trim();
        save('cmv_simple_prompt', $('prompt').value);
    };
    $('prompt-clear').onclick = () => {
        $('prompt').value = '';
        $('prompt').dispatchEvent(new Event('input'));
        $('prompt').focus();
    };
    $('refine-cancel').onclick = clearRefine;
    $('create').onclick = create;
    $('generation-stop').onclick = stopGeneration;
    $('error-close').onclick = hideError;
    $('note-close').onclick = hideNote;

    $('result-download').onclick = downloadResult;
    $('result-edit-this').onclick = editThis;
    $('result-compare').onclick = () => openViewer({ kind: 'result', index: state.selectedResultIndex }, { compare: true });
    $('result-refine').onclick = refine;
    $('result-prompt').onclick = () => openPromptDialog();

    $('prompt-dialog-close').onclick = closePromptDialog;
    $('prompt-dialog-cancel').onclick = closePromptDialog;
    $('prompt-backdrop').onclick = closePromptDialog;
    $('prompt-dialog-run').onclick = rerunFromDialog;
    $('prompt-dialog-copy').onclick = async () => {
        try { await navigator.clipboard.writeText($('prompt-dialog-text').value); } catch {}
    };

    $('session-open').onclick = openSession;
    $('session-close').onclick = closeSession;
    $('session-backdrop').onclick = closeSession;
    $('session-clear').onclick = () => {
        state.session = [];
        saveSession();
        paintSession();
    };

    $('model-install').onclick = installModel;
    $('model-recheck').onclick = async () => {
        const button = $('model-recheck');
        button.disabled = true;
        button.textContent = 'Checking…';
        try {
            await loadHealth(true);
            await loadDownloads();
        } finally {
            button.disabled = false;
            button.textContent = 'Check again';
        }
    };
    $('model-downloads').addEventListener('click', event => {
        const button = event.target.closest('[data-download-action]');
        if (button) downloadAction(Number(button.dataset.downloadId), button.dataset.downloadAction);
    });

    $('runtime-settings-open').onclick = openRuntimeSettings;
    $('runtime-settings-close').onclick = closeRuntimeSettings;
    $('runtime-settings-cancel').onclick = closeRuntimeSettings;
    $('runtime-settings-backdrop').onclick = closeRuntimeSettings;
    $('runtime-power').onclick = runtimePowerClick;
    $('runtime-installs').addEventListener('click', runtimeInstallClick);
    $('runtime-folders').addEventListener('click', runtimeFolderClick);
    $('runtime-rescan').onclick = rescanDesktop;
    $('runtime-action').onclick = runtimeActionClick;
    $('runtime-settings-form').onsubmit = saveRuntimeSettings;

    addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
            event.preventDefault();
            if (!$('prompt-dialog').hidden) rerunFromDialog();
            else create();
        }
        if (event.key === 'Escape') {
            closePromptDialog();
            closeSession();
            closeRuntimeSettings();
            closeContextMenu();
        }
    });
    addEventListener('resize', () => syncCanvasRowHeight());
    document.addEventListener('contextmenu', openStudioContextMenu);
    document.addEventListener('click', event => {
        if (!event.target.closest('.image-context-menu')) closeContextMenu();
    });
    window.addEventListener('scroll', closeContextMenu, { passive: true });
}

function getCachedJson(key, maxAgeMs = 0) {
    try {
        const raw = sessionStorage.getItem(key) || localStorage.getItem(key);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (maxAgeMs > 0 && parsed?._t && (Date.now() - parsed._t > maxAgeMs)) {
            return null;
        }
        return parsed.data !== undefined ? parsed.data : parsed;
    } catch {
        return null;
    }
}

function setCachedJson(key, data, isLocal = false) {
    try {
        const payload = JSON.stringify({ data, _t: Date.now() });
        if (isLocal) {
            localStorage.setItem(key, payload);
        } else {
            sessionStorage.setItem(key, payload);
        }
    } catch {}
}

function initialAmbient() {
    try {
        const node = $('simple-ambient-initial');
        const embedded = node ? JSON.parse(node.textContent) : null;
        if (Array.isArray(embedded)) return embedded;
    } catch {}
    const cached = getCachedJson('cmv_simple_ambient_items', 30 * 60 * 1000);
    return Array.isArray(cached) ? cached : [];
}

async function hydrateAmbient() {
    state.ambient = initialAmbient();
    refillAmbientDeck();
    if (!state.ambient.length) { resetAmbient(); return; }
    let last = null;
    try { last = localStorage.getItem('cmv_simple_ambient_last'); } catch {}
    const previous = state.ambient.find(item => typeof item === 'string' ? item === last
        : [item.original_url, item.preview_url, item.thumbnail_url].includes(last));
    if (last && previous) {
        const loaded = await setAmbient(previous);
        if (loaded === null) return; // A newer selection owns the background now.
        if (!loaded) {
            try { localStorage.removeItem('cmv_simple_ambient_last'); } catch {}
        }
    } else if (last) {
        try { localStorage.removeItem('cmv_simple_ambient_last'); } catch {}
    }
    if (!document.querySelector('.ambient-container.has-ambient') && state.ambient.length) {
        await ambientPick();
    }
    preloadAmbientPool();
}

function save(key, value) {
    try { localStorage.setItem(key, String(value)); } catch {}
}

function applyVisualSettings() {
    document.documentElement.style.setProperty('--studio-card-opacity', `${state.opacity ?? 44}%`);
    document.documentElement.style.setProperty('--studio-ambient-blur', `${state.blur ?? 10}px`);
    document.documentElement.style.setProperty(
        '--studio-ambient-size',
        state.fit === 'contain' ? 'contain' : (state.fit === 'original' ? 'auto' : 'cover')
    );
    document.body.setAttribute('data-ambient-fit', state.fit || 'cover');
}

function restartAmbientTimer(force = false) {
    const interval = state.interval > 0 && state.ambient.length > 1 ? state.interval : 0;
    if (!force && state.timer && state.timerInterval === interval) return;
    if (state.timer) {
        clearInterval(state.timer);
        state.timer = null;
    }
    state.timerInterval = interval;
    if (interval) {
        state.timer = setInterval(() => ambientPick(false), interval);
    }
}

function setAmbientInterval(seconds) {
    const interval = Math.max(0, Number(seconds) || 0) * 1000;
    save('cmv_simple_ambient_interval', interval);
    if (interval === state.interval) return;
    state.interval = interval;
    // A load started under the old interval must not switch the image early.
    state.ambientRequest += 1;
    state.ambientPickRequest += 1;
    state.ambientPicking = false;
    restartAmbientTimer(true);
}

function ambientPoolUrl() {
    const current = Number(state.ambientCurrent);
    return '/api/simple/ambient?limit=72'
        + (Number.isSafeInteger(current) && current > 0 ? `&current_id=${current}` : '');
}

// Resolve an actual decodable image before exposing it in the result or background.
function loadImageUrl(item, preferOriginal = false) {
    const candidates = typeof item === 'string' ? [item]
        : (preferOriginal
            ? [item?.original_url, item?.preview_url, item?.thumbnail_url]
            : [item?.preview_url, item?.thumbnail_url, item?.original_url]);
    const urls = [...new Set(candidates.filter(url => typeof url === 'string' && url))];
    return new Promise(resolve => {
        function next() {
            const url = urls.shift();
            if (!url) { resolve(null); return; }
            const image = new Image();
            image.onload = () => image.naturalWidth > 0 ? resolve(url) : next();
            image.onerror = next;
            image.src = url;
        }
        next();
    });
}

function ambientKey(item) {
    return typeof item === 'string' ? item : item?.id || item?.preview_url || item?.original_url || item?.thumbnail_url;
}

function resetAmbient() {
    state.ambientRequest += 1;
    state.ambientPickRequest += 1;
    state.ambientPicking = false;
    state.ambient = [];
    state.ambientDeck = [];
    state.ambientHistory = [];
    state.ambientCurrent = null;
    state.layer = 0;
    document.querySelector('.ambient-container')?.classList.remove('has-ambient');
    for (const id of ['ambient-a', 'ambient-b', 'ambient-backdrop-a', 'ambient-backdrop-b']) {
        const layer = $(id);
        if (layer) {
            layer.style.backgroundImage = '';
            layer.classList.remove('active');
        }
    }
    try {
        localStorage.removeItem('cmv_simple_ambient_last');
        localStorage.removeItem('cmv_simple_ambient_items');
    } catch {}
    try { sessionStorage.removeItem('cmv_simple_ambient_items'); } catch {}
    restartAmbientTimer();
}

function openResultFullscreen(startIndex = 0) {
    openViewer({ kind: 'result', index: startIndex });
}

/** Items of the viewer: the photos of the shown result (or the panel) and every result. */
function viewerItems() {
    const results = (state.lastOutputs || [])
        .filter(item => item?.preview_url || item?.thumbnail_url)
        .map((item, index, all) => ({
            kind: 'result',
            index,
            label: all.length > 1 ? `Result ${index + 1}` : 'Result',
            src: item.preview_url || item.thumbnail_url,
        }));
    const copy = REF_COPY[state.mode];
    const panel = copy ? [
        state.photo && { kind: 'ref', slot: 'main', label: copy.main[0], src: state.photo.objectUrl },
        copy.subject && state.subject && { kind: 'ref', slot: 'subject', label: copy.subject[0], src: state.subject.objectUrl },
    ].filter(Boolean) : [];
    return { panel, results, compareRefs: state.resultRefs.length ? state.resultRefs : panel };
}

/**
 * Full-screen viewer. ``start`` is ``{kind: 'ref', slot}`` or ``{kind: 'result', index}``;
 * ``compare`` opens the side-by-side view of the photos and the selected result.
 */
function openViewer(start = { kind: 'result', index: 0 }, { compare = false } = {}) {
    const { panel, results, compareRefs } = viewerItems();
    const items = [...panel, ...results];
    if (!items.length) return;
    let index = Math.max(0, items.findIndex(item => item.kind === start.kind
        && (start.kind === 'ref' ? item.slot === start.slot : item.index === (start.index ?? state.selectedResultIndex))));
    let sideBySide = compare && results.length > 0;
    let scale = 1;

    const overlay = document.createElement('div');
    overlay.className = 'result-lightbox';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Full-screen preview');
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'result-lightbox-close';
    close.setAttribute('aria-label', 'Close preview');
    close.textContent = '×';
    const toolbar = document.createElement('div');
    toolbar.className = 'viewer-toolbar';
    const label = document.createElement('span');
    label.className = 'viewer-label';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'viewer-toggle';
    toggle.hidden = !(results.length && compareRefs.length);
    toolbar.append(label, toggle);
    const stage = document.createElement('div');
    stage.className = 'viewer-stage';
    const makeNav = (text, direction) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `result-lightbox-nav ${direction}`;
        button.setAttribute('aria-label', text);
        button.textContent = direction === 'prev' ? '‹' : '›';
        return button;
    };
    const previous = makeNav('Previous image', 'prev');
    const next = makeNav('Next image', 'next');

    const render = () => {
        stage.replaceChildren();
        overlay.classList.toggle('is-compare', sideBySide);
        previous.hidden = next.hidden = sideBySide || items.length < 2;
        toggle.textContent = sideBySide ? 'One image' : 'Compare with the photos';
        if (sideBySide) {
            const current = items[index].kind === 'result' ? items[index] : results[state.selectedResultIndex] || results[0];
            label.textContent = 'Photos and result';
            for (const item of [...compareRefs, current]) {
                const figure = document.createElement('figure');
                figure.className = 'viewer-figure';
                const image = document.createElement('img');
                image.src = item.src;
                image.alt = item.label;
                const caption = document.createElement('figcaption');
                caption.textContent = item.label;
                figure.append(image, caption);
                stage.append(figure);
            }
            return;
        }
        const item = items[index];
        label.textContent = items.length > 1 ? `${item.label} · ${index + 1} of ${items.length}` : item.label;
        scale = 1;
        const image = document.createElement('img');
        image.className = 'result-lightbox-image';
        image.src = item.src;
        image.alt = item.label;
        image.addEventListener('wheel', event => {
            event.preventDefault();
            scale = Math.max(.5, Math.min(3, scale + (event.deltaY < 0 ? .15 : -.15)));
            image.style.transform = `scale(${scale})`;
        }, { passive: false });
        image.addEventListener('dblclick', () => {
            scale = 1;
            image.style.transform = 'scale(1)';
        });
        stage.append(image);
        if (item.kind === 'result') selectResultIndex(item.index);
    };
    const move = direction => {
        index = (index + direction + items.length) % items.length;
        render();
    };
    previous.addEventListener('click', () => move(-1));
    next.addEventListener('click', () => move(1));
    toggle.addEventListener('click', () => {
        sideBySide = !sideBySide;
        render();
    });
    overlay.append(close, toolbar, previous, next, stage);
    const dismiss = () => {
        overlay.remove();
        document.removeEventListener('keydown', onKeydown);
    };
    const onKeydown = event => {
        if (event.key === 'Escape') dismiss();
        if (!sideBySide && event.key === 'ArrowLeft' && items.length > 1) move(-1);
        if (!sideBySide && event.key === 'ArrowRight' && items.length > 1) move(1);
    };
    close.addEventListener('click', dismiss);
    overlay.addEventListener('click', event => {
        if (event.target === overlay || event.target === stage) dismiss();
    });
    document.addEventListener('keydown', onKeydown);
    render();
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('is-open'));
    close.focus();
}

function esc(value) {
    const div = document.createElement('div');
    div.textContent = String(value ?? '');
    return div.innerHTML;
}

/**
 * POST that answers with server-sent events: ``progress`` events go to ``onProgress``,
 * the ``result`` event resolves and an ``error`` event rejects like ``json()`` does.
 * A plain JSON answer (a validation error before the stream starts) is handled too.
 */
async function streamJson(url, options, onProgress) {
    const response = await fetch(url, options);
    const type = response.headers.get('Content-Type') || '';
    if (!type.includes('text/event-stream')) {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw Object.assign(new Error(data.error || `${response.status} ${response.statusText}`), { data, status: response.status });
        }
        return data;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
        // eslint-disable-next-line no-await-in-loop, no-restricted-syntax -- stream chunks arrive in order, one at a time
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            boundary = buffer.indexOf('\n\n');
            const payload = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
            if (!payload) continue;
            let event;
            try {
                event = JSON.parse(payload);
            } catch {
                continue;
            }
            if (event.event === 'progress') {
                onProgress?.(event);
            } else if (event.event === 'result') {
                reader.cancel().catch(() => {});
                return event;
            } else if (event.event === 'error') {
                reader.cancel().catch(() => {});
                throw Object.assign(new Error(event.error || 'The agent failed.'), { data: event, status: event.status || 500 });
            }
        }
    }
    throw new Error('The agent stream ended without an answer.');
}

async function json(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw Object.assign(
            new Error(data.suggestion || data.error || `${response.status} ${response.statusText}`),
            { data, status: response.status },
        );
    }
    return data;
}

function latestDownloads(items) {
    const seen = new Set();
    const result = [];
    for (const item of items || []) {
        const key = `${item.folder}/${item.filename}`;
        if (seen.has(key)) continue;
        seen.add(key);
        result.push(item);
    }
    return result;
}

function bytes(value) {
    const amount = Number(value || 0);
    if (!amount) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let number = amount;
    let index = 0;
    while (number >= 1024 && index < units.length - 1) {
        number /= 1024;
        index += 1;
    }
    return `${number >= 100 || index === 0 ? number.toFixed(0) : number.toFixed(1)} ${units[index]}`;
}

function downloadStatus(item) {
    const labels = {
        queued: 'Queued',
        downloading: item.file_size_bytes ? `${Math.round(item.progress || 0)}%` : 'Downloading…',
        paused: 'Paused',
        completed: 'Complete',
        failed: 'Failed',
        cancelled: 'Cancelled',
    };
    return labels[item.status] || item.status;
}

function paintDownloads() {
    const wrap = $('model-downloads');
    if (!wrap) return;
    wrap.hidden = !state.downloads.length;
    wrap.innerHTML = state.downloads.map(item => {
        const canPause = ['queued', 'downloading'].includes(item.status) && item.id > 0;
        const canResume = ['paused', 'failed', 'cancelled'].includes(item.status) && item.id > 0;
        const canCancel = ['queued', 'downloading', 'paused'].includes(item.status) && item.id > 0;
        const indeterminate = item.status === 'downloading' && !item.file_size_bytes;
        const percent = item.file_size_bytes ? Math.round(item.progress || 0) : null;
        const size = item.file_size_bytes
            ? `${bytes(item.downloaded_bytes)} / ${bytes(item.file_size_bytes)}`
            : (item.downloaded_bytes ? bytes(item.downloaded_bytes) : item.folder);
        return `<div class="model-download-row">
            <div class="model-download-head">
                <div class="model-download-copy">
                    <strong>${esc(item.display_name || item.filename)}</strong>
                    <small>${esc(downloadStatus(item))}${size ? ` · ${esc(size)}` : ''}</small>
                </div>
                <div class="model-download-actions">
                    ${percent !== null && ['queued', 'downloading', 'paused'].includes(item.status) ? `<span class="cmv-download-percent">${percent}%</span>` : ''}
                    ${canPause ? `<button class="download-mini-btn" type="button" data-download-action="pause" data-download-id="${item.id}">Pause</button>` : ''}
                    ${canResume ? `<button class="download-mini-btn" type="button" data-download-action="resume" data-download-id="${item.id}">${item.status === 'failed' ? 'Retry' : 'Resume'}</button>` : ''}
                    ${canCancel ? `<button class="download-mini-btn cmv-download-cancel" type="button" data-download-action="cancel" data-download-id="${item.id}">Cancel</button>` : ''}
                </div>
            </div>
            <div class="model-download-track${indeterminate ? ' is-indeterminate' : ''}"><div class="model-download-fill" style="width:${Math.max(0, Math.min(100, Number(item.progress || 0)))}%"></div></div>
            ${item.error ? `<p class="model-download-error">${esc(item.error)}</p>` : ''}
        </div>`;
    }).join('');
    paintHealth(state.health || { status: 'checking', message: 'Checking local components' });
}

async function loadDownloads() {
    const id = state.modelId;
    if (!id) return;
    try {
        const data = await json(`/api/simple/downloads?profile_id=${encodeURIComponent(id)}`);
        if (state.modelId !== id) return;
        state.downloads = latestDownloads(data.items || []);
        paintDownloads();
        if (state.downloads.some(item => ['queued', 'downloading'].includes(item.status))) {
            startDownloadPolling();
        }
    } catch {}
}

async function installModel() {
    if (!state.modelId) return;
    const button = $('model-install');
    button.disabled = true;
    button.querySelector('span').textContent = 'Preparing…';
    try {
        const data = await json(`/api/simple/models/${encodeURIComponent(state.modelId)}/install`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        });
        if (Array.isArray(data.downloads) && data.downloads.length) {
            state.downloads = latestDownloads(data.downloads);
            paintDownloads();
            startDownloadPolling();
        } else {
            await loadDownloads();
            await loadHealth();
        }
        if (data.unavailable?.length) {
            error('Some components could not be installed', data.unavailable.join('\n'));
        }
    } catch (err) {
        if (err.data?.open_settings) openRuntimeSettings();
            error('Could not start download', err.message);
    } finally {
        button.disabled = false;
        paintHealth(state.health || {});
    }
}

async function downloadAction(id, action) {
    try {
        await json(`/api/simple/downloads/${id}/${action}`, { method: 'POST' });
        await loadDownloads();
        if (action === 'resume') startDownloadPolling();
    } catch (err) {
            error('Could not update download', err.message);
    }
}

function startDownloadPolling() {
    clearInterval(state.downloadPoll);
    state.downloadPoll = setInterval(async () => {
        await loadDownloads();
        const active = state.downloads.some(item => ['queued', 'downloading'].includes(item.status));
        if (!active) {
            clearInterval(state.downloadPoll);
            state.downloadPoll = null;
            if (state.downloads.length && state.downloads.every(item => item.status === 'completed')) {
                setTimeout(() => {
                    loadHealth(true);
                    loadDownloads();
                }, 450);
            }
        }
    }, 850);
}

function shuffleArray(arr) {
    const copy = [...arr];
    for (let i = copy.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
}

function refillAmbientDeck() {
    if (!state.ambient.length) {
        state.ambientDeck = [];
        return;
    }
    const recentSet = new Set(state.ambientHistory);
    const unseen = state.ambient.filter(item => !recentSet.has(ambientKey(item)));
    const seen = state.ambient.filter(item => recentSet.has(ambientKey(item)));

    const shuffledUnseen = shuffleArray(unseen);
    const shuffledSeen = shuffleArray(seen);
    const newDeck = [...shuffledUnseen, ...shuffledSeen];

    // Avoid immediate repeat with currently shown image
    if (newDeck.length > 1 && ambientKey(newDeck[0]) === state.ambientCurrent) {
        const first = newDeck.shift();
        newDeck.push(first);
    }
    state.ambientDeck = newDeck;
}

async function loadMoreAmbient() {
    if (state.ambientLoadingMore) return;
    state.ambientLoadingMore = true;
    const request = state.ambientPoolRequest;
    try {
        const data = await json(ambientPoolUrl());
        if (request !== state.ambientPoolRequest) return;
        const items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) { resetAmbient(); return; }
        if (items.length) {
            const existingIds = new Set(state.ambient.map(it => it.id || it));
            const newItems = items.filter(it => !existingIds.has(it.id || it));
            if (newItems.length) {
                state.ambient.push(...newItems);
                setCachedJson('cmv_simple_ambient_items', state.ambient.slice(0, 120), true);
                state.ambientDeck.push(...shuffleArray(newItems));
            }
        }
    } catch {}
    finally {
        state.ambientLoadingMore = false;
    }
}

async function loadAmbient() {
    const request = ++state.ambientPoolRequest;
    try {
        const data = await json(ambientPoolUrl());
        if (request !== state.ambientPoolRequest) return;
        const items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) { resetAmbient(); return; }
        if (items.length) {
            state.ambientRequest += 1;
            state.ambient = items;
            setCachedJson('cmv_simple_ambient_items', items, true);
            refillAmbientDeck();
            preloadAmbientPool();
            if (!document.querySelector('.ambient-container.has-ambient')
                || !items.some(item => ambientKey(item) === state.ambientCurrent)) {
                await ambientPick();
            }
            restartAmbientTimer();
        }
    } catch {}
}

function watchAmbientSources() {
    const events = new EventSource('/api/folders/events');
    let previous = null;
    events.onmessage = event => {
        try {
            const sources = JSON.parse(event.data);
            const signature = JSON.stringify(Object.entries(sources).map(([id, source]) =>
                [id, source.image_count, source.enabled, source.source_status, source.revision]));
            if (previous !== null && signature !== previous) loadAmbient();
            previous = signature;
        } catch {}
    };
}

async function ambientPick(resetTimer = true) {
    if (!state.ambient.length) return;
    if (!resetTimer && state.ambientPicking) return;
    const request = ++state.ambientPickRequest;
    state.ambientPicking = true;
    try {
        if (!state.ambientDeck.length) {
            refillAmbientDeck();
        }
        // Try each candidate at most once; a missing source must not stop the slideshow.
        const attempts = state.ambientDeck.length;
        for (let attempt = 0; attempt < attempts; attempt += 1) {
            let nextItem = state.ambientDeck.shift();
            if (!nextItem) break;
            if (ambientKey(nextItem) === state.ambientCurrent && state.ambientDeck.length > 0) {
                const alt = state.ambientDeck.shift();
                state.ambientDeck.push(nextItem);
                nextItem = alt;
            }
            // Failover is sequential so only the first working candidate is downloaded/applied.
            // eslint-disable-next-line no-await-in-loop, no-restricted-syntax
            const loaded = await setAmbient(nextItem);
            if (loaded === null) return;
            if (loaded) break;
            state.ambient = state.ambient.filter(item => ambientKey(item) !== ambientKey(nextItem));
        }
        preloadAmbientPool();

        if (resetTimer) {
            restartAmbientTimer(true);
        }

        if (state.ambientDeck.length < 8) {
            loadMoreAmbient();
        }
    } finally {
        if (request === state.ambientPickRequest) state.ambientPicking = false;
    }
}

function preloadAmbientPool() {
    const items = state.ambientDeck.slice(0, 4);
    for (const item of items) {
        const src = typeof item === 'string' ? item : item?.original_url || item?.preview_url || item?.thumbnail_url;
        if (src) {
            const img = new Image();
            img.src = src;
        }
    }
}

async function setAmbient(item) {
    const request = ++state.ambientRequest;
    const url = await loadImageUrl(item, true);
    if (request !== state.ambientRequest) return null;
    if (!url) return false;
    applyAmbient(url);
    state.ambientCurrent = ambientKey(item);
    state.ambientHistory.push(state.ambientCurrent);
    if (state.ambientHistory.length > 30) state.ambientHistory.shift();
    return true;
}

function applyAmbient(url) {
    const next = state.layer ? $('ambient-a') : $('ambient-b');
    const previous = state.layer ? $('ambient-b') : $('ambient-a');
    const nextBg = state.layer ? $('ambient-backdrop-a') : $('ambient-backdrop-b');
    const prevBg = state.layer ? $('ambient-backdrop-b') : $('ambient-backdrop-a');
    const container = document.querySelector('.ambient-container');
    if (!next || !previous) return;
    const formattedUrl = `url("${String(url).replaceAll('"', '%22')}")`;
    next.style.backgroundImage = formattedUrl;
    next.classList.add('active');
    previous.classList.remove('active');
    if (nextBg && prevBg) {
        nextBg.style.backgroundImage = formattedUrl;
        nextBg.classList.add('active');
        prevBg.classList.remove('active');
    }
    if (container) container.classList.add('has-ambient');
    try { localStorage.setItem('cmv_simple_ambient_last', url); } catch {}
    state.layer = state.layer ? 0 : 1;
}

function canvasWidthLimits() {
    const min = 360;
    return { min, max: 1100 };
}

function wireCanvasResize() {
    const card = document.querySelector('.studio-canvas-card');
    const layout = $('studio-layout');
    if (!card || !layout || card.querySelector('.canvas-resize-handle')) return;
    const handle = document.createElement('button');
    handle.type = 'button';
    handle.className = 'canvas-resize-handle';
    handle.setAttribute('aria-label', 'Resize result panel');
    handle.title = 'Drag to resize';
    card.appendChild(handle);

    let startX = 0;
    let startWidth = 0;
    const onMove = event => {
        const limits = canvasWidthLimits();
        const nextWidth = Math.round(Math.max(limits.min, Math.min(limits.max, startWidth + event.clientX - startX)));
        state.canvasWidth = nextWidth;
        layout.style.setProperty('--canvas-column-width', `${nextWidth}px`);
        save('cmv_simple_canvas_width', nextWidth);
    };
    const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('pointercancel', onUp);
        handle.classList.remove('is-resizing');
    };
    handle.addEventListener('pointerdown', event => {
        event.preventDefault();
        startX = event.clientX;
        startWidth = card.getBoundingClientRect().width;
        handle.classList.add('is-resizing');
        handle.setPointerCapture?.(event.pointerId);
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp, { once: true });
        window.addEventListener('pointercancel', onUp, { once: true });
    });
    window.addEventListener('resize', () => {
        if (!state.canvasWidth) return;
        const limits = canvasWidthLimits();
        state.canvasWidth = Math.max(limits.min, Math.min(limits.max, state.canvasWidth));
        layout.style.setProperty('--canvas-column-width', `${state.canvasWidth}px`);
        save('cmv_simple_canvas_width', state.canvasWidth);
    });
}

function resizePrompt() {
    const textarea = $('prompt');
    if (!textarea) return;
    const minH = 84;
    const maxH = 220;
    textarea.style.height = 'auto';
    const targetH = Math.max(minH, Math.min(textarea.scrollHeight, maxH));
    textarea.style.height = `${targetH}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxH ? 'auto' : 'hidden';
}

function playSuccessChime() {
    try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        const now = ctx.currentTime;
        const notes = [659.25, 987.77, 1318.51];
        notes.forEach((freq, i) => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(freq, now + i * 0.08);
            gain.gain.setValueAtTime(0.12, now + i * 0.08);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.08 + 0.6);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(now + i * 0.08);
            osc.stop(now + i * 0.08 + 0.65);
        });
        setTimeout(() => { try { ctx.close(); } catch {} }, 1200);
    } catch {}
}

function selectResultIndex(index) {
    if (!state.lastOutputs || !state.lastOutputs[index]) return;
    state.selectedResultIndex = index;
    state.last = state.lastOutputs[index];
    const current = state.last;
    const url = current.preview_url || current.thumbnail_url;

    const img = document.querySelector('.canvas-artwork-wrap .canvas-result-img');
    if (img) {
        img.src = url;
        img.alt = `Result ${index + 1}`;
    }

    const thumbsContainer = $('result-thumbnails');
    if (thumbsContainer) {
        thumbsContainer.querySelectorAll('.canvas-thumb-btn').forEach((btn, i) => {
            btn.classList.toggle('active', i === index);
        });
    }

}

function runtimeDetection(kind, title, text) {
    const card = $('runtime-detection');
    card.className = `runtime-detection-card${kind ? ` is-${kind}` : ''}`;
    $('runtime-detection-title').textContent = title;
    $('runtime-detection-text').textContent = text;
}

function runtimeStatusText(status) {
    if (!status) return 'ComfyUI status is unknown.';
    const owned = status.mode === 'managed' ? ' Started by Create; it closes together with the app.' : '';
    if (['ready', 'external'].includes(status.status)) return `ComfyUI is available at ${status.host || '127.0.0.1'}:${status.port || 8188}.${owned}`;
    if (status.status === 'busy') return `ComfyUI is connected and currently generating.${owned}`;
    if (status.status === 'starting') return 'ComfyUI is starting…';
    if (status.last_error) return status.last_error;
    if (status.status === 'stopped') return 'ComfyUI is not running.';
    return `Current status: ${status.status || 'unknown'}.`;
}

function runtimeUp(status) {
    return Boolean(status) && ['ready', 'external', 'busy'].includes(status.status);
}

function paintRuntimeDot() {
    const dot = $('runtime-settings-dot');
    dot.className = 'runtime-settings-dot';
    if (runtimeUp(state.runtimeStatus)) dot.classList.add('is-ready');
    else if (state.runtimeConfig?.install_path) dot.classList.add('is-configured');
}

const RUNTIME_ACTIONS = {
    start: { label: 'Start ComfyUI', icon: '<path d="M12 2v10"></path><path d="M18.4 6.6a9 9 0 1 1-12.77.04"></path>' },
    restart: { label: 'Restart ComfyUI', icon: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"></path><path d="M21 3v5h-5"></path>' },
    settings: { label: 'Set up ComfyUI', icon: '<path d="M12 15V3"></path><path d="m7 10 5 5 5-5"></path><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>' },
};

/** What the status line offers: start an offline ComfyUI, or restart the one Create started. */
function runtimeAction() {
    const health = state.health || {};
    if (state.runtimeBusy || state.job) return null;
    if (health.comfyui_online === false) {
        if (!state.runtimeConfig) return null;
        return state.runtimeConfig.install_path ? 'start' : 'settings';
    }
    const managed = state.runtimeStatus?.mode === 'managed';
    if ((health.restart_required || state.runtimeStale) && managed && runtimeUp(state.runtimeStatus)) return 'restart';
    return null;
}

function paintRuntimeAction() {
    const button = $('runtime-action');
    const action = runtimeAction();
    button.hidden = !action;
    if (!action) return;
    button.dataset.action = action;
    button.querySelector('svg').innerHTML = RUNTIME_ACTIONS[action].icon;
    button.querySelector('span').textContent = RUNTIME_ACTIONS[action].label;
}

function paintRuntimePower() {
    const button = $('runtime-power');
    const status = state.runtimeStatus;
    // Only a ComfyUI that Create started can be stopped from here.
    const canStop = status?.mode === 'managed' && ['ready', 'busy', 'starting'].includes(status.status);
    const canStart = !runtimeUp(status) && status?.status !== 'starting' && Boolean(state.runtimeConfig?.install_path);
    button.hidden = !(canStop || canStart || state.runtimeBusy);
    button.disabled = Boolean(state.runtimeBusy);
    button.dataset.action = canStop ? 'stop' : 'start';
    button.textContent = state.runtimeBusy ? 'Starting…' : canStop ? 'Stop ComfyUI' : 'Start ComfyUI';
}

function rememberRuntimeStatus(status) {
    state.runtimeStatus = status;
    setCachedJson('cmv_runtime_status', status, false);
    paintRuntimeDot();
    paintRuntimePower();
}

async function startRuntime(kind = 'start') {
    if (state.runtimeBusy) return;
    state.runtimeBusy = kind === 'restart' ? 'restarting' : 'starting';
    state.runtimeStartedAt = Date.now();
    paintHealth(state.health);
    paintRuntimePower();
    try {
        rememberRuntimeStatus(await json(`/api/comfyui/${kind}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        }));
    } catch (err) {
        state.runtimeBusy = null;
        paintHealth(state.health);
        paintRuntimePower();
        return error('Could not start ComfyUI', err.message);
    }
    clearInterval(state.runtimePoll);
    // The manager reports "starting" until the API answers, or "error" after an exit or its timeout.
    return new Promise(resolve => {
        const tick = async () => {
            let status = null;
            try { status = await json('/api/comfyui/status'); } catch {}
            if (!state.runtimeBusy) return;
            if (status) rememberRuntimeStatus(status);
            const up = runtimeUp(status);
            const failed = status && !up && status.status !== 'starting';
            if (!up && !failed) {
                paintHealth(state.health);
                return;
            }
            clearInterval(state.runtimePoll);
            state.runtimePoll = null;
            state.runtimeBusy = null;
            // A fresh start runs with the saved settings.
            if (up) state.runtimeStale = false;
            paintRuntimePower();
            await loadHealth(true);
            if (failed) {
                let log = '';
                try { log = (await json('/api/comfyui/logs?lines=25')).logs.join('\n'); } catch {}
                error('ComfyUI did not start', runtimeStatusText(status), log);
            }
            resolve(up);
        };
        state.runtimePoll = setInterval(tick, 1000);
    });
}

async function stopRuntime() {
    const button = $('runtime-power');
    button.disabled = true;
    button.textContent = 'Stopping…';
    try {
        rememberRuntimeStatus({ ...(await json('/api/comfyui/stop', { method: 'POST' })), online: false });
        state.runtimeStale = false;
        runtimeDetection('', 'ComfyUI stopped', runtimeStatusText(state.runtimeStatus));
    } catch (err) {
        runtimeDetection('error', 'Could not stop ComfyUI', err.message);
    } finally {
        button.disabled = false;
        paintRuntimePower();
        loadHealth(true);
    }
}

function runtimeActionClick() {
    const action = $('runtime-action').dataset.action;
    if (action === 'settings') return openRuntimeSettings();
    if (action === 'start' || action === 'restart') return startRuntime(action);
}

async function runtimePowerClick() {
    if ($('runtime-power').dataset.action === 'stop') return stopRuntime();
    runtimeDetection('', 'Starting ComfyUI…', 'The first start with custom nodes can take a minute.');
    const up = await startRuntime('start');
    runtimeDetection(up ? 'good' : 'error', up ? 'ComfyUI is running' : 'ComfyUI did not start', runtimeStatusText(state.runtimeStatus));
}

async function loadRuntimeSummary() {
    const cachedConfig = getCachedJson('cmv_runtime_config', 120 * 1000);
    const cachedStatus = getCachedJson('cmv_runtime_status', 30 * 1000);
    if (cachedConfig) state.runtimeConfig = cachedConfig;
    if (cachedStatus) {
        state.runtimeStatus = cachedStatus;
        paintRuntimeDot();
    }
    const [configResult, statusResult] = await Promise.allSettled([
        json('/api/comfyui/config'),
        json('/api/comfyui/status'),
    ]);
    if (configResult.status === 'fulfilled') {
        state.runtimeConfig = configResult.value;
        setCachedJson('cmv_runtime_config', configResult.value, false);
    }
    if (statusResult.status === 'fulfilled') {
        state.runtimeStatus = statusResult.value;
        setCachedJson('cmv_runtime_status', statusResult.value, false);
    }
    paintRuntimeDot();
    paintRuntimePower();
    if (state.health) paintHealth(state.health);
}

const FOLDER_KEYS = ['models_dir', 'input_dir', 'output_dir'];
const FOLDER_TITLES = {
    models_dir: 'Choose the models folder',
    input_dir: 'Choose the input folder',
    output_dir: 'Choose the output folder',
};

async function openRuntimeSettings() {
    $('runtime-settings-backdrop').hidden = false;
    $('runtime-settings-dialog').hidden = false;
    document.body.classList.add('simple-modal-open');
    await Promise.all([loadRuntimeSummary(), loadDesktop()]);
    const config = state.runtimeConfig || {};
    state.runtimeDraft = {
        installationId: config.desktop_installation_id || state.desktop.installations[0]?.id || null,
        folders: Object.fromEntries(FOLDER_KEYS.map(key => [key, config[key] || null])),
    };
    $('runtime-host').value = config.host || '127.0.0.1';
    $('runtime-port').value = config.port || 8188;
    $('runtime-civitai-token').value = '';
    $('runtime-civitai-token').placeholder = config.has_civitai_token ? 'Saved; leave empty to keep it' : 'Not set';
    paintDesktopChoice();
    paintRuntimeCard();
}

async function loadDesktop() {
    try {
        state.desktop = await json('/api/comfyui/installations');
    } catch {
        state.desktop = { installations: [], folders: {} };
    }
}

function paintRuntimeCard() {
    if (!state.desktop?.installations.length) {
        return runtimeDetection('error', 'ComfyUI Desktop is needed', 'Install ComfyUI Desktop, open it once, then press "Check again".');
    }
    const status = state.runtimeStatus;
    runtimeDetection(runtimeUp(status) ? 'good' : '', runtimeUp(status) ? 'ComfyUI is running' : 'ComfyUI', runtimeStatusText(status));
}

function samePath(a, b) {
    const norm = value => String(value || '').trim().replace(/[\\/]+/g, '\\').replace(/\\$/, '').toLowerCase();
    return Boolean(a) && norm(a) === norm(b);
}

function paintDesktopChoice() {
    const desktop = state.desktop || { installations: [], folders: {} };
    const draft = state.runtimeDraft;
    const found = desktop.installations.length > 0;
    $('runtime-no-desktop').hidden = found;
    $('runtime-installs').hidden = !found;
    $('runtime-installs').innerHTML = desktop.installations.map(item => {
        const active = item.id === draft.installationId;
        return `<button type="button" class="runtime-install${active ? ' is-active' : ''}" role="radio" aria-checked="${active}" data-install="${esc(item.id)}">
            <span class="runtime-install-dot"></span>
            <span class="runtime-install-main"><strong>${esc(item.name)}</strong><small title="${esc(item.install_path)}">${esc(item.install_path)}</small></span>
            ${item.version ? `<span class="runtime-install-tag">${esc(item.version)}</span>` : ''}
            ${item.last_used ? '<span class="runtime-install-tag">last used</span>' : ''}
        </button>`;
    }).join('');
    $('runtime-folders').querySelectorAll('.runtime-folder').forEach(row => {
        const key = row.dataset.folder;
        const own = draft.folders[key];
        const path = own || desktop.folders?.[key] || '';
        const pathLine = row.querySelector('[data-role="path"]');
        pathLine.textContent = path || 'Desktop default';
        pathLine.title = path;
        row.querySelector('[data-role="source"]').textContent = own ? 'Create only' : 'Same as Desktop';
        row.classList.toggle('is-custom', Boolean(own));
        row.querySelector('[data-action="pick"]').disabled = !found;
        row.querySelector('[data-action="reset"]').hidden = !own;
    });
    $('runtime-save').disabled = !found;
}

async function pickRuntimeFolder(key) {
    const desktopPath = state.desktop?.folders?.[key] || '';
    const current = state.runtimeDraft.folders[key] || desktopPath;
    let path;
    try {
        if (window.cmvDesktop?.chooseFolder) {
            path = await window.cmvDesktop.chooseFolder({ title: FOLDER_TITLES[key], defaultPath: current });
        } else {
            const data = await json('/api/simple/pick-comfyui-directory', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initial_path: current, title: FOLDER_TITLES[key] }),
            });
            path = data.cancelled ? null : data.path;
        }
    } catch (err) {
        return runtimeDetection('error', 'Could not open the folder picker', err.message);
    }
    if (!path) return;
    // Choosing Desktop's own folder simply follows Desktop again.
    state.runtimeDraft.folders[key] = samePath(path, desktopPath) ? null : path;
    paintDesktopChoice();
}

function runtimeFolderClick(event) {
    const button = event.target.closest('[data-action]');
    const key = button?.closest('.runtime-folder')?.dataset.folder;
    if (!key) return;
    if (button.dataset.action === 'pick') return pickRuntimeFolder(key);
    state.runtimeDraft.folders[key] = null;
    paintDesktopChoice();
}

function runtimeInstallClick(event) {
    const button = event.target.closest('[data-install]');
    if (!button) return;
    state.runtimeDraft.installationId = button.dataset.install;
    paintDesktopChoice();
}

async function rescanDesktop() {
    const button = $('runtime-rescan');
    button.disabled = true;
    try {
        await loadDesktop();
        state.runtimeDraft.installationId ||= state.desktop.installations[0]?.id || null;
        paintDesktopChoice();
        paintRuntimeCard();
    } finally {
        button.disabled = false;
    }
}

function closeRuntimeSettings() {
    const dialog = $('runtime-settings-dialog');
    if (!dialog || dialog.hidden) return;
    $('runtime-settings-backdrop').hidden = true;
    dialog.hidden = true;
    document.body.classList.remove('simple-modal-open');
}

async function saveRuntimeSettings(event) {
    event.preventDefault();
    const saveButton = $('runtime-save');
    const before = state.runtimeConfig || {};
    const draft = state.runtimeDraft;
    const payload = {
        desktop_installation_id: draft.installationId,
        host: $('runtime-host').value.trim() || '127.0.0.1',
        port: Number($('runtime-port').value) || 8188,
        // An empty folder follows Desktop again.
        ...Object.fromEntries(FOLDER_KEYS.map(key => [key, draft.folders[key] || ''])),
    };
    const token = $('runtime-civitai-token').value.trim();
    if (token) payload.civitai_api_token = token;
    const relaunch = draft.installationId !== before.desktop_installation_id
        || payload.host !== before.host || payload.port !== Number(before.port)
        || FOLDER_KEYS.some(key => (draft.folders[key] || null) !== (before[key] || null));
    saveButton.disabled = true;
    saveButton.textContent = 'Saving…';
    try {
        await json('/api/comfyui/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        await loadRuntimeSummary();
        closeRuntimeSettings();
        if (relaunch && runtimeUp(state.runtimeStatus)) {
            // A running ComfyUI keeps its folders until it starts again.
            if (state.runtimeStatus.mode === 'managed') {
                state.runtimeStale = true;
                showNote('Saved. Restart ComfyUI to use the new settings.');
            } else {
                showNote('Saved. ComfyUI is running outside Create; the new settings apply the next time Create starts it.');
            }
        }
        await loadHealth(true);
        await loadDownloads();
    } catch (err) {
        runtimeDetection('error', 'Could not save settings', err.message);
    } finally {
        saveButton.disabled = false;
        saveButton.textContent = 'Save';
    }
}

let activeContextMenu = null;

function closeContextMenu() {
    if (activeContextMenu) {
        activeContextMenu.remove();
        activeContextMenu = null;
    }
}

function createMenuItem({ icon, label, badge, onClick }) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'image-context-menu__item';
    btn.innerHTML = `${icon}<span class="image-context-menu__item-label">${esc(label)}</span>${badge ? `<span class="image-context-menu__badge">${esc(badge)}</span>` : ''}`;
    btn.addEventListener('click', () => {
        closeContextMenu();
        if (onClick) onClick();
    });
    return btn;
}

function createSubmenuItem({ icon, label, badge, items }) {
    const wrap = document.createElement('div');
    wrap.className = 'image-context-menu__submenu-wrap';

    const parentBtn = document.createElement('button');
    parentBtn.type = 'button';
    parentBtn.className = 'image-context-menu__item image-context-menu__item--submenu';
    parentBtn.innerHTML = `${icon}<span class="image-context-menu__item-label">${esc(label)}</span>${badge ? `<span class="image-context-menu__badge">${esc(badge)}</span>` : ''}<span class="image-context-menu__chevron" aria-hidden="true">›</span>`;

    const submenu = document.createElement('div');
    submenu.className = 'image-context-menu image-context-menu--submenu';
    submenu.hidden = true;

    items.forEach(it => {
        const itemBtn = document.createElement('button');
        itemBtn.type = 'button';
        itemBtn.className = 'image-context-menu__item';
        itemBtn.innerHTML = `${it.icon || '<span style="width:16px;display:inline-block;text-align:center;font-weight:700;color:var(--accent,#2dd4bf);flex-shrink:0">' + (it.active ? '✓' : '') + '</span>'}<span class="image-context-menu__item-label">${esc(it.label)}</span>`;
        itemBtn.addEventListener('click', e => {
            e.stopPropagation();
            closeContextMenu();
            if (it.onClick) it.onClick();
        });
        submenu.appendChild(itemBtn);
    });

    const openSub = () => {
        submenu.hidden = false;
        const rect = wrap.getBoundingClientRect();
        if (rect.right + 220 > window.innerWidth) {
            submenu.classList.add('image-context-menu--opens-left');
        } else {
            submenu.classList.remove('image-context-menu--opens-left');
        }
    };
    const closeSub = () => {
        submenu.hidden = true;
    };

    wrap.addEventListener('pointerenter', openSub);
    wrap.addEventListener('pointerleave', closeSub);

    wrap.appendChild(parentBtn);
    wrap.appendChild(submenu);
    return wrap;
}

function createSliderItem({ icon, label, min, max, step, value, format, onInput, onChange }) {
    const wrap = document.createElement('div');
    wrap.className = 'image-context-menu__slider-item';
    wrap.addEventListener('click', e => e.stopPropagation());
    wrap.addEventListener('mousedown', e => e.stopPropagation());
    wrap.addEventListener('contextmenu', e => e.stopPropagation());

    const header = document.createElement('div');
    header.className = 'image-context-menu__slider-header';

    const titleWrap = document.createElement('span');
    titleWrap.className = 'image-context-menu__slider-title';
    titleWrap.innerHTML = `${icon}<span>${esc(label)}</span>`;

    const valBadge = document.createElement('span');
    valBadge.className = 'image-context-menu__slider-val';
    valBadge.textContent = format ? format(value) : value;

    header.appendChild(titleWrap);
    header.appendChild(valBadge);

    const range = document.createElement('input');
    range.type = 'range';
    range.className = 'image-context-menu__range';
    range.min = String(min);
    range.max = String(max);
    range.step = String(step || 1);
    range.value = String(value);

    range.addEventListener('input', () => {
        const num = Number(range.value);
        valBadge.textContent = format ? format(num) : num;
        if (onInput) onInput(num);
    });

    range.addEventListener('change', () => {
        const num = Number(range.value);
        if (onChange) onChange(num);
    });

    wrap.appendChild(header);
    wrap.appendChild(range);
    return wrap;
}

function openStudioContextMenu(event) {
    if (event.target.closest('input, textarea, [contenteditable="true"]')) {
        return;
    }
    event.preventDefault();
    closeContextMenu();

    const menu = document.createElement('div');
    menu.className = 'image-context-menu studio-context-menu';

    const title = document.createElement('div');
    title.className = 'image-context-menu__title';
    title.textContent = 'Studio settings';
    menu.appendChild(title);

    menu.appendChild(createMenuItem({
        icon: '<svg viewBox="0 0 24 24"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>',
        label: 'Change background',
        onClick: () => ambientPick(),
    }));

    const sep1 = document.createElement('div');
    sep1.className = 'image-context-menu__separator';
    menu.appendChild(sep1);

    menu.appendChild(createSliderItem({
        icon: '<svg viewBox="0 0 24 24"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
        label: 'Card opacity',
        min: 10,
        max: 95,
        step: 1,
        value: state.opacity ?? 44,
        format: v => `${v}%`,
        onInput: v => {
            state.opacity = v;
            applyVisualSettings();
        },
        onChange: v => {
            state.opacity = v;
            save('cmv_simple_glass_opacity', state.opacity);
        },
    }));

    menu.appendChild(createSliderItem({
        icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><line x1="12" y1="1" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="23"/><line x1="1" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="23" y2="12"/></svg>',
        label: 'Background blur',
        min: 0,
        max: 40,
        step: 1,
        value: state.blur ?? 10,
        format: v => (v === 0 ? 'No blur' : `${v} px`),
        onInput: v => {
            state.blur = v;
            applyVisualSettings();
        },
        onChange: v => {
            state.blur = v;
            save('cmv_simple_ambient_blur', state.blur);
        },
    }));

    menu.appendChild(createSliderItem({
        icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
        label: 'Change interval',
        min: 0,
        max: 600,
        step: 15,
        value: Math.round((state.interval || 0) / 1000),
        format: v => {
            if (v === 0) return 'Disabled';
            if (v < 60) return `${v} sec`;
            const m = Math.floor(v / 60);
            const s = v % 60;
            return s ? `${m} min ${s} sec` : `${m} min`;
        },
        onInput: setAmbientInterval,
        onChange: setAmbientInterval,
    }));

    const sep2 = document.createElement('div');
    sep2.className = 'image-context-menu__separator';
    menu.appendChild(sep2);

    const fitLabels = {
        cover: 'Cover',
        contain: 'Contain',
        original: '1:1',
    };
    menu.appendChild(createSubmenuItem({
        icon: '<svg viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
        label: 'Background fit',
        badge: fitLabels[state.fit] || 'Cover',
        items: [
            {
                label: 'Cover',
                active: state.fit === 'cover',
                onClick: () => {
                    state.fit = 'cover';
                    save('cmv_simple_ambient_fit', state.fit);
                    applyVisualSettings();
                },
            },
            {
                label: 'Contain',
                active: state.fit === 'contain',
                onClick: () => {
                    state.fit = 'contain';
                    save('cmv_simple_ambient_fit', state.fit);
                    applyVisualSettings();
                },
            },
            {
                label: 'Original (1:1)',
                active: state.fit === 'original',
                onClick: () => {
                    state.fit = 'original';
                    save('cmv_simple_ambient_fit', state.fit);
                    applyVisualSettings();
                },
            },
        ],
    }));

    menu.appendChild(createMenuItem({
        icon: '<svg viewBox="0 0 24 24"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
        label: 'Completion sound',
        badge: state.sound ? 'On' : 'Off',
        onClick: () => {
            state.sound = !state.sound;
            save('cmv_simple_sound_alert', state.sound);
            if (state.sound) playSuccessChime();
        },
    }));

    menu.appendChild(createMenuItem({
        icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21H9.6v-.1A1.7 1.7 0 0 0 8.5 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 8.5 4.1a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V2h4v.4A1.7 1.7 0 0 0 15 4.1a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 8.5a1.7 1.7 0 0 0 .6 1 1.7 1.7 0 0 0 1.1.4h.9v4h-.9a1.7 1.7 0 0 0-1.7 1.1Z"/></svg>',
        label: 'ComfyUI settings…',
        onClick: () => openRuntimeSettings(),
    }));

    document.body.appendChild(menu);
    activeContextMenu = menu;

    const menuRect = menu.getBoundingClientRect();
    let x = event.clientX;
    let y = event.clientY;
    if (x + menuRect.width > window.innerWidth - 8) {
        x = Math.max(8, window.innerWidth - menuRect.width - 8);
    }
    if (y + menuRect.height > window.innerHeight - 8) {
        y = Math.max(8, window.innerHeight - menuRect.height - 8);
    }
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
}

document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', init)
    : init();
