// Runs the console's real script (CONSOLE_JS from assets.py, on stdin) against a fake task page and a fake clock: the
// page's parts (data-refresh-parts) follow the server every 15 s. Exits 0 only if: an unchanged part is kept as it is
// and a changed one replaced; a part new to the page goes where the server puts it; at most one request is in flight,
// a hung one is given up and its late answer never lands over a newer one; the notice shows within ~2 rounds of
// hanging and hides on the next answer; a tab shown again asks at once; a 401 reloads the page; and data-final inside
// a changed part stops the asking, also when the tab is shown again later.
const vm = require('vm');
const src = require('fs').readFileSync(0, 'utf8');
let now = 0, timers = [], nextId = 1;
const setT = (fn, ms, rep) => { const id = nextId++; timers.push({id, fn, at: now + ms, ms, rep}); return id; };
const clear = id => { timers = timers.filter(t => t.id !== id); };
const advance = ms => { const end = now + ms; for (;;) { timers.sort((a, b) => a.at - b.at); const t = timers[0];
  if (!t || t.at > end) break; now = t.at; if (t.rep) t.at += t.ms; else clear(t.id); t.fn(); } now = end; };
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r)); };

// Just enough DOM for the parts: an answer is a row of <div data-part="name"[ data-final]>text</div>, no nesting.
const node = (name, html, final) => ({ name, innerHTML: html, final, parent: null,
  getAttribute(n) { return n === 'data-part' ? this.name : null; },
  replaceWith(other) { const k = this.parent.kids; k[k.indexOf(this)] = other; other.parent = this.parent; this.parent = null; },
  after(other) { const k = this.parent.kids; k.splice(k.indexOf(this) + 1, 0, other); other.parent = this.parent; } });
const box = () => ({ kids: [],
  querySelector(sel) { const m = /^\[data-part="(.+)"\]$/.exec(sel);
    if (m) return this.kids.find(k => k.name === m[1]) || null;
    if (sel === '[data-final]') return this.kids.find(k => k.final) || null;
    return null; },
  querySelectorAll(sel) { return sel === '[data-part]' ? this.kids.slice() : []; },
  prepend(n) { this.kids.unshift(n); n.parent = this; } });
const parse = html => { const b = box();
  for (const m of html.matchAll(/<div data-part="([^"]+)"( data-final)?>(.*?)<\/div>/g)) {
    const n = node(m[1], m[3], !!m[2]); n.parent = b; b.kids.push(n); }
  return b; };
const answer = parts => parts.map(([n, t, f]) => `<div data-part="${n}"${f ? ' data-final' : ''}>${t}</div>`).join('');

