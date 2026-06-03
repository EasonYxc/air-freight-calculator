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

const userDataDir = mkdtempSync(path.join(tmpdir(), 'airport-display-'));
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

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

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

function assertIncludes(text, expected, label, failures) {
  if (!text.includes(expected)) {
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
  await waitForExpression(cdp, `document.readyState === 'complete' && location.href === ${JSON.stringify(fileUrl)} && !!document.getElementById('sOri')`);
  await sleep(800);

  const { result } = await cdp.send('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const text = id => document.getElementById(id)?.textContent.trim() || '';
      const value = id => document.getElementById(id)?.value || '';
      const setValue = (id, next) => {
        const el = document.getElementById(id);
        if (!el) return '';
        el.value = next;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return text(id + '-airport-name');
      };
      return {
        sOriValue: value('sOri'),
        sDstValue: value('sDst'),
        sOriName: text('sOri-airport-name'),
        sDstName: text('sDst-airport-name'),
        pOri1Name: text('pOri-1-airport-name'),
        pDst1Name: text('pDst-1-airport-name'),
        sOriAfterCan: setValue('sOri', 'CAN'),
        pDst1AfterCan: setValue('pDst-1', 'CAN')
      };
    })()`,
  });

  const data = result.value;
  const failures = [];

  assertIncludes(data.sOriName, 'SZX', 'single origin airport label', failures);
  assertIncludes(data.sOriName, '深圳宝安国际机场', 'single origin airport label', failures);
  assertIncludes(data.sDstName, 'BKK', 'single destination airport label', failures);
  assertIncludes(data.sDstName, '曼谷素万那普国际机场', 'single destination airport label', failures);
  assertIncludes(data.pOri1Name, 'SZX', 'provider origin airport label', failures);
  assertIncludes(data.pOri1Name, '深圳宝安国际机场', 'provider origin airport label', failures);
  assertIncludes(data.pDst1Name, 'BKK', 'provider destination airport label', failures);
  assertIncludes(data.pDst1Name, '曼谷素万那普国际机场', 'provider destination airport label', failures);
  assertIncludes(data.sOriAfterCan, 'CAN', 'typed single origin airport label', failures);
  assertIncludes(data.sOriAfterCan, '广州白云国际机场', 'typed single origin airport label', failures);
  assertIncludes(data.pDst1AfterCan, 'CAN', 'typed provider destination airport label', failures);
  assertIncludes(data.pDst1AfterCan, '广州白云国际机场', 'typed provider destination airport label', failures);

  if (exceptions.length) {
    failures.push(`page threw ${exceptions.length} runtime exception(s)`);
  }

  if (failures.length) {
    console.error('Airport display verification failed:');
    for (const failure of failures) console.error(`- ${failure}`);
    console.error(`Observed: ${JSON.stringify(data, null, 2)}`);
    process.exitCode = 1;
  } else {
    console.log('Airport display verification passed.');
    console.log(JSON.stringify(data, null, 2));
  }

  cdp.close();
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  await cleanup();
}
