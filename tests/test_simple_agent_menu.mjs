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

test('model tags preserve unknown capabilities and identify family inference', () => {
    const h = harness();
    assert.match(h.run('agentTags(null, false)'), /Photos unknown/);
    assert.doesNotMatch(h.run('agentTags(null, false)'), /Text only/);
    assert.match(h.run('agentTags(false, false, "family")'), /Text only/);
    assert.match(h.run('agentTags(true, false, "family")'), /Photos\*/);
});

test('native catalogs show text-only and unknown models dimmed in photo modes', () => {
    const h = harness();
    h.run(`state.mode = 'reference'; state.agents = {
        codex: {label: 'Codex', models: [
            {id:'text', name:'Text model', vision:false}, {id:'unknown', name:'Future', vision:null},
            {id:'vision', name:'Vision model', vision:true, variants:['low','high']}]},
        claude: {label:'Claude Code', models:[{id:'sonnet',name:'Sonnet',vision:true,vision_source:'family'}]}
    }; state.agentChoice = {agentKind:'codex',model:'vision',name:'Vision model',vision:true,variant:'high'};
    paintAgentMenu();`);
    const menu = h.elements.get('agent-menu-body').innerHTML;
    assert.match(menu, /is-blocked[^>]+data-native-model="text"/);
    assert.match(menu, /is-blocked[^>]+data-native-model="unknown"/);
    assert.match(menu, /data-native-variant="high"/);
    assert.match(menu, /Photos\* inferred from model family/);
    assert.deepEqual(JSON.parse(h.run('JSON.stringify(agentRequestFields())')),
        {agent_host:'codex',agent_model:'vision',agent_variant:'high'});
});

test('refresh updates a saved Antigravity level and keeps unknown vision distinct', async () => {
    const h = harness();
    h.run(`state.agentChoice = {agentKind:'antigravity',base:'future',model:'future-high',level:'high',vision:true};
        json = async () => ({antigravity:{models:[{id:'future',name:'Future',vision:null,
            levels:[{id:'future-low',level:'low'}]}]}});`);
    await h.run('loadAgents(true)');
    const choice = JSON.parse(h.saved.get('cmv_create_agent'));
    assert.equal(choice.model, 'future-low');
    assert.equal(choice.vision, null);
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Photos unknown/);
});

test('after a page restart cached models show before a slow discovery finishes', async () => {
    const h = harness();
    const payload = { codex: { label: 'Codex', models: [{ id: 'saved', name: 'Saved model', vision: null }] }, agents: [] };
    h.saved.set('cmv_create_agent_catalog_v1', JSON.stringify({ version: 1, savedAt: Date.now(), payload }));
    h.run('restoreAgentCatalog(); json = () => new Promise(resolve => { globalThis.finishDiscovery = resolve; });');
    const pending = h.run('loadAgents()');
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Saved model/);
    assert.doesNotMatch(h.elements.get('agent-menu-body').innerHTML, /Asking the installed/);
    h.run('finishDiscovery({ codex: { label: "Codex", models: [{ id: "new", name: "New model", vision: true }] }, agents: [] });');
    await pending;
    assert.match(h.elements.get('agent-menu-body').innerHTML, /New model/);
    assert.match(h.saved.get('cmv_create_agent_catalog_v1'), /New model/);
});

test('a discovery failure preserves the last catalog and its capabilities', async () => {
    const h = harness();
    h.run('state.agents = { claude: { label: "Claude Code", models: [{ id: "sonnet", name: "Sonnet", vision: null }] }, agents: [] }; json = async () => { throw new Error("Offline"); };');
    await h.run('loadAgents(true)');
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Sonnet/);
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Photos unknown/);
    assert.match(h.elements.get('agent-menu-body').innerHTML, /Offline/);
});

test('one unavailable host keeps its saved cards while other hosts refresh', async () => {
    const h = harness();
    h.run(`state.agents = { codex: { label: 'Codex', models: [{ id: 'saved', name: 'Saved', vision: null }] } };
        json = async () => ({ codex: { label: 'Codex', models: [], error: 'Codex offline' },
            claude: { label: 'Claude Code', models: [{ id: 'new', name: 'New Claude', vision: true }] }, agents: [] });`);
    await h.run('loadAgents(true)');
    const menu = h.elements.get('agent-menu-body').innerHTML;
    assert.match(menu, /Saved/);
    assert.match(menu, /Photos unknown/);
    assert.match(menu, /Codex offline/);
    assert.match(menu, /New Claude/);
});
