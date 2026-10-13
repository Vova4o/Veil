// A model select fed by GET /api/ai/models?visible=1, with the model's reasoning level next to it.
// Used by the Viewer's AI tools and Library's AI rating; Integrations › Models uses reasoningChoices.

export const DEFAULT_MODEL_VALUE = 'default';

const REASONING_ORDER = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];

function byDepth(names) {
    const rank = name => {
        const index = REASONING_ORDER.indexOf(name);
        return index < 0 ? REASONING_ORDER.length : index;
    };
    return [...names].sort((a, b) => rank(a) - rank(b));
}

/** Reasoning levels a model offers: Antigravity levels, else its variants after a "Default" entry. */
export function reasoningChoices(group, model) {
    if (!group || !model) return [];
    if (group.host === 'antigravity') {
        return byDepth((model.levels || []).map(item => item.level)).map(level => ({ value: level, label: level }));
    }
    const variants = byDepth(model.variants || []);
    if (!variants.length) return [];
    const fallback = model.default_variant ? `Default (${model.default_variant})` : 'Default';
    return [{ value: '', label: fallback }, ...variants.map(variant => ({ value: variant, label: variant }))];
}

/** Antigravity runs its medium level when none is picked. */
export function defaultReasoning(group, choices) {
    if (group?.host !== 'antigravity' || !choices.length) return '';
    return (choices.find(choice => choice.value === 'medium') || choices[0]).value;
}

function modelValue(group, model) {
    return JSON.stringify({ host: group.host, model: model.id });
}

/** Text for HTML, attributes included: option values carry JSON with quotes. */
function escapeText(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

/**
 * Fill ``select`` with the shown models grouped by provider, a "Default model" entry first,
 * and keep a reasoning select beside it for the picked model.
 */
export function populateModelPicker(select, groups, { needsPhotos = false, defaultLabel = 'Default model' } = {}) {
    const previous = select.value;
    const shown = (groups || [])
        .map(group => ({ ...group, models: group.models.filter(model => !needsPhotos || model.vision !== false) }))
        .filter(group => group.models.length);
    const byValue = new Map();
    select.innerHTML = `<option value="${DEFAULT_MODEL_VALUE}">${escapeText(defaultLabel)}</option>`
        + shown.map(group => {
            const label = group.source ? `${group.label} · ${group.source}` : group.label;
            const options = group.models.map(model => {
                const value = modelValue(group, model);
                byValue.set(value, { group, model });
                return `<option value="${escapeText(value)}">${escapeText(model.name)}</option>`;
            }).join('');
            return `<optgroup label="${escapeText(label)}">${options}</optgroup>`;
        }).join('');
    select.value = byValue.has(previous) ? previous : DEFAULT_MODEL_VALUE;

    let reasoning = select.parentElement?.querySelector(`select[data-reasoning-for="${select.id}"]`);
    if (!reasoning) {
        const row = document.createElement('div');
        row.className = 'model-reasoning-row';
        select.replaceWith(row);
        reasoning = document.createElement('select');
        reasoning.dataset.reasoningFor = select.id;
        reasoning.setAttribute('aria-label', 'Reasoning');
        reasoning.title = 'Reasoning';
        row.append(select, reasoning);
        select.addEventListener('change', () => paintReasoning(select));
    }
    select.modelPicker = { byValue, reasoning };
    paintReasoning(select);
}

function paintReasoning(select) {
    const { byValue, reasoning } = select.modelPicker;
    const picked = byValue.get(select.value);
    const choices = picked ? reasoningChoices(picked.group, picked.model) : [];
    const previous = reasoning.value;
    reasoning.innerHTML = choices.map(choice => `<option value="${escapeText(choice.value)}">${escapeText(choice.label)}</option>`).join('');
    reasoning.hidden = !choices.length;
    reasoning.parentElement.classList.toggle('has-reasoning', choices.length > 0);
    if (choices.some(choice => choice.value === previous)) reasoning.value = previous;
    else if (choices.length) reasoning.value = defaultReasoning(picked.group, choices);
}

/** The ``agent`` field of an AI request; the default model needs none. */
export function pickedAgent(select) {
    if (!select.value || select.value === DEFAULT_MODEL_VALUE) return {};
    const ref = JSON.parse(select.value);
    const reasoning = select.modelPicker?.reasoning;
    return { agent: { ...ref, variant: reasoning && !reasoning.hidden && reasoning.value ? reasoning.value : null } };
}
