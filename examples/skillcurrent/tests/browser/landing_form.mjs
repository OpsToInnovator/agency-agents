// Browser test for the beta landing page's waitlist form.
//
// Starts `skillcurrent serve` on a temporary database, opens /beta in headless
// Chromium, submits the form, and checks that the sign-up reached the
// database and that the validation path shows the right message.
//
// Run directly:   node tests/browser/landing_form.mjs
// Or via pytest:  tests/test_landing_browser.py (skips when Playwright is absent)
//
// Needs Node 18+ and the `playwright` package (npm i -g playwright, or set
// NODE_PATH to a global node_modules that has it).

import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const python = process.env.PYTHON || 'python3';
const port = 18000 + Math.floor(Math.random() * 1000);
const work = mkdtempSync(join(tmpdir(), 'skillcurrent-landing-'));
const db = join(work, 'db.sqlite');
const env = { ...process.env, PYTHONPATH: root, SKILLCURRENT_DB: db };

function cli(...args) {
  return execFileSync(python, ['-m', 'skillcurrent', ...args], { cwd: root, env, encoding: 'utf8' });
}

async function waitForServer(url, ms = 10000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    try { const r = await fetch(url); if (r.ok) return; } catch (e) { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error('server did not start');
}

function assert(cond, msg) { if (!cond) throw new Error('assertion failed: ' + msg); }

const server = spawn(python, ['-m', 'skillcurrent', 'serve', '--port', String(port)], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
let browser;
let failed = false;
try {
  cli('--json', 'init', '--team', 'acme', '--owner', 'ana');
  await waitForServer(`http://127.0.0.1:${port}/api/health`);

  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  // Hermetic: the page links Google Fonts; abort anything off-origin so the test runs offline and identically everywhere.
  await page.route('**/*', (route) => (route.request().url().startsWith(`http://127.0.0.1:${port}/`) ? route.continue() : route.abort()));

  await page.goto(`http://127.0.0.1:${port}/beta`);
  await page.waitForSelector('#beta-form');

  // Validation path: a bad email never leaves the browser.
  await page.fill('#email', 'not-an-email');
  await page.click('#submit');
  await page.waitForSelector('#status:not([hidden])');
  assert((await page.textContent('#status')).includes('work email'), 'bad email shows the email warning');
  assert(cli('--team', 'acme', '--as', 'ana', '--json', 'beta').trim() === '[]', 'nothing stored after a rejected email');

  // Happy path: the entry reaches the database through POST /api/beta.
  await page.fill('#email', 'Lead@Example.com');
  await page.selectOption('#team_size', '5-15');
  await page.check('#t1');
  await page.check('#t3');
  await page.fill('#note', 'we copy files today');
  await page.click('#submit');
  await page.waitForFunction(() => document.querySelector('#status.ok') !== null, null, { timeout: 5000 });
  assert((await page.textContent('#status')).includes("You're on the list"), 'success message shown');
  assert((await page.inputValue('#email')) === '', 'form reset after success');

  const rows = JSON.parse(cli('--team', 'acme', '--as', 'ana', '--json', 'beta'));
  assert(rows.length === 1, 'one sign-up stored');
  assert(rows[0].email === 'lead@example.com', 'email normalised');
  assert(rows[0].team_size === '5-15', 'team size stored');
  assert(JSON.stringify(rows[0].tools) === JSON.stringify(['claude-code', 'codex']), 'tools stored');
  assert(rows[0].note === 'we copy files today', 'note stored');
  assert(errors.length === 0, 'no browser errors: ' + errors.join(' | '));

  // Static host path: with no endpoint answering, the form shows the entry to send by hand.
  await page.evaluate(() => { document.getElementById('beta-form').dataset.endpoint = '/no-such-endpoint'; });
  await page.fill('#email', 'second@example.com');
  await page.selectOption('#team_size', '1-4');
  await page.click('#submit');
  await page.waitForFunction(() => document.querySelector('#status.bad') !== null, null, { timeout: 5000 });
  assert((await page.textContent('#status')).includes('second@example.com'), 'fallback shows the entry to copy');

  console.log('landing form: ok');
} catch (err) {
  failed = true;
  console.error('landing form: FAILED\n' + (err && err.stack ? err.stack : err));
} finally {
  if (browser) await browser.close();
  server.kill();
  rmSync(work, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
