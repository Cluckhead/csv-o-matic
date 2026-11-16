#!/usr/bin/env node
/*
  Check an explainer page in a real browser, once, before delivery.

    node check_page.js page.html [--out DIR] [--widths 320,1440,2560]

  Loads the page at each width in Light and Dark, and reports:
    - console errors, uncaught exceptions, and any network request (a standalone file needs none)
    - horizontal page overflow and the elements causing it
    - whether the embedded IBM Plex faces are in use
    - that the page opens in Light even when the device prefers dark
    - interactive targets smaller than 44 px (prose links excepted)
  At the middle width it also exercises the models: every range input to its min and max,
  every preset and prose trigger, the theme switch (slider positions must survive it), and the
  print handler (all <details> open before printing, restored after). It loads the page once
  with JavaScript disabled to confirm the headline and figure summaries still read.
  Writes screenshots to DIR (default: ./page-check). Exit code 1 means something needs fixing.

  Needs Playwright with Chromium. If `require("playwright")` fails, install it in the
  working folder (npm i playwright && npx playwright install chromium) or set PLAYWRIGHT_CHROMIUM
  to a Chromium executable.
*/
"use strict";
const path = require("path");
const fs = require("fs");

// Find Playwright: the local project, then global module folders (npm root -g, NODE_PATH, common installs).
let chromium;
(function findPlaywright() {
  const { execSync } = require("child_process");
  const tries = ["playwright"];
  try { tries.push(path.join(execSync("npm root -g", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(), "playwright")); } catch (e) { /* no npm */ }
  (process.env.NODE_PATH || "").split(path.delimiter).filter(Boolean).forEach((d) => tries.push(path.join(d, "playwright")));
  ["/opt/node22/lib/node_modules", "/usr/local/lib/node_modules", "/usr/lib/node_modules"].forEach((d) => tries.push(path.join(d, "playwright")));
  for (const t of tries) { try { ({ chromium } = require(t)); return; } catch (e) { /* next */ } }
  console.error("Playwright is not available. Install it (npm i playwright) or set NODE_PATH to where it is installed.");
  process.exit(2);
})();

const args = process.argv.slice(2);
if (!args.length || args[0].startsWith("--")) { console.error("usage: node check_page.js page.html [--out DIR] [--widths 320,1440,2560]"); process.exit(2); }
const page = path.resolve(args[0]);
const opt = (name, dflt) => { const i = args.indexOf(name); return i > 0 ? args[i + 1] : dflt; };
const out = path.resolve(opt("--out", "page-check"));
const widths = opt("--widths", "320,1440,2560").split(",").map(Number);
fs.mkdirSync(out, { recursive: true });
const url = "file://" + page;

const problems = [], warnings = [], notes = [];
const fail = (m) => problems.push(m), warn = (m) => warnings.push(m), note = (m) => notes.push(m);

async function launch() {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM;
  try { return await chromium.launch(executablePath ? { executablePath } : {}); }
  catch (e) {
    if (fs.existsSync("/opt/pw-browsers/chromium")) return chromium.launch({ executablePath: "/opt/pw-browsers/chromium" });
    throw e;
  }
}

function watch(p, label) {
  p.on("console", (m) => { if (m.type() === "error") fail(`${label}: console error: ${m.text()}`); });
  p.on("pageerror", (e) => fail(`${label}: uncaught exception: ${e.message}`));
  p.on("request", (r) => { const u = r.url(); if (/^https?:/.test(u)) fail(`${label}: network request to ${u} (a standalone file should load nothing)`); });
}

const layoutProbe = () => {
  const doc = document.documentElement, vw = doc.clientWidth;
  const scrollers = (el) => { for (let n = el.parentElement; n; n = n.parentElement) { const s = getComputedStyle(n); if (/(auto|scroll)/.test(s.overflowX)) return true; } return false; };
  const wide = [];
  document.querySelectorAll("body *").forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > vw + 1 && !scrollers(el) && getComputedStyle(el).position !== "fixed") wide.push(el.tagName.toLowerCase() + (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : ""));
  });
  const clipped = [];
  document.querySelectorAll(".metric, .pill, .chip, .tag, .kicker, h1, h2, h3, th, td, label, output").forEach((el) => {
    const s = getComputedStyle(el);
    if ((s.overflowX === "hidden" || s.overflowX === "clip" || s.textOverflow === "ellipsis") && el.scrollWidth > el.clientWidth + 1) clipped.push(el.tagName.toLowerCase() + ": " + el.textContent.trim().slice(0, 40));
  });
  const small = [];
  document.querySelectorAll("button, input, select, [role=button], [role=slider], nav a, summary").forEach((el) => {
    if (el.closest(".prose") && el.tagName === "A") return;
    if (el.matches("button.trigger")) return; // inline prose triggers are padded text; checked by eye
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    if (r.width < 44 && r.height < 44) small.push((el.getAttribute("aria-label") || el.textContent || el.tagName).trim().slice(0, 30));
  });
  return {
    overflow: doc.scrollWidth - vw, wide: [...new Set(wide)].slice(0, 8), clipped: clipped.slice(0, 8), small: [...new Set(small)].slice(0, 10),
    plexSans: document.fonts.check('16px "IBM Plex Sans"'), plexMono: document.fonts.check('16px "IBM Plex Mono"'),
    faces: [...document.fonts].filter((f) => /IBM Plex/.test(f.family)).map((f) => f.family + " " + f.weight + " " + f.status),
    theme: doc.getAttribute("data-theme"), height: doc.scrollHeight
  };
};

