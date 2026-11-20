// #195 review — behavioral test for the two chat.html fixes that greps can't prove:
//   BUG-1: composer htmx:afterRequest must NOT run the success path (composer.reset →
//          draft wipe) on a network failure (xhr.status === 0 / detail.successful === false).
//   BUG-2: a stale #messages swap for a dialog the user already left must be dropped
//          (htmx:beforeSwap shouldSwap=false when the request path's dialog != #dialog_id),
//          and afterSettle must classify search-vs-history from the request path (per-response),
//          not a global flag.
//
// The inline chat.html JS has no browser harness in this repo, so this test builds a minimal
// DOM/event mock, evaluates the page's <script> in that sandbox, dispatches the real handlers
// the page registered, and asserts on observable state. Run by tests/test_web_js.py via node.
//
// It is intentionally faithful: it evaluates the ACTUAL script text from the template, so a
// regression in the handler logic fails here.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const chatPath = path.resolve(__dirname, '../../src/tg_messenger/web/templates/chat.html');
const html = fs.readFileSync(chatPath, 'utf8');

// Pull the big inline <script> (the one wiring the composer + dialog list).
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const pageScript = scripts.find((s) => s.includes('openDialog') && s.includes('webClientId'));
if (!pageScript) throw new Error('could not locate the page script');

let failures = 0;
function check(name, cond) {
  if (cond) { console.log('ok   ' + name); }
  else { console.log('FAIL ' + name); failures++; }
}

