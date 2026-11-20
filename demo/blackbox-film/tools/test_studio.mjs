/**
 * The studio's tests: it boots the real `index.html` and the real `js/app.js` in jsdom, with a stand-in 2D context, and
 * drives the controls the way a person would. No screenshot comparison — the point is behaviour: does the stage paint,
 * does a scene change move the caption, does a validated edit survive a reload, does a corrupt draft fall back to the
 * film instead of white-screening the page.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { JSDOM } from "jsdom";

const root = new URL("../", import.meta.url);

/** A canvas context that records nothing and answers the two questions the renderer actually asks of it. */
function makeContext() {
  const gradients = { addColorStop() {} };
  const base = {
    font: "400 16px InterRegular",
    fillStyle: "#000",
    strokeStyle: "#000",
    lineWidth: 1,
    globalAlpha: 1,
    textAlign: "left",
    textBaseline: "alphabetic",
    measureText(text) {
      const size = Number((/(\d+(?:\.\d+)?)px/.exec(this.font) || [0, 16])[1]);
      return { width: String(text).length * size * 0.52 };
    },
    createLinearGradient: () => gradients,
    createRadialGradient: () => gradients,
    createPattern: () => null,
    getImageData: () => ({ data: new Uint8ClampedArray(4) }),
    setLineDash() {},
    save() {},
    restore() {},
  };
  return new Proxy(base, {
    get(target, prop) {
      if (prop in target) return target[prop];
      return () => undefined;
    },
    set(target, prop, value) {
      target[prop] = value;
      return true;
    },
  });
}

async function boot({ hash = "", stored = null, seedStorage = true } = {}) {
  const html = await readFile(new URL("studio/index.html", root), "utf8");
  const withoutScripts = html.replace(/<script[\s\S]*?<\/script>/g, "");
  const dom = new JSDOM(withoutScripts, {
    url: `http://127.0.0.1:8181/studio/${hash}`,
    pretendToBeVisual: true,
  });
  const { window } = dom;

  if (seedStorage) {
    if (stored === "__CORRUPT__") window.localStorage.setItem("blackbox.studio.blueprint.v1", "{not json");
    else if (stored) window.localStorage.setItem("blackbox.studio.blueprint.v1", stored);
  }

  const context = makeContext();
  window.HTMLCanvasElement.prototype.getContext = () => context;

  const specText = await readFile(new URL("studio/spec.json", root), "utf8");
  const calls = [];
  const fetchStub = async (url) => {
    calls.push(String(url));
    if (String(url).includes("spec.json")) {
      return { ok: true, status: 200, json: async () => JSON.parse(specText) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };

  // The module reads these as globals; give it the window's, and a fetch that serves spec.json from disk.
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    fetch: globalThis.fetch,
    requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame,
    localStorage: globalThis.localStorage,
    HTMLElement: globalThis.HTMLElement,
  };
  globalThis.window = window;
  globalThis.document = window.document;
  globalThis.fetch = fetchStub;
  globalThis.requestAnimationFrame = (fn) => window.setTimeout(() => fn(window.performance.now()), 16);
  globalThis.cancelAnimationFrame = (handle) => window.clearTimeout(handle);
  globalThis.localStorage = window.localStorage;
  globalThis.HTMLElement = window.HTMLElement;

  await import(new URL("studio/js/app.js", root).href + `?t=${Math.random()}`);
  // Let the async boot() finish: fetch → fonts → build → paint.
  await new Promise((resolve) => setTimeout(resolve, 60));

  return {
    window,
    document: window.document,
    context,
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

test("the studio boots and paints seven scenes", async () => {
  const app = await boot();
  try {
    const tabs = app.document.querySelectorAll(".scene-tab");
    assert.equal(tabs.length, 7, "seven scene tabs");
    assert.equal(app.document.querySelectorAll(".segment").length, 7, "seven timeline segments");
    assert.equal(app.document.querySelectorAll(".transcript li").length, 7, "seven transcript entries");
    assert.match(app.document.getElementById("meta-filmlength").textContent, /55\.0 s/);
    assert.equal(app.document.getElementById("stage-badge").textContent, "PAUSED");
    assert.ok(app.calls.some((url) => url.includes("spec.json")), "the blueprint is fetched, not hardcoded");

    // The page and the runtime are two hand-written files; every id the runtime reaches for has to be in the page. This
    // is the check that catches a rename on one side of the rewrite while the other side still looks finished.
    const source = await readFile(new URL("studio/js/app.js", root), "utf8");
    const ids = [...new Set([...source.matchAll(/\bel\("([a-z0-9-]+)"\)/g)].map((match) => match[1]))];
    assert.ok(ids.length > 25, `the runtime names its controls, found ${ids.length}`);
    const missing = ids.filter((id) => !app.document.getElementById(id));
    assert.deepEqual(missing, [], `ids the runtime reaches for that the page does not have: ${missing.join(", ")}`);
  } finally {
    app.restore();
  }
});

test("a scene change moves the caption, the chip and the deep link", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    const next = document.getElementById("btn-next");
    next.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 40));

    assert.equal(document.querySelectorAll('.scene-tab[aria-current="true"]').length, 1);
    assert.match(document.getElementById("scrub-scene").textContent, /scene 02/);
    assert.ok(document.getElementById("live-caption").textContent.length > 0, "the caption is populated for scene 2");
    await new Promise((resolve) => setTimeout(resolve, 420));
    assert.match(window.location.hash, /scene=2/);
  } finally {
    app.restore();
  }
});

