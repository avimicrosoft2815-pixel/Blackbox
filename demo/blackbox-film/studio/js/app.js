/**
 * Director Studio — the runtime.
 *
 * The page is built on the reference integration's shell (one centred column, a row of pills in the header, drawers
 * under it, seven scrub bars in the footer) and re-dressed for BlackBox. The stage itself is not DOM: it is the film's
 * own canvas, painted by `renderFrame()` from `studio/js/frame.js` — the module the encoder imports — so the preview
 * cannot drift from the export.
 *
 * This file is only the director around that canvas:
 *  - the clock is `performance.now()`-based, and the audio is *not* the clock. Narration is nudged into sync when a
 *    scene starts; if a browser refuses to play audio, the film still runs and the captions still land on time.
 *  - every scene's length comes from the blueprint, which got it from the measured MP3s. Nothing here invents timing.
 *  - drawers (Edit / Blueprint / Script) hold the copy, the measurements and the raw scene JSON; one is open at a time.
 *  - keyboard shortcuts live in one table, so the list in the keys drawer cannot rot.
 */

import { captionAt, locate, sceneStartMs, timecode, totalDurationMs } from "./motionMath.js";
import { renderFrame } from "./frame.js";
import {
  applyDraft,
  cloneSpec,
  inspectScene,
  loadSpec,
  readStoredSpec,
  validateDraft,
  validateSpec,
  writeStoredSpec,
} from "./blueprint.js";

const canvas = document.getElementById("stage-canvas");
const ctx = canvas.getContext("2d");
const el = (id) => document.getElementById(id);

const state = {
  spec: null,
  filmSpec: null,
  index: 0,
  elapsedMs: 0,
  playing: false,
  speed: 1,
  muted: false,
  captions: true,
  reducedMotion: false,
  drawer: "none",
  manualSplit: null,
  lastFrameAt: 0,
  storage: null,
  lastJsonAt: -1,
  lastSfxScene: -1,
};

let audio = null;

// Built once, then reused: the chrome updates on every animation frame, and querying the document 60 times a second to
// find the same seven pills and seven bars is work the browser does not need to repeat.
const dom = { segments: [], transcript: [], tabs: [] };

/* ── boot ───────────────────────────────────────────────────────────────────────────────────────────────────────── */

async function boot() {
  try {
    state.storage = safeStorage();
    const filmSpec = await loadSpec("spec.json");
    state.filmSpec = cloneSpec(filmSpec);
    const restored = readStoredSpec(state.storage, cloneSpec(filmSpec));
    state.spec = restored.spec;
    if (["corrupt", "applied", "unavailable"].includes(restored.outcome)) {
      showBanner(restored.outcome, restored.detail);
    }
    await ensureFonts();
    buildSceneTabs();
    buildSegments();
    buildTranscript();
    wireTransport();
    wireKeyboard();
    wirePanels();
    readDeepLink();
    resizeCanvas();
    paint();
    tick();
    showSpecNotices();
    el("meta-version").textContent = state.spec.product?.version ?? "pre-alpha";
    el("meta-filmlength").textContent = `${(totalDurationMs(state.spec) / 1000).toFixed(1)} s`;
    el("tc-total").textContent = timecode(totalDurationMs(state.spec));
    el("foot-note").textContent = `${state.spec.product?.version ?? "pre-alpha"} · synthetic incident · no network, no account`;
  } catch (error) {
    el("stage-badge").textContent = "BLUEPRINT ERROR";
    el("live-caption").textContent = String(error.message ?? error);
    showBanner("corrupt", `the studio could not start: ${error.message}`);
    console.error(error);
  }
}