// ---- Minimal DOM mock ------------------------------------------------------
class El {
  constructor(id) {
    this.id = id;
    this.value = '';
    this._textContent = '';
    this.innerHTML = '';
    this.style = {};
    this.dataset = {};
    this.disabled = false;
    this.defaultValue = '';
    this.attrs = {};
    this._listeners = {};
    this.children = [];
  }
  // DOM-faithful: assigning textContent replaces the children (renderNotifList relies on
  // `list.textContent = ''` wiping previous rows between renders).
  get textContent() { return this._textContent; }
  set textContent(v) { this._textContent = v; if (v === '') this.children.length = 0; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  dispatch(type, detail) {
    const evt = { type, detail, target: this, preventDefault() { this.defaultPrevented = true; }, defaultPrevented: false };
    for (const fn of (this._listeners[type] || [])) fn(evt);
    return evt;
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  appendChild(c) { this.children.push(c); return c; }
  insertAdjacentElement() {}
  insertAdjacentHTML() {}
  remove() {}
  reset() {
    // like a real form reset: clears text-ish fields and the reply_to hidden field
    this._resetCount = (this._resetCount || 0) + 1;
    ids.get('composer-text').value = '';
    ids.get('reply_to').value = '';
  }
  focus() {}
  cloneNode() { return new El(this.id); }
  classList = { add() {}, remove() {}, toggle() {} };
}

const ids = new Map();
function el(id) { if (!ids.has(id)) ids.set(id, new El(id)); return ids.get(id); }
// Pre-create the elements the script queries by id at load time.
[
  'web_client_id', 'dialog_id', 'outbound_nonce', 'reply_to', 'composer-text', 'composer',
  'suggest-btn', 'attach-file', 'suggest-error', 'composer-error', 'messages', 'messages-empty',
  'reaction-toast', 'variant-panel', 'reply-banner', 'reply-banner-text', 'reply-cancel',
  'tabs', 'dialogs', 'dialog-search', 'current-tab', 'outbound-indicator', 'message-search',
  'message-search-input', 'suggest-settings-btn', 'suggest-settings-panel', 'active-profile',
  'sse-status', 'logout-link', 'lang-settings',
].forEach(el);

const composer = el('composer');
// Bubbles currently in #messages. Each: { dataset: { id, dialog } }. Tests set this to model a
// mixed DOM during an in-flight dialog switch (bubbles of the previous dialog still present).
let messageBubbles = [];
const document_ = {
  getElementById: (id) => (ids.has(id) ? ids.get(id) : el(id)),
  // Resolve the sidebar-row selector the unread-badge handler uses (#323); everything
  // else stays null, as before.
  querySelector: (sel) => {
    const m = /^#dialogs li\[data-dialog="([^"]*)"\]$/.exec(sel || '');
    if (m) return dialogLis.get(m[1]) || null;
    return null;
  },
  querySelectorAll: (sel) => {
    if (!sel) return [];
    // The tab-total recount's row-badge scan (#323 slice 3): '#dialogs li .unread'
    const lm = /^#dialogs li \.unread$/.exec(sel);
    if (lm) {
      const out = [];
      for (const li of dialogLis.values()) {
        const b = typeof li.querySelector === 'function' ? li.querySelector('.unread') : null;
        if (b) out.push(b);
      }
      return out;
    }
    // The deletion handler's per-id bubble selector (#323 slice 2):
    // '.msg[data-dialog="D"][data-id="M"]'
    const dm = /^\.msg\[data-dialog="([^"]*)"\]\[data-id="([^"]*)"\]$/.exec(sel);
    if (dm) {
      return messageBubbles.filter(
        (b) => String(b.dataset.dialog) === dm[1] && String(b.dataset.id) === dm[2]);
    }
    if (!sel.startsWith('#messages .msg[data-id]')) return [];
    // markDialogRead scopes to a specific dialog: '#messages .msg[data-id][data-dialog="<id>"]'
    const m = /\[data-dialog="([^"]*)"\]/.exec(sel);
    if (m) return messageBubbles.filter((b) => String(b.dataset.dialog) === m[1]);
    return messageBubbles;  // unscoped (old form) — return all
  },
  body: el('__body'),
  createElement: () => new El('__created'),
  addEventListener: () => {},
};
// composer.querySelector('button[type=submit]') is used — return a stub button.
composer.querySelector = () => ({ disabled: false });

// Record every fetch (so we can assert which dialogs got a /read POST).
const fetchCalls = [];
// Payload served for /api/unread-totals (#323 slice 3) — the test rewrites it.
let totalsResponse = { dm: 4, groups: 2 };
let totalsFetchCount = 0;  // survives the fetchCalls.length = 0 resets other sections do
// Every EventSource the page opened (unread stream #323, per-dialog streams).
const eventSources = [];
// Fake sidebar rows for the unread-badge handler: dialog id (string) -> li mock.
const dialogLis = new Map();
class FakeFormData { constructor() { this._d = {}; } append(k, v) { this._d[k] = v; } get(k) { return this._d[k]; } }

const sandbox = {
  document: document_,
  window: {},
  sessionStorage: { getItem: () => 'cid-test', setItem: () => {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  crypto: { randomUUID: () => 'cid-test' },
  console,
  EventSource: class {
    constructor(url) { this.url = url; this.onmessage = null; eventSources.push(this); }
    close() {}
  },
  setTimeout: (fn) => { return 0; },  // don't actually schedule debounced work
  clearTimeout: () => {},
  fetch: (url, opts) => {
    fetchCalls.push({ url, body: opts && opts.body });
    if (String(url).includes('/api/unread-totals')) {
      totalsFetchCount++;
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve(totalsResponse),
        text: () => Promise.resolve(''),
      });
    }
    return Promise.resolve({ ok: true, status: 204, text: () => Promise.resolve('') });
  },
  FormData: FakeFormData,
  CSS: { escape: (s) => String(s) },
  DOMParser: class { parseFromString() { return { body: { textContent: '' } }; } },
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  Map, Set, JSON, RegExp, encodeURIComponent, parseInt, setInterval: () => 0, clearInterval: () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(pageScript, sandbox, { filename: 'chat.html:inline' });

// ---- BUG-1: network failure must NOT wipe the draft --------------------------
{
  const composerText = el('composer-text');
  const replyTo = el('reply_to');
  const dialogId = el('dialog_id');
  composerText.value = 'my precious draft';
  replyTo.value = '555';
  dialogId.value = '7';
  const beforeReset = composer._resetCount || 0;

  // htmx fires afterRequest with successful=false on a network drop (xhr.status === 0).
  composer.dispatch('htmx:afterRequest', { successful: false, xhr: { status: 0 } });

  check('BUG-1: draft preserved on network failure', composerText.value === 'my precious draft');
  check('BUG-1: reply target preserved on network failure', replyTo.value === '555');
  check('BUG-1: form was NOT reset on network failure', (composer._resetCount || 0) === beforeReset);

  // sanity: a real success (successful=true) DOES run the success path. Drive activeDialogId to
  // the submitted dialog via htmx:confirm (which sets activeDialogId = #dialog_id) so the
  // reset branch runs. The confirm handler early-returns for an empty/whitespace draft, so give
  // it a real draft; outboundReady short-circuits the network prep and just marks-submitting.
  dialogId.value = '7';
  composerText.value = 'sent text';
  composer.dataset.outboundReady = '1';
  // seed compose state so state.outboundReady/draft line up with the short-circuit guard
  composer.dispatch('htmx:confirm', { detail: { issueRequest() {} }, preventDefault() {} });
  // #300: realistic parameters — htmx serializes the form (text included) when issuing the
  // POST, so the completion carries what was actually sent.
  composer.dispatch('htmx:afterRequest', {
    successful: true, xhr: { status: 200 },
    requestConfig: { parameters: { dialog_id: '7', text: 'sent text', reply_to: '' } },
  });
  check('BUG-1: success path still runs on a 2xx (draft cleared)', composerText.value === '');

  // a network failure surfaces an error into the composer-error slot
  el('composer-error').textContent = '';
  composer.dispatch('htmx:sendError', {});
  check('BUG-1: network failure shows a composer error', el('composer-error').textContent.length > 0);
}

// ---- BUG-2: stale search/history swap for a left dialog is dropped -----------
{
  const messages = el('messages');
  const dialogId = el('dialog_id');

  // The user is now viewing dialog B (=8). A slow response for dialog A (=7) arrives.
  dialogId.value = '8';
  const staleEvt = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/dialogs/7/search?q=hi' },
  });
  check('BUG-2: stale search response for a left dialog is dropped', staleEvt.detail.shouldSwap === false);

