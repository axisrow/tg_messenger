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
    this.textContent = '';
    this.innerHTML = '';
    this.style = {};
    this.dataset = {};
    this.disabled = false;
    this.defaultValue = '';
    this.attrs = {};
    this._listeners = {};
    this.children = [];
  }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  dispatch(type, detail) {
    const evt = { type, detail, preventDefault() { this.defaultPrevented = true; }, defaultPrevented: false };
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
  querySelector: () => null,
  querySelectorAll: (sel) => {
    if (!sel || !sel.startsWith('#messages .msg[data-id]')) return [];
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
class FakeFormData { constructor() { this._d = {}; } append(k, v) { this._d[k] = v; } get(k) { return this._d[k]; } }

const sandbox = {
  document: document_,
  window: {},
  sessionStorage: { getItem: () => 'cid-test', setItem: () => {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  crypto: { randomUUID: () => 'cid-test' },
  console,
  EventSource: class { constructor() {} close() {} },
  setTimeout: (fn) => { return 0; },  // don't actually schedule debounced work
  clearTimeout: () => {},
  fetch: (url, opts) => {
    fetchCalls.push({ url, body: opts && opts.body });
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
  composer.dispatch('htmx:afterRequest', { successful: true, xhr: { status: 200 } });
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

// ---- #203: a stale /send echo for a left dialog is dropped (behavioral, not grep) -------------
// The /send path carries no dialogId, so the history/search guard above never covered it; the
// send branch scopes the swap by composer.dataset.submittingDialogId (stamped in htmx:confirm,
// live at beforeSwap). This drives the real handler so a regression where the send branch stopped
// setting shouldSwap=false (or read the wrong field) would fail here — unlike the grep test.
{
  const messages = el('messages');
  const dialogId = el('dialog_id');

  // User sent in A (=7), then switched to B (=8) before A's /send POST returned.
  dialogId.value = '8';
  composer.dataset.submittingDialogId = '7';
  const staleSend = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
  });
  check("#203: stale /send echo for the left dialog is dropped", staleSend.detail.shouldSwap === false);

  // The normal case: send in the dialog the user is still viewing → the echo swaps in.
  composer.dataset.submittingDialogId = '8';
  const liveSend = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
  });
  check("#203: same-dialog /send echo is allowed to swap", liveSend.detail.shouldSwap === true);

  // Missing submittingDialogId (no send in flight through the composer) must not drop a swap.
  delete composer.dataset.submittingDialogId;
  const noStamp = messages.dispatch('htmx:beforeSwap', {
    shouldSwap: true,
    pathInfo: { requestPath: '/send' },
  });
  check("#203: /send with no submitting dialog stamped is not dropped", noStamp.detail.shouldSwap === true);
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

console.log(failures === 0 ? '\nALL PASSED' : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