(async () => {
  const browser = await launch();

  // 1. Layout at each width, in both themes. The device prefers dark: the page must still open Light.
  for (const w of widths) {
    const ctx = await browser.newContext({ viewport: { width: w, height: w < 760 ? 760 : Math.round(w * 0.5625) }, colorScheme: "dark", deviceScaleFactor: 1 });
    const p = await ctx.newPage(); watch(p, `${w}px`);
    await p.goto(url); await p.waitForTimeout(500);
    for (const theme of ["light", "dark"]) {
      if (theme === "dark") {
        const sw = await p.$('[data-theme-set="dark"]');
        if (sw) { await sw.click(); await p.waitForTimeout(300); } else { warn("no Light/Dark switch found"); continue; }
      }
      const r = await p.evaluate(layoutProbe);
      const label = `${w}px ${theme}`;
      if (theme === "light" && r.theme !== "light") fail(`${label}: page did not open in Light (data-theme=${r.theme}) with a dark device setting`);
      if (r.overflow > 0) fail(`${label}: page scrolls sideways by ${r.overflow}px; widest offenders: ${r.wide.join(", ")}`);
      if (r.clipped.length) warn(`${label}: clipped text: ${r.clipped.join(" | ")}`);
      if (theme === "light" && w === widths[0] && r.small.length) warn(`${label}: targets under 44px: ${r.small.join(", ")}`);
      if (theme === "light" && w === widths[0]) {
        if (!r.plexSans || !r.plexMono) warn(`IBM Plex not in use (sans ${r.plexSans}, mono ${r.plexMono}); run embed_fonts.py`);
        note(`fonts: ${r.faces.join("; ") || "none embedded"}`);
      }
      if (theme === "light") note(`${w}px: page height ${r.height}px`);
      await p.screenshot({ path: path.join(out, `${w}-${theme}-top.png`) });
      await p.evaluate(() => { const s = document.querySelector(".workbench, .stage, figure"); if (s) s.scrollIntoView({ block: "start", behavior: "instant" }); });
      await p.waitForTimeout(250);
      await p.screenshot({ path: path.join(out, `${w}-${theme}-figure.png`) });
      await p.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    }
    await ctx.close();
  }

  // 2. Interaction at the middle width.
  const mid = widths[Math.floor(widths.length / 2)];
  const ctx = await browser.newContext({ viewport: { width: mid, height: 900 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage(); watch(p, "interaction");
  await p.goto(url); await p.waitForTimeout(500);
  const live = () => p.evaluate(() => [...document.querySelectorAll("[aria-live]")].map((n) => n.textContent.trim()).join(" || "));
  const ranges = await p.$$eval('input[type="range"]', (els) => els.map((e) => ({ id: e.id, min: e.min, max: e.max, value: e.value, step: e.step })));
  if (!ranges.length) note("no range inputs found");
  for (const r of ranges) {
    if (!r.id) { warn("a range input has no id, so it cannot be labelled or preserved across republishes"); continue; }
    const before = await live();
    const results = [];
    for (const v of [r.min, r.max]) {
      await p.evaluate(({ id, v }) => { const e = document.getElementById(id); e.value = v; e.dispatchEvent(new Event("input", { bubbles: true })); }, { id: r.id, v });
      await p.waitForTimeout(120);
      results.push(await live());
    }
    const isTime = /time|hour|day|date/i.test(r.id);
    if (!isTime && results.every((x) => x === before)) warn(`range #${r.id}: moving it to min and max did not change any aria-live readout`);
    await p.evaluate(({ id, v }) => { const e = document.getElementById(id); e.value = v; e.dispatchEvent(new Event("input", { bubbles: true })); }, { id: r.id, v: r.value });
  }
  const presets = await p.$$("button[data-preset]");
  for (const b of presets) {
    if (!(await b.isVisible())) continue;
    await b.click(); await p.waitForTimeout(120);
    const pressed = await b.getAttribute("aria-pressed");
    if (pressed !== "true") warn(`preset "${(await b.textContent()).trim()}" did not report aria-pressed=true after clicking`);
  }
  // Prediction prompts: answering must reveal the explanation.
  const predicts = await p.$$(".predict");
  for (const box of predicts) {
    const choice = await box.$("[data-choice]");
    if (!choice) { warn("a .predict prompt has no [data-choice] buttons"); continue; }
    await choice.click(); await p.waitForTimeout(120);
    const shown = await box.$eval("[data-reveal]", (e) => getComputedStyle(e).display !== "none").catch(() => false);
    if (!shown) fail("answering a prediction prompt did not reveal its explanation ([data-reveal])");
  }
  note(`exercised ${ranges.length} range input(s), ${presets.length} preset/trigger button(s) and ${predicts.length} prediction prompt(s)`);

  // Theme switch must not reset model state.
  if (ranges.length && (await p.$('[data-theme-set="dark"]'))) {
    const first = ranges.find((r) => !/time/i.test(r.id)) || ranges[0];
    const st = Number(first.step) > 0 ? Number(first.step) : 1, lo = Number(first.min);
    let target = lo + Math.round(((Number(first.max) - lo) / 3) / st) * st;   // a value the slider can actually hold
    target = String(Number(target.toFixed(6)));
    await p.evaluate(({ id, v }) => { const e = document.getElementById(id); e.value = v; e.dispatchEvent(new Event("input", { bubbles: true })); }, { id: first.id, v: target });
    await p.waitForTimeout(100);
    const liveBefore = await live();
    await p.click('[data-theme-set="dark"]'); await p.waitForTimeout(200);
    await p.click('[data-theme-set="light"]'); await p.waitForTimeout(200);
    const after = await p.$eval("#" + first.id, (e) => e.value);
    if (after !== target) fail(`theme switch reset #${first.id} from ${target} to ${after}`);
    if ((await live()) !== liveBefore) fail("theme switch changed the model readout");
  }

  // Print: every <details> opens before printing and returns to its state afterwards.
  const printCheck = await p.evaluate(() => {
    const ds = [...document.querySelectorAll("details")];
    const before = ds.map((d) => d.open);
    window.dispatchEvent(new Event("beforeprint"));
    const during = ds.every((d) => d.open);
    window.dispatchEvent(new Event("afterprint"));
    const after = ds.map((d) => d.open);
    return { count: ds.length, during, restored: JSON.stringify(before) === JSON.stringify(after) };
  });
  if (printCheck.count && !printCheck.during) fail("not every <details> opens before printing");
  if (printCheck.count && !printCheck.restored) fail("<details> states are not restored after printing");
  await p.emulateMedia({ media: "print" });
  await p.screenshot({ path: path.join(out, `print-preview.png`) });
  const chromeHidden = await p.evaluate(() => { const a = document.querySelector(".appbar"); return !a || getComputedStyle(a).display === "none"; });
  if (!chromeHidden) warn("the application bar still shows in print");
  await ctx.close();

  // 3. JavaScript disabled: the headline and figure summaries must still read.
  const nctx = await browser.newContext({ viewport: { width: mid, height: 900 }, javaScriptEnabled: false });
  const np = await nctx.newPage();
  await np.goto(url); await np.waitForTimeout(300);
  const nojs = await np.evaluate(() => ({
    h1: (document.querySelector("h1") || {}).textContent || "",
    summaries: document.querySelectorAll(".fig-summary").length,
    figures: document.querySelectorAll(".stage, figure").length
  }));
  if (!nojs.h1.trim()) fail("with JavaScript disabled there is no visible h1");
  if (nojs.figures && !nojs.summaries) warn("figures have no .fig-summary text account for readers without JavaScript");
  await np.screenshot({ path: path.join(out, `no-js.png`) });
  await nctx.close();

  await browser.close();
  const report = { page, problems, warnings, notes, screenshots: out };
  fs.writeFileSync(path.join(out, "report.json"), JSON.stringify(report, null, 2));
  console.log(problems.length ? `PROBLEMS (${problems.length})` : "No problems found.");
  problems.forEach((m) => console.log("  ✗ " + m));
  if (warnings.length) console.log(`Warnings (${warnings.length})`);
  warnings.forEach((m) => console.log("  ! " + m));
  notes.forEach((m) => console.log("  · " + m));
  console.log("Screenshots: " + out);
  process.exit(problems.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
