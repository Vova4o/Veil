// Integrations › Model sources: the Civitai API key for model downloads here and on Colab.
import { customConfirm } from './utils.js';

const PANE = 'models';
const CIVITAI_KEYS_URL = 'https://civitai.com/user/account';

let civitai = null;
let account = null; // { username } after a check
let checkError = null;
let busy = false;

function $(id) {
    return document.getElementById(id);
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.error) {
        const error = new Error(data.error || `${response.status} ${response.statusText}`);
        error.code = data.code;
        throw error;
    }
    return data;
}

function toast(message, isError = false) {
    const node = $('toast');
    if (!node) return;
    node.textContent = message;
    node.classList.toggle('toast-error', isError);
    node.classList.add('show');
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => node.classList.remove('show'), 3600);
}

function button(label, onClick, className = 'btn btn-secondary') {
    const node = el('button', className, label);
    node.type = 'button';
    node.addEventListener('click', onClick);
    return node;
}

/* ---------- data ---------- */

async function load() {
    try {
        civitai = (await requestJson('/api/model-sources')).civitai;
    } catch (error) {
        toast(error.message, true);
    }
    render();
}

async function run(action) {
    busy = true;
    render();
    try {
        await action();
    } finally {
        busy = false;
        render();
    }
}

function save(input) {
    const token = input.value.trim();
    if (!token) {
        input.focus();
        return;
    }
    run(async () => {
        try {
            const data = await requestJson('/api/model-sources/civitai', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token }),
            });
            civitai = data.civitai;
            account = data.account;
            checkError = data.warning;
            toast(data.account ? `Saved. Civitai knows this key as ${data.account.username}.` : 'Saved.');
        } catch (error) {
            checkError = error.message;
        }
    });
}

function check() {
    run(async () => {
        try {
            account = (await requestJson('/api/model-sources/civitai/check', { method: 'POST' })).account;
            checkError = null;
        } catch (error) {
            account = null;
            checkError = error.message;
        }
    });
}

async function remove() {
    const confirmed = await customConfirm(
        'Remove the Civitai API key?',
        'Models that Civitai serves only to signed-in users will stop downloading, here and on Colab.',
    );
    if (!confirmed) return;
    run(async () => {
        try {
            civitai = (await requestJson('/api/model-sources/civitai', { method: 'DELETE' })).civitai;
            account = null;
            checkError = null;
            toast('Civitai API key removed.');
        } catch (error) {
            toast(error.message, true);
        }
    });
}

/* ---------- card ---------- */

function storageText() {
    if (civitai.storage === 'system') return 'Kept in the system credential store';
    if (civitai.storage === 'config') return 'Kept in CMV’s settings file: no system credential store is available';
    return null;
}

function civitaiCard() {
    const saved = Boolean(civitai?.saved);
    const node = el('article', 'cli-card remote-card');
    const top = el('div', 'cli-topline');
    const heading = el('div');
    heading.append(el('h3', '', 'Civitai'), el('p', 'cli-path', 'API key for models and LoRAs from civitai.com'));
    top.append(heading, el('span', `cli-status ${saved ? 'available' : 'neutral'}`, saved ? 'Saved' : 'Not set'));
    node.append(top);

    if (!civitai) {
        node.append(el('p', 'cli-message', 'Checking…'));
        return node;
    }
    node.append(el('p', 'cli-message', saved
        ? 'Used for Civitai downloads on this computer and for Colab runtimes. For Colab, CMV turns each Civitai link into a short-lived download link here, so the key never leaves this computer.'
        : 'Some models, such as Moody Krea2 in Create, download from Civitai only with your API key. Create one under API Keys in your Civitai account settings and paste it here.'));

    const badges = el('div', 'badge-row');
    [storageText(), account?.username && `Civitai account: ${account.username}`]
        .filter(Boolean)
        .forEach(text => badges.append(el('span', 'profile-badge', text)));
    if (badges.childElementCount) node.append(badges);

    const form = el('div', 'source-token');
    const row = el('div', 'remote-login-row');
    const input = el('input', 'remote-login-code source-token-input');
    input.type = 'password';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.placeholder = saved ? 'Paste a new key to replace the saved one' : 'Civitai API key';
    input.setAttribute('aria-label', 'Civitai API key');
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter') save(input);
    });
    const saveButton = button(busy ? 'Checking…' : (saved ? 'Replace' : 'Save'), () => save(input), 'btn btn-primary');
    saveButton.disabled = busy;
    row.append(input, saveButton);
    form.append(row);
    const link = el('a', 'remote-login-link', 'Open Civitai account settings');
    link.href = CIVITAI_KEYS_URL;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    form.append(link);
    node.append(form);

    if (checkError) node.append(el('p', 'card-test-result error', checkError));

    if (saved) {
        const row = el('div', 'card-actions');
        const checkButton = button('Check', check);
        const removeButton = button('Remove', remove);
        checkButton.disabled = busy;
        removeButton.disabled = busy;
        row.append(checkButton, removeButton);
        node.append(row);
    }
    return node;
}

function render() {
    const grid = $('models-grid');
    if (!grid) return;
    grid.replaceChildren(civitaiCard());
}

function onPane() {
    if (window.location.hash.replace('#', '') === PANE && !civitai) load();
}

render();
window.addEventListener('hashchange', onPane);
onPane();
