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
const document_ = {
  getElementById: (id) => (ids.has(id) ? ids.get(id) : el(id)),
  querySelector: () => null,
  querySelectorAll: () => [],
  body: el('__body'),
  createElement: () => new El('__created'),
  addEventListener: () => {},
};
// composer.querySelector('button[type=submit]') is used — return a stub button.
composer.querySelector = () => ({ disabled: false });

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
  fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('') }),
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

console.log(failures === 0 ? '\nALL PASSED' : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
