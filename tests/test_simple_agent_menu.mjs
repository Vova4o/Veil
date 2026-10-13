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