  // A response for the CURRENT dialog B is allowed to swap.
  const freshEvt = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/dialogs/8/messages' },
  });
  check('BUG-2: current-dialog response is allowed to swap', freshEvt.detail.shouldSwap === true);

  // afterSettle classifies search vs history from the request path (per-response):
  // a search swap must NOT scroll-to-end or clear the unread badge.
  let scrolledToEnd = false;
  Object.defineProperty(messages, 'scrollHeight', { get: () => 1000, configurable: true });
  messages.scrollTop = 0;
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/8/search?q=hi' } });
  check('BUG-2: a search swap does not scroll to the end', messages.scrollTop === 0);

  // a history swap DOES scroll to the end
  messages.scrollTop = 0;
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/8/messages' } });
  check('BUG-2: a history swap scrolls to the end', messages.scrollTop === 1000);

  // negative dialog ids (groups/channels) are handled by the guard
  dialogId.value = '-100200';
  const negFresh = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true, pathInfo: { requestPath: '/dialogs/-100200/messages' },
  });
  check('BUG-2: negative dialog id matches (group/channel)', negFresh.detail.shouldSwap === true);
  const negStale = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true, pathInfo: { requestPath: '/dialogs/7/messages' },
  });
  check('BUG-2: negative-current, other-dialog response dropped', negStale.detail.shouldSwap === false);
}

// ---- #203/#209: a stale /send echo for a left dialog is dropped (behavioral, not grep) --------
// The /send path carries no dialogId, so the history/search guard above never covered it; the
// send branch scopes the swap by the dialog THIS request was sent to — htmx snapshots the form
// into evt.detail.requestConfig.parameters at POST time, so the guard is per-request (#209), not
// a shared composer.dataset stamp. This drives the real handler so a regression where the send
// branch stopped setting shouldSwap=false (or read the wrong field) would fail here.
{
  const messages = el('messages');
  const dialogId = el('dialog_id');

  // User sent in A (=7), then switched to B (=8) before A's /send POST returned.
  dialogId.value = '8';
  const staleSend = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
    requestConfig: { parameters: { dialog_id: '7' } },
  });
  check("#203: stale /send echo for the left dialog is dropped", staleSend.detail.shouldSwap === false);

  // The normal case: send in the dialog the user is still viewing → the echo swaps in.
  const liveSend = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
    requestConfig: { parameters: { dialog_id: '8' } },
  });
  check("#203: same-dialog /send echo is allowed to swap", liveSend.detail.shouldSwap === true);

  // A /send-shaped swap without request parameters (defensive) must not be dropped.
  const noParams = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
  });
  check("#203: /send with no request parameters is not dropped", noParams.detail.shouldSwap === true);
}

// ---- #209: two CONCURRENT in-flight sends across dialogs must not cross-post ------------------
// Scenario: send in A (=7) → switch to B (=8) → submit B while A's POST is still in flight →
// A's echo returns last. The old guard read ONE shared composer.dataset.submittingDialogId that
// B's submit overwrote to '8', so A's late echo saw submitted==open dialog and appended A's
// bubble into B's pane; A's afterRequest then consumed the same overwritten slot and reset B's
// composer. The fix keys both handlers off the request's OWN parameters snapshot.
{
  const messages = el('messages');
  const dialogId = el('dialog_id');
  const composerText = el('composer-text');

  // Send in A: htmx:confirm sets activeDialogId and records A's draft in its compose state.
  dialogId.value = '7';
  composerText.value = 'draft A';
  composer.dispatch('htmx:confirm', { issueRequest() {} });
  // Switch to B and submit there while A's POST is still in flight.
  dialogId.value = '8';
  composerText.value = 'draft B';
  composer.dispatch('htmx:confirm', { issueRequest() {} });
  // Model the legacy shared-stamp contamination (#209): after B's submit the old code held
  // submittingDialogId='8'. The fixed guard must ignore it and use A's own request parameters.
  composer.dataset.submittingDialogId = '8';

  // A's late echo (its request carried dialog_id=7) must NOT swap into B's open pane.
  const echoA = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
    requestConfig: { parameters: { dialog_id: '7', reply_to: '' } },
  });
  check('#209: concurrent-send echo from A is NOT swapped into B', echoA.detail.shouldSwap === false);

  // B's own echo still swaps.
  const echoB = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
    requestConfig: { parameters: { dialog_id: '8', reply_to: '' } },
  });
  check('#209: concurrent-send echo from B still swaps', echoB.detail.shouldSwap === true);

  // A's completion (afterRequest) must clean up FOR A — not reset B's composer mid-typing.
  const resetsBefore = composer._resetCount || 0;
  composer.dispatch('htmx:afterRequest', {
    successful: true,
    xhr: { status: 200 },
    requestConfig: { parameters: { dialog_id: '7', text: 'draft A', reply_to: '' } },
  });
  check("#209: A's completion does not reset the composer while B is open",
        (composer._resetCount || 0) === resetsBefore);
  check("#209: B's draft survives A's completion", composerText.value === 'draft B');
  check("#209: the form still targets B after A's completion", dialogId.value === '8');

  // B's completion runs the normal success path (reset + clear) for B itself.
  composer.dispatch('htmx:afterRequest', {
    successful: true,
    xhr: { status: 200 },
    requestConfig: { parameters: { dialog_id: '8', text: 'draft B', reply_to: '' } },
  });
  check("#209: B's own completion clears B's composer", composerText.value === '');
  check("#209: the form still targets B after B's completion", dialogId.value === '8');
  delete composer.dataset.submittingDialogId;
}