test("an edit is validated, applied to the stage, and saved for the next visit", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    document.getElementById("tab-editor").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

    const headline = document.getElementById("f-headline-1");
    headline.value = "What happened when";
    document.getElementById("f-headline-2").value = "the stream stopped.";
    document.getElementById("f-accent").value = "1";
    document.getElementById("editor-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));

    const hint = document.getElementById("editor-hint");
    assert.equal(hint.dataset.tone, "ok", `edit should be accepted: ${hint.textContent}`);
    const saved = JSON.parse(window.localStorage.getItem("blackbox.studio.blueprint.v1"));
    assert.equal(saved.schema, "blackbox.studio.blueprint.v1");
    assert.deepEqual(saved.scenes["scene-1-the-problem"].headlines, ["What happened when", "the stream stopped."]);
  } finally {
    app.restore();
  }
});

test("an edit that would not fit the column is refused, with the field marked", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    document.getElementById("tab-editor").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    document.getElementById("f-headline-1").value = "x".repeat(120);
    document.getElementById("editor-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));

    const hint = document.getElementById("editor-hint");
    assert.equal(hint.dataset.tone, "error");
    assert.equal(document.getElementById("f-headline-1").closest(".field").dataset.invalid, "true");
    assert.equal(window.localStorage.getItem("blackbox.studio.blueprint.v1"), null, "nothing invalid is stored");
  } finally {
    app.restore();
  }
});

test("a duration shorter than the narration is refused", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    document.getElementById("tab-editor").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    document.getElementById("f-duration").value = "3000";
    document.getElementById("editor-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    const hint = document.getElementById("editor-hint");
    assert.equal(hint.dataset.tone, "error");
    assert.match(hint.textContent, /narration ends at/);
  } finally {
    app.restore();
  }
});

test("a corrupted draft is discarded, the film's own copy loads, and the page says so", async () => {
  const app = await boot({ stored: "__CORRUPT__" });
  try {
    const banner = app.document.getElementById("banner");
    assert.equal(banner.hidden, false);
    assert.match(banner.textContent, /film's own copy/i);
    assert.equal(app.document.querySelectorAll(".scene-tab").length, 7, "the studio still opens");
  } finally {
    app.restore();
  }
});

test("reverting clears the draft and restores the film's headline", async () => {
  const app = await boot({ stored: JSON.stringify({
    schema: "blackbox.studio.blueprint.v1",
    scenes: { "scene-1-the-problem": { headlines: ["Edited line one", "Edited line two"], accentLine: 0, durationMs: 9100 } },
  }) });
  try {
    const { document, window } = app;
    document.getElementById("tab-editor").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert.equal(document.getElementById("f-headline-1").value, "Edited line one", "the stored draft is applied on load");

    document.getElementById("btn-revert").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert.equal(document.getElementById("f-headline-1").value, "When an AI app fails,", "back to the film's copy");
    assert.equal(window.localStorage.getItem("blackbox.studio.blueprint.v1"), null);
  } finally {
    app.restore();
  }
});

test("the inspector reports measured facts and checks", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    document.getElementById("tab-inspector").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const facts = [...document.querySelectorAll("#inspector-list dt")].map((node) => node.textContent);
    assert.ok(facts.includes("mode") && facts.includes("duration") && facts.includes("clip length"));
    const checks = document.querySelectorAll("#inspector-checks li");
    assert.ok(checks.length >= 4);
    assert.ok([...checks].every((node) => node.dataset.ok === "true"), "the shipped blueprint passes its own checks");
  } finally {
    app.restore();
  }
});

test("the keyboard drives playback, captions and panels", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    const press = (key, extra = {}) => window.dispatchEvent(new window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...extra }));

    press(" ", { code: "Space" });
    assert.equal(document.getElementById("play-label").textContent, "Pause");
    press(" ", { code: "Space" });
    assert.equal(document.getElementById("play-label").textContent, "Play");

    press("c");
    assert.equal(document.getElementById("btn-captions").getAttribute("aria-pressed"), "false");
    press("c");
    assert.equal(document.getElementById("btn-captions").getAttribute("aria-pressed"), "true");

    press("i");
    assert.equal(document.getElementById("pane-inspector").hidden, false);
    press("ArrowRight");
    // The chrome is painted on the animation frame, not inside the key handler.
    await new Promise((resolve) => setTimeout(resolve, 40));
    assert.match(document.getElementById("scrub-scene").textContent, /scene 02/);
    assert.equal(document.getElementById("live-caption").textContent.length > 0, true);
  } finally {
    app.restore();
  }
});

