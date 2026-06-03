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

const userDataDir = mkdtempSync(path.join(tmpdir(), 'surcharge-minimum-'));
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
  await waitForExpression(cdp, `document.readyState === 'complete' && location.href === ${JSON.stringify(fileUrl)} && typeof calcFees === 'function'`);

  const { result } = await cdp.send('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const ctx = { sw: 100, cbmV: 2, mainCost: 500 };
      const run = fee => calcFees([fee], ctx, 'CNY');
      const perKgMin = run({enabled:true, code:'TC', name:'处理费', type:'per_kg', amount:'0.06', min:'30'});
      const perKgHigher = run({enabled:true, code:'FSC', name:'燃油附加费', type:'per_kg', amount:'0.6', min:'30'});
      const perCbmMin = run({enabled:true, code:'VOL', name:'体积附加费', type:'per_cbm', amount:'5', min:'30'});
      const percentMin = run({enabled:true, code:'PCT', name:'比例附加费', type:'percent', amount:'2', min:'30'});
      const manualNoMin = run({enabled:true, code:'MAN', name:'手动费', type:'manual', amount:'12', min:'30'});
      return {
        perKgMinTotal: perKgMin.total,
        perKgMinCost: perKgMin.items[0].cost,
        perKgMinApplied: perKgMin.items[0].minApplied,
        perKgMinRaw: perKgMin.items[0].rawCost,
        perKgHigherTotal: perKgHigher.total,
        perKgHigherApplied: perKgHigher.items[0].minApplied,
        perCbmMinTotal: perCbmMin.total,
        perCbmMinApplied: perCbmMin.items[0].minApplied,
        percentMinTotal: percentMin.total,
        percentMinApplied: percentMin.items[0].minApplied,
        manualNoMinTotal: manualNoMin.total,
        manualNoMinApplied: manualNoMin.items[0].minApplied,
        minChargeLabel: feeTypeText('min_charge'),
        minNote: perKgMin.notes.join('；')
      };
    })()`,
  });

  const data = result.value;
  const failures = [];
  assertEqual(data.perKgMinTotal, 30, 'per kg minimum total', failures);
  assertEqual(data.perKgMinCost, 30, 'per kg minimum row cost', failures);
  assertEqual(data.perKgMinRaw, 6, 'per kg raw cost', failures);
  assertEqual(data.perKgMinApplied, true, 'per kg minimum applied flag', failures);
  assertEqual(data.perKgHigherTotal, 60, 'per kg calculated higher total', failures);
  assertEqual(data.perKgHigherApplied, false, 'per kg calculated higher applied flag', failures);
  assertEqual(data.perCbmMinTotal, 30, 'per CBM minimum total', failures);
  assertEqual(data.perCbmMinApplied, true, 'per CBM minimum applied flag', failures);
  assertEqual(data.percentMinTotal, 30, 'percent minimum total', failures);
  assertEqual(data.percentMinApplied, true, 'percent minimum applied flag', failures);
  assertEqual(data.manualNoMinTotal, 12, 'manual ignores minimum total', failures);
  assertEqual(data.manualNoMinApplied, false, 'manual minimum applied flag', failures);
  assertIncludes(data.minChargeLabel, '按 kg', 'minimum charge label', failures);
  assertIncludes(data.minNote, '最低收费', 'minimum application note', failures);

  if (exceptions.length) {
    failures.push(`page threw ${exceptions.length} runtime exception(s)`);
  }

  if (failures.length) {
    console.error('Surcharge minimum verification failed:');
    for (const failure of failures) console.error(`- ${failure}`);
    console.error(`Observed: ${JSON.stringify(data, null, 2)}`);
    process.exitCode = 1;
  } else {
    console.log('Surcharge minimum verification passed.');
    console.log(JSON.stringify(data, null, 2));
  }

  cdp.close();
} catch (error) {
  console.error(error.stack || error.message);
  process.exitCode = 1;
} finally {
  await cleanup();
}