// ---- #300: a send completing must NOT wipe a draft edited while it was in flight ---------------
// Reproduction of the issue: send "first", then replace the composer contents with a NEW draft
// before the POST returns. The old afterRequest unconditionally cleared the compose state and
// composer.reset() — the unsent second text was lost. The fix snapshots the submitted text from
// the request's own parameters (htmx serializes the form — text included — when the POST is
// issued) and keeps any draft that no longer matches it, mirroring the #48 reply_to rule.
{
  const composerText = el('composer-text');
  const dialogId = el('dialog_id');

  // Send "first" in dialog 7, then edit the composer while the POST is in flight.
  dialogId.value = '7';
  composerText.value = 'first message';
  composer.dispatch('htmx:confirm', { issueRequest() {} });  // state.draft='first', POST issued
  composerText.value = 'second draft — not sent yet';        // user replaces the field contents
  composerText.dispatch('input');                            // → state.draft='second draft…'
  composer.dispatch('htmx:afterRequest', {
    successful: true, xhr: { status: 200 },
    requestConfig: { parameters: { dialog_id: '7', text: 'first message', reply_to: '' } },
  });
  check('#300: a draft edited during an in-flight send survives its completion',
        composerText.value === 'second draft — not sent yet');
  // The per-dialog compose state must have kept it too (observable by switching away and back:
  // openDialog saves/restores the draft from the state map).
  sandbox.openDialog({ dataset: { dialog: '8', canSend: '1' } });
  sandbox.openDialog({ dataset: { dialog: '7', canSend: '1' } });
  check('#300: the per-dialog state kept the edited draft',
        composerText.value === 'second draft — not sent yet');

  // Control: with NO edit during flight, completion still clears the composer as before.
  composerText.value = 'clean send';
  composer.dispatch('htmx:confirm', { issueRequest() {} });
  composer.dispatch('htmx:afterRequest', {
    successful: true, xhr: { status: 200 },
    requestConfig: { parameters: { dialog_id: '7', text: 'clean send', reply_to: '' } },
  });
  check('#300: an unedited draft is still cleared on completion', composerText.value === '');
}

// ---- #195 round 2: switch-before-history-response must NOT mark the left dialog read ----------
{
  const messages = el('messages');
  const dialogId = el('dialog_id');
  Object.defineProperty(messages, 'scrollHeight', { get: () => 1000, configurable: true });

  // Scenario: user opened unread dialog A (=7), then immediately switched to B (=8).
  // A's slow history response arrives. beforeSwap drops it (shouldSwap=false); htmx runs
  // afterSettle only inside the shouldSwap block, so afterSettle for A never fires. We assert
  // the guard drops it AND that even if afterSettle were reached, the request-path dialog guard
  // stops it from marking B read with A's content.
  dialogId.value = '8';               // user is now on B
  messageBubbles = [{ dataset: { id: '42', dialog: '7' } }];  // (A's bubbles, if they had swapped)

  const staleA = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true, pathInfo: { requestPath: '/dialogs/7/messages' },
  });
  check('round2: stale history response for the LEFT dialog is dropped', staleA.detail.shouldSwap === false);

  // Belt-and-suspenders: even if afterSettle fires for A's path while on B, no /read is sent
  // (request dialog 7 != current 8), so A is NOT marked read and B is not marked read with A.
  fetchCalls.length = 0;
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/7/messages' } });
  const readCallsForA = fetchCalls.filter((c) => String(c.url).includes('/dialogs/7/read'));
  const readCallsForB = fetchCalls.filter((c) => String(c.url).includes('/dialogs/8/read'));
  check('round2: no /read POST for the left dialog A', readCallsForA.length === 0);
  check('round2: no /read POST for B from A’s settle', readCallsForB.length === 0);

  // Positive path: an ACCEPTED history swap for the CURRENT dialog B DOES mark B read with B's max id.
  fetchCalls.length = 0;
  dialogId.value = '8';
  messageBubbles = [
    { dataset: { id: '5', dialog: '8' } },
    { dataset: { id: '9', dialog: '8' } },
    { dataset: { id: '3', dialog: '8' } },
  ];
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/8/messages' } });
  const readB = fetchCalls.filter((c) => String(c.url).includes('/dialogs/8/read'));
  check('round2: accepted history for the open dialog marks it read', readB.length === 1);
  check('round2: /read carries the highest shown message id', readB.length === 1 && readB[0].body.get('max_id') === '9');

  // A search swap for the open dialog does NOT mark read (search is not "seeing the whole chat").
  fetchCalls.length = 0;
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/8/search?q=hi' } });
  check('round2: a search swap does not mark read', fetchCalls.filter((c) => String(c.url).includes('/read')).length === 0);
}