function safeStorage() {
  try {
    const probe = "__blackbox_probe__";
    window.localStorage.setItem(probe, "1");
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch {
    return null;
  }
}

/** The stage must not paint before the woff2 files have loaded, or the first frames use a fallback face. */
async function ensureFonts() {
  if (!document.fonts || !document.fonts.load) return;
  const families = ["InterBlack", "InterBold", "InterSemiBold", "InterMedium", "InterRegular", "MonoRegular", "MonoMedium", "MonoBold"];
  await Promise.all(families.map((family) => document.fonts.load(`400 20px ${family}`).catch(() => null)));
  await document.fonts.ready;
}

function showBanner(outcome, detail) {
  const banner = el("banner");
  const tone = outcome === "corrupt" || outcome === "unavailable" ? "Note" : "Draft";
  banner.innerHTML = "";
  const strong = document.createElement("strong");
  strong.textContent = `${tone}. `;
  banner.append(strong, document.createTextNode(detail));
  banner.hidden = false;
}

/**
 * The reference puts a spec-notice chip in the header when the blueprint has problems. Ours counts real ones: structural
 * faults from `validateSpec`, plus any scene whose own checks fail.
 */
function showSpecNotices() {
  const structural = validateSpec(state.spec);
  const failures = [];
  state.spec.scenes.forEach((scene, index) => {
    const { checks } = inspectScene(state.spec, index, { measureHeadline });
    checks.filter((check) => !check.ok).forEach((check) => {
      failures.push(`scene ${index + 1} (${scene.shortTitle}): ${check.text}`);
    });
  });
  const all = [...structural, ...failures];
  const chip = el("spec-notice");
  if (all.length === 0) {
    chip.hidden = true;
    chip.textContent = "";
    chip.removeAttribute("title");
    return;
  }
  chip.hidden = false;
  chip.textContent = `${all.length} SPEC NOTICE${all.length === 1 ? "" : "S"}`;
  chip.title = all.join("\n");
}

/* ── the stage ─────────────────────────────────────────────────────────────────────────────────────────────────── */

function resizeCanvas() {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const cssWidth = canvas.clientWidth || 1280;
  const width = Math.round(cssWidth * ratio);
  const height = Math.round((width * 9) / 16);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function paint() {
  const scene = state.spec.scenes[state.index];
  renderFrame(ctx, state.spec, state.index, scenePct(), {
    width: canvas.width,
    height: canvas.height,
    showCaptions: state.captions,
    motionReduced: state.reducedMotion,
    manualSplit: state.manualSplit,
    timecode: `${timecode(state.elapsedMs)} · ${scene.shortTitle}`,
  });
}

function scenePct() {
  const scene = state.spec.scenes[state.index];
  const local = state.elapsedMs - sceneStartMs(state.spec, state.index);
  return scene.durationMs === 0 ? 0 : Math.max(0, Math.min(100, (local / scene.durationMs) * 100));
}

/* ── the clock ─────────────────────────────────────────────────────────────────────────────────────────────────── */

function tick(now) {
  requestAnimationFrame(tick);
  // One clock, one domain. The animation frame's timestamp is the only time source the film reads; mixing it with
  // `performance.now()` elsewhere would make the delta between two frames meaningless — the two can count from different
  // origins — and a clock that silently froze was the first thing this rewrite's tests found.
  const stamp = now ?? performance.now();
  const delta = state.lastFrameAt ? Math.max(0, stamp - state.lastFrameAt) : 0;
  state.lastFrameAt = stamp;
  if (state.playing) setElapsed(state.elapsedMs + delta * state.speed, { fromClock: true });
  paint();
  syncChrome();
}

function setElapsed(ms, { fromClock = false } = {}) {
  const total = totalDurationMs(state.spec);
  state.elapsedMs = Math.max(0, Math.min(total, ms));
  const { index } = locate(state.spec, state.elapsedMs);
  if (index !== state.index) {
    state.index = index;
    state.manualSplit = null;
    state.lastJsonAt = -1;
    loadSceneAudio();
    syncTabs();
  }
  if (!fromClock) {
    syncAudioToClock();
    paint();
  } else if (audio && !audio.paused) {
    const drift = Math.abs(audio.currentTime * 1000 - audioTargetMs());
    if (drift > 260) syncAudioToClock();
  }
  if (!state.playing) paint();
}

function play() {
  if (state.playing) return;
  if (state.elapsedMs >= totalDurationMs(state.spec) - 1) state.elapsedMs = 0;
  state.playing = true;
  loadSceneAudio();
  startAudio();
  syncPlayButton();
}

function pause() {
  state.playing = false;
  if (audio) audio.pause();
  syncPlayButton();
}

function togglePlay() {
  if (state.playing) pause(); else play();
}

/* ── audio ─────────────────────────────────────────────────────────────────────────────────────────────────────── */

const LEAD_MS = 500;

function audioTargetMs() {
  return Math.max(0, state.elapsedMs - sceneStartMs(state.spec, state.index) - LEAD_MS);
}

function loadSceneAudio() {
  // Audio is a nicety, never a dependency: a browser that refuses to construct, decode or play it still gets the film,
  // the captions and the timing — and the transport says so instead of pretending sound is on.
  try {
    const scene = state.spec.scenes[state.index];
    const source = scene.narration?.audio ? `assets/narration/${scene.narration.audio.split("/").pop()}` : "";
    if (!source) return;
    if (!audio) {
      audio = new window.Audio();
      audio.preload = "auto";
      audio.addEventListener("ended", () => { /* the clock owns the film, not the clip */ });
    }
    const url = new URL(source, window.location.href).href;
    if (audio.src !== url) {
      audio.src = url;
      audio.load();
    }
    syncAudioToClock();
  } catch (error) {
    audio = null;
    state.muted = true;
    el("btn-mute").textContent = "no audio";
    console.warn("narration unavailable in this browser:", error?.name ?? error);
  }
}

function startAudio() {
  if (!audio || state.muted) return;
  audio.muted = false;
  const target = audioTargetMs() / 1000;
  if (Number.isFinite(target) && Math.abs(audio.currentTime - target) > 0.12) {
    try { audio.currentTime = target; } catch { /* seeking before metadata is not fatal */ }
  }
  const started = audio.play();
  if (started && typeof started.catch === "function") {
    started.catch(() => {
      // Autoplay policy: the film keeps running, and the transport says so rather than pretending.
      el("btn-mute").textContent = "sound blocked";
    });
  }
}

function syncAudioToClock() {
  if (!audio) return;
  const target = audioTargetMs() / 1000;
  const withinScene = target >= 0 && target < (state.spec.scenes[state.index].narration?.audioMs ?? 0) / 1000 + 0.5;
  if (!state.playing || state.muted || !withinScene) {
    if (!audio.paused) audio.pause();
    if (withinScene && Number.isFinite(target)) {
      try { audio.currentTime = Math.max(0, target); } catch { /* ignore */ }
    }
    return;
  }
  if (audio.paused) startAudio();
}

/* ── chrome ───────────────────────────────────────────────────────────────────────────────────────────────────── */

function syncPlayButton() {
  // The glyph is two inline SVGs toggled by a data attribute: the bundled faces are subsets, and a triangle typed into
  // the mono font is a box in some builds. The label carries the meaning either way.
  el("btn-play").dataset.state = state.playing ? "playing" : "paused";
  el("play-label").textContent = state.playing ? "Pause" : "Play";
}

function syncChrome() {
  const scene = state.spec.scenes[state.index];
  const pct = scenePct();

  el("tc-now").textContent = timecode(state.elapsedMs);
  el("scrub-pct").textContent = `${pct.toFixed(0)}%`;
  el("scrub-scene").textContent = `scene ${String(scene.step).padStart(2, "0")} · ${scene.shortTitle}`;
  el("scene-window").textContent = scene.timeWindow ?? "";
  el("stage-badge").textContent = state.playing ? "PLAYING" : "PAUSED";

  const caption = captionAt(scene.narration?.captions, pct);
  if (el("live-caption").textContent !== caption) el("live-caption").textContent = caption;

  // The words being spoken, and the cues the scene was built around: the reference shows both under the bars.
  if (el("scene-script").textContent !== (scene.narration?.script ?? "")) {
    el("scene-script").textContent = scene.narration?.script ?? "";
  }
  if (state.lastSfxScene !== state.index) {
    state.lastSfxScene = state.index;
    el("scene-sfx").textContent = (scene.narration?.sfxNotes ?? []).join(" · ") || "—";
  }

  dom.segments.forEach(({ node, fill }, i) => {
    if (node.dataset.active !== String(i === state.index)) node.dataset.active = String(i === state.index);
    const width = i < state.index ? "100%" : i > state.index ? "0%" : `${pct.toFixed(1)}%`;
    if (fill.style.width !== width) fill.style.width = width;
  });

  if (dom.transcriptIndex !== state.index) {
    dom.transcriptIndex = state.index;
    dom.transcript.forEach((node) => {
      node.dataset.active = String(Number(node.dataset.scene) === state.index);
    });
  }

  // The JSON drawer re-serialises at most twice a second, and only at a new percent: a live feed of the scene's own
  // blueprint is useful, a 60 Hz one is a space heater.
  if (state.drawer === "inspector") {
    const whole = Math.round(pct);
    if (whole !== state.lastJsonAt) {
      state.lastJsonAt = whole;
      el("json-view").textContent = JSON.stringify(scene, null, 2);
      el("json-scene-label").textContent = `scene ${String(scene.step).padStart(2, "0")} · ${Math.round(pct)}%`;
    }
  }

  if (state.drawer === "editor") refreshEditorNumbers();
}

function buildSceneTabs() {
  const list = el("scene-tabs");
  list.innerHTML = "";
  state.spec.scenes.forEach((scene, index) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "scene-tab";
    button.dataset.index = String(index);
    button.setAttribute("role", "tab");
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = String(scene.step).padStart(2, "0");
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = scene.shortTitle;
    button.append(n, document.createTextNode(" · "), t);
    button.addEventListener("click", () => jumpToScene(index));
    item.append(button);
    list.append(item);
  });
  dom.tabs = [...list.querySelectorAll(".scene-tab")];
  syncTabs();
}

function syncTabs() {
  dom.tabs.forEach((node) => {
    node.setAttribute("aria-current", String(Number(node.dataset.index) === state.index));
  });
  if (state.drawer === "editor") fillEditor();
  if (state.drawer === "inspector") fillInspector();
}

/**
 * Seven bars, one per scene, in the footer. Clicking a bar jumps to that point in that scene; dragging one scrubs inside
 * it. The reference's `onPointerDown/Move/Up` trio, with the pointer capture made optional because not every host
 * implements it.
 */
function buildSegments() {
  const segments = el("segments");
  segments.innerHTML = "";
  state.spec.scenes.forEach((scene, index) => {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "segment";
    node.dataset.index = String(index);
    node.title = `Drag or click to scrub ${scene.shortTitle} · ${(scene.durationMs / 1000).toFixed(2)} s`;
    node.setAttribute("aria-label", `Scene ${scene.step}: ${scene.shortTitle}`);

    const fill = document.createElement("span");
    fill.className = "fill";
    const cap = document.createElement("span");
    cap.className = "cap";
    cap.textContent = String(scene.step).padStart(2, "0");
    node.append(fill, cap);

    const scrubTo = (clientX) => {
      const rect = node.getBoundingClientRect();
      if (!rect.width) return;
      const within = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      jumpToMs(sceneStartMs(state.spec, index) + scene.durationMs * within);
    };

    node.addEventListener("pointerdown", (event) => {
      node.setPointerCapture?.(event.pointerId);
      node.dataset.scrubbing = "true";
      scrubTo(event.clientX);
    });
    node.addEventListener("pointermove", (event) => {
      if (node.dataset.scrubbing === "true") scrubTo(event.clientX);
    });
    const stop = (event) => {
      node.dataset.scrubbing = "false";
      if (node.hasPointerCapture?.(event.pointerId)) node.releasePointerCapture(event.pointerId);
    };
    node.addEventListener("pointerup", stop);
    node.addEventListener("pointercancel", stop);
    node.addEventListener("click", (event) => scrubTo(event.clientX));

    segments.append(node);
  });
  dom.segments = [...segments.querySelectorAll(".segment")].map((node) => ({ node, fill: node.querySelector(".fill") }));
}

function buildTranscript() {
  const list = el("transcript");
  list.innerHTML = "";
  state.spec.scenes.forEach((scene, index) => {
    const item = document.createElement("li");
    item.dataset.scene = String(index);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "scene-jump";
    button.textContent = `${String(scene.step).padStart(2, "0")} ${scene.shortTitle} · ${timecode(sceneStartMs(state.spec, index))}`;
    button.addEventListener("click", () => jumpToScene(index));
    item.append(button);
    const script = document.createElement("p");
    script.textContent = scene.narration?.script ?? "";
    item.append(script);
    (scene.narration?.captions ?? []).forEach((caption) => {
      const cap = document.createElement("p");
      cap.className = "cap";
      const tc = document.createElement("span");
      tc.className = "tc";
      tc.textContent = `${timecode(sceneStartMs(state.spec, index) + caption.atMs)}  `;
      cap.append(tc, document.createTextNode(caption.text));
      item.append(cap);
    });
    list.append(item);
  });
  dom.transcript = [...list.querySelectorAll("li")];
  dom.transcriptIndex = -1;
}

/* ── navigation ────────────────────────────────────────────────────────────────────────────────────────────────── */

function jumpToScene(index) {
  const clamped = Math.max(0, Math.min(state.spec.scenes.length - 1, index));
  setElapsed(sceneStartMs(state.spec, clamped) + 1);
  syncTabs();
  if (state.playing) startAudio();
  writeDeepLink();
}

function jumpToMs(ms) {
  setElapsed(ms);
  syncTabs();
  if (state.playing) startAudio();
  writeDeepLink();
}

function readDeepLink() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const scene = Number(params.get("scene"));
  const pct = Number(params.get("pct"));
  if (Number.isFinite(scene) && scene >= 1 && scene <= state.spec.scenes.length) {
    const index = scene - 1;
    const within = Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0;
    state.elapsedMs = sceneStartMs(state.spec, index) + (state.spec.scenes[index].durationMs * within) / 100;
    state.index = index;
    loadSceneAudio();
    syncTabs();
  }
}

