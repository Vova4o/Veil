import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../app/static/js/features/simple-mode-v2.js', import.meta.url), 'utf8');

function harness() {
    const elements = new Map();
    const saved = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            hidden: id !== 'agent-menu', innerHTML: '', textContent: '', dataset: {},
            classList: { add() {}, remove() {} }, setAttribute() {},
        });
        return elements.get(id);
    };
    const context = vm.createContext({
        document: { readyState: 'loading', addEventListener() {}, getElementById: element,
            createElement: () => ({ textContent: '', get innerHTML() { return this.textContent; } }) },
        localStorage: { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value),
            removeItem: key => saved.delete(key) },
    });
    vm.runInContext(source, context);
    return { run: code => vm.runInContext(code, context), elements, saved };
}

test('agent 502 errors show an agent message with optional diagnostics', () => {
    const h = harness();
    h.run(`handleGenerateError({ status: 502, message: 'Invalid JSON result.',
        data: { code: 'contract_violation', stage: 'agent', technical_error: 'Expected a string.' } });`);
    assert.equal(h.elements.get('error-title').textContent, 'The agent could not prepare the prompt');
    assert.equal(h.elements.get('error-text').textContent, 'Invalid JSON result.');
    assert.equal(h.elements.get('error-details').hidden, false);
    assert.equal(h.elements.get('error-technical').textContent, 'Expected a string.');
    h.run(`handleGenerateError({ status: 502, message: 'Timed out.', data: { code: 'cli_timeout', stage: 'agent' } });`);
    assert.equal(h.elements.get('error-title').textContent, 'The agent could not prepare the prompt');
    assert.equal(h.elements.get('error-details').hidden, true);
});

test('ComfyUI 502 errors keep the generation error message', () => {
    const h = harness();
    h.run(`handleGenerateError({ status: 502, message: 'Queue rejected.', data: { code: 'comfyui_rejected' } });`);
    assert.equal(h.elements.get('error-title').textContent, 'ComfyUI is unavailable or rejected the task');
});

test('model tags mark only text-only and free models', () => {
    const h = harness();
    assert.equal(h.run('agentTags({ vision: true })'), '');
    assert.equal(h.run('agentTags({ vision: null })'), '');
    assert.match(h.run('agentTags({ vision: false })'), /Text only/);
    assert.match(h.run('agentTags({ vision: true, free: true })'), /Free/);
    assert.doesNotMatch(h.run('agentTags({ vision: true, free: true })'), /Photos/);
});

const CODEX = { id: 'codex', host: 'codex', label: 'Codex', kind: 'tool', models: [
    { id: 'text', name: 'Text model', vision: false }, { id: 'unknown', name: 'Future', vision: null },
    { id: 'vision', name: 'Vision model', vision: true, variants: ['low', 'high'] }] };

test('photo modes dim text-only models and the picked model shows its reasoning levels', () => {
    const h = harness();
    h.run(`state.mode = 'reference'; state.agents = { groups: [${JSON.stringify(CODEX)},
        { id: 'api:acme', host: 'api:acme', label: 'Acme', kind: 'api', models: [{ id: 'm', name: 'Acme M', vision: null }] }] };
    state.agentChoice = { agentKind: 'codex', model: 'vision', name: 'Vision model', vision: true, variant: 'high' };
    paintAgentMenu();`);
    const menu = h.elements.get('agent-menu-body').innerHTML;
    assert.match(menu, /is-blocked[^>]+data-agent-model="text"/);
    // Unknown photo support is left to the provider instead of being blocked.
    assert.doesNotMatch(menu, /is-blocked[^>]+data-agent-model="unknown"/);
    assert.match(menu, /data-agent-variant="high"/);
    assert.match(menu, /Acme M/);
    assert.deepEqual(JSON.parse(h.run('JSON.stringify(agentRequestFields())')),
        { agent_host: 'codex', agent_model: 'vision', agent_variant: 'high' });
    h.run(`state.agentChoice = agentChoiceFor(state.agents.groups[1], state.agents.groups[1].models[0]);`);
    assert.deepEqual(JSON.parse(h.run('JSON.stringify(agentRequestFields())')),
        { agent_host: 'api:acme', agent_model: 'm', agent_variant: null });
    assert.equal(h.run('agentLabel(state.agentChoice)'), 'Acme · Acme M');
});