// ---- #195 round 3: a /send (or /media) settle must NOT mark a dialog read (wrong-dialog race) --
{
  const messages = el('messages');
  const dialogId = el('dialog_id');
  Object.defineProperty(messages, 'scrollHeight', { get: () => 1000, configurable: true });

  // Scenario: user sends to A (=7), then immediately switches to B (=8) before A's /send response
  // settles. A's bubbles are still in the DOM (openDialog doesn't clear #messages synchronously).
  // A late /send afterSettle fires on #messages. It must NOT mark ANY dialog read:
  //   - /send is not a history load → the shared handler returns early.
  //   - even if it didn't, markDialogRead(B) would scan only B's bubbles, never A's id.
  dialogId.value = '8';  // user is now on B
  // mixed DOM: A's bubble (id 42) still present; no B bubbles yet (B's history not loaded)
  messageBubbles = [{ dataset: { id: '42', dialog: '7' } }];
  fetchCalls.length = 0;
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/send' } });
  const anyRead = fetchCalls.filter((c) => String(c.url).includes('/read'));
  check('round3: a /send settle marks NO dialog read', anyRead.length === 0);

  // /media is fetch-based (no afterSettle) in the app, but defensively: a /media path is also
  // classified as non-history, so even if it reached afterSettle it would not mark read.
  fetchCalls.length = 0;
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/7/media' } });
  check('round3: a /media-shaped settle marks NO dialog read',
        fetchCalls.filter((c) => String(c.url).includes('/read')).length === 0);

  // Even a HISTORY settle for A while on B marks nothing (dialog mismatch) — and crucially, if the
  // pane holds ONLY A's bubbles, an accepted history for B would still not pick up A's id.
  fetchCalls.length = 0;
  dialogId.value = '8';
  messageBubbles = [{ dataset: { id: '42', dialog: '7' } }];  // only A's bubble present
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/8/messages' } });
  const readWrong = fetchCalls.filter((c) => String(c.url).includes('/read'));
  check('round3: history-settle for B with only A bubbles present marks nothing (no foreign id)',
        readWrong.length === 0);

  // Positive control: B's history with B's bubbles marks B read by B's own max id.
  fetchCalls.length = 0;
  messageBubbles = [{ dataset: { id: '11', dialog: '8' } }, { dataset: { id: '42', dialog: '7' } }];
  messages.dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/8/messages' } });
  const readMixed = fetchCalls.filter((c) => String(c.url).includes('/dialogs/8/read'));
  check('round3: mixed DOM — B marked read by B’s max id, not A’s',
        readMixed.length === 1 && readMixed[0].body.get('max_id') === '11');
}

