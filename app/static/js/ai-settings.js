// Integrations › Providers and Models, modelled on OpenCode desktop's settings.
// Providers connects local AI tools and API providers; Models chooses which of their
// models the pickers in Create and the Viewer show, and the default model.
import { defaultReasoning, reasoningChoices } from './model-picker.js';
import { customConfirm } from './utils.js';

const ICONS_URL = document.body.dataset.providerIcons || '/static/icons/providers.svg';
const PANES = ['providers', 'models', 'social', 'remote', 'sources'];
// Older links: Local AI tools now live in Providers.
const PANE_ALIASES = { cli: 'providers' };
// Model pickers reload their lists when this is newer than their copy.
const MODELS_CHANGED_KEY = 'mediastudio_ai_models_changed_at';
const COLLAPSED_KEY = 'mediastudio_ai_models_collapsed_v1';
const LEGACY_KEYS = [
    'mediastudio_ai_profiles_cache_v1', 'mediastudio_ai_connected_cli_types_v1', 'mediastudio_ai_model_catalog_v1', 'mediastudio_ai_cli_probes_v1',
];
const TOOL_DOWNLOADS = {
    opencode: 'https://opencode.ai/download',
    claude: 'https://docs.anthropic.com/en/docs/claude-code/setup',
    codex: 'https://developers.openai.com/codex/cli',
    antigravity: 'https://antigravity.google/docs/cli/install',
};
const KEY_SOURCE_TAGS = { system: 'API key', environment: 'Environment', none: 'No key' };

const state = {
    providers: [],
    tools: [],
    templates: [],
    secretStore: { available: true },
    defaultModel: null,
    catalog: null,
    catalogLoading: false,
    catalogError: '',
    checks: new Map(),
    collapsed: readCollapsed(),
    iconIds: null,
};

const $ = id => document.getElementById(id);

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function svgIcon(markup, className) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    if (className) svg.setAttribute('class', className);
    svg.innerHTML = markup;
    return svg;
}

function iconName(name) {
    if (!state.iconIds) return name || 'custom';
    // A regional or plan variant ("google-vertex", "moonshotai-cn") uses its family mark.
    let candidate = name || '';
    while (candidate && !state.iconIds.has(candidate)) {
        candidate = candidate.includes('-') ? candidate.slice(0, candidate.lastIndexOf('-')) : '';
    }
    return candidate || 'custom';
}

function providerIcon(name, className = 'provider-icon') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `${ICONS_URL}#${iconName(name)}`);
    svg.append(use);
    return svg;
}

function setIcon(svg, name) {
    svg.querySelector('use')?.setAttribute('href', `${ICONS_URL}#${iconName(name)}`);
}

async function loadIconIds() {
    try {
        const text = await (await fetch(ICONS_URL)).text();
        state.iconIds = new Set([...text.matchAll(/<symbol id="([^"]+)"/g)].map(match => match[1]));
    } catch {
        state.iconIds = null;
    }
}

function tag(text, variant = '') {
    return el('span', `settings-tag${variant ? ` is-${variant}` : ''}`, text);
}

function button(label, onClick, className = 'settings-ghost-button') {
    const node = el('button', className, label);
    node.type = 'button';
    node.addEventListener('click', onClick);
    return node;
}

async function requestJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.error) {
        const error = new Error(data.error || `${response.status} ${response.statusText}`);
        error.code = data.code;
        error.technicalError = data.technical_error;
        throw error;
    }
    return data;
}

function jsonOptions(method, body) {
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

function showToast(message, isError = false) {
    const toast = $('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle('toast-error', isError);
    toast.classList.add('show');
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 3200);
}

function markModelsChanged() {
    try { localStorage.setItem(MODELS_CHANGED_KEY, String(Date.now())); } catch { /* pickers refresh on their TTL */ }
}

function readCollapsed() {
    try {
        const stored = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || '{}');
        return stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
    } catch {
        return {};
    }
}

function writeCollapsed() {
    try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify(state.collapsed)); } catch { /* per-visit only */ }
}

/* ------------------------------------------------------------------ */
/* Pane routing                                                        */
/* ------------------------------------------------------------------ */

function currentPane() {
    const hash = window.location.hash.replace('#', '');
    const pane = PANE_ALIASES[hash] || hash;
    return PANES.includes(pane) ? pane : 'providers';
}

