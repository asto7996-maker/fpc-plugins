/**
 * Loads a patched cabinet's real CSS bundle plus the injected v15 style block
 * and asserts the override wins on touch/mobile and stays off on desktop.
 *
 *   node verify.mjs /srv/cabinet
 */
import puppeteer from 'puppeteer-core';
import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';

const ROOT = process.argv[2] || '/srv/cabinet';
const TYPES = { '.css': 'text/css', '.js': 'text/javascript', '.html': 'text/html' };

// Extract exactly what the deploy script injected into index.html.
const indexHtml = readFileSync(join(ROOT, 'index.html'), 'utf8');
const styleBlock = indexHtml.match(
  /<style id="bedolaga-scroll-fix-v15">[\s\S]*?<\/style>/,
)?.[0];
if (!styleBlock) throw new Error('v15 style block not found in index.html');
const cssHref = indexHtml.match(/<link rel="stylesheet"[^>]*href="([^"]+)"/)[1];

const probe = `<!doctype html><html class="dark"><head>
<link rel="stylesheet" href="${cssHref}">
${styleBlock}
</head><body>
<button class="hover-border-gradient" style="width:320px;height:64px">cta</button>
<div class="animate-unlimited-flow" style="width:200px;height:8px"></div>
</body></html>`;

const server = http.createServer((req, res) => {
  if (req.url === '/probe.html') {
    res.writeHead(200, { 'content-type': 'text/html' }).end(probe);
    return;
  }
  const file = join(ROOT, normalize(req.url.split('?')[0]));
  if (!file.startsWith(ROOT) || !existsSync(file)) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
  res.end(readFileSync(file));
});
await new Promise((r) => server.listen(0, r));
const url = `http://127.0.0.1:${server.address().port}/probe.html`;

const browser = await puppeteer.launch({
  executablePath: '/usr/local/bin/google-chrome',
  headless: 'new',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});

async function read(viewport, mobile) {
  const page = await browser.newPage();
  await page.setViewport({ ...viewport, hasTouch: mobile, isMobile: mobile });
  // Headless Chrome reports `hover: none` by default because it has no
  // pointer device, so the media features have to be stated explicitly.
  // puppeteer's whitelist rejects `hover`, so go through CDP directly.
  const cdp = await page.createCDPSession();
  await cdp.send('Emulation.setEmulatedMedia', {
    features: [
      { name: 'hover', value: mobile ? 'none' : 'hover' },
      { name: 'pointer', value: mobile ? 'coarse' : 'fine' },
    ],
  });
  await page.goto(url, { waitUntil: 'load' });
  const out = await page.evaluate(() => {
    const cta = getComputedStyle(document.querySelector('.hover-border-gradient'));
    const bar = getComputedStyle(document.querySelector('.animate-unlimited-flow'));
    return {
      ctaAnimation: cta.animationName,
      borderAngle: cta.getPropertyValue('--border-angle').trim(),
      hasConicBorder: cta.backgroundImage.includes('conic-gradient'),
      barAnimation: bar.animationName,
      hoverCapable: matchMedia('(hover: hover)').matches,
    };
  });
  await page.close();
  return out;
}

const phone = await read({ width: 412, height: 900, deviceScaleFactor: 2.75 }, true);
const desktop = await read({ width: 1440, height: 900, deviceScaleFactor: 1 }, false);

await browser.close();
server.close();

console.log('phone  ', phone);
console.log('desktop', desktop);

const checks = [
  ['phone: gradient rotation stopped', phone.ctaAnimation === 'none'],
  ['phone: angle pinned to 135deg', phone.borderAngle === '135deg'],
  ['phone: conic border still painted', phone.hasConicBorder],
  ['phone: unlimited-flow stopped', phone.barAnimation === 'none'],
];

// Chrome only reports `hover: hover` when the OS actually exposes a pointer
// device. Headless has none, and neither does a bare Xvfb/VNC display, so the
// desktop half of the media query cannot be exercised here.
if (desktop.hoverCapable) {
  checks.push(
    ['desktop: rotation untouched', desktop.ctaAnimation === 'border-rotate'],
    ['desktop: unlimited-flow untouched', desktop.barAnimation === 'unlimitedFlow'],
  );
} else {
  console.log('\nSKIP  desktop assertions — this environment reports hover:none');
}

let failed = 0;
for (const [label, pass] of checks) {
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${label}`);
  if (!pass) failed++;
}
process.exit(failed ? 1 : 0);
