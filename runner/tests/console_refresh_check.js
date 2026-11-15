// Runs the console's real script (CONSOLE_JS from assets.py, on stdin) against a fake page and a fake clock:
// the run page's live part refreshes every 15 s; the server hangs (a fetch that never settles), then answers again.
// Exits 0 only if: the notice is hidden while answers come, shows within ~2 rounds of hanging, at most one request
// is in flight at a time (hung ones are aborted), and the next answer hides it and replaces the part.
const vm = require('vm');
let src = require('fs').readFileSync(0, 'utf8');
let now = 0, timers = [], nextId = 1;
const setT = (fn, ms, rep) => { const id = nextId++; timers.push({id, fn, at: now + ms, ms, rep}); return id; };
const clear = id => { timers = timers.filter(t => t.id !== id); };
const advance = ms => { const end = now + ms; for (;;) { timers.sort((a, b) => a.at - b.at); const t = timers[0];
  if (!t || t.at > end) break; now = t.at; if (t.rep) t.at += t.ms; else clear(t.id); t.fn(); } now = end; };
const flush = () => new Promise(r => setImmediate(r));
const el = (attrs = {}) => ({ attrs, hidden: false, textContent: '', innerHTML: '',
  getAttribute(n) { return this.attrs[n] ?? null; }, querySelector(sel) {
    if (sel === '[data-final]') return null;
    if (sel === '[data-reason]') { const m = /data-reason="([^"]+)"/.exec(this.innerHTML);
      return m ? { getAttribute: () => m[1], textContent: m[1] } : null; }
    return null; } });
const part = el({'data-refresh': '/qa/1/live', 'data-every': '15'});
part.innerHTML = '<p data-reason="working">old</p>';
const lost = el(); lost.hidden = true; const lostAt = el(); const announce = el();
const document = { hidden: false, addEventListener() {}, querySelectorAll: s => s === '[data-refresh]' ? [part] : [],
  querySelector: s => ({'[data-lost]': lost, '[data-lost-at]': lostAt, '[data-announce]': announce})[s] || null };
let mode = 'ok', inFlight = 0, maxInFlight = 0, sent = 0, aborted = 0;
const fetch = (url, opts) => { sent++; inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
  return new Promise((res, rej) => {
    const done = () => { inFlight--; };
    if (opts.signal) opts.signal.addEventListener('abort', () => { aborted++; done(); rej(new Error('abort')); });
    if (mode === 'ok') { done(); res({status: 200, ok: true, text: () => Promise.resolve('<p data-reason="new">fresh ' + sent + '</p>')}); }
  }); };
const RealDate = Date; class FakeDate extends RealDate { constructor(...a) { super(...(a.length ? a : [now])); } static now() { return now; } }
const ctx = { document, fetch, setInterval: (f, ms) => setT(f, ms, true), clearInterval: clear,
  setTimeout: (f, ms) => setT(f, ms, false), clearTimeout: clear, AbortController, Date: FakeDate,
  location: { reload() {} }, window: { AbortController, confirm: () => true } };
ctx.window.document = document;
vm.runInNewContext(src, ctx);
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
(async () => {
  for (let i = 0; i < 3; i++) { advance(15000); await flush(); await flush(); }
  if (!lost.hidden) fail('notice shown while answers come');
  if (!part.innerHTML.includes('fresh')) fail('part not replaced by answers');
  mode = 'hang';
  const hungAt = now;
  let shownAfter = null;
  for (let s = 0; s < 120; s++) { advance(1000); await flush(); await flush(); if (!lost.hidden && shownAfter === null) shownAfter = now - hungAt; }
  if (shownAfter === null) fail('notice never shown while requests hang');
  if (shownAfter > 31000) fail('notice shown only after ' + shownAfter + ' ms');
  if (maxInFlight > 1) fail('requests piled up: ' + maxInFlight + ' in flight at once');
  if (aborted < 3) fail('hung requests were not given up (' + aborted + ' aborted)');
  if (!/^\d\d:\d\d$/.test(lostAt.textContent)) fail('no time of the last answer: ' + lostAt.textContent);
  mode = 'ok';
  for (let s = 0; s < 31; s++) { advance(1000); await flush(); await flush(); }
  if (!lost.hidden) fail('notice still shown after the console answered again');
  if (!part.innerHTML.includes('fresh')) fail('part not refreshed after recovery');
  console.log(`ok: notice after ${shownAfter / 1000} s of hanging, ${sent} requests, max ${maxInFlight} in flight, ${aborted} aborted, hidden again after recovery`);
})();