function activatePane(pane) {
    document.querySelectorAll('.integrations-pane').forEach(section => {
        section.hidden = section.dataset.pane !== pane;
    });
    document.querySelectorAll('.rail-link').forEach(link => {
        const active = link.dataset.pane === pane;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    });
    if (pane === 'models' && !state.catalog && !state.catalogLoading) loadModels();
}

/* ------------------------------------------------------------------ */
/* Providers                                                           */
/* ------------------------------------------------------------------ */

async function loadProviders() {
    try {
        const data = await requestJson('/api/ai/providers');
        state.providers = data.providers || [];
        state.tools = data.local_tools || [];
        state.templates = data.templates || [];
        state.secretStore = data.secret_store || { available: false };
        state.defaultModel = data.default_model || null;
    } catch (error) {
        showToast(error.message, true);
    }
    renderProviders();
    renderDefaultModelSelect();
}

function templateById(id) {
    return state.templates.find(item => item.id === id) || null;
}

function providerRow({ iconId, name, tags = [], meta = null, metaClass = '', actions = [] }) {
    const row = el('div', 'settings-provider-row');
    const lead = el('div', 'settings-provider-lead');
    lead.append(providerIcon(iconId));
    const copy = el('div', 'settings-provider-copy');
    const main = el('div', 'settings-provider-main');
    main.append(el('span', 'settings-provider-name', name), ...tags);
    copy.append(main);
    if (meta) {
        const line = el('p', `settings-provider-meta${metaClass ? ` ${metaClass}` : ''}`, meta);
        line.title = meta;
        copy.append(line);
    }
    lead.append(copy);
    const controls = el('div', 'settings-provider-actions');
    controls.append(...actions);
    row.append(lead, controls);
    return row;
}

function connectedToolRow(tool) {
    return providerRow({
        iconId: tool.icon,
        name: tool.label,
        tags: [tag('Local tool')],
        meta: tool.executable,
        actions: [
            button('Details', () => openToolDialog(tool)),
            button('Disconnect', () => disconnectTool(tool), 'settings-ghost-button is-danger'),
        ],
    });
}

function connectedProviderRow(provider) {
    const template = templateById(provider.template);
    const tags = [tag(template ? KEY_SOURCE_TAGS[provider.api_key_source] : 'Custom')];
    if (!provider.has_credentials) tags.push(tag('Key missing', 'warning'));
    const check = state.checks.get(provider.id);
    let meta = provider.base_url;
    let metaClass = '';
    if (provider.credential_error) {
        meta = provider.credential_error;
        metaClass = 'is-error';
    } else if (check?.busy) {
        meta = 'Checking the model list…';
    } else if (check?.error) {
        meta = check.error;
        metaClass = 'is-error';
    } else if (check?.models !== undefined) {
        meta = `Connected · ${check.models} models`;
        metaClass = 'is-success';
    }
    const checkButton = button(check?.busy ? 'Checking…' : 'Check', () => checkProvider(provider));
    checkButton.disabled = Boolean(check?.busy);
    return providerRow({
        iconId: provider.template || 'custom',
        name: provider.name,
        tags,
        meta,
        metaClass,
        actions: [
            checkButton,
            button('Edit', () => (template ? openConnectDialog(template, provider) : openCustomDialog(provider))),
            button('Disconnect', () => disconnectProvider(provider), 'settings-ghost-button is-danger'),
        ],
    });
}

function popularRow({ iconId, name, tags = [], description, onConnect }) {
    const row = el('div', 'settings-provider-row');
    const lead = el('div', 'settings-provider-lead');
    lead.append(providerIcon(iconId));
    const copy = el('div', 'settings-provider-copy');
    const main = el('div', 'settings-provider-main');
    main.append(el('span', 'settings-provider-name', name), ...tags);
    copy.append(main);
    if (description) copy.append(el('p', 'settings-provider-description', description));
    lead.append(copy);
    const connect = el('button', 'settings-button');
    connect.type = 'button';
    connect.append(svgIcon('<path d="M12 5v14"/><path d="M5 12h14"/>'), el('span', '', 'Connect'));
    connect.addEventListener('click', onConnect);
    row.append(lead, connect);
    return row;
}

