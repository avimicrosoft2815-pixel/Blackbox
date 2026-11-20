/**
 * The tour page's behaviour: four small things, no framework.
 *
 * 1. Reveals — sections rise in the first moment they are on screen, and a hairline at the top says how far down you are.
 * 2. Copy — every command block has a button that copies it, and says whether the copy actually happened.
 * 3. Numbers — `stats.json` is fetched and rendered; a number without a source is not rendered, and a file that cannot be
 *    read says so instead of leaving a plausible-looking zero on the page.
 * 4. Sound — the page's own cues, synthesised here rather than shipped as audio files. Off until the visitor asks for it,
 *    because a page that makes noise on arrival is a page people close.
 */

/* ── the page's sound design ─────────────────────────────────────────────────────────────────────────────── */

const sound = (() => {
  let context = null;
  let enabled = false;

  function audio() {
    if (!context) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) return null;
      context = new Ctor();
    }
    if (context.state === "suspended") context.resume();
    return context;
  }

  function envelope(node, start, peak, duration) {
    const gain = context.createGain();
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(peak, start + Math.min(0.03, duration * 0.3));
    gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
    node.connect(gain);
    gain.connect(context.destination);
    return gain;
  }

  function whoosh() {
    if (!enabled) return;
    const ctx = audio();
    if (!ctx) return;
    const length = Math.round(ctx.sampleRate * 0.55);
    const buffer = ctx.createBuffer(1, length, ctx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < length; i += 1) data[i] = (Math.random() * 2 - 1) * (1 - i / length) ** 2.2;
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    const filter = ctx.createBiquadFilter();
    filter.type = "bandpass";
    filter.Q.value = 0.9;
    const now = ctx.currentTime;
    filter.frequency.setValueAtTime(700, now);
    filter.frequency.exponentialRampToValueAtTime(2600, now + 0.4);
    source.connect(filter);
    envelope(filter, now, 0.05, 0.5);
    source.start(now);
    source.stop(now + 0.55);
  }

  function stamp() {
    if (!enabled) return;
    const ctx = audio();
    if (!ctx) return;
    const now = ctx.currentTime;
    for (const frequency of [1180, 1774]) {
      const osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.value = frequency;
      envelope(osc, now, 0.045, 0.18);
      osc.start(now);
      osc.stop(now + 0.2);
    }
  }

  function click() {
    if (!enabled) return;
    const ctx = audio();
    if (!ctx) return;
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    osc.type = "triangle";
    osc.frequency.setValueAtTime(620, now);
    osc.frequency.exponentialRampToValueAtTime(420, now + 0.05);
    envelope(osc, now, 0.035, 0.07);
    osc.start(now);
    osc.stop(now + 0.08);
  }

  return {
    whoosh,
    stamp,
    click,
    get enabled() { return enabled; },
    toggle() {
      enabled = !enabled;
      if (!enabled && context) context.suspend();
      if (enabled) { audio(); click(); }
      return enabled;
    },
  };
})();

/* ── 1. reveals and the progress hairline ───────────────────────────────────────────────────────────────────── */

const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
const reveals = [...document.querySelectorAll(".reveal")];

if (reduced || typeof IntersectionObserver !== "function") {
  reveals.forEach((node) => node.classList.add("seen"));
} else {
  const watcher = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("seen");
      watcher.unobserve(entry.target);
      sound.whoosh();
    }
  }, { rootMargin: "0px 0px -12% 0px", threshold: 0.08 });
  reveals.forEach((node) => watcher.observe(node));
}

const progress = document.getElementById("progress");
const onScroll = () => {
  const height = document.documentElement.scrollHeight - window.innerHeight;
  const ratio = height > 0 ? Math.min(1, Math.max(0, window.scrollY / height)) : 0;
  progress.style.width = `${(ratio * 100).toFixed(2)}%`;
};
window.addEventListener("scroll", onScroll, { passive: true });
window.addEventListener("resize", onScroll);
onScroll();

