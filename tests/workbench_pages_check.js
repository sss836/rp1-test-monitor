/* Execute the real page router and operations code. No network or robot writes. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = path.resolve(__dirname, '../src/rp1_monitor/web');
const pagesSource = fs.readFileSync(path.join(web, 'pages.js'), 'utf8');
const appSource = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
const chooseSource = appSource.slice(appSource.indexOf('function chooseRobot('), appSource.indexOf('function render()'));
const names = ['fleet', 'overview', 'temperature', 'history', 'processes', 'storage', 'diagnostics'];

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.events = {}; this.attributes = {}; this.dataset = {};
    this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false; this.open = false;
    this.checked = false; this.classes = new Set();
    this.classList = {add: x => this.classes.add(x), remove: x => this.classes.delete(x)};
  }
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  fire(type, values = {}) {
    const event = {target: this, button: 0, prevented: false, preventDefault() { this.prevented = true; }, ...values};
    for (const fn of this.events[type] || []) fn(event);
    return event;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  add(child) { this.append(child); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  get options() { return this.children; }
  querySelectorAll(selector) {
    if (selector !== '.chat-message') return [];
    return this.children.filter(child => (child.className || '').split(' ').includes('chat-message'));
  }
  show() { this.open = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.focused = true; }
}

function robot(id) {
  return {robot_id: id, name: `模拟 ${id}`, source_kind: 'simulated', enabled: true, online: true,
    data_fresh: true, catalog: [], health: {motor_map: {dof: id === 'B' ? 28 : 24}}};
}
const robots = [robot('A'), robot('B')];

function harness(start = 'https://monitor.test/robots/B/temperature', prefix = '/') {
  const elements = {}, listeners = {}, documentListeners = {}, transitions = [], refreshes = [], switches = [];
  const get = id => elements[id] ||= new Element();
  const panels = names.map(name => { const panel = new Element(); panel.dataset.pagePanel = name; return panel; });
  const links = names.map(name => { const link = new Element('a'); link.dataset.page = name; return link; });
  const stack = [new URL(start)], window = {}, location = {};
  let cursor = 0, inlineOpens = 0, changed = 0;
  Object.defineProperties(location, {
    href: {get: () => stack[cursor].href}, pathname: {get: () => stack[cursor].pathname},
  });
  const emit = type => (listeners[type] || []).forEach(fn => fn());
  const history = {
    pushState(_state, _title, url) { stack.splice(cursor + 1); stack.push(new URL(url, location.href)); cursor++; },
    back() { if (cursor > 0) { cursor--; emit('popstate'); } },
    forward() { if (cursor + 1 < stack.length) { cursor++; emit('popstate'); } },
    get length() { return stack.length; },
  };
  const document = {baseURI: `https://monitor.test${prefix}`, title: '', getElementById: get,
    createElement: tag => new Element(tag),
    querySelectorAll: selector => selector === '[data-page-panel]' ? panels : selector === 'a[data-page]' ? links : [],
    querySelector: () => new Element(),
    addEventListener(type, fn) { (documentListeners[type] ||= []).push(fn); },
    dispatchEvent(event) { transitions.push(event.detail); (documentListeners[event.type] || []).forEach(fn => fn(event)); },
  };
  window.addEventListener = (type, fn) => (listeners[type] ||= []).push(fn);
  window.scrollTo = () => {};
  window.matchMedia = () => ({matches: false});
  window.rp1RefreshView = page => refreshes.push(page);
  window.rp1Operations = {openInline() { inlineOpens++; get('ops-dialog').show(); }, changed() { changed++; }};
  const context = {URL, console, document, window, location, history, selected: null, robots: [],
    Option: class Option extends Element { constructor(text, value) { super('option'); this.textContent = text; this.value = value; } },
    CustomEvent: class CustomEvent { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    catalogSignature: '', historyVersion: 0, curveBusy: false, curveData: null,
    byId: get, setText: (id, text) => { get(id).textContent = text; }, loadCurve() {},
    render() { window.rp1Pages.update(context.robots, context.selected); switches.push(context.selected); },
  };
  vm.createContext(context);
  vm.runInContext(pagesSource, context, {filename: 'pages.js'});
  context.selected = window.rp1Pages.robotId();
  vm.runInContext(chooseSource, context, {filename: 'app.js:chooseRobot'});
  const ready = (items = robots, selected = context.selected, unavailable = false) => {
    context.robots = items; context.selected = selected;
    window.rp1Pages.update(items, selected, {unavailable});
  };
  const visible = () => panels.filter(panel => !panel.hidden).map(panel => panel.dataset.pagePanel);
  return {context, window, get, panels, links, history, location, transitions, refreshes, switches, ready, visible,
    get inlineOpens() { return inlineOpens; }, get changed() { return changed; }};
}

function checkNavigation() {
  const h = harness();
  assert.equal(h.window.rp1Pages.robotId(), 'B');
  assert.equal(h.window.rp1Pages.currentPage(), 'temperature');
  assert.deepEqual(h.visible(), [], 'a direct device page waits for authorized API data');
  assert.equal(h.get('page-loading').hidden, false);
  h.ready();
  assert.deepEqual(h.visible(), ['temperature']);
  assert.equal(h.get('robot-switch').value, 'B');
  assert.equal(h.get('workspace-device-id').textContent, 'B');
  assert.equal(h.get('workspace-profile').textContent, '28 自由度');
  assert.match(h.get('workspace-source').textContent, /模拟数据/);
  assert.equal(h.links.find(link => link.dataset.page === 'temperature').getAttribute('aria-current'), 'page');
  h.get('thermal-dialog').showModal(); h.get('ops-model-dialog').showModal();
  const click = h.links.find(link => link.dataset.page === 'history').fire('click');
  assert.equal(click.prevented, true);
  assert.equal(h.location.pathname, '/robots/B/history');
  assert.deepEqual(h.visible(), ['history']);
  assert.equal(h.context.selected, 'B'); assert.equal(h.changed, 0);
  assert.equal(h.get('thermal-dialog').open, false); assert.equal(h.get('ops-model-dialog').open, false);
  assert.equal(h.get('page-title').focused, true);
  assert.equal(h.refreshes.at(-1), 'history');
  const before = h.history.length;
  h.links.find(link => link.dataset.page === 'overview').fire('click', {ctrlKey: true});
  assert.equal(h.history.length, before, 'modified links keep the normal browser new-tab behavior');
  h.get('robot-switch').value = 'A'; h.get('robot-switch').fire('change');
  assert.equal(h.location.pathname, '/robots/A/history');
  assert.equal(h.context.selected, 'A'); assert.equal(h.changed, 1);
  h.history.back();
  assert.equal(h.location.pathname, '/robots/B/history'); assert.equal(h.context.selected, 'B');
  assert.deepEqual(h.visible(), ['history']);
  h.history.back();
  assert.equal(h.location.pathname, '/robots/B/temperature'); assert.deepEqual(h.visible(), ['temperature']);
  h.history.forward(); h.history.forward();
  assert.equal(h.location.pathname, '/robots/A/history'); assert.equal(h.context.selected, 'A');
  for (const name of names) {
    h.window.rp1Pages.navigate(name, 'A');
    assert.deepEqual(h.visible(), [name], `only ${name} may be visible`);
  }
  h.window.rp1Pages.navigate('fleet');
  h.get('robot-switch').value = 'B'; h.get('robot-switch').fire('change');
  assert.equal(h.location.pathname, '/robots/B/overview', 'fleet device selection opens its own overview page');
  assert.equal(h.context.selected, 'B');
}

function checkUnknownAndOffline() {
  const unknown = harness('https://monitor.test/robots/MISSING/temperature');
  unknown.ready(robots, 'MISSING');
  assert.equal(unknown.window.rp1Pages.robotId(), 'MISSING');
  assert.deepEqual(unknown.visible(), []);
  assert.equal(unknown.get('page-not-found').hidden, false);
  assert.equal(unknown.get('page-loading').hidden, true);
  assert.equal(unknown.context.selected, 'MISSING', 'unknown deep links never silently select the first robot');
  assert.equal(unknown.location.pathname, '/robots/MISSING/temperature');
  assert.equal(unknown.window.rp1Pages.isActive('temperature'), false);
  const offline = harness('https://monitor.test/robots/MISSING/temperature');
  offline.ready([], 'MISSING', true);
  assert.deepEqual(offline.visible(), []);
  assert.equal(offline.get('page-loading').hidden, false);
  assert.equal(offline.get('page-not-found').hidden, true);
  offline.ready(robots, 'MISSING');
  assert.equal(offline.get('page-not-found').hidden, false);
  offline.ready(robots, 'MISSING', true);
  assert.equal(offline.get('page-loading').hidden, false);
  assert.match(offline.get('page-loading').textContent, /连接中断/);
  const malformed = harness('https://monitor.test/robots/%E0%A4%A/temperature');
  malformed.ready();
  assert.equal(malformed.window.rp1Pages.currentPage(), null);
  assert.deepEqual(malformed.visible(), []); assert.equal(malformed.get('page-not-found').hidden, false);
  const badView = harness('https://monitor.test/robots/A/made-up');
  badView.ready(); assert.equal(badView.window.rp1Pages.currentPage(), null); assert.deepEqual(badView.visible(), []);
}

function checkMountAndEncodedUrls() {
  const h = harness('https://monitor.test/monitor/robots/RP1%2D02/temperature', '/monitor/');
  h.ready([robot('RP1-02')], 'RP1-02');
  assert.equal(h.window.rp1Pages.robotId(), 'RP1-02'); assert.deepEqual(h.visible(), ['temperature']);
  h.window.rp1Pages.navigate('overview');
  assert.equal(h.location.pathname, '/monitor/robots/RP1-02/overview');
  assert.equal(h.window.rp1Pages.url('fleet'), 'https://monitor.test/monitor/robots');
  assert.equal(h.links.find(link => link.dataset.page === 'history').href, 'https://monitor.test/monitor/robots/RP1-02/history');
  const root = harness('https://monitor.test/monitor/', '/monitor/');
  root.ready(); assert.deepEqual(root.visible(), ['fleet']);
  const foreignPrefix = harness('https://monitor.test/other/robots/A/overview', '/monitor/');
  foreignPrefix.ready(); assert.equal(foreignPrefix.window.rp1Pages.currentPage(), null);
  const encoded = h.window.rp1Pages.url('temperature', 'unregistered/?value');
  assert.match(encoded, /unregistered%2F%3Fvalue\/temperature$/);
}

async function checkConversation() {
  const h = harness('https://monitor.test/robots/B/overview'), requests = [];
  h.ready();
  Object.assign(h.context, {Date, AbortSignal, token: '', endpoint: value => new URL(value, h.context.document.baseURI),
    fetch: async (url, options) => {
      const body = options.body ? JSON.parse(options.body) : undefined;
      requests.push({url: String(url), body});
      const value = String(url).endsWith('/assistant') ? {answer: '仅模拟观测回答', mode: 'local'} :
        String(url).endsWith('/capabilities') ? {profile: {}, items: []} :
        String(url).endsWith('/doctor') ? {checks: []} : {items: []};
      return {ok: true, json: async () => value};
    }});
  h.get('ops-minutes').value = '10';
  vm.runInContext(fs.readFileSync(path.join(web, 'operations.js'), 'utf8'), h.context, {filename: 'operations.js'});
  const settle = async () => { for (let index = 0; index < 4; index++) await new Promise(setImmediate); };
  h.window.rp1Pages.navigate('diagnostics', 'B'); await settle();
  assert.equal(h.get('ops-dialog').open, true);
  h.get('ops-question').value = '机器人 B 当前关节温度如何？';
  h.get('ops-compose').fire('submit'); await settle();
  assert.equal(h.get('ops-transcript').querySelectorAll('.chat-message').length, 2);
  const firstMessages = h.get('ops-transcript').children.slice();
  h.window.rp1Pages.navigate('temperature', 'B');
  assert.equal(h.get('ops-dialog').open, false);
  h.window.rp1Pages.navigate('diagnostics', 'B'); await settle();
  assert.deepEqual(h.get('ops-transcript').children, firstMessages, 'same-device tabs retain the actual transcript');
  h.get('ops-question').value = '与刚才的观测比较'; h.get('ops-compose').fire('submit'); await settle();
  const answers = requests.filter(request => request.url.endsWith('/assistant'));
  assert.equal(answers.length, 2);
  assert.equal(answers[1].body.history.length, 2, 'same-device conversation history reaches the next real request');
  assert.match(answers[1].body.history[0].content, /机器人 B/);
  h.window.rp1Pages.navigate('diagnostics', 'A'); await settle();
  assert.equal(h.context.selected, 'A');
  assert.equal(h.get('ops-transcript').querySelectorAll('.chat-message').length, 0, 'switching robots clears private device context');
  h.get('ops-question').value = '机器人 A 当前情况'; h.get('ops-compose').fire('submit'); await settle();
  const last = requests.filter(request => request.url.endsWith('/assistant')).at(-1);
  assert.match(last.url, /robot-monitors\/A\/assistant$/); assert.equal(last.body.history.length, 0);
}

(async () => {
  checkNavigation(); checkUnknownAndOffline(); checkMountAndEncodedUrls(); await checkConversation();
  process.stdout.write('PASS: robot workspace navigation and conversation isolation\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