let deepLinkTimer = null;
function writeDeepLink() {
  if (deepLinkTimer) return;
  deepLinkTimer = window.setTimeout(() => {
    deepLinkTimer = null;
    const pct = Math.round(scenePct());
    const hash = `#scene=${state.index + 1}&pct=${pct}`;
    if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  }, 350);
}

/* ── transport ─────────────────────────────────────────────────────────────────────────────────────────────────── */

function wireTransport() {
  el("btn-play").addEventListener("click", togglePlay);
  el("btn-prev").addEventListener("click", () => jumpToScene(state.index - 1));
  el("btn-next").addEventListener("click", () => jumpToScene(state.index + 1));

  el("speed").addEventListener("change", (event) => {
    state.speed = Number(event.target.value) || 1;
    startAudio();
  });

  el("btn-mute").addEventListener("click", () => {
    state.muted = !state.muted;
    el("btn-mute").setAttribute("aria-pressed", String(state.muted));
    el("btn-mute").textContent = state.muted ? "muted" : "sound on";
    if (state.muted && audio) audio.pause();
    if (!state.muted) syncAudioToClock();
  });

  el("btn-captions").addEventListener("click", () => {
    state.captions = !state.captions;
    el("btn-captions").setAttribute("aria-pressed", String(state.captions));
    el("btn-captions").textContent = state.captions ? "captions on" : "captions off";
    paint();
  });

  el("btn-motion").addEventListener("click", () => {
    setReducedMotion(!state.reducedMotion);
  });

  const preferred = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
  if (preferred && preferred.matches) setReducedMotion(true);
  if (preferred && preferred.addEventListener) {
    preferred.addEventListener("change", (event) => setReducedMotion(event.matches));
  }

  window.addEventListener("resize", () => { resizeCanvas(); paint(); });

  el("btn-help").addEventListener("click", () => {
    const help = el("help");
    help.hidden = !help.hidden;
    el("btn-help").setAttribute("aria-expanded", String(!help.hidden));
  });
}

