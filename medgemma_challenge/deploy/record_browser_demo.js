import fs from "fs";
import path from "path";
import { chromium } from "playwright";

const urlArg = process.argv[2];
if (!urlArg) {
  console.error("Usage: node record_browser_demo.js <LIVE_URL>");
  process.exit(1);
}
const liveUrl = urlArg.replace(/\/+$/, "");

const outputDir = path.resolve("videos");
fs.mkdirSync(outputDir, { recursive: true });

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function run() {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: {
      dir: outputDir,
      size: { width: 1440, height: 900 }
    }
  });

  const page = await context.newPage();
  await page.goto(liveUrl, { waitUntil: "networkidle", timeout: 120000 });
  await delay(1500);

  await page.selectOption("#target_language", "spanish");
  await delay(300);
  await page.click("#submit-btn");
  await delay(7000);
  await page.evaluate(() => {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  });
  await delay(2500);
  await page.evaluate(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  await delay(2000);

  const video = page.video();
  await context.close();
  await browser.close();

  const videoPath = await video.path();
  console.log(`Recorded video: ${videoPath}`);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