const root = parse(answer([['head', 'Working'], ['question', 'Which one?'], ['mrs', '']]));
root.getAttribute = n => ({'data-refresh-parts': '/tasks/AL-1/live', 'data-every': '15'})[n] ?? null;
const lost = { hidden: true }, lostAt = { textContent: '' };
const handlers = {};
const document = { hidden: false,
  addEventListener(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
  removeEventListener(ev, fn) { handlers[ev] = (handlers[ev] || []).filter(f => f !== fn); },
  querySelectorAll: s => s === '[data-refresh-parts]' ? [root] : [],
  querySelector: s => ({'[data-lost]': lost, '[data-lost-at]': lostAt})[s] || null,
  createElement: () => { const t = { content: box() };
    Object.defineProperty(t, 'innerHTML', { set(h) { t.content = parse(h); } }); return t; } };
const show = () => (handlers.visibilitychange || []).forEach(fn => fn());

// The server: each request gets the answer `next` holds when it is sent; `mode` says how it answers.
let mode = 'ok', next = '', inFlight = 0, maxInFlight = 0, sent = 0, aborted = 0, reloads = 0, late = [];
const fetch = (url, opts) => { sent++; inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
  const body = next;
  return new Promise((res, rej) => {
    let settled = false;
    const reply = r => { if (settled) return; settled = true; inFlight--; res(r); };
    if (opts.signal) opts.signal.addEventListener('abort', () => { if (settled) return; settled = true; inFlight--; aborted++; rej(new Error('abort')); });
    if (mode === 'ok') reply({status: 200, ok: true, text: () => Promise.resolve(body)});
    else if (mode === '401') reply({status: 401, ok: false, text: () => Promise.resolve('')});
    else if (mode === 'slow') late.push(() => reply({status: 200, ok: true, text: () => Promise.resolve(body)}));
  }); };
const RealDate = Date; class FakeDate extends RealDate { constructor(...a) { super(...(a.length ? a : [now])); } static now() { return now; } }
const ctx = { document, fetch, setInterval: (f, ms) => setT(f, ms, true), clearInterval: clear,
  setTimeout: (f, ms) => setT(f, ms, false), clearTimeout: clear, AbortController, Date: FakeDate,
  location: { reload() { reloads++; } }, window: { AbortController, confirm: () => true } };
ctx.window.document = document;
vm.runInNewContext(src, ctx);
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const names = () => root.kids.map(k => k.name).join(',');
const text = n => root.querySelector(`[data-part="${n}"]`).innerHTML;

(async () => {
  // An answer where only the head changed: the question being read stays the very same node.
  const question = root.querySelector('[data-part="question"]');
  next = answer([['head', 'Still working'], ['question', 'Which one?'], ['mrs', '']]);
  advance(15000); await flush();
  if (text('head') !== 'Still working') fail('a changed part was not replaced');
  if (root.querySelector('[data-part="question"]') !== question) fail('an unchanged part was replaced');

  // A part new to the page goes where the server puts it, not at the end.
  next = answer([['head', 'Still working'], ['hold', 'On hold'], ['question', 'Which one?'], ['mrs', '']]);
  advance(15000); await flush();
  if (names() !== 'head,hold,question,mrs') fail('a new part was put out of order: ' + names());

  // A slow answer: the request is given up after a round, no second one starts meanwhile, and its late answer (the
  // question still waiting) never lands over the newer one (the question answered).
  mode = 'slow';
  next = answer([['head', 'Asking'], ['hold', ''], ['question', 'STALE: still waiting'], ['mrs', '']]);
  advance(15000); await flush();
  const before = sent;
  advance(5000); await flush();
  if (sent !== before) fail('a second request was sent while one was in flight');
  advance(10000); await flush();
  if (aborted < 1) fail('a request that did not answer within a round was not given up');
  mode = 'ok';
  next = answer([['head', 'Working again'], ['hold', ''], ['question', ''], ['mrs', '']]);
  advance(15000); await flush();
  late.forEach(f => f()); late = []; await flush();
  if (text('question') !== '') fail('an older answer landed over a newer one: ' + text('question'));
  if (maxInFlight > 1) fail('requests piled up: ' + maxInFlight + ' in flight at once');

  // A server that hangs: the notice within two rounds, with the time of the last answer; an answer hides it.
  mode = 'hang';
  const hungAt = now; let shownAfter = null;
  for (let s = 0; s < 60; s++) { advance(1000); await flush(); if (!lost.hidden && shownAfter === null) shownAfter = now - hungAt; }
  if (shownAfter === null) fail('notice never shown while requests hang');
  if (shownAfter > 31000) fail('notice shown only after ' + shownAfter + ' ms');
  if (!/^\d\d:\d\d$/.test(lostAt.textContent)) fail('no time of the last answer: ' + lostAt.textContent);
  if (maxInFlight > 1) fail('hanging requests piled up: ' + maxInFlight);
  mode = 'ok';
  for (let s = 0; s < 31; s++) { advance(1000); await flush(); }
  if (!lost.hidden) fail('notice still shown after the console answered again');

  // A hidden tab asks nothing; shown again, it asks at once.
  document.hidden = true;
  let quiet = sent;
  advance(10 * 60000); await flush();
  if (sent !== quiet) fail('a hidden tab kept asking');
  document.hidden = false; show(); await flush();
  if (sent === quiet) fail('no request at once when the tab came back');

  // Signed out: the page reloads, to the sign-in.
  mode = '401';
  advance(15000); await flush();
  if (reloads !== 1) fail('a 401 did not reload the page');
  mode = 'ok';

  // data-final inside a part that changed stops the asking for good, also after the tab is shown again.
  next = answer([['head', 'Done', true], ['hold', ''], ['question', ''], ['mrs', 'MR !4']]);
  advance(15000); await flush();
  if (text('head') !== 'Done') fail('the last answer was not put on the page');
  quiet = sent;
  advance(5 * 60000); await flush();
  if (sent !== quiet) fail('kept asking after data-final in a changed part (' + (sent - quiet) + ' more requests)');
  document.hidden = true; document.hidden = false; show(); await flush();
  if (sent !== quiet) fail('asked again when the tab came back after the last answer');
  if (!lost.hidden) fail('notice shown after the last answer');

  console.log(`ok: ${sent} requests, max ${maxInFlight} in flight, ${aborted} aborted, the late answer dropped, stops on data-final`);
})();