function setReducedMotion(value) {
  state.reducedMotion = value;
  el("btn-motion").setAttribute("aria-pressed", String(value));
  el("btn-motion").textContent = value ? "reduced motion" : "full motion";
  // Only the ambient glow moves in the film; reduced motion parks it in the preview. The export is unaffected, and the
  // keys drawer says so — a toggle that silently did nothing would be the same lie in a quieter voice.
  el("btn-motion").title = value
    ? "the preview parks the ambient glow; the exported film always renders full motion"
    : "the film's own frames, ambient glow included";
  paint();
}

/* ── keyboard: one table, so the drawer's list cannot rot ──────────────────────────────────────────────────────── */

function wireKeyboard() {
  window.addEventListener("keydown", (event) => {
    const tag = (event.target && event.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || event.target.isContentEditable;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    if ((event.code === "Space" || event.key === " ") && !typing) {
      event.preventDefault();
      togglePlay();
      return;
    }
    if (typing) return;

    switch (event.key) {
      case "Escape":
        showPane("none");
        break;
      case "ArrowLeft":
        event.preventDefault();
        if (event.shiftKey) jumpToMs(state.elapsedMs - state.spec.scenes[state.index].durationMs / 100);
        else jumpToScene(state.index - 1);
        break;
      case "ArrowRight":
        event.preventDefault();
        if (event.shiftKey) jumpToMs(state.elapsedMs + state.spec.scenes[state.index].durationMs / 100);
        else jumpToScene(state.index + 1);
        break;
      case "a":
      case "A":
        nudgeDivider(-3);
        break;
      case "d":
      case "D":
        nudgeDivider(3);
        break;
      case "c":
      case "C":
        el("btn-captions").click();
        break;
      case "m":
      case "M":
        el("btn-mute").click();
        break;
      case "e":
      case "E":
        showPane("editor");
        break;
      case "i":
      case "I":
        showPane("inspector");
        break;
      case "t":
      case "T":
        showPane("transcript");
        break;
      case "r":
      case "R":
        revertDraft();
        break;
      default:
        break;
    }
  });
}

function nudgeDivider(delta) {
  if (state.spec.scenes[state.index].mode !== "before-after") return;
  const current = state.manualSplit ?? 40;
  state.manualSplit = Math.max(8, Math.min(92, current + delta));
  paint();
}

/* ── drawers ──────────────────────────────────────────────────────────────────────────────────────────────────── */

const PANES = {
  editor: { tab: "tab-editor", pane: "pane-editor" },
  inspector: { tab: "tab-inspector", pane: "pane-inspector" },
  transcript: { tab: "tab-transcript", pane: "pane-transcript" },
};

function wirePanels() {
  Object.entries(PANES).forEach(([name, ids]) => {
    el(ids.tab).addEventListener("click", () => showPane(state.drawer === name ? "none" : name));
  });

  el("editor-form").addEventListener("submit", (event) => {
    event.preventDefault();
    commitEditor();
  });
  el("btn-revert").addEventListener("click", revertDraft);
  el("btn-copy-json").addEventListener("click", copyBlueprintJson);
  el("btn-png").addEventListener("click", downloadFramePng);

  ["f-eyebrow", "f-headline-1", "f-headline-2", "f-accent", "f-body", "f-duration"].forEach((id) => {
    el(id).addEventListener("input", () => {
      const hint = el("editor-hint");
      hint.textContent = "unsaved — press Apply";
      hint.removeAttribute("data-tone");
    });
  });
}

function showPane(name) {
  state.drawer = name === state.drawer ? "none" : name;
  Object.entries(PANES).forEach(([pane, ids]) => {
    const open = pane === state.drawer;
    el(ids.pane).hidden = !open;
    el(ids.tab).setAttribute("aria-selected", String(open));
  });
  if (state.drawer === "editor") fillEditor();
  if (state.drawer === "inspector") fillInspector();
}

/* ── editor ───────────────────────────────────────────────────────────────────────────────────────────────────── */

function fillEditor() {
  const scene = state.spec.scenes[state.index];
  el("editor-scene-label").textContent = `scene ${String(scene.step).padStart(2, "0")} · ${scene.shortTitle}`;
  el("f-eyebrow").value = scene.copy.eyebrow ?? "";
  el("f-headline-1").value = scene.copy.headlines?.[0] ?? "";
  el("f-headline-2").value = scene.copy.headlines?.[1] ?? "";
  el("f-accent").value = String(scene.copy.accentLine ?? 0);
  el("f-body").value = scene.copy.body ?? "";
  el("f-duration").value = String(scene.durationMs);
  const hint = el("editor-hint");
  hint.textContent = "changes stay in this browser; the film re-renders from spec.json";
  hint.removeAttribute("data-tone");
  document.querySelectorAll("#editor-form .field").forEach((field) => field.removeAttribute("data-invalid"));
}

function refreshEditorNumbers() {
  const scene = state.spec.scenes[state.index];
  if (!scene || !document.activeElement || document.activeElement.id !== "f-duration") return;
  const durationMs = Number(el("f-duration").value);
  const hint = el("editor-hint");
  const audioEnd = (scene.narration?.audioMs ?? 0) + LEAD_MS;
  if (!Number.isFinite(durationMs)) return;
  if (durationMs < audioEnd) {
    hint.textContent = `narration ends at ${(audioEnd / 1000).toFixed(2)} s`;
    hint.dataset.tone = "error";
  } else if (hint.dataset.tone === "error") {
    // The number is being retyped and now fits: take the warning away instead of leaving a stale one under a valid field.
    hint.textContent = "unsaved — press Apply";
    hint.removeAttribute("data-tone");
  }
}

/** Measure on the stage's own context, so validation and painting agree about how wide a line is. */
function measureHeadline(line, size) {
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  const scale = canvas.width / 1920 || 1;
  ctx.font = `900 ${size * scale}px InterBlack`;
  const width = ctx.measureText(line).width / scale;
  ctx.restore();
  return width;
}

function commitEditor() {
  const scene = state.spec.scenes[state.index];
  const result = validateDraft({
    scene,
    eyebrow: el("f-eyebrow").value,
    headlines: [el("f-headline-1").value, el("f-headline-2").value],
    accentLine: Number(el("f-accent").value),
    body: el("f-body").value,
    durationMs: Number(el("f-duration").value),
    measure: measureHeadline,
  });

  document.querySelectorAll("#editor-form .field").forEach((field) => field.removeAttribute("data-invalid"));
  const hint = el("editor-hint");

  if (!result.ok) {
    const first = result.errors[0];
    hint.textContent = first.message;
    hint.dataset.tone = "error";
    const fieldId = {
      eyebrow: "f-eyebrow", "headline-1": "f-headline-1", "headline-2": "f-headline-2", body: "f-body", duration: "f-duration",
    }[first.field];
    if (fieldId) {
      el(fieldId).closest(".field")?.setAttribute("data-invalid", "true");
      el(fieldId).focus();
    }
    return;
  }

  state.spec = cloneSpec(state.spec);
  applyDraft(state.spec, state.index, {
    eyebrow: el("f-eyebrow").value,
    headlines: result.headlines,
    accentLine: Number(el("f-accent").value),
    body: el("f-body").value,
    durationMs: Number(el("f-duration").value),
  });

  const saved = writeStoredSpec(state.storage, state.spec, state.filmSpec);

  el("meta-filmlength").textContent = `${(totalDurationMs(state.spec) / 1000).toFixed(1)} s`;
  el("tc-total").textContent = timecode(totalDurationMs(state.spec));
  buildSceneTabs();
  buildSegments();
  buildTranscript();
  syncTabs();
  showSpecNotices();
  paint();

  // Set last: rebuilding the tabs re-fills the editor, which resets this line. A confirmation the next render erases is
  // worse than no confirmation.
  hint.textContent = `${saved.detail} · re-render the film to export it`;
  hint.dataset.tone = saved.ok ? "ok" : "error";
}

function revertDraft() {
  state.spec = cloneSpec(state.filmSpec);
  if (state.storage) {
    try { state.storage.removeItem("blackbox.studio.blueprint.v1"); } catch { /* nothing to clear */ }
  }
  state.index = Math.min(state.index, state.spec.scenes.length - 1);
  state.elapsedMs = Math.min(state.elapsedMs, totalDurationMs(state.spec));
  state.lastJsonAt = -1;
  buildSceneTabs();
  buildSegments();
  buildTranscript();
  syncTabs();
  if (state.drawer === "editor") fillEditor();
  showSpecNotices();
  el("meta-filmlength").textContent = `${(totalDurationMs(state.spec) / 1000).toFixed(1)} s`;
  el("tc-total").textContent = timecode(totalDurationMs(state.spec));
  paint();
}

/* ── blueprint drawer ──────────────────────────────────────────────────────────────────────────────────────────── */

function fillInspector() {
  const { facts, checks } = inspectScene(state.spec, state.index, { measureHeadline });
  const list = el("inspector-list");
  list.innerHTML = "";
  facts.forEach(([key, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    list.append(dt, dd);
  });
  const checkList = el("inspector-checks");
  checkList.innerHTML = "";
  checks.forEach((check) => {
    const li = document.createElement("li");
    li.dataset.ok = String(check.ok);
    li.textContent = check.text;
    checkList.append(li);
  });

  const scene = state.spec.scenes[state.index];
  el("json-view").textContent = JSON.stringify(scene, null, 2);
  el("json-scene-label").textContent = `scene ${String(scene.step).padStart(2, "0")} · ${Math.round(scenePct())}%`;
  state.lastJsonAt = Math.round(scenePct());
}

async function copyBlueprintJson() {
  const text = JSON.stringify(state.spec, null, 2);
  const button = el("btn-copy-json");
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else if (typeof document.execCommand === "function") {
      // The fallback the clipboard API replaced: a transient textarea and a range selection. `execCommand` reports
      // failure by returning false, so its return value is read rather than assumed.
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "true");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      if (!copied) throw new Error("execCommand refused the copy");
    } else {
      throw new Error("this browser offers no clipboard path");
    }
    button.textContent = "Copied full blueprint JSON";
  } catch {
    button.textContent = "Copy blocked — select the JSON instead";
  }
  window.setTimeout(() => { button.textContent = "Copy full blueprint JSON"; }, 2200);
}