test('the reasoning chip shows the picked model levels from light to deep', () => {
    const h = harness();
    h.run(`state.agentChoice = { agentKind: 'antigravity', base: 'g', model: 'g-high', level: 'high', name: 'Gemini',
        levels: [{ id: 'g-high', level: 'high' }, { id: 'g-low', level: 'low' }, { id: 'g-medium', level: 'medium' }] };
        paintAgentReasoning();`);
    assert.equal(h.elements.get('agent-reasoning-wrap').hidden, false);
    assert.match(h.elements.get('agent-reasoning').innerHTML, /low.*medium.*high/s);
    assert.equal(h.elements.get('agent-reasoning').value, 'g-high');
    assert.equal(h.run('agentLabel(state.agentChoice)'), 'Antigravity · Gemini');
    h.run(`state.agentChoice = { agentKind: 'opencode', model: 'opencode/longcat', name: 'LongCat', variants: [] }; paintAgentReasoning();`);
    assert.equal(h.elements.get('agent-reasoning-wrap').hidden, true);
});

test('refresh updates a saved Antigravity level', async () => {
    const h = harness();
    h.run(`state.agentChoice = {agentKind:'antigravity',base:'future',model:'future-high',level:'high',vision:true};
        json = async () => ({ groups: [{ id: 'antigravity', host: 'antigravity', label: 'Antigravity', models: [
            { id: 'future', name: 'Future', vision: true, levels: [{ id: 'future-low', level: 'low' }] }] }] });`);
    await h.run('loadAgents(true)');
    const choice = JSON.parse(h.saved.get('cmv_create_agent'));
    assert.equal(choice.model, 'future-low');
    assert.equal(choice.level, 'low');
    assert.deepEqual(JSON.parse(h.run('JSON.stringify(agentRequestFields())')),
        { agent_host: 'antigravity', agent_model: 'future-low', agent_variant: null });
});

test('after a page restart cached models show before a slow discovery finishes', async () => {
    const h = harness();
    const payload = { groups: [{ id: 'codex', host: 'codex', label: 'Codex', models: [{ id: 'saved', name: 'Saved model', vision: true }] }] };
    h.saved.set('cmv_create_agent_catalog_v2', JSON.stringify({ version: 2, savedAt: Date.now(), payload }));
    h.run('restoreAgentCatalog(); json = () => new Promise(resolve => { globalThis.finishDiscovery = resolve; });');
    const pending = h.run('loadAgents()');
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Saved model/);
    assert.doesNotMatch(h.elements.get('agent-menu-body').innerHTML, /Asking the connected/);
    h.run('finishDiscovery({ groups: [{ id: "codex", host: "codex", label: "Codex", models: [{ id: "new", name: "New model", vision: true }] }] });');
    await pending;
    assert.match(h.elements.get('agent-menu-body').innerHTML, /New model/);
    assert.match(h.saved.get('cmv_create_agent_catalog_v2'), /New model/);
});

test('a catalog saved before Integrations changed the shown models is reloaded', () => {
    const h = harness();
    h.run('state.agentsSavedAt = Date.now();');
    assert.equal(h.run('agentCatalogStale()'), false);
    h.saved.set('cmv_ai_models_changed_at', String(Date.now() + 1000));
    assert.equal(h.run('agentCatalogStale()'), true);
});

test('a discovery failure preserves the last catalog', async () => {
    const h = harness();
    h.run('state.agents = { groups: [{ id: "claude", host: "claude", label: "Claude Code", models: [{ id: "sonnet", name: "Sonnet", vision: true }] }] }; json = async () => { throw new Error("Offline"); };');
    await h.run('loadAgents(true)');
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Sonnet/);
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Offline/);
});

test('one unavailable host keeps its saved cards while other hosts refresh', async () => {
    const h = harness();
    h.run(`state.agents = { groups: [{ id: 'codex', host: 'codex', label: 'Codex', models: [{ id: 'saved', name: 'Saved', vision: false }] }] };
        json = async () => ({ groups: [{ id: 'codex', host: 'codex', label: 'Codex', models: [], error: 'Codex offline' },
            { id: 'claude', host: 'claude', label: 'Claude Code', models: [{ id: 'new', name: 'New Claude', vision: true }] }] });`);
    await h.run('loadAgents(true)');
    const menu = h.elements.get('agent-menu-body').innerHTML;
    assert.match(menu, /Saved/);
    assert.match(menu, /Text only/);
    assert.match(menu, /Codex offline/);
    assert.match(menu, /New Claude/);
});
