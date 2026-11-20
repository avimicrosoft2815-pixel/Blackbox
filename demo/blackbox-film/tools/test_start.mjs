/**
 * The tour page's tests: it boots `start/index.html` and `start/start.js` in jsdom, feeds them the committed
 * `start/stats.json`, and checks the four things the page promises.
 *
 * The page's argument is that a number on it always has a source. That is a claim a test can hold: no card without a
 * source, no card for a fact the measurement does not contain, and — when the file cannot be read at all — a visible
 * failure instead of a plausible-looking zero.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { JSDOM } from "jsdom";

const root = new URL("../", import.meta.url);

async function boot({ stats = true, statsBody = null } = {}) {
  const html = await readFile(new URL("start/index.html", root), "utf8");
  const withoutScripts = html.replace(/<script[\s\S]*?<\/script>/g, "");
  const dom = new JSDOM(withoutScripts, { url: "http://127.0.0.1:8181/tour", pretendToBeVisual: true });
  const { window } = dom;
  const calls = [];

  const committed = statsBody ?? (stats ? await readFile(new URL("start/stats.json", root), "utf8") : null);
  const fetchStub = async (url) => {
    calls.push(String(url));
    if (!committed) return { ok: false, status: 404, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => JSON.parse(committed) };
  };

  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    fetch: globalThis.fetch,
    IntersectionObserver: globalThis.IntersectionObserver,
  };
  globalThis.window = window;
  globalThis.document = window.document;
  globalThis.fetch = fetchStub;
  // jsdom has no IntersectionObserver; the page must reveal without it (it has a branch for exactly that).
  delete globalThis.IntersectionObserver;

  await import(new URL("start/start.js", root).href + `?t=${Math.random()}`);
  await new Promise((resolve) => setTimeout(resolve, 60));

  return {
    window,
    document: window.document,
    calls,
    restore() {
      Object.entries(previous).forEach(([key, value]) => {
        if (value === undefined) delete globalThis[key];
        else globalThis[key] = value;
      });
      window.close();
    },
  };
}

test("the tour renders every number it names, each with its source", async () => {
  const app = await boot();
  try {
    const stats = JSON.parse(await readFile(new URL("start/stats.json", root), "utf8"));
    assert.equal(stats.schema, "blackbox.demo-stats/1");
    assert.ok(stats.commit, "the measurement names the commit it was taken at");
    assert.ok(stats.notRun.length >= 3, "the page lists what could not be measured here");

    const cards = [...app.document.querySelectorAll("#facts .fact")];
    assert.ok(cards.length >= 12, `expected a dozen cards, found ${cards.length}`);
    const missing = cards.filter((card) => card.classList.contains("missing"));
    assert.deepEqual(missing.map((card) => card.textContent), [], "every card the page asks for is in stats.json");

    for (const card of cards) {
      const source = card.querySelector(".fact-source").textContent.trim();
      assert.ok(source.length > 0, `a card without a source: ${card.textContent}`);
      const value = card.querySelector(".fact-value").textContent.trim();
      assert.match(value, /^-?\d+(?:\.\d+)?$/, `a card's value should be a number, found ${value}`);
    }

    assert.match(app.document.getElementById("stats-stamp").textContent, /measured at commit/);
    assert.equal(app.document.querySelectorAll("#suites li").length, stats.suites.length);
    assert.equal(app.document.querySelectorAll("#not-run li").length, stats.notRun.length);
  } finally {
    app.restore();
  }
});

test("a missing measurement is said out loud, not rendered as zero", async () => {
  const app = await boot({ stats: false });
  try {
    const stamp = app.document.getElementById("stats-stamp");
    assert.match(stamp.textContent, /could not be read/);
    assert.equal(app.document.querySelectorAll("#facts .fact").length, 0, "no cards are invented");
  } finally {
    app.restore();
  }
});

test("every command block copies, and says when it cannot", async () => {
  const app = await boot();
  try {
    const buttons = [...app.document.querySelectorAll("[data-copy]")];
    assert.equal(buttons.length, 5, "five steps, five copy buttons");
    for (const button of buttons) {
      const block = button.closest(".step").querySelector("pre code");
      assert.ok(block.textContent.includes("blackbox") || block.textContent.includes("git clone"),
        "each block holds the command it claims to");
      button.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
    }
    await new Promise((resolve) => setTimeout(resolve, 40));
    // jsdom offers neither a clipboard nor execCommand, so the honest answer is the blocked one.
    assert.ok(buttons.every((button) => /copy blocked/.test(button.textContent)));
  } finally {
    app.restore();
  }
});

test("the sound design is off until it is asked for", async () => {
  const app = await boot();
  try {
    const button = app.document.getElementById("sound");
    assert.equal(button.getAttribute("aria-pressed"), "false");
    assert.equal(button.textContent, "sound off");
    button.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
    assert.equal(button.getAttribute("aria-pressed"), "true");
    assert.equal(button.textContent, "sound on");
  } finally {
    app.restore();
  }
});

test("every link and asset the page points at exists on disk", async () => {
  const html = await readFile(new URL("start/index.html", root), "utf8");
  const locals = [...html.matchAll(/(?:href|src|poster)="([^"]+)"/g)]
    .map((match) => match[1])
    .filter((href) => !/^(https?:|data:|#)/.test(href));
  assert.ok(locals.length >= 8, `expected the page to point at its own files, found ${locals.length}`);
  // Resolved against the page itself, so `../export/…` is checked where the browser would look for it.
  const page = new URL("start/index.html", root);
  for (const href of locals) {
    const path = new URL(href, page);
    const info = await stat(path).catch(() => null);
    assert.ok(info, `missing file for ${href}`);
  }
});