// ---- #227: a slow /suggest response must not land in another dialog's composer ----------------
// The suggest click snapshots the dialog and composer; the LLM answer arrives seconds later.
// Without post-await ownership guards, the response for dialog A overwrites whatever the user
// is typing in dialog B and is persisted as B's draft (wrong-recipient send path).
// These checks await real microtasks, so they run inside an async wrapper; the process summary
// moved here too (it must print after the awaited checks).
await (async () => {
  const drain = () => new Promise((r) => setTimeout(r, 0));  // node's real setTimeout
  const composerText = el('composer-text');
  const dialogId = el('dialog_id');
  const suggestBtn = el('suggest-btn');
  const suggestError = el('suggest-error');

  function controlledFetch() {
    let resolve;
    const origFetch = sandbox.fetch;
    sandbox.fetch = (url, opts) => {
      sandbox.fetch = origFetch;
      fetchCalls.push({ url, opts });
      return new Promise((res) => {
        resolve = (body, ok = true) =>
          res({ ok, status: ok ? 200 : 500, text: () => Promise.resolve(body) });
      });
    };
    return () => resolve;
  }

  // 1) switch-away race: suggest in A (=7), open B (=8) and type while the fetch is pending.
  dialogId.value = '7';
  composerText.value = '';
  composer.dispatch('htmx:confirm', { issueRequest() {} });  // activeDialogId = '7'
  let getResolve = controlledFetch();
  suggestBtn.dispatch('click');
  await drain();
  const suggestCall = fetchCalls[fetchCalls.length - 1];
  check('#227: suggest request carries an abort signal',
        !!(suggestCall.opts && suggestCall.opts.signal));
  dialogId.value = '8';
  composerText.value = '';
  composer.dispatch('htmx:confirm', { issueRequest() {} });  // activeDialogId = '8'
  composerText.value = 'draft B';                            // user typing in B
  getResolve()('suggested for A');
  await drain(); await drain();
  check("#227: A's late suggestion does NOT overwrite B's composer",
        composerText.value === 'draft B');
  check('#227: the form still targets B after the stale suggestion', dialogId.value === '8');

  // 2) same-dialog typing race: the user starts typing while the suggestion is pending.
  dialogId.value = '7';
  composerText.value = '';
  composer.dispatch('htmx:confirm', { issueRequest() {} });
  getResolve = controlledFetch();
  suggestBtn.dispatch('click');
  await drain();
  composerText.value = 'half-typed reply';
  getResolve()('suggested text');
  await drain(); await drain();
  check('#227: a suggestion never clobbers text typed while it was pending',
        composerText.value === 'half-typed reply');
  check('#227: the thinking hint is cleared when the suggestion is dropped',
        suggestError.textContent === '');

  // 3) happy path: no switch, no typing — the suggestion lands in the composer.
  dialogId.value = '7';
  composerText.value = '';
  composer.dispatch('htmx:confirm', { issueRequest() {} });
  getResolve = controlledFetch();
  suggestBtn.dispatch('click');
  await drain();
  check('#227: thinking hint shows while the request is in flight',
        suggestError.textContent.length > 0);
  getResolve()('use this draft');
  await drain(); await drain();
  check('#227: same-dialog suggestion still lands in the composer',
        composerText.value === 'use this draft');
  check('#227: the thinking hint is cleared on success', suggestError.textContent === '');
  check('#227: the suggest button is re-enabled', suggestBtn.disabled === false);

  // 4) controller ownership: aborting A during a switch to read-only B must not let
  // A's stale finally undo setComposerEnabled(false) for B.
  dialogId.value = '7';
  composerText.value = '';
  composer.dispatch('htmx:confirm', { issueRequest() {} });
  getResolve = controlledFetch();
  suggestBtn.dispatch('click');
  await drain();
  sandbox.openDialog({ dataset: { dialog: '8', canSend: '0' } });
  check('#227 follow-up: switching to read-only B disables Suggest', suggestBtn.disabled === true);
  getResolve()('late suggestion for A');
  await drain(); await drain();
  check("#227 follow-up: A's stale finally does not re-enable Suggest in B",
        suggestBtn.disabled === true);
})();

// ---- #300 review: a caption edited during a MEDIA UPLOAD survives the upload's completion ----
// The send path above covers htmx:afterRequest; this covers the OTHER clearComposeState caller,
// the attach-file change handler (same data-loss class found during the fix). Controlled fetch
// holds the upload response until the composer has been edited, then releases it.
await (async () => {
  const drain = () => new Promise((r) => setTimeout(r, 0));
  const composerText = el('composer-text');
  const attachFile = el('attach-file');
  attachFile.files = [{}];  // a file was picked (the handler only checks truthiness)

  function controlledUpload() {
    let resolve;
    const origFetch = sandbox.fetch;
    sandbox.fetch = (url, opts) => {
      sandbox.fetch = origFetch;
      fetchCalls.push({ url, opts });
      return new Promise((res) => {
        resolve = () => res({ ok: true, text: () => Promise.resolve('') });
      });
    };
    return () => resolve;
  }

  await drain();  // flush any microtasks left over from the earlier blocks

  // 1) edited-during-upload: the caption is replaced while the POST is in flight.
  sandbox.openDialog({ dataset: { dialog: '7', canSend: '1' } });  // activeDialogId = '7'
  composerText.value = 'caption sent with the file';
  composerText.dispatch('input');  // state.draft synced, as real typing does
  let finishUpload = controlledUpload();
  attachFile.dispatch('change');
  composerText.value = 'typed while uploading';  // user edits during flight
  composerText.dispatch('input');                // → state.draft='typed while uploading'
  finishUpload()();
  await drain(); await drain();
  check('#300 media: a draft typed during a media upload survives its completion',
        composerText.value === 'typed while uploading');
  // The per-dialog compose state must have kept it too (observable by switching away and back).
  sandbox.openDialog({ dataset: { dialog: '8', canSend: '1' } });
  sandbox.openDialog({ dataset: { dialog: '7', canSend: '1' } });
  check('#300 media: the per-dialog state kept the upload-time draft',
        composerText.value === 'typed while uploading');

  // 2) control: an unedited caption is cleared when the upload completes.
  composerText.value = 'clean caption';
  composerText.dispatch('input');
  finishUpload = controlledUpload();
  attachFile.dispatch('change');
  finishUpload()();
  await drain(); await drain();
  check('#300 media: an unedited caption is cleared on upload completion',
        composerText.value === '');
})();

