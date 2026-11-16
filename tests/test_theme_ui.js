/* Run with node tests/test_theme_ui.js. Uses the shipped controller without a browser. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../jobflow/web_assets/theme.js'), 'utf8');

function harness(options = {}) {
  let hour = options.hour ?? 12;
  let saved = options.saved ?? null;
  let buttonReady = !options.loading;
  const windowEvents = {}, documentEvents = {}, buttonEvents = {};
  const intervals = [];
  const attributes = {};
  const button = {
    innerHTML: '',
    setAttribute(name, value) { attributes[name] = value; },
    addEventListener(name, callback) { buttonEvents[name] = callback; },
  };
  const root = {dataset: {}};
  const meta = {content: ''};
  const document = {
    documentElement: root, readyState: options.loading ? 'loading' : 'complete', hidden: false,
    querySelector: () => meta,
    getElementById: () => buttonReady ? button : null,
    addEventListener(name, callback) { documentEvents[name] = callback; },
  };
  const window = {
    localStorage: {
      getItem(key) {
        assert.equal(key, 'jobflow-theme');
        if (options.blockStorage) throw new Error('Storage disabled');
        return saved;
      },
      setItem(key, value) {
        assert.equal(key, 'jobflow-theme');
        if (options.blockStorage) throw new Error('Storage disabled');
        saved = value;
      },
    },
    addEventListener(name, callback) { windowEvents[name] = callback; },
    setInterval(callback, delay) { assert.equal(delay, 60000); intervals.push(callback); },
  };
  class Clock extends Date { getHours() { return hour; } }
  vm.runInNewContext(source, {window, document, Date: Clock}, {filename: 'theme.js'});
  return {
    root, meta, attributes, button,
    saved: () => saved,
    click: () => buttonEvents.click(),
    tick(value) { hour = value; intervals.forEach(callback => callback()); },
    wake(value) { hour = value; documentEvents.visibilitychange(); },
    load() { buttonReady = true; documentEvents.DOMContentLoaded(); },
    storage(value, key = 'jobflow-theme') { saved = value; windowEvents.storage({key}); },
  };
}

for (const [hour, expected] of [[0,'dark'], [6,'dark'], [7,'light'], [12,'light'], [18,'light'], [19,'dark'], [23,'dark']]) {
  assert.equal(harness({hour}).root.dataset.theme, expected, 'Uses browser-local day/night boundaries');
}

{
  const app = harness({hour: 12, loading: true});
  assert.equal(app.root.dataset.theme, 'light', 'Theme is set before the button or CSS is parsed');
  assert.equal(app.meta.content, '#f5f6f7');
  assert.equal(app.attributes['aria-label'], undefined);
  app.load();
  assert.equal(app.attributes['aria-label'], 'Switch to dark mode');
  assert.equal(app.attributes['aria-pressed'], 'false');
  app.click();
  assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.saved(), 'dark');
  assert.equal(app.attributes['aria-label'], 'Switch to light mode');
  assert.equal(app.attributes.title, 'Switch to light mode');
  assert.equal(app.attributes['aria-pressed'], 'true');
  assert.equal(app.meta.content, '#101416');
  assert.ok(app.button.innerHTML.includes('aria-hidden="true"'));
  assert.equal(harness({hour: 10, saved: app.saved()}).root.dataset.theme, 'dark', 'Manual choice survives a fresh page load');
}

{
  const app = harness({hour: 6});
  app.tick(7);
  assert.equal(app.root.dataset.theme, 'light', 'An open page changes at the morning boundary');
  app.wake(19);
  assert.equal(app.root.dataset.theme, 'dark', 'Waking a background tab rechecks the time');
  app.click();
  app.tick(23);
  assert.equal(app.root.dataset.theme, 'light', 'The clock cannot override a user selection');
}

{
  const app = harness({hour: 12, blockStorage: true});
  assert.equal(app.root.dataset.theme, 'light');
  app.click();
  app.tick(13);
  assert.equal(app.root.dataset.theme, 'dark', 'Toggle works for the current session even when storage is unavailable');
  app.click();
  assert.equal(app.root.dataset.theme, 'light');
}

{
  const app = harness({hour: 12, saved: 'unsupported'});
  assert.equal(app.root.dataset.theme, 'light', 'Invalid stored values use the day/night default');
  app.storage('dark');
  assert.equal(app.root.dataset.theme, 'dark', 'Another tab can synchronize the chosen appearance');
  app.storage(null, null);
  assert.equal(app.root.dataset.theme, 'light', 'Clearing storage restores the time-based default');
  app.storage('dark', 'unrelated-key');
  assert.equal(app.root.dataset.theme, 'light', 'Unrelated storage events do not change the page');
}

console.log('Theme UI checks passed: day/night, early paint, toggle, persistence, wake, storage fallback and synchronization.');