test("a deep link opens on the frame it names", async () => {
  const app = await boot({ hash: "#scene=5&pct=40" });
  try {
    assert.match(app.document.getElementById("scrub-scene").textContent, /scene 05/);
    assert.equal(app.document.querySelectorAll('.scene-tab[aria-current="true"]')[0].dataset.index, "4");
  } finally {
    app.restore();
  }
});

test("reduced motion is honoured when the system asks for it", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    const button = document.getElementById("btn-motion");
    button.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert.equal(button.getAttribute("aria-pressed"), "true");
    assert.equal(button.textContent, "reduced motion");
  } finally {
    app.restore();
  }
});

test("one drawer opens at a time, and Escape closes it", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    const click = (id) => document.getElementById(id).dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const pane = (id) => document.getElementById(id).hidden;

    click("tab-editor");
    assert.equal(pane("pane-editor"), false, "Edit opens");
    assert.equal(document.getElementById("tab-editor").getAttribute("aria-selected"), "true");

    click("tab-inspector");
    assert.equal(pane("pane-inspector"), false, "Blueprint opens");
    assert.equal(pane("pane-editor"), true, "and Edit closes: the shell shows one drawer at a time");

    click("tab-inspector");
    assert.equal(pane("pane-inspector"), true, "clicking the open tab closes it");

    click("tab-transcript");
    assert.equal(pane("pane-transcript"), false);
    window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    assert.equal(pane("pane-transcript"), true, "Escape leaves no drawer open");
    assert.ok(document.querySelectorAll(".drawer:not([hidden])").length <= 1, "never two drawers at once");
  } finally {
    app.restore();
  }
});

test("a footer bar scrubs inside its own scene, and the speed select sets the rate", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    const clock = () => {
      const [, mm, ss, cc] = /(\d\d):(\d\d)\.(\d\d)/.exec(document.getElementById("tc-now").textContent);
      return Number(mm) * 60000 + Number(ss) * 1000 + Number(cc) * 10;
    };
    const press = (key, extra = {}) => document.dispatchEvent(new window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...extra }));

    // Scene 3 is ~13.1 s in; a click at the half of the third bar must land on the third scene and read about 50 %.
    const bar = document.querySelectorAll(".segment")[2];
    bar.getBoundingClientRect = () => ({ left: 0, width: 200, top: 0, height: 8, right: 200, bottom: 8, x: 0, y: 0 });
    bar.dispatchEvent(new window.MouseEvent("click", { bubbles: true, clientX: 100 }));
    await new Promise((resolve) => setTimeout(resolve, 40));
    assert.match(document.getElementById("scrub-scene").textContent, /scene 03/);
    const pct = Number(document.getElementById("scrub-pct").textContent.replace("%", ""));
    assert.ok(pct > 48 && pct < 52, `half of the bar is half the scene, got ${pct}%`);

    // 2x is twice the clock, measured the same way at both rates so the comparison does not lean on absolute timings.
    const measure = async () => {
      document.getElementById("btn-next").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      document.getElementById("btn-prev").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 30));
      press(" ", { code: "Space" });
      const before = clock();
      await new Promise((resolve) => setTimeout(resolve, 260));
      const after = clock();
      press(" ", { code: "Space" });
      return after - before;
    };

    const single = await measure();
    const select = document.getElementById("speed");
    select.value = "2";
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
    const doubled = await measure();

    assert.ok(single > 60, `the clock runs at all, got ${single} ms in 260 ms`);
    assert.ok(doubled > single * 1.5, `2x should outrun 1x, got ${doubled} ms against ${single} ms`);
  } finally {
    app.restore();
  }
});

test("the frame PNG renders at export size, and the copy pill admits when it cannot copy", async () => {
  const app = await boot();
  try {
    const { document, window } = app;
    const seen = [];
    const original = window.HTMLCanvasElement.prototype.getContext;
    window.HTMLCanvasElement.prototype.getContext = function patched(...args) {
      seen.push(`${this.width}x${this.height}`);
      return original.apply(this, args);
    };
    let asked = "";
    window.HTMLCanvasElement.prototype.toBlob = function toBlob(callback, type) {
      asked = type;
      this.__blobbed = true;
      callback(null);
    };
    window.URL.createObjectURL = () => "blob:stub";

    document.getElementById("btn-png").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert.equal(asked, "image/png", "the pill asks for a PNG");
    assert.ok(seen.includes("1920x1080"), `the frame is rendered at export size, saw ${seen.join(", ")}`);

    // jsdom has no clipboard and no execCommand: the pill must say so rather than claim a copy that never happened.
    const copy = document.getElementById("btn-copy-json");
    copy.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.match(copy.textContent, /Copy blocked/);
  } finally {
    app.restore();
  }
});