// ---- #323 slice 1: live unread badges from the page-level stream -------------
{
  // Fake sidebar rows: dialog 9 with no badge yet, dialog 10 already showing 2.
  const makeBadge = (dialogId, count) => {
    const badge = new El('badge-' + dialogId);
    badge.className = 'unread';
    badge.textContent = String(count);
    return badge;
  };
  const li9 = new El('li-9');
  li9.dataset.dialog = '9';
  li9.querySelector = (sel) => (sel === '.unread' ? li9.children.find((c) => c.className === 'unread') || null : null);
  dialogLis.set('9', li9);
  const li10 = new El('li-10');
  li10.dataset.dialog = '10';
  const badge10 = makeBadge('10', 2);
  li10.appendChild(badge10);
  li10.querySelector = (sel) => (sel === '.unread' ? badge10 : null);
  dialogLis.set('10', li10);

  const stream = eventSources.find((s) => s.url === '/stream/unread');
  check('#323: the page opened a /stream/unread EventSource at load', !!stream);
  check('#323: the unread stream runs regardless of any open dialog', typeof stream.onmessage === 'function');

  const frame = (dialogId) => ({ data: JSON.stringify({ type: 'unread', dialog_id: dialogId }) });

  // First bump on a badge-less row creates the badge at 1.
  stream.onmessage(frame(9));
  const badge9 = li9.querySelector('.unread');
  check('#323: a first incoming message creates the missing badge at 1',
        !!badge9 && badge9.textContent === '1');

  // Second bump increments the existing badge.
  stream.onmessage(frame(9));
  check('#323: further incoming messages increment the badge', badge9.textContent === '2');

  // A row already carrying a server-rendered count increments from it.
  stream.onmessage(frame(10));
  check('#323: an existing server count increments (2 → 3)', badge10.textContent === '3');

  // The OPEN dialog never gets a badge bump (it's on screen; /read clears it).
  el('dialog_id').value = '7';
  stream.onmessage(frame(7));
  check('#323: a message in the open dialog does not bump any badge',
        li9.querySelector('.unread').textContent === '2' && badge10.textContent === '3');

  // A row absent from the current list (other tab / filtered) is skipped, no crash.
  el('dialog_id').value = '';
  stream.onmessage(frame(999));
  check('#323: a frame for a row outside the list is a no-op',
        li9.querySelector('.unread').textContent === '2' && badge10.textContent === '3');

  // Malformed / foreign frames are ignored.
  stream.onmessage({ data: 'not json' });
  stream.onmessage({ data: JSON.stringify({ type: 'deleted', dialog_id: 9 }) });
  check('#323: malformed and foreign-type frames are ignored',
        li9.querySelector('.unread').textContent === '2');

  dialogLis.clear();
  el('dialog_id').value = '';
}

// ---- #323 slice 2: deletions remove their bubbles live (no polling) ----------
{
  // Open dialog 7 through the REAL openDialog path, then drive the stream's actual
  // onmessage handler with a deleted frame. Model bubbles of the open dialog, plus a
  // foreign-dialog bubble whose id COLLIDES with a deleted one (Telegram ids are not
  // unique across dialogs — the #95/#96 guard) and an id outside the deletion set.
  sandbox.openDialog({ dataset: { dialog: '7', canSend: '1' } });
  el('dialog_id').value = '7';
  messageBubbles = [
    { dataset: { id: '5', dialog: '7' }, removed: false, remove() { this.removed = true; } },
    { dataset: { id: '6', dialog: '7' }, removed: false, remove() { this.removed = true; } },
    { dataset: { id: '7', dialog: '7' }, removed: false, remove() { this.removed = true; } },
    { dataset: { id: '5', dialog: '8' }, removed: false, remove() { this.removed = true; } },
  ];
  const stream = eventSources.find((s) => s.url.startsWith('/stream/7'));
  check('#323 s2: opening a dialog opened its per-dialog stream', !!stream);
  check('#323 s2: the stream has a message handler wired', typeof stream.onmessage === 'function');

  stream.onmessage({ data: JSON.stringify({ type: 'deleted', message_ids: [5, 6] }) });
  check('#323 s2: deleted bubbles are removed immediately',
        messageBubbles[0].removed && messageBubbles[1].removed);
  check('#323 s2: a live bubble outside the deletion set stays', !messageBubbles[2].removed);
  check('#323 s2: a colliding id of ANOTHER dialog is not removed', !messageBubbles[3].removed);

  // Missing ids / malformed frames are a no-op, not a crash.
  stream.onmessage({ data: JSON.stringify({ type: 'deleted' }) });
  stream.onmessage({ data: 'not json' });
  check('#323 s2: a frame without ids and a malformed frame are no-ops',
        !messageBubbles[2].removed && !messageBubbles[3].removed);
}