/* ── 2. copy ──────────────────────────────────────────────────────────────────────────────────────────────────── */

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  // The fallback the clipboard API replaced. `execCommand` reports failure by returning false, so read it.
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "true");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  const copied = typeof document.execCommand === "function" ? document.execCommand("copy") : false;
  area.remove();
  return copied;
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const block = button.closest(".step")?.querySelector("pre code");
    if (!block) return;
    const text = block.textContent.replace(/\s+$/, "");
    let copied = false;
    try { copied = await copyText(text); } catch { copied = false; }
    button.dataset.done = copied ? "true" : "false";
    button.textContent = copied ? "copied" : "copy blocked — select the block";
    sound.stamp();
    window.setTimeout(() => {
      button.textContent = "copy";
      delete button.dataset.done;
    }, 2400);
  });
});

/* ── 3. the numbers ─────────────────────────────────────────────────────────────────────────────────────────── */

function card(fact) {
  const node = document.createElement("div");
  node.className = "fact";
  const value = document.createElement("span");
  value.className = "fact-value";
  value.textContent = String(fact.value);
  node.append(value);
  if (fact.unit) {
    const unit = document.createElement("span");
    unit.className = "fact-unit";
    unit.textContent = fact.unit;
    node.append(unit);
  }
  const label = document.createElement("p");
  label.className = "fact-label";
  label.textContent = fact.label;
  const source = document.createElement("p");
  source.className = "fact-source";
  source.textContent = fact.source;
  node.append(label, source);
  return node;
}

const stamp = document.getElementById("stats-stamp");
const facts = document.getElementById("facts");
const suites = document.getElementById("suites");
const notRun = document.getElementById("not-run");

function fail(message) {
  stamp.textContent = message;
  stamp.style.color = "var(--warn)";
}

try {
  const response = await fetch("stats.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`stats.json answered ${response.status}`);
  const stats = await response.json();

  const when = new Date(stats.measuredAt);
  const readable = Number.isNaN(when.getTime()) ? stats.measuredAt : when.toISOString().replace("T", " ").slice(0, 19) + "Z";
  stamp.textContent = `measured at commit ${stats.commit || "unknown"} · ${readable} · ${stats.machine} · written by ${stats.sources?.stats ?? "tools/stats.mjs"}`;

  const desired = [
    "incident.capsules", "incident.verifyOk", "incident.verifyDeclared", "incident.findings", "incident.cannotKnow",
    "tests.python", "tests.film", "film.scenes", "film.seconds", "film.cues", "film.subtitles", "film.hd", "film.uhd",
  ];
  const byId = new Map(stats.facts.map((fact) => [fact.id, fact]));
  for (const id of desired) {
    const fact = byId.get(id);
    if (fact) facts.append(card(fact));
    else {
      const missing = card({ value: "—", unit: "", label: `${id} was not measured`, source: "tools/stats.mjs" });
      missing.classList.add("missing");
      facts.append(missing);
    }
  }

  for (const suite of stats.suites ?? []) {
    const item = document.createElement("li");
    const state = document.createElement("strong");
    state.textContent = `${suite.passed} pass`;
    item.append(state, document.createTextNode(` · ${suite.failed} fail · `));
    const code = document.createElement("code");
    code.textContent = suite.command;
    item.append(code);
    if (suite.what) item.append(document.createTextNode(` — ${suite.what}`));
    if (!suite.ok) item.style.color = "var(--warn)";
    suites.append(item);
  }

  for (const item of stats.notRun ?? []) {
    const line = document.createElement("li");
    const what = document.createElement("strong");
    what.textContent = item.what;
    line.append(what, document.createTextNode(` — ${item.why}`));
    notRun.append(line);
  }
} catch (error) {
  fail(`the measurement could not be read (${error.message}) — run \`node tools/stats.mjs\`, then reload`);
}

const soundButton = document.getElementById("sound");
soundButton.addEventListener("click", () => {
  const on = sound.toggle();
  soundButton.setAttribute("aria-pressed", String(on));
  soundButton.textContent = on ? "sound on" : "sound off";
});
document.querySelectorAll(".pill-solid, .door, .links a").forEach((node) => {
  node.addEventListener("click", () => sound.click());
});
