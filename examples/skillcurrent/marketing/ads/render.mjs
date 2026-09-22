// Render every .board in creatives.html to out/<id>.png at exact pixel size.
// Needs Node 18+ and the `playwright` package (npm i -g playwright, or NODE_PATH
// pointing at a global node_modules that has it).
//
//   node marketing/ads/render.mjs            # 1x
//   node marketing/ads/render.mjs --scale 2  # 2x for retina placements

import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { mkdirSync } from 'node:fs';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const here = dirname(fileURLToPath(import.meta.url));
const scale = Number((process.argv.find((a) => a.startsWith('--scale=')) || '--scale=1').split('=')[1]) || (process.argv.includes('--scale') ? Number(process.argv[process.argv.indexOf('--scale') + 1]) : 1);
const out = join(here, 'out');
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 2100 }, deviceScaleFactor: scale });
await page.goto(pathToFileURL(resolve(here, 'creatives.html')).href);
// Let web fonts settle when the machine can reach Google Fonts; fall back silently otherwise.
await page.evaluate(() => (document.fonts && document.fonts.ready) || null).catch(() => null);
await page.waitForTimeout(600);

const ids = await page.$$eval('.board', (els) => els.map((e) => e.id));
for (const id of ids) {
  const el = await page.$('#' + id);
  const path = join(out, id + '.png');
  await el.screenshot({ path, type: 'png' });
  console.log('wrote', path);
}
await browser.close();
