#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { copyFileSync, existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const source = path.resolve(root, 'estimator.html');
const target = path.resolve(root, 'index.html');
const args = new Set(process.argv.slice(2));

const checks = [
  { cmd: 'node', args: ['--check', 'tools/sync-and-verify.mjs'] },
  { cmd: 'node', args: ['--check', 'tools/verify-airport-display.mjs'] },
  { cmd: 'node', args: ['--check', 'tools/verify-form-persistence.mjs'] },
  { cmd: 'node', args: ['--check', 'tools/verify-surcharge-minimum.mjs'] },
  { cmd: 'node', args: ['tools/verify-airport-display.mjs', 'estimator.html'], retries: 1 },
  { cmd: 'node', args: ['tools/verify-airport-display.mjs', 'index.html'], retries: 1 },
  { cmd: 'node', args: ['tools/verify-form-persistence.mjs', 'estimator.html'], retries: 1 },
  { cmd: 'node', args: ['tools/verify-form-persistence.mjs', 'index.html'], retries: 1 },
  { cmd: 'node', args: ['tools/verify-surcharge-minimum.mjs', 'estimator.html'], retries: 1 },
  { cmd: 'node', args: ['tools/verify-surcharge-minimum.mjs', 'index.html'], retries: 1 },
];

function usage() {
  console.log(`Usage:
  node tools/sync-and-verify.mjs
  node tools/sync-and-verify.mjs --check-only

Default:
  1. Copy estimator.html to index.html.
  2. Confirm the two files are identical.
  3. Run all browser verification scripts against both files.

Options:
  --check-only  Do not copy; only verify estimator.html and index.html are already identical.
  --help        Show this message.`);
}

function rel(file) {
  return path.relative(root, file) || '.';
}

function ensureFile(file) {
  if (!existsSync(file)) {
    throw new Error(`Missing required file: ${rel(file)}`);
  }
}

function filesEqual(a, b) {
  return readFileSync(a).equals(readFileSync(b));
}

function runCommandOnce(cmd, cmdArgs) {
  return new Promise((resolve, reject) => {
    const label = [cmd, ...cmdArgs].join(' ');
    console.log(`\n> ${label}`);
    const child = spawn(cmd, cmdArgs, { cwd: root, stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', code => {
      if (code === 0) resolve();
      else reject(new Error(`${label} exited with code ${code}`));
    });
  });
}

async function runCommand(cmd, cmdArgs, retries = 0) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      await runCommandOnce(cmd, cmdArgs);
      return;
    } catch (error) {
      if (attempt >= retries) throw error;
      console.warn(`${error.message}; retrying once...`);
    }
  }
}

async function main() {
  if (args.has('--help') || args.has('-h')) {
    usage();
    return;
  }

  ensureFile(source);
  ensureFile(target);

  if (args.has('--check-only')) {
    if (!filesEqual(source, target)) {
      throw new Error('estimator.html and index.html differ. Run node tools/sync-and-verify.mjs to sync before committing.');
    }
    console.log('estimator.html and index.html are already identical.');
  } else {
    copyFileSync(source, target);
    console.log('Synced estimator.html -> index.html');
  }

  if (!filesEqual(source, target)) {
    throw new Error('Sync check failed: estimator.html and index.html still differ.');
  }
  console.log('Sync check passed.');

  for (const check of checks) {
    await runCommand(check.cmd, check.args, check.retries || 0);
  }

  console.log('\nAll sync and verification checks passed.');
}

main().catch(error => {
  console.error(`\n${error.message}`);
  process.exitCode = 1;
});
