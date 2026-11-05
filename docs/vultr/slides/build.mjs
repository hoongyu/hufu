// Build the submission slides: index.html -> out/vultr/hufu-slides.pdf, the 16:9 cover image, and one
// PNG per slide for checking. Zero dependencies: drives headless Edge (or Chrome) over the DevTools protocol.
//   node docs/vultr/slides/build.mjs
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");
const out = join(root, "out", "vultr");
mkdirSync(join(out, "slides"), { recursive: true });

const browsers = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
];
const exe = browsers.find((b) => existsSync(b));
if (!exe) throw new Error("no Edge or Chrome found");
const port = 9400 + Math.floor(Math.random() * 400);
const proc = spawn(exe, ["--headless=new", "--disable-gpu", "--hide-scrollbars", `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), "slides-"))}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let targets = [];
for (let i = 0; i < 50 && !targets.length; i++) {
  try { targets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).filter((t) => t.type === "page"); } catch { /* starting */ }
  await sleep(200);
}
const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0;
const pending = new Map();
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async (expression) => (await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true })).result.result.value;

await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: pathToFileURL(join(here, "index.html")).href });
await sleep(1500);
const ready = await evaluate(`(async () => {
  await Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 8000))]);
  await Promise.all([...document.images].map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; })));
  return { fonts: [...document.fonts].filter(f => f.status === "loaded").map(f => f.family).filter((v, i, a) => a.indexOf(v) === i),
           slides: document.querySelectorAll(".slide").length,
           broken: [...document.images].filter(i => !i.naturalWidth).map(i => i.getAttribute("src")) };
})()`);
console.log("fonts loaded:", ready.fonts.join(", ") || "none (system fallback)", "| slides:", ready.slides, "| broken images:", ready.broken.length);

const pdf = await send("Page.printToPDF", { preferCSSPageSize: true, printBackground: true, marginTop: 0, marginBottom: 0, marginLeft: 0, marginRight: 0 });
writeFileSync(join(out, "hufu-slides.pdf"), Buffer.from(pdf.result.data, "base64"));
console.log("wrote", join(out, "hufu-slides.pdf"));

// one PNG per slide (2x) for checking, and slide 1 as the 16:9 cover image
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 900, deviceScaleFactor: 2, mobile: false });
for (let i = 0; i < ready.slides; i++) {
  const box = await evaluate(`(() => { const r = document.querySelectorAll(".slide")[${i}].getBoundingClientRect();
    return { x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height }; })()`);
  const shot = await send("Page.captureScreenshot", { format: "png", clip: { ...box, scale: 1 }, captureBeyondViewport: true });
  const png = Buffer.from(shot.result.data, "base64");
  writeFileSync(join(out, "slides", `${String(i + 1).padStart(2, "0")}.png`), png);
  if (i === 0) writeFileSync(join(out, "hufu-cover.png"), png);
}
console.log("wrote", ready.slides, "slide PNGs and hufu-cover.png");
ws.close();
proc.kill();
process.exit(0);
