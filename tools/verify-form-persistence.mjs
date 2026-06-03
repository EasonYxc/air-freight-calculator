#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = process.cwd();
const htmlPath = path.resolve(root, process.argv[2] || 'estimator.html');
const chromePath = process.env.CHROME_PATH || [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
].find(existsSync);

if (!chromePath) {
  console.error('No Chrome-compatible browser found. Set CHROME_PATH to run this check.');
  process.exit(2);
}

if (!existsSync(htmlPath)) {
  console.error(`HTML file not found: ${htmlPath}`);
  process.exit(2);
}

const userDataDir = mkdtempSync(path.join(tmpdir(), 'form-persistence-'));
const chrome = spawn(chromePath, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  '--remote-debugging-port=0',
  `--user-data-dir=${userDataDir}`,
  'about:blank',
], { stdio: 'ignore' });

let closing = false;
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function cleanup() {
  if (closing) return;
  closing = true;
  if (chrome.exitCode === null) {
    chrome.kill('SIGTERM');
    await Promise.race([
      new Promise(resolve => chrome.once('exit', resolve)),
      sleep(2000),
    ]);
  }
  for (let i = 0; i < 5; i++) {
    try {
      rmSync(userDataDir, { recursive: true, force: true });
      return;
    } catch (error) {
      if (i === 4) throw error;
      await sleep(150);
    }
  }
}

process.on('exit', () => {
  if (!closing) {
    chrome.kill('SIGTERM');
    rmSync(userDataDir, { recursive: true, force: true });
  }
});

async function waitForDevToolsPort() {
  const portFile = path.join(userDataDir, 'DevToolsActivePort');
  for (let i = 0; i < 80; i++) {
    if (existsSync(portFile)) {
      return readFileSync(portFile, 'utf8').trim().split('\n')[0];
    }
    await sleep(100);
  }
  throw new Error('Timed out waiting for Chrome DevTools port.');
}

async function createPage(port, url) {
  const endpoint = `http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`;
  let response = await fetch(endpoint, { method: 'PUT' });
  if (!response.ok) response = await fetch(endpoint);
  if (!response.ok) throw new Error(`Unable to create Chrome page: ${response.status}`);
  return response.json();
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    const waiters = new Map();
    const handlers = new Map();
    let seq = 0;

    function send(method, params = {}) {
      const id = ++seq;
      ws.send(JSON.stringify({ id, method, params }));
      return new Promise((res, rej) => pending.set(id, { res, rej, method }));
    }

    function waitForEvent(method, timeout = 10000) {
      return new Promise((res, rej) => {
        const timer = setTimeout(() => rej(new Error(`Timed out waiting for ${method}`)), timeout);
        const list = waiters.get(method) || [];
        list.push(payload => {
          clearTimeout(timer);
          res(payload);
        });
        waiters.set(method, list);
      });
    }

    function on(method, fn) {
      const list = handlers.get(method) || [];
      list.push(fn);
      handlers.set(method, list);
    }

    ws.addEventListener('open', () => resolve({ send, waitForEvent, on, close: () => ws.close() }));
    ws.addEventListener('error', reject);
    ws.addEventListener('message', event => {
      const msg = JSON.parse(event.data);
      if (msg.id && pending.has(msg.id)) {
        const item = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) item.rej(new Error(`${item.method}: ${msg.error.message}`));
        else item.res(msg.result);
        return;
      }
      handlers.get(msg.method)?.forEach(fn => fn(msg.params));
      const list = waiters.get(msg.method);
      if (list?.length) list.shift()(msg.params);
    });
  });
}

async function waitForExpression(cdp, expression, timeout = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const { result } = await cdp.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
    });
    if (result.value) return;
    await sleep(100);
  }
  throw new Error(`Timed out waiting for expression: ${expression}`);
}

function assertEqual(actual, expected, label, failures) {
  if (actual !== expected) {
    failures.push(`${label} expected "${expected}", got "${actual}"`);
  }
}