function renderProviders() {
    const connected = $('connected-providers');
    connected.replaceChildren();
    const tools = state.tools.filter(tool => tool.connected);
    tools.forEach(tool => connected.append(connectedToolRow(tool)));
    state.providers.forEach(provider => connected.append(connectedProviderRow(provider)));
    if (!connected.children.length) connected.append(el('div', 'settings-provider-empty', 'No connected providers'));

    const note = $('secret-store-note');
    note.hidden = state.secretStore.available !== false;
    note.textContent = state.secretStore.available === false
        ? `${state.secretStore.message || 'The system credential store is unavailable.'} API keys can come from environment variables instead.`
        : '';

    const popular = $('popular-providers');
    popular.replaceChildren();
    state.tools.filter(tool => !tool.connected).forEach(tool => {
        popular.append(popularRow({
            iconId: tool.icon,
            name: tool.label,
            tags: [
                ...(tool.type === 'opencode' ? [tag('Recommended', 'accent')] : []),
                tag(tool.installed ? 'Disconnected' : 'Local tool'),
            ],
            description: tool.note,
            onConnect: () => (tool.installed ? reconnectTool(tool) : openToolDialog(tool)),
        }));
    });
    const connectedTemplates = new Set(state.providers.map(provider => provider.template));
    state.templates
        .filter(template => template.popular && !connectedTemplates.has(template.id))
        .forEach(template => {
            popular.append(popularRow({
                iconId: template.id,
                name: template.name,
                description: template.note,
                onConnect: () => openConnectDialog(template),
            }));
        });
    if (!popular.children.length) popular.append(el('div', 'settings-provider-empty', 'Every popular provider is connected.'));
}

async function checkProvider(provider) {
    state.checks.set(provider.id, { busy: true });
    renderProviders();
    try {
        const data = await requestJson(`/api/ai/providers/${encodeURIComponent(provider.id)}/check`, { method: 'POST' });
        state.checks.set(provider.id, { models: data.models });
    } catch (error) {
        state.checks.set(provider.id, { error: error.message });
    }
    renderProviders();
}

async function disconnectProvider(provider) {
    const confirmed = await customConfirm(
        'Disconnect provider',
        `Disconnect ${provider.name}? Its stored API key is deleted and its models leave the pickers.`,
    );
    if (!confirmed) return;
    try {
        await requestJson(`/api/ai/providers/${encodeURIComponent(provider.id)}`, { method: 'DELETE' });
        state.checks.delete(provider.id);
        showToast(`${provider.name} disconnected.`);
        afterProvidersChanged();
    } catch (error) {
        showToast(error.message, true);
    }
}

async function setToolConnected(tool, connected) {
    try {
        const data = await requestJson(`/api/ai/local-tools/${tool.type}`, jsonOptions('PATCH', { connected }));
        state.tools = data.local_tools || state.tools;
        showToast(connected ? `${tool.label} connected.` : `${tool.label} disconnected.`);
        afterProvidersChanged();
    } catch (error) {
        showToast(error.message, true);
    }
}

async function disconnectTool(tool) {
    const confirmed = await customConfirm(
        'Disconnect local tool',
        `Hide ${tool.label} and its models from the pickers? The tool itself stays installed and signed in.`,
    );
    if (confirmed) await setToolConnected(tool, false);
}

function reconnectTool(tool) {
    return setToolConnected(tool, true);
}

function afterProvidersChanged() {
    state.catalog = null;
    markModelsChanged();
    loadProviders();
    if (currentPane() === 'models') loadModels();
}

/* --- connect dialog (template) --- */

let connectTarget = null;

function updateConnectKeyFields() {
    const source = $('connect-key-source').value;
    $('connect-api-key-field').hidden = source !== 'system';
    $('connect-env-field').hidden = source !== 'environment';
}

function openConnectDialog(template, provider = null) {
    connectTarget = { template, provider };
    $('connect-form').reset();
    $('connect-error').hidden = true;
    setIcon($('connect-icon'), template.id);
    $('connect-title').textContent = provider ? `Edit ${provider.name}` : `Connect ${template.name}`;
    $('connect-description').textContent = template.local
        ? `${template.name} runs on this computer. Check the address of its server.`
        : `Enter your ${template.name} API key to use ${template.name} models in this app.`;
    $('connect-base-url').value = provider?.base_url || template.base_url;
    $('connect-base-url-field').hidden = !template.local && !provider;
    const systemOption = $('connect-key-source').querySelector('option[value="system"]');
    systemOption.disabled = state.secretStore.available === false;
    const defaultSource = template.local ? 'none' : (state.secretStore.available === false ? 'environment' : 'system');
    $('connect-key-source').value = provider?.api_key_source || defaultSource;
    $('connect-env').value = provider?.api_key_env || template.env?.[0] || '';
    $('connect-api-key-label').textContent = `${template.name} API key`;
    $('connect-api-key').placeholder = provider ? 'Leave empty to keep the stored key' : 'API key';
    $('connect-api-key-help').textContent = 'Saved in the operating system credential store, never in config files.';
    $('connect-docs').hidden = !template.doc;
    $('connect-docs-link').href = template.doc || '#';
    $('connect-submit').textContent = provider ? 'Save' : 'Connect';
    updateConnectKeyFields();
    $('connect-dialog').showModal();
    ($('connect-key-source').value === 'system' ? $('connect-api-key') : $('connect-key-source')).focus();
}

