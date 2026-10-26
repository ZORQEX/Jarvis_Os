// Live check of «Архитектура» against a dev core: drop a plan into the workspace, wait for the 3D model, run a
// command in words, pick a wall in the 2D plan, switch the 3D views, export. Screenshots go to <outDir>.
// Usage: node architect.mjs <port> <tokenFile> <outDir> <plan.png> ["команда" …]
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile, out, plan, ...cmds] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const log = [];
page.on("console", (m) => log.push(`[${m.type()}] ${m.text()}`));
page.on("pageerror", (e) => log.push(`[pageerror] ${e.message}`));
const t0 = Date.now();
const T = () => ((Date.now() - t0) / 1000).toFixed(1) + "s";
await page.goto(`http://127.0.0.1:${port}/?t=${token}`);
await page.waitForSelector("#nav");
await page.keyboard.press("Control+0");
await page.waitForSelector('[data-ws="architect"]:not([hidden])');
await page.screenshot({ path: `${out}/01-empty.png` });
if (plan) {
  await page.setInputFiles("#apFile", plan);
  console.log(T(), "uploaded");
  await page.waitForFunction(() => {
    const s = document.querySelectorAll(".ap-stage");
    return s.length && [...s].some((x) => (x.dataset.state === "done" && x.querySelector(".l").textContent.startsWith("3D"))
      || x.dataset.state === "error") || document.querySelector(".ap-q:not([hidden])");
  }, null, { timeout: 300000 });
  console.log(T(), "built or asked");
  await page.waitForSelector(".ap-view canvas", { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${out}/02-built.png` });
}
let i = 3;
for (const c of cmds) {
  const before = await page.evaluate(() => document.querySelector(".ap-sum")?.textContent || "");
  await page.fill("#apCmdIn", c);
  await page.press("#apCmdIn", "Enter");
  await page.waitForFunction((b) => (document.querySelector(".ap-sum")?.textContent || "") !== b || document.querySelector(".ap-q:not([hidden])"),
    before, { timeout: 180000 }).catch(() => console.log("no change seen for", c));
  await page.waitForTimeout(2500);
  console.log(T(), "command:", c, "→", await page.evaluate(() => document.querySelector(".ap-sum")?.textContent?.trim().split("\n")[0]));
  await page.screenshot({ path: `${out}/0${i++}-cmd.png` });
}
const wall = await page.$(".ap-svg .ap-wall");
if (wall) {
  await wall.click();
  await page.waitForTimeout(800);
  console.log(T(), "selected:", await page.evaluate(() => document.querySelector(".ap-insp-h b")?.textContent));
  await page.screenshot({ path: `${out}/0${i++}-select.png` });
}
for (const m of ["top", "walk"]) {
  await page.click(`[data-mode="${m}"]`);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${out}/0${i++}-${m}.png` });
}
fs.writeFileSync(`${out}/console.log`, log.join("\n"));
console.log(T(), "console errors:", log.filter((l) => l.startsWith("[error]") || l.startsWith("[pageerror]")).length);
await browser.close();
