// ==UserScript==
// @name         Learning Flow Recorder
// @namespace    ai-native-learning
// @version      0.2.0
// @description  Records the real click-path a student takes across Canvas and the third-party tools it hands off to. Local only; nothing is uploaded.
// @author       ai-native-learning
// @match        *://canvas.wisc.edu/*
// @match        *://*.wisc.edu/*
// @match        *://*.instructure.com/*
// @match        *://*.inscloudgate.net/*
// @match        *://sso.canvaslms.com/*
// @match        *://*.gradescope.com/*
// @match        *://*.mheducation.com/*
// @match        *://*.cengage.com/*
// @match        *://cengageorg.my.site.com/*
// @match        *://*.webassign.net/*
// @match        *://*.unizin.org/*
// @match        *://*.pressbooks.pub/*
// @match        *://*.box.com/*
// @match        *://docs.google.com/*
// @match        *://drive.google.com/*
// @match        *://accounts.google.com/*
// @match        *://*.quizlet.com/*
// @match        *://*.piazza.com/*
// @match        *://*.registerblast.com/*
// @match        *://*.zoom.us/*
// @match        *://*.kaltura.com/*
// @match        *://*.tophat.com/*
// @match        *://*.pearson.com/*
// @exclude      *://*.duosecurity.com/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @run-at       document-start
// ==/UserScript==

/*
 * WHAT THIS RECORDS
 *   - every URL you land on (including SPA route changes)
 *   - the LABEL of links/buttons you click (not page content)
 *   - hand-offs between domains (the expensive part of the flow)
 *   - file downloads you trigger, and files you attach to upload inputs (name/size only)
 *   - how long you sat on each page
 *
 * WHAT IT DOES NOT RECORD
 *   - page text, answers, grades, messages, passwords, form values
 *   - anything at all over the network. Storage is Tampermonkey-local.
 *
 * Export/clear any time from the Tampermonkey menu.
 */

(function () {
  'use strict';

  const KEY = 'flow_events';
  const MAX_EVENTS = 8000;

  const now = () => Date.now();
  const load = () => { try { return JSON.parse(GM_getValue(KEY, '[]')); } catch (e) { return []; } };
  const save = (ev) => { try { GM_setValue(KEY, JSON.stringify(ev.slice(-MAX_EVENTS))); } catch (e) {} };

  let buffer = [];
  let flushTimer = null;

  function flush() {
    if (!buffer.length) return;
    const all = load().concat(buffer);
    buffer = [];
    save(all);
  }

  function push(type, detail) {
    buffer.push({
      t: now(),
      type,
      host: location.host,
      path: location.pathname + location.search,
      ...detail,
    });
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, 800);
  }

  // ---- label extraction (bounded, no page content) -------------------------
  function labelOf(el) {
    if (!el) return '';
    const t = (el.getAttribute('aria-label') ||
               el.getAttribute('title') ||
               el.innerText ||
               el.value ||
               '').trim().replace(/\s+/g, ' ');
    return t.slice(0, 120);
  }

  // ---- navigation ----------------------------------------------------------
  let landedAt = now();
  let lastPath = location.href;

  function onNav(kind) {
    const dwell = Math.round((now() - landedAt) / 1000);
    if (dwell > 0) push('dwell', { seconds: dwell, from: lastPath });
    landedAt = now();
    lastPath = location.href;
    push('nav', { kind, title: (document.title || '').slice(0, 120), ref: document.referrer || null });
  }

  // initial landing
  push('nav', {
    kind: 'load',
    title: (document.title || '').slice(0, 120),
    ref: document.referrer || null,
    crossDomain: (() => {
      try { return document.referrer && new URL(document.referrer).host !== location.host; }
      catch (e) { return false; }
    })(),
  });

  // SPA route changes
  ['pushState', 'replaceState'].forEach((fn) => {
    const orig = history[fn];
    history[fn] = function () {
      const r = orig.apply(this, arguments);
      setTimeout(() => onNav(fn), 0);
      return r;
    };
  });
  window.addEventListener('popstate', () => onNav('popstate'));
  window.addEventListener('hashchange', () => onNav('hashchange'));

  // ---- clicks --------------------------------------------------------------
  document.addEventListener('click', (e) => {
    const el = e.target && e.target.closest
      ? e.target.closest('a,button,[role="button"],input[type="submit"],input[type="button"]')
      : null;
    if (!el) return;

    const href = el.getAttribute && el.getAttribute('href');
    let leavesTo = null;
    if (href) {
      try {
        const u = new URL(href, location.href);
        if (u.host !== location.host) leavesTo = u.host;
      } catch (err) {}
    }

    push('click', {
      tag: el.tagName.toLowerCase(),
      label: labelOf(el),
      href: href ? href.slice(0, 300) : null,
      leavesTo,
      download: el.hasAttribute && el.hasAttribute('download') ? true : undefined,
    });
  }, true);

  // ---- file attach (name + size only) --------------------------------------
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || el.tagName !== 'INPUT' || el.type !== 'file') return;
    const files = Array.from(el.files || []).map((f) => ({
      name: f.name.slice(0, 120),
      kb: Math.round(f.size / 1024),
    }));
    if (files.length) push('file_attach', { files });
  }, true);

  // ---- form submits (no values) --------------------------------------------
  document.addEventListener('submit', (e) => {
    const f = e.target;
    push('form_submit', {
      action: (f && f.getAttribute && f.getAttribute('action') || '').slice(0, 300),
      fields: f && f.elements ? f.elements.length : null,
    });
  }, true);

  window.addEventListener('beforeunload', () => {
    const dwell = Math.round((now() - landedAt) / 1000);
    if (dwell > 0) buffer.push({ t: now(), type: 'dwell', host: location.host, path: location.pathname, seconds: dwell });
    flush();
  });

  // ---- menu ----------------------------------------------------------------
  GM_registerMenuCommand('Flow Recorder: export JSON', () => {
    flush();
    const data = load();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'flow-events-' + new Date().toISOString().slice(0, 10) + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  GM_registerMenuCommand('Flow Recorder: stats', () => {
    flush();
    const d = load();
    const hosts = {};
    d.forEach((e) => { hosts[e.host] = (hosts[e.host] || 0) + 1; });
    const jumps = d.filter((e) => e.leavesTo).length;
    alert(
      'events: ' + d.length +
      '\nspan: ' + (d.length ? new Date(d[0].t).toLocaleString() + ' -> now' : 'n/a') +
      '\ncross-domain jumps: ' + jumps +
      '\n\n' + Object.entries(hosts).sort((a, b) => b[1] - a[1]).map(([h, n]) => n + '  ' + h).join('\n')
    );
  });

  GM_registerMenuCommand('Flow Recorder: clear', () => {
    if (confirm('Erase all recorded flow events?')) { buffer = []; GM_setValue(KEY, '[]'); }
  });

  // read-out hook for tooling
  window.__flowRecorderDump = () => { flush(); return load(); };
})();
