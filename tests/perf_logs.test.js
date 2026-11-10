const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'perf_drilldown.js'), 'utf8');
const functions = source.slice(source.indexOf('function drillExpectedPath('), source.indexOf('async function drillOpenRecord('));

function setup(alternates) {
  const copied = [];
  const buttons = [];
  const hint = {
    hidden: true,
    innerHTML: '',
    querySelectorAll() {
      return [...this.innerHTML.matchAll(/data-drill-copy-path="(\d+)"/g)].map(match => {
        const button = {
          dataset: { drillCopyPath: match[1] },
          addEventListener(event, callback) { this.click = callback; },
        };
        buttons.push(button);
        return button;
      });
    },
  };
  const context = vm.createContext({
    drillLogs: { dir: 'C:\\primary', alternates, source: 'config', checked: true, found: 1, total: 2 },
    drillEl: () => hint,
    escapeHtml: text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    navigator: { clipboard: { async writeText(text) { copied.push(text); } } },
    setTimeout() {},
  });
  vm.runInContext(functions, context);
  vm.runInContext('drillRenderLogsHint()', context);
  return { context, hint, buttons, copied };
}

test('each path is displayed safely and its button copies the corresponding path', async () => {
  const alternates = ['\\\\other-pc\\logs', 'D:\\logs<&>'];
  const { hint, buttons, copied, context } = setup(alternates);
  assert.equal(hint.hidden, false);
  assert.equal(buttons.length, 3);
  assert.match(hint.innerHTML, /Expected logs folder/);
  assert.match(hint.innerHTML, /Alternate logs folder/);
  assert.match(hint.innerHTML, /D:\\logs&lt;&amp;&gt;/);
  for (const button of buttons) {
    await button.click();
    assert.equal(button.textContent, 'Copied');
  }
  assert.deepEqual(copied, ['C:\\primary', ...alternates]);
  assert.equal(vm.runInContext('drillExpectedPath({ RawFile: "test.csv" })', context), 'C:\\primary\\test.csv');
});

test('single-folder metadata still renders one copy button', () => {
  const { buttons, hint } = setup(undefined);
  assert.equal(buttons.length, 1);
  assert.doesNotMatch(hint.innerHTML, /Alternate logs folder/);
});