async function submitConnect(event) {
    event.preventDefault();
    if (!connectTarget) return;
    const { template, provider } = connectTarget;
    const source = $('connect-key-source').value;
    const payload = {
        api_key_source: source,
        base_url: $('connect-base-url').value.trim() || template.base_url,
    };
    if (source === 'system' && $('connect-api-key').value.trim()) payload.api_key = $('connect-api-key').value.trim();
    if (source === 'environment') payload.api_key_env = $('connect-env').value.trim();
    if (!provider) payload.template = template.id;
    $('connect-submit').disabled = true;
    try {
        if (provider) {
            await requestJson(`/api/ai/providers/${encodeURIComponent(provider.id)}`, jsonOptions('PATCH', payload));
        } else {
            await requestJson('/api/ai/providers', jsonOptions('POST', payload));
        }
        $('connect-dialog').close();
        showToast(provider ? `${template.name} updated.` : `${template.name} connected.`);
        if (provider) state.checks.delete(provider.id);
        afterProvidersChanged();
    } catch (error) {
        $('connect-error').textContent = error.message;
        $('connect-error').hidden = false;
    } finally {
        $('connect-submit').disabled = false;
    }
}

/* --- custom provider dialog --- */

let customTarget = null;

function addCustomModelRow(model = { id: '', name: '' }) {
    const row = el('div', 'custom-model-row');
    const field = (value, placeholder, role) => {
        const input = el('input');
        input.value = value || '';
        input.placeholder = placeholder;
        input.spellcheck = false;
        input.dataset.role = role;
        const wrap = el('label', 'field');
        wrap.append(input);
        return wrap;
    };
    const remove = el('button', '', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', 'Remove model');
    remove.addEventListener('click', () => row.remove());
    row.append(field(model.id, 'model-id', 'id'), field(model.name === model.id ? '' : model.name, 'Display name', 'name'), remove);
    $('custom-models').append(row);
}

function openCustomDialog(provider = null) {
    customTarget = provider;
    $('custom-form').reset();
    $('custom-error').hidden = true;
    $('custom-models').replaceChildren();
    $('custom-title').textContent = provider ? `Edit ${provider.name}` : 'Custom provider';
    $('custom-id').value = provider?.id || '';
    $('custom-id').disabled = Boolean(provider);
    $('custom-name').value = provider?.name || '';
    $('custom-base-url').value = provider?.base_url || '';
    $('custom-api-key').placeholder = provider?.api_key_source === 'system' ? 'Leave empty to keep the stored key' : 'API key';
    $('custom-api-key-help').textContent = provider?.api_key_source === 'system'
        ? 'Leave empty to keep the stored key.'
        : 'Leave empty for a server that needs no key.';
    (provider?.models || []).forEach(model => addCustomModelRow(model));
    $('custom-submit').textContent = provider ? 'Save' : 'Connect';
    $('custom-dialog').showModal();
    (provider ? $('custom-name') : $('custom-id')).focus();
}

async function submitCustom(event) {
    event.preventDefault();
    const models = [...$('custom-models').querySelectorAll('.custom-model-row')]
        .map(row => ({
            id: row.querySelector('[data-role="id"]').value.trim(),
            name: row.querySelector('[data-role="name"]').value.trim(),
        }))
        .filter(model => model.id);
    const apiKey = $('custom-api-key').value.trim();
    const payload = {
        name: $('custom-name').value.trim(),
        base_url: $('custom-base-url').value.trim(),
        models: models.map(model => ({ id: model.id, name: model.name || model.id })),
    };
    if (customTarget) {
        if (apiKey) {
            payload.api_key_source = 'system';
            payload.api_key = apiKey;
        }
    } else {
        payload.id = $('custom-id').value.trim();
        payload.api_key_source = apiKey ? 'system' : 'none';
        if (apiKey) payload.api_key = apiKey;
    }
    $('custom-submit').disabled = true;
    try {
        if (customTarget) {
            await requestJson(`/api/ai/providers/${encodeURIComponent(customTarget.id)}`, jsonOptions('PATCH', payload));
            state.checks.delete(customTarget.id);
        } else {
            await requestJson('/api/ai/providers', jsonOptions('POST', payload));
        }
        $('custom-dialog').close();
        showToast(customTarget ? `${payload.name} updated.` : `${payload.name} connected.`);
        afterProvidersChanged();
    } catch (error) {
        $('custom-error').textContent = error.message;
        $('custom-error').hidden = false;
    } finally {
        $('custom-submit').disabled = false;
    }
}

/* --- all providers dialog --- */

function renderAllProviders() {
    const query = $('all-providers-search').value.trim().toLocaleLowerCase();
    const list = $('all-providers-list');
    list.replaceChildren();
    const matches = item => !query || item.name.toLocaleLowerCase().includes(query) || item.id.includes(query);
    const item = (iconId, name, meta, onClick) => {
        const row = el('button', 'all-providers-item');
        row.type = 'button';
        row.append(providerIcon(iconId), el('span', 'settings-provider-name', name));
        if (meta) row.append(el('span', 'all-providers-meta', meta));
        row.addEventListener('click', onClick);
        return row;
    };
    const close = () => $('all-providers-dialog').close();
    const custom = { id: 'custom', name: 'Custom OpenAI-compatible provider' };
    if (matches(custom)) {
        list.append(item('custom', custom.name, 'Any address', () => { close(); openCustomDialog(); }));
    }
    const tools = state.tools.filter(tool => !tool.connected && matches({ id: tool.type, name: tool.label }));
    const connectedTemplates = new Set(state.providers.map(provider => provider.template));
    const templates = state.templates.filter(template => matches(template));
    const popular = templates.filter(template => template.popular);
    const other = templates.filter(template => !template.popular);
    const section = (label, rows) => {
        if (!rows.length) return;
        list.append(el('div', 'all-providers-group', label), ...rows);
    };
    section('Local tools', tools.map(tool => item(tool.icon, tool.label, tool.installed ? 'Disconnected' : 'Not installed', () => {
        close();
        if (tool.installed) reconnectTool(tool);
        else openToolDialog(tool);
    })));
    const templateRow = template => item(
        template.id,
        template.name,
        connectedTemplates.has(template.id) ? 'Connected' : (template.local ? 'This computer' : `${template.models || 0} models`),
        () => { close(); openConnectDialog(template); },
    );
    section('Popular', popular.map(templateRow));
    section('Other', other.map(templateRow));
    if (!list.children.length) list.append(el('div', 'settings-provider-empty', 'No providers found'));
}

function openAllProviders() {
    $('all-providers-search').value = '';
    renderAllProviders();
    $('all-providers-dialog').showModal();
    $('all-providers-search').focus();
}

/* --- local tool dialog --- */

let toolTarget = null;

function currentOs() {
    const ua = navigator.userAgent;
    if (/windows/i.test(ua)) return 'windows';
    if (/mac os|macintosh/i.test(ua)) return 'macos';
    return 'linux';
}

function copyCommand(command, copyButton) {
    navigator.clipboard?.writeText(command).then(() => {
        copyButton.textContent = 'Copied';
        window.setTimeout(() => { copyButton.textContent = 'Copy'; }, 1600);
    }, () => showToast('Copy failed', true));
}

function installBlock(tool, install) {
    const block = el('div', 'cli-install');
    if (!install) {
        block.append(el('p', 'cli-install-title', `${tool.label} is not installed.`));
        const link = el('a', '', 'Download from the official site');
        link.href = TOOL_DOWNLOADS[tool.type];
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        const note = el('p', 'cli-install-note');
        note.append(link, '. After installing, press Check again.');
        block.append(note);
        return block;
    }
    block.append(el('p', 'cli-install-title', 'Install the CLI with a terminal command:'));
    const detected = currentOs();
    ['windows', 'macos', 'linux']
        .sort((a, b) => (a === detected ? -1 : b === detected ? 1 : 0))
        .forEach(osKey => {
            const entry = install[osKey];
            if (!entry) return;
            const row = el('div', `cli-install-os${osKey === detected ? ' current' : ''}`);
            row.append(el('span', 'cli-install-label', entry.label));
            if (entry.command) {
                row.append(el('code', '', entry.command));
                const copy = button('Copy', () => copyCommand(entry.command, copy), 'cli-copy');
                row.append(copy);
            } else if (entry.url) {
                const link = el('a', '', 'Download from the official site');
                link.href = entry.url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                row.append(link);
            }
            block.append(row);
        });
    const note = el('p', 'cli-install-note');
    note.append('If the command is not on PATH afterwards, run ', el('code', '', install.path_setup_command), `. ${install.note} `);
    const docs = el('a', '', 'Installation docs');
    docs.href = install.docs_url;
    docs.target = '_blank';
    docs.rel = 'noopener noreferrer';
    note.append(docs);
    block.append(note);
    return block;
}

function authenticationLabel(authentication) {
    const labels = {
        available: 'Signed in',
        missing: 'Not signed in',
        error: 'Sign-in error',
        unknown: 'Sign-in not verified',
        unavailable: 'Not installed',
    };
    return labels[authentication?.status] || 'Status unknown';
}

async function probeTool() {
    const tool = toolTarget;
    if (!tool) return;
    const body = $('tool-body');
    body.replaceChildren(el('p', 'tool-status-line', 'Checking the installation and sign-in…'));
    $('tool-recheck').disabled = true;
    try {
        const { integration } = await requestJson(`/api/ai/cli-integrations/${tool.type}`);
        if (toolTarget !== tool) return;
        body.replaceChildren();
        $('tool-path').textContent = integration.executable || 'Not found on this computer';
        if (!integration.installed) {
            body.append(installBlock(tool, integration.install));
            return;
        }
        const tags = el('div', 'badge-row');
        if (integration.version) tags.append(tag(integration.version));
        tags.append(tag(authenticationLabel(integration.authentication),
            integration.authentication?.status === 'available' ? 'accent' : 'warning'));
        (integration.installations || []).forEach(item => tags.append(tag(item.label)));
        body.append(tags);
        if (integration.authentication?.message) {
            body.append(el('p', 'tool-status-line', integration.authentication.message));
        }
        const known = state.tools.find(item => item.type === tool.type);
        if (known && !known.installed) loadProviders();
        if (known && known.installed && !known.connected) {
            body.append(button('Connect', () => { $('tool-dialog').close(); reconnectTool(known); }, 'btn btn-primary'));
        }
    } catch (error) {
        body.replaceChildren(el('p', 'tool-status-line', error.message));
    } finally {
        $('tool-recheck').disabled = false;
    }
}

function openToolDialog(tool) {
    toolTarget = tool;
    setIcon($('tool-icon'), tool.icon);
    $('tool-title').textContent = tool.label;
    $('tool-path').textContent = tool.executable || '';
    $('tool-dialog').showModal();
    probeTool();
}

/* ------------------------------------------------------------------ */
/* Models                                                              */
/* ------------------------------------------------------------------ */

async function loadModels(refresh = false) {
    if (state.catalogLoading) return;
    state.catalogLoading = true;
    state.catalogError = '';
    const refreshButton = $('models-refresh');
    refreshButton.disabled = true;
    refreshButton.classList.add('is-busy');
    if (!state.catalog) renderModels();
    try {
        state.catalog = await requestJson(`/api/ai/models${refresh ? '?refresh=1' : ''}`);
        state.defaultModel = state.catalog.default_model || null;
        if (refresh) markModelsChanged();
    } catch (error) {
        state.catalogError = error.message;
    } finally {
        state.catalogLoading = false;
        refreshButton.disabled = false;
        refreshButton.classList.remove('is-busy');
    }
    renderModels();
}

// Seeing photos is the norm; only the exceptions and free models get a mark.
function modelTags(group, model) {
    const tags = [];
    if (model.vision === false) tags.push(tag('Text only', 'subtle'));
    if (model.free) tags.push(tag('Free', 'subtle'));
    if (group.kind === 'api' && model.manual) tags.push(tag('Added', 'subtle'));
    return tags;
}

function modelSwitch(checked, label, onChange) {
    const wrap = el('label', 'settings-switch');
    wrap.title = label;
    const input = el('input');
    input.type = 'checkbox';
    input.checked = checked;
    input.setAttribute('aria-label', label);
    input.addEventListener('change', () => onChange(input.checked, input));
    const control = el('span', 'settings-switch-control');
    control.append(el('span', 'settings-switch-thumb'));
    wrap.append(input, control);
    return wrap;
}

function matchesQuery(group, model, query) {
    if (!query) return true;
    return [model.name, model.id, group.label].some(value => String(value || '').toLocaleLowerCase().includes(query));
}

function renderModels() {
    const container = $('models-groups');
    const status = $('models-status');
    container.replaceChildren();
    renderDefaultModelSelect();
    if (!state.catalog) {
        status.hidden = false;
        status.replaceChildren(el('span', '', state.catalogError || 'Asking the connected providers for their models…'));
        return;
    }
    const query = $('models-search').value.trim().toLocaleLowerCase();
    let shownGroups = 0;
    state.catalog.groups.forEach(group => {
        const models = group.models.filter(model => matchesQuery(group, model, query));
        if (query && !models.length) return;
        shownGroups += 1;
        const expanded = Boolean(query) || !state.collapsed[group.id];
        const section = el('section', 'settings-models-group');
        section.dataset.expanded = String(expanded);

        const header = el('div', 'settings-models-group-header');
        const trigger = el('button', 'settings-models-group-trigger');
        trigger.type = 'button';
        trigger.setAttribute('aria-expanded', String(expanded));
        const chevron = el('span', 'settings-models-group-chevron');
        chevron.append(svgIcon('<path d="m6 9 6 6 6-6"/>'));
        const label = el('span', 'settings-models-group-label');
        label.append(providerIcon(group.icon), el('span', 'settings-models-group-title', group.label));
        if (group.source) label.append(tag(group.source));
        trigger.append(chevron, label);
        trigger.addEventListener('click', () => {
            if (query) return;
            state.collapsed[group.id] = !state.collapsed[group.id];
            if (!state.collapsed[group.id]) delete state.collapsed[group.id];
            writeCollapsed();
            renderModels();
        });
        const actions = el('div', 'settings-models-group-actions');
        const visible = group.models.filter(model => model.visible).length;
        actions.append(el('span', '', `${visible} of ${group.models.length} shown`));
        if (group.models.length) {
            const all = visible === group.models.length;
            actions.append(button(all ? 'Hide all' : 'Show all', () => setGroupVisibility(group, !all)));
        }
        header.append(trigger, actions);
        section.append(header);

        if (expanded) {
            const list = el('div', 'settings-list');
            list.dataset.variant = 'catalog';
            if (group.error) list.append(el('p', 'settings-models-group-error', group.error));
            models.forEach(model => {
                const row = el('div', 'settings-row');
                row.title = model.id;
                const copy = el('div', 'settings-row-copy');
                const line = el('div', 'settings-model-line');
                line.append(el('span', 'settings-row-title', model.name), ...modelTags(group, model));
                copy.append(line);
                row.append(copy,
                    modelSwitch(model.visible, `Show ${model.name}`, (checked, input) => toggleModel(model, checked, input)));
                list.append(row);
            });
            if (!group.error && !models.length) list.append(el('p', 'settings-provider-empty', 'This provider lists no models.'));
            section.append(list);
        }
        container.append(section);
    });
    status.hidden = shownGroups > 0;
    if (!shownGroups) {
        status.replaceChildren();
        if (query) {
            status.append(el('span', '', 'No models match'), el('strong', '', `“${$('models-search').value.trim()}”`));
        } else {
            status.append(el('span', '', 'No connected provider offers models yet.'));
            const link = el('a', 'settings-link-button', 'Connect a provider');
            link.href = '#providers';
            status.append(link);
        }
    }
}

async function saveVisibility(changes) {
    const data = await requestJson('/api/ai/models/visibility', jsonOptions('PATCH', { changes }));
    markModelsChanged();
    return data.visibility;
}

async function toggleModel(model, visible, input) {
    const previous = model.visible;
    model.visible = visible;
    try {
        await saveVisibility({ [model.key]: visible === model.default_visible ? null : visible });
        renderModels();
        } catch (error) {
        model.visible = previous;
        input.checked = previous;
        showToast(error.message, true);
    }
}

async function setGroupVisibility(group, visible) {
    const changes = {};
    group.models.forEach(model => { changes[model.key] = visible === model.default_visible ? null : visible; });
    try {
        await saveVisibility(changes);
        group.models.forEach(model => { model.visible = visible; });
        renderModels();
        } catch (error) {
        showToast(error.message, true);
    }
}

function shortModelName(model) {
    return String(model || '').split('/').pop();
}

function modelValue(ref) {
    return ref ? JSON.stringify({ host: ref.host, model: ref.model }) : '';
}

function findCatalogModel(value) {
    if (!value) return null;
    const ref = JSON.parse(value);
    for (const group of state.catalog?.groups || []) {
        if (group.host !== ref.host) continue;
        const model = group.models.find(item => item.id === ref.model);
        if (model) return { group, model };
    }
    return null;
}

function renderDefaultModelSelect() {
    const select = $('default-model');
    const current = modelValue(state.defaultModel);
    select.replaceChildren(new Option('Automatic (free OpenCode model)', ''));
    let found = !current;
    (state.catalog?.groups || []).forEach(group => {
        const models = group.models.filter(model => model.visible);
        if (!models.length) return;
        const optgroup = document.createElement('optgroup');
        optgroup.label = group.source ? `${group.label} · ${group.source}` : group.label;
        models.forEach(model => {
            const value = modelValue({ host: group.host, model: model.id });
            if (value === current) found = true;
            optgroup.append(new Option(model.vision === false ? `${model.name} (text only)` : model.name, value));
        });
        select.append(optgroup);
    });
    if (!found) {
        // A default the pickers no longer show still runs; keep it selectable.
        const name = shortModelName(state.defaultModel.model);
        select.append(new Option(state.catalog ? `${name} (hidden)` : name, current));
    }
    select.value = current;
    renderDefaultReasoning(state.defaultModel?.variant || null);
}

/** The reasoning levels of the chosen default model; hidden when it has none. */
function renderDefaultReasoning(variant) {
    const select = $('default-reasoning');
    const picked = findCatalogModel($('default-model').value);
    const choices = picked ? reasoningChoices(picked.group, picked.model) : [];
    select.replaceChildren(...choices.map(choice => new Option(choice.label, choice.value)));
    select.hidden = !choices.length;
    if (!choices.length) return;
    select.value = choices.some(choice => choice.value === (variant || ''))
        ? variant || ''
        : defaultReasoning(picked.group, choices);
}

async function saveDefaultModel() {
    const value = $('default-model').value;
    const reasoning = $('default-reasoning');
    const model = value ? { ...JSON.parse(value), variant: !reasoning.hidden && reasoning.value ? reasoning.value : null } : null;
    try {
        const data = await requestJson('/api/ai/default-model', jsonOptions('PUT', { model }));
        state.defaultModel = data.default_model;
        markModelsChanged();
        showToast(state.defaultModel ? 'Default model saved.' : 'The default model is chosen automatically.');
    } catch (error) {
        showToast(error.message, true);
        renderDefaultModelSelect();
    }
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */

function wireDialogs() {
    document.querySelectorAll('dialog.settings-dialog').forEach(dialog => {
        dialog.querySelectorAll('[data-close]').forEach(closeButton => {
            closeButton.addEventListener('click', () => dialog.close());
        });
        dialog.addEventListener('click', event => {
            if (event.target === dialog) dialog.close();
        });
    });
    $('connect-key-source').addEventListener('change', updateConnectKeyFields);
    $('connect-form').addEventListener('submit', submitConnect);
    $('custom-form').addEventListener('submit', submitCustom);
    $('custom-add-model').addEventListener('click', () => addCustomModelRow());
    $('all-providers-search').addEventListener('input', renderAllProviders);
    $('tool-recheck').addEventListener('click', probeTool);
    $('tool-dialog').addEventListener('close', () => { toolTarget = null; });
}

function init() {
    LEGACY_KEYS.forEach(key => { try { localStorage.removeItem(key); } catch { /* nothing kept */ } });
    $('show-all-providers').addEventListener('click', openAllProviders);
    $('models-refresh').addEventListener('click', () => loadModels(true));
    $('models-search').addEventListener('input', renderModels);
    $('default-model').addEventListener('change', () => {
        renderDefaultReasoning(null);
        saveDefaultModel();
    });
    $('default-reasoning').addEventListener('change', saveDefaultModel);
    wireDialogs();
    window.addEventListener('hashchange', () => {
        activatePane(currentPane());
        window.scrollTo({ top: 0, behavior: 'instant' });
    });
    activatePane(currentPane());
    return Promise.all([loadIconIds(), loadProviders()]).then(renderProviders);
}

await init();