/** The reference's "1080p PNG" pill: render the current frame at export size and hand it to the browser as a file. */
function downloadFramePng() {
  const width = 1920;
  const height = 1080;
  const offscreen = document.createElement("canvas");
  offscreen.width = width;
  offscreen.height = height;
  const offCtx = offscreen.getContext("2d");
  const scene = state.spec.scenes[state.index];
  renderFrame(offCtx, state.spec, state.index, scenePct(), {
    width,
    height,
    showCaptions: state.captions,
    motionReduced: state.reducedMotion,
    manualSplit: state.manualSplit,
    timecode: `${timecode(state.elapsedMs)} · ${scene.shortTitle}`,
  });

  const name = `blackbox-scene-${String(scene.step).padStart(2, "0")}-${String(Math.round(scenePct())).padStart(3, "0")}.png`;
  const save = (href, revoke) => {
    const link = document.createElement("a");
    link.href = href;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    if (revoke) window.setTimeout(() => URL.revokeObjectURL(href), 4000);
  };

  if (typeof offscreen.toBlob === "function") {
    offscreen.toBlob((blob) => {
      if (!blob) return;
      save(URL.createObjectURL(blob), true);
    }, "image/png");
    return;
  }
  try {
    save(offscreen.toDataURL("image/png"), false);
  } catch {
    el("btn-png").textContent = "PNG unavailable";
  }
}

boot();
