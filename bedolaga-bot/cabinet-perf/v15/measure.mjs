/**
 * Measures the main-thread paint cost of the Subscription page chrome while
 * scrolling, with and without the animated conic-gradient border.
 *
 *   node measure.mjs            # headless
 *   DISPLAY=:1 node measure.mjs --headful
 */
import puppeteer from 'puppeteer-core';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const HEADFUL = process.argv.includes('--headful');
const CPU_THROTTLE = Number(process.env.CPU_THROTTLE || 6); // ~mid-range Android
const SCROLL_MS = 6000;
const PAGE = pathToFileURL(resolve('./bench.html')).href;

// Main-thread work that a per-frame paint animation adds to a scroll.
const WORK = new Set([
  'UpdateLayoutTree', // style recalc
  'Layout',
  'PrePaint',
  'Paint',
  'UpdateLayerTree',
  'Commit',
  'RasterTask',
  'CompositeLayers',
]);

async function run(fix) {
  const browser = await puppeteer.launch({
    executablePath: '/usr/local/bin/google-chrome',
    headless: HEADFUL ? false : 'new',
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--window-size=412,900',
      '--force-device-scale-factor=2.75',
      '--disable-features=CalculateNativeWinOcclusion',
    ],
  });

  try {
    const page = await browser.newPage();
    const cdp = await page.target().createCDPSession();

    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 412,
      height: 900,
      deviceScaleFactor: 2.75,
      mobile: true,
    });
    await cdp.send('Emulation.setCPUThrottlingRate', { rate: CPU_THROTTLE });

    await page.goto(`${PAGE}${fix ? '?fix=1' : ''}`, { waitUntil: 'load' });
    await new Promise((r) => setTimeout(r, 1500)); // let animations settle

    const events = [];
    cdp.on('Tracing.dataCollected', ({ value }) => events.push(...value));

    await cdp.send('Tracing.start', {
      transferMode: 'ReportEvents',
      traceConfig: {
        includedCategories: [
          'devtools.timeline',
          'disabled-by-default-devtools.timeline',
          'disabled-by-default-devtools.timeline.frame',
        ],
      },
    });

    // Repeated flick-scrolls, like a finger dragging the page.
    const started = Date.now();
    let y = 400;
    while (Date.now() - started < SCROLL_MS) {
      await cdp.send('Input.synthesizeScrollGesture', {
        x: 200,
        y: 450,
        yDistance: y,
        speed: 1200,
        gestureSourceType: 'touch',
      });
      y = -y;
    }

    const stopped = new Promise((r) => cdp.once('Tracing.tracingComplete', r));
    await cdp.send('Tracing.end');
    await stopped;

    const byName = {};
    for (const e of events) {
      if (e.ph !== 'X' || !WORK.has(e.name)) continue;
      byName[e.name] = (byName[e.name] || 0) + e.dur / 1000;
    }
    const totalMs = Object.values(byName).reduce((a, b) => a + b, 0);

    return {
      totalMs: +totalMs.toFixed(1),
      paintMs: +(byName.Paint || 0).toFixed(1),
      rasterMs: +(byName.RasterTask || 0).toFixed(1),
      styleMs: +(byName.UpdateLayoutTree || 0).toFixed(1),
    };
  } finally {
    await browser.close();
  }
}

const before = await run(false);
const after = await run(true);

const pct = (a, b) => (a === 0 ? 'n/a' : `${(((a - b) / a) * 100).toFixed(0)}% less`);

console.log(`\nSubscription scroll bench — ${CPU_THROTTLE}x CPU throttle, ${SCROLL_MS / 1000}s of scrolling`);
console.log(`headless=${!HEADFUL}\n`);
// Dropped frames are deliberately not reported: neither headless Chrome nor a
// bare Xvfb/VNC display has a real vsync deadline, so a frame counter here
// reads zero regardless. Main-thread render work is the causal quantity and
// it is measured directly.
const rows = [
  ['main-thread render work (ms)', 'totalMs'],
  ['  Paint (ms)', 'paintMs'],
  ['  RasterTask (ms)', 'rasterMs'],
  ['  Style recalc (ms)', 'styleMs'],
];
console.log('metric'.padEnd(30), 'current'.padStart(9), 'fixed'.padStart(9), '  delta');
for (const [label, key] of rows) {
  console.log(
    label.padEnd(30),
    String(before[key]).padStart(9),
    String(after[key]).padStart(9),
    ' ',
    pct(before[key], after[key]),
  );
}
console.log();