// ---- #323 slice 3: tab totals + the group-message bell ----------------------
// Needs real microtask drains: the page fetched /api/unread-totals at script load
// (totalsResponse = {dm:4, groups:2}, set before the sandbox ran).
await (async () => {
  const drain = () => new Promise((r) => setTimeout(r, 0));
  const stream = eventSources.find((s) => s.url === '/stream/unread');
  const dmBadge = () => el('tab-badge-dm').textContent;
  const groupsBadge = () => el('tab-badge-groups').textContent;
  const bell = el('notif-bell');
  const list = el('notif-list');
  await drain(); await drain();

  check('#323 s3: the page fetched the totals endpoint at load', totalsFetchCount > 0);
  check('#323 s3: the load seed renders both tab badges', dmBadge() === '4' && groupsBadge() === '2');

  // A DM frame bumps the DM total (row 9 is not in the list DOM here — badge no-op).
  stream.onmessage({ data: JSON.stringify({ type: 'unread', dialog_id: 9, kind: 'dm', title: 'Ann' }) });
  check('#323 s3: a dm frame bumps the DM total', dmBadge() === '5' && groupsBadge() === '2');

  // A group frame bumps the Groups total AND rings the bell.
  stream.onmessage({ data: JSON.stringify({ type: 'unread', dialog_id: 9, kind: 'group', title: 'Devs' }) });
  check('#323 s3: a group frame bumps the Groups total', dmBadge() === '5' && groupsBadge() === '3');
  check('#323 s3: a group message reveals the bell', bell.style.display === 'inline-block');
  check('#323 s3: the bell shows its count', el('notif-count').textContent === '1');

  // Channels/bots badge their tab but do NOT ring the bell (the issue asks for groups).
  stream.onmessage({ data: JSON.stringify({ type: 'unread', dialog_id: -100123, kind: 'channel', title: 'News' }) });
  check('#323 s3: a channel frame badges its tab without ringing the bell',
        groupsBadge() === '4' && el('notif-count').textContent === '1');

  // Unknown kind (lookup degraded): no bucket, no bell.
  stream.onmessage({ data: JSON.stringify({ type: 'unread', dialog_id: 999, kind: null, title: null }) });
  check('#323 s3: an unknown kind skips totals and bell',
        dmBadge() === '5' && groupsBadge() === '4' && el('notif-count').textContent === '1');

  // The OPEN dialog bumps nothing (existing slice-1 guard, totals and bell included).
  el('dialog_id').value = '9';
  stream.onmessage({ data: JSON.stringify({ type: 'unread', dialog_id: 9, kind: 'group', title: 'Devs' }) });
  check('#323 s3: the open dialog bumps neither badge nor bell',
        dmBadge() === '5' && groupsBadge() === '4' && el('notif-count').textContent === '1');
  el('dialog_id').value = '';

  // Opening the bell list acknowledges the count and lists one row per group. The
  // dropdown stays VISIBLE after the ack (hiding the wrap would hide the open list —
  // found in the real-browser pass; only the button hides).
  bell.dispatch('click');
  check('#323 s3: opening the bell list acknowledges the count',
        bell.style.display === 'none' && el('notif-count').textContent === '');
  check('#323 s3: the dropdown lists the notifying group',
        list.children.length === 1 && list.children[0].textContent === 'Devs');
  check('#323 s3: the bell is marked expanded while open', bell.attrs['aria-expanded'] === 'true');

  // A second click closes the dropdown.
  bell.dispatch('click');
  check('#323 s3: a second click closes the dropdown', list.style.display === 'none');

  // Repeated group messages accumulate ×N in the dropdown.
  stream.onmessage({ data: JSON.stringify({ type: 'unread', dialog_id: 9, kind: 'group', title: 'Devs' }) });
  stream.onmessage({ data: JSON.stringify({ type: 'unread', dialog_id: 9, kind: 'group', title: 'Devs' }) });
  check('#323 s3: repeated group messages accumulate ×N in the dropdown',
        list.children[0].textContent === 'Devs ×3' && el('notif-count').textContent === '2');

  // A /dialogs fetch re-renders the rows from the server; the visible tab's total
  // re-syncs from the row badges (drift model). A non-list path must not recount.
  const badgeA = new El('badge-12');
  badgeA.className = 'unread';
  badgeA.textContent = '3';
  const liA = new El('li-12');
  liA.querySelector = (sel) => (sel === '.unread' ? badgeA : null);
  dialogLis.set('12', liA);
  el('current-tab').value = 'dm';
  el('__body').dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs?tab=dm' } });
  check('#323 s3: a /dialogs fetch re-syncs the visible tab total from its rows', dmBadge() === '3');
  el('__body').dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/7/messages' } });
  check('#323 s3: a per-dialog settle does not recount', dmBadge() === '3');

  // Reading the open dialog subtracts its badge from the visible tab's total.
  let badgeADetached = false;
  badgeA.remove = () => { badgeADetached = true; };
  liA.querySelector = (sel) => (sel === '.unread' && !badgeADetached ? badgeA : null);
  el('dialog_id').value = '12';
  messageBubbles = [{ dataset: { id: '5', dialog: '12' } }];
  el('messages').dispatch('htmx:afterSettle', { pathInfo: { requestPath: '/dialogs/12/messages' } });
  check('#323 s3: reading the open dialog clears its share of the tab total',
        dmBadge() === '' && badgeADetached);

  dialogLis.clear();
  el('dialog_id').value = '';
  messageBubbles = [];
})();

console.log(failures === 0 ? '\nALL PASSED' : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
