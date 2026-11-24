// Takes the README screenshots from a running demo instance (see scripts/demo_smoke.sh).
//
//   ORBIT_BASE_URL=http://127.0.0.1:8765 ORBIT_DEMO_PASSWORD=... \
//   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node scripts/screenshots/capture.mjs [outDir]
//
// The instance must be seeded (`python -m orbit.cli seed-demo`) and run with ORBIT_CLAUDE_MODE=fake.
// Playwright is not a dependency of this repository: point PLAYWRIGHT_MODULE at an install, or
// leave it unset to use `playwright` from node_modules.
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const base = (process.env.ORBIT_BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const password = process.env.ORBIT_DEMO_PASSWORD;
if (!password) {
  console.error("Set ORBIT_DEMO_PASSWORD to the demo instance's web password.");
  process.exit(1);
}
const outDir = process.argv[2] || "docs/images";
mkdirSync(outDir, { recursive: true });

const SIZES = {
  mobile: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true },
  desktop: { viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 },
};

const browser = await chromium.launch();
try {
  for (const [name, options] of Object.entries(SIZES)) {
    const context = await browser.newContext({ ...options, colorScheme: "light" });
    const page = await context.newPage();
    await page.goto(`${base}/login`);
    await page.fill('input[name="password"]', password);
    await Promise.all([page.waitForURL((url) => !url.pathname.startsWith("/login")), page.click('button[type="submit"]')]);

    const shot = async (file, url, { full = false } = {}) => {
      await page.goto(`${base}${url}`);
      await page.waitForLoadState("networkidle");
      await page.screenshot({ path: path.join(outDir, `${file}-${name}.png`), fullPage: full });
      console.log(`${file}-${name}`);
    };
    const firstHref = async (pattern) => {
      const hrefs = await page.$$eval("a", (links) => links.map((a) => a.getAttribute("href") || ""));
      return hrefs.find((href) => pattern.test(href));
    };

    await shot("today", "/");
    await shot("people", "/people");
    await page.goto(`${base}/people`);
    const toma = await page.locator('a:has-text("Tomas Dvorak")').first().getAttribute("href");
    await shot("person", toma);
    await shot("ledger", "/ledger");
    await shot("inbox", "/inbox");
    await page.goto(`${base}/reviews`);
    const review = await firstHref(/^\/reviews\/\d+$/);
    await shot("weekly-review", review);
    await shot("claude-usage", "/ops/claude");
    await context.close();
  }
} finally {
  await browser.close();
}