function assertIncludes(text, expected, label, failures) {
  if (!String(text || '').includes(expected)) {
    failures.push(`${label} expected to include "${expected}", got "${text || '(empty)'}"`);
  }
}

try {
  const port = await waitForDevToolsPort();
  const fileUrl = pathToFileURL(htmlPath).href;
  const page = await createPage(port, fileUrl);
  const cdp = await connect(page.webSocketDebuggerUrl);
  const exceptions = [];

  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  cdp.on('Runtime.exceptionThrown', evt => exceptions.push(evt));
  await waitForExpression(cdp, `document.readyState === 'complete' && location.href === ${JSON.stringify(fileUrl)} && !!document.getElementById('sProv')`);
  await sleep(800);

  await cdp.send('Runtime.evaluate', {
    expression: `(() => {
      const setValue = (id, value) => {
        const el = document.getElementById(id);
        if (!el) throw new Error('Missing element ' + id);
        el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const setChecked = (id, value) => {
        const el = document.getElementById(id);
        if (!el) throw new Error('Missing checkbox ' + id);
        el.checked = value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const click = selector => {
        const el = document.querySelector(selector);
        if (!el) throw new Error('Missing click target ' + selector);
        el.click();
      };
      setValue('sProv', '刷新保留服务商');
      setValue('sGw', '123.45');
      setValue('sOri', 'CAN');
      setValue('sDst', 'HKG');
      setChecked('sAp', false);
      click('#sVolToggle [data-vm="cbm"]');
      setValue('sCbmV', '2.345');
      setChecked('sFeeEnable', true);
      const firstFee = document.querySelector('#sFeeList .fee-row');
      firstFee.querySelector('.fee-check').checked = true;
      firstFee.querySelector('.fee-check').dispatchEvent(new Event('change', { bubbles: true }));
      firstFee.querySelector('.fee-amt').value = '9.88';
      firstFee.querySelector('.fee-amt').dispatchEvent(new Event('input', { bubbles: true }));
      firstFee.querySelector('.fee-min').value = '50';
      firstFee.querySelector('.fee-min').dispatchEvent(new Event('input', { bubbles: true }));
      click('#modeSeg [data-mode="compare"]');
      click('.add-btn');
      setValue('pName-3', '新增服务商持久化');
      setValue('pOri-3', 'HKG');
      setValue('pDst-3', 'CAN');
      setChecked('pFeeEnable-3', true);
      const fee = document.querySelector('#pFeeList-3 .fee-row');
      fee.querySelector('.fee-code').value = 'TEST';
      fee.querySelector('.fee-code').dispatchEvent(new Event('input', { bubbles: true }));
      fee.querySelector('.fee-name').value = '刷新保留附加费';
      fee.querySelector('.fee-name').dispatchEvent(new Event('input', { bubbles: true }));
      fee.querySelector('.fee-check').checked = true;
      fee.querySelector('.fee-check').dispatchEvent(new Event('change', { bubbles: true }));
    })()`,
    returnByValue: true,
  });
  await sleep(900);

  const reloaded = cdp.waitForEvent('Page.loadEventFired');
  await cdp.send('Page.reload', { ignoreCache: true });
  await reloaded;
  await waitForExpression(cdp, `document.readyState === 'complete' && !!document.getElementById('sProv')`);
  await sleep(1200);

  const { result } = await cdp.send('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const value = id => document.getElementById(id)?.value || '';
      const checked = id => !!document.getElementById(id)?.checked;
      const text = id => document.getElementById(id)?.textContent.trim() || '';
      const row = document.querySelector('#pFeeList-3 .fee-row');
      return {
        activeMode: document.querySelector('#modeSeg .seg-btn.active')?.dataset.mode || '',
        singleModeDisplay: getComputedStyle(document.getElementById('singleMode')).display,
        compareModeDisplay: getComputedStyle(document.getElementById('compareMode')).display,
        sVolMode: document.querySelector('#sVolToggle .tg-btn.active')?.dataset.vm || '',
        sCbmVisible: getComputedStyle(document.getElementById('sCbm')).display,
        sProv: value('sProv'),
        sGw: value('sGw'),
        sOri: value('sOri'),
        sOriName: text('sOri-airport-name'),
        sDst: value('sDst'),
        sAp: checked('sAp'),
        sFeeEnable: checked('sFeeEnable'),
        sFeeAmount: document.querySelector('#sFeeList .fee-row .fee-amt')?.value || '',
        sFeeMin: document.querySelector('#sFeeList .fee-row .fee-min')?.value || '',
        providerCards: [...document.querySelectorAll('.pr-card')].map(el => el.id).join(','),
        pName3: value('pName-3'),
        pOri3: value('pOri-3'),
        pOri3Name: text('pOri-3-airport-name'),
        pDst3: value('pDst-3'),
        pFeeEnable3: checked('pFeeEnable-3'),
        pFeeCode3: row?.querySelector('.fee-code')?.value || '',
        pFeeName3: row?.querySelector('.fee-name')?.value || '',
        localStorageKeys: Object.keys(localStorage).sort().join(',')
      };
    })()`,
  });

  const data = result.value;
  const failures = [];
  assertEqual(data.activeMode, 'compare', 'active mode', failures);
  assertEqual(data.compareModeDisplay, 'block', 'compare mode visibility', failures);
  assertEqual(data.sVolMode, 'cbm', 'single volume mode', failures);
  assertEqual(data.sCbmVisible, 'block', 'single CBM section visibility', failures);
  assertEqual(data.sProv, '刷新保留服务商', 'single provider name', failures);
  assertEqual(data.sGw, '123.45', 'single gross weight', failures);
  assertEqual(data.sOri, 'CAN', 'single origin code', failures);
  assertIncludes(data.sOriName, '广州白云国际机场', 'single origin full name', failures);
  assertEqual(data.sDst, 'HKG', 'single destination code', failures);
  assertEqual(data.sAp, false, 'single allow pivot checkbox', failures);
  assertEqual(data.sFeeEnable, true, 'single fee enable checkbox', failures);
  assertEqual(data.sFeeAmount, '9.88', 'single fee amount', failures);
  assertEqual(data.sFeeMin, '50', 'single fee min', failures);
  assertIncludes(data.providerCards, 'pcard-3', 'provider cards', failures);
  assertEqual(data.pName3, '新增服务商持久化', 'provider 3 name', failures);
  assertEqual(data.pOri3, 'HKG', 'provider 3 origin code', failures);
  assertIncludes(data.pOri3Name, '香港国际机场', 'provider 3 origin full name', failures);
  assertEqual(data.pDst3, 'CAN', 'provider 3 destination code', failures);
  assertEqual(data.pFeeEnable3, true, 'provider 3 fee enable checkbox', failures);
  assertEqual(data.pFeeCode3, 'TEST', 'provider 3 fee code', failures);
  assertEqual(data.pFeeName3, '刷新保留附加费', 'provider 3 fee name', failures);
  assertIncludes(data.localStorageKeys, 'airFreightCalculatorState:v1', 'localStorage key', failures);

  if (exceptions.length) {
    failures.push(`page threw ${exceptions.length} runtime exception(s)`);
  }

  if (failures.length) {
    console.error('Form persistence verification failed:');
    for (const failure of failures) console.error(`- ${failure}`);
    console.error(`Observed: ${JSON.stringify(data, null, 2)}`);
    process.exitCode = 1;
  } else {
    console.log('Form persistence verification passed.');
    console.log(JSON.stringify(data, null, 2));
  }

  cdp.close();
} catch (error) {
  console.error(error.stack || error.message);
  process.exitCode = 1;
} finally {
  await cleanup();
}
