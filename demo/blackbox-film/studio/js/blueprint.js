/**
 * The blueprint: loading, validating, editing and storing the film's copy.
 *
 * Three rules, because this is the module that lets a person type into a film:
 *
 *  1. The file is the source of truth. `studio/spec.json` is what the encoder reads, so the studio never pretends an
 *     edit changed the export — it says "kept in this browser" and offers the exact command to re-render.
 *  2. Stored edits are untrusted input. `localStorage` can hold anything a previous version, a stray script or a
 *     person with devtools left there, so what comes back is shape-checked field by field and discarded if it does not
 *     fit. A corrupt draft must never be able to stop the studio from opening.
 *  3. Edits are validated against the film's own constraints: a headline must fit its column at the smallest size the
 *     layout will drop to, a duration must be long enough to hold its narration, and the caption timing must stay
 *     inside the scene.
 */

export const STORAGE_KEY = "blackbox.studio.blueprint.v1";
export const SCHEMA = "blackbox.demo-blueprint/1";

const HEADLINE_LIMIT = 60;
const BODY_LIMIT = 320;
const MIN_DURATION = 2000;
const MAX_DURATION = 30000;

/** Fetch and shape-check the blueprint the film is built from. */
export async function loadSpec(url = "spec.json") {
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) throw new Error(`spec.json could not be loaded (HTTP ${response.status})`);
  const spec = await response.json();
  const problems = validateSpec(spec);
  if (problems.length > 0) throw new Error(`spec.json is not a usable blueprint: ${problems[0]}`);
  return spec;
}

/** Structural problems that make a blueprint unusable. Empty array means usable. */
export function validateSpec(spec) {
  const problems = [];
  if (!spec || typeof spec !== "object") return ["blueprint is not an object"];
  if (spec.schema !== SCHEMA) problems.push(`schema should be ${SCHEMA}`);
  if (!Array.isArray(spec.scenes) || spec.scenes.length === 0) return problems.concat("no scenes");
  spec.scenes.forEach((scene, index) => {
    const label = `scene ${index + 1}`;
    if (typeof scene.id !== "string" || !scene.id) problems.push(`${label}: missing id`);
    if (!Number.isFinite(scene.durationMs) || scene.durationMs <= 0) problems.push(`${label}: bad durationMs`);
    if (!scene.copy || !Array.isArray(scene.copy.headlines)) problems.push(`${label}: missing copy.headlines`);
    if (!scene.narration || !Array.isArray(scene.narration.captions)) problems.push(`${label}: missing captions`);
  });
  return problems;
}

/** A deep copy, so "revert" is a swap rather than a reverse-engineering of every edit. */
export function cloneSpec(spec) {
  return JSON.parse(JSON.stringify(spec));
}

export function isStored(state) {
  return Boolean(state && state.kept && typeof state.kept === "object");
}

/**
 * Read the draft from `localStorage`. Anything unexpected — no storage, a JSON error, a shape from an older build, a
 * field of the wrong type — is dropped, and the reason is returned so the studio can say why instead of silently
 * ignoring the person's work.
 */
export function readStoredSpec(storage, spec) {
  const empty = { spec, stored: null, outcome: "empty", detail: "no draft saved yet" };
  if (!storage) return { ...empty, outcome: "unavailable", detail: "this browser blocks local storage" };

  let raw = null;
  try {
    raw = storage.getItem(STORAGE_KEY);
  } catch (error) {
    return { ...empty, outcome: "unavailable", detail: `local storage could not be read (${error.name})` };
  }
  if (!raw) return empty;

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ...empty, outcome: "corrupt", detail: "the saved draft was not valid JSON, so the film's own copy was loaded" };
  }

  if (!parsed || typeof parsed !== "object" || parsed.schema !== STORAGE_KEY) {
    return { ...empty, outcome: "corrupt", detail: "the saved draft came from another format, so the film's own copy was loaded" };
  }

  const draft = cloneSpec(spec);
  const kept = parsed.scenes && typeof parsed.scenes === "object" ? parsed.scenes : null;
  if (!kept) return { ...empty, outcome: "corrupt", detail: "the saved draft had no scenes, so the film's own copy was loaded" };

  let applied = 0;
  let rejected = 0;
  spec.scenes.forEach((scene, index) => {
    const saved = kept[scene.id];
    if (!saved || typeof saved !== "object") return;
    const target = draft.scenes[index];

    const take = (value, apply, test) => {
      if (value === undefined) return;
      if (test(value)) apply(value);
      else rejected += 1;
    };

    take(saved.eyebrow, (v) => { target.copy.eyebrow = v; }, (v) => typeof v === "string" && v.length <= 60);
    take(saved.headlines, (v) => { target.copy.headlines = v.slice(); },
      (v) => Array.isArray(v) && v.length === 2 && v.every((line) => typeof line === "string" && line.length <= HEADLINE_LIMIT));
    take(saved.accentLine, (v) => { target.copy.accentLine = v; }, (v) => v === -1 || v === 0 || v === 1);
    take(saved.body, (v) => { target.copy.body = v; }, (v) => typeof v === "string" && v.length <= BODY_LIMIT);
    take(saved.durationMs, (v) => { target.durationMs = v; }, (v) => Number.isFinite(v) && v >= MIN_DURATION && v <= MAX_DURATION);
    applied += 1;
  });

  if (applied === 0) {
    return { spec, stored: null, outcome: "corrupt", detail: "the saved draft matched no scene, so the film's own copy was loaded" };
  }
  return {
    spec: draft,
    stored: parsed,
    outcome: "applied",
    detail: rejected > 0 ? `${applied} scene(s) restored, ${rejected} field(s) rejected` : `${applied} scene(s) restored from this browser`,
  };
}

export function writeStoredSpec(storage, draft, originalSpec) {
  if (!storage) return { ok: false, detail: "this browser blocks local storage" };
  const scenes = {};
  draft.scenes.forEach((scene) => {
    scenes[scene.id] = {
      eyebrow: scene.copy.eyebrow,
      headlines: scene.copy.headlines,
      accentLine: scene.copy.accentLine,
      body: scene.copy.body,
      durationMs: scene.durationMs,
    };
  });
  const unchanged = JSON.stringify(scenes) === JSON.stringify(Object.fromEntries(originalSpec.scenes.map((scene) => [scene.id, {
    eyebrow: scene.copy.eyebrow,
    headlines: scene.copy.headlines,
    accentLine: scene.copy.accentLine,
    body: scene.copy.body,
    durationMs: scene.durationMs,
  }])));
  try {
    if (unchanged) {
      storage.removeItem(STORAGE_KEY);
      return { ok: true, cleared: true, detail: "back to the film's own copy" };
    }
    storage.setItem(STORAGE_KEY, JSON.stringify({ schema: STORAGE_KEY, savedAt: new Date().toISOString(), scenes }));
    return { ok: true, cleared: false, detail: "saved in this browser" };
  } catch (error) {
    return { ok: false, detail: `could not save (${error.name}); the draft lives in this tab only` };
  }
}

/**
 * Validate one scene's edit. `measure` is a function that reports the widest headline width at a given size, supplied by
 * the studio so validation uses the *same* canvas and fonts that draw the frame.
 */
export function validateDraft({ scene, eyebrow, headlines, accentLine, body, durationMs, measure }) {
  const errors = [];
  const trimmed = headlines.map((line) => String(line ?? "").trim());

  if (!eyebrow.trim()) errors.push({ field: "eyebrow", message: "an eyebrow gives the scene its number and title" });
  if (eyebrow.length > 60) errors.push({ field: "eyebrow", message: "keep the eyebrow under 60 characters" });
  if (!trimmed[0]) errors.push({ field: "headline-1", message: "the first headline line cannot be empty" });
  trimmed.forEach((line, index) => {
    if (line.length > HEADLINE_LIMIT) {
      errors.push({ field: `headline-${index + 1}`, message: `headline lines are capped at ${HEADLINE_LIMIT} characters` });
    }
  });
  if (body.length > BODY_LIMIT) errors.push({ field: "body", message: `body copy is capped at ${BODY_LIMIT} characters` });

  // The layout drops to 44 px in a split column before it gives up, so a headline has to fit at 44 px at the latest.
  if (measure) {
    trimmed.filter(Boolean).forEach((line, index) => {
      const widest = measure(line, 44);
      if (widest > 660) {
        errors.push({
          field: `headline-${index + 1}`,
          message: "this line cannot fit the column even at the smallest headline size — shorten it",
        });
      }
    });
  }

  if (!Number.isFinite(durationMs) || durationMs < MIN_DURATION || durationMs > MAX_DURATION) {
    errors.push({ field: "duration", message: `a scene runs between ${MIN_DURATION / 1000} s and ${MAX_DURATION / 1000} s` });
  } else {
    const audioEnd = (scene.narration?.audioMs ?? 0) + 500;
    if (audioEnd > durationMs) {
      errors.push({ field: "duration", message: `the narration ends at ${(audioEnd / 1000).toFixed(2)} s — give it at least that long` });
    }
  }

  return { ok: errors.length === 0, errors, headlines: trimmed };
}

/** Apply a validated edit to a draft blueprint (mutates the draft, which the caller owns). */
export function applyDraft(draft, sceneIndex, { eyebrow, headlines, accentLine, body, durationMs }) {
  const scene = draft.scenes[sceneIndex];
  scene.copy.eyebrow = eyebrow.trim();
  scene.copy.headlines = headlines.slice(0, 2);
  scene.copy.accentLine = accentLine;
  scene.copy.body = body.trim();
  scene.durationMs = Math.round(durationMs);
}

/** Everything the inspector reports about one scene, measured rather than asserted. */
export function inspectScene(spec, index, { measureHeadline } = {}) {
  const scene = spec.scenes[index];
  const total = spec.scenes.reduce((sum, s) => sum + s.durationMs, 0);
  const audioMs = scene.narration?.audioMs ?? 0;
  const captions = scene.narration?.captions ?? [];
  const facts = [
    ["scene", `${String(scene.step).padStart(2, "0")} · ${scene.id}`],
    ["mode", scene.mode],
    ["layout", scene.splitLayout ? "split stage" : "full bleed"],
    ["duration", `${(scene.durationMs / 1000).toFixed(2)} s`],
    ["share of film", `${((scene.durationMs / (total || 1)) * 100).toFixed(1)} %`],
    ["narration", scene.narration?.audio ?? "none"],
    ["clip length", `${(audioMs / 1000).toFixed(2)} s`],
    ["tail after speech", `${((scene.durationMs - audioMs - 500) / 1000).toFixed(2)} s`],
    ["captions", `${captions.length}`],
    ["accent", scene.copy.accentLine === -1 ? "none" : `line ${scene.copy.accentLine + 1}`],
    ["disclosure", scene.label ?? "none — claims are the product's own"],
  ];
  if (measureHeadline && Array.isArray(scene.copy.headlines)) {
    const sizes = scene.copy.headlines.map((line) => {
      for (let size = scene.splitLayout ? 76 : 84; size >= 44; size -= 2) {
        if (measureHeadline(line, size) <= (scene.splitLayout ? 660 : 660)) return size;
      }
      return 44;
    });
    facts.push(["headline size", `${Math.min(...sizes)} px`]);
  }

  const checks = [];
  const narrationFits = audioMs + 500 <= scene.durationMs;
  checks.push({ ok: narrationFits, text: narrationFits
    ? "narration fits with its lead-in"
    : "narration is longer than the scene" });
  const captionsInside = captions.every((caption) => caption.atPct >= 0 && caption.atPct <= 100);
  checks.push({ ok: captionsInside, text: captionsInside ? "every caption lands inside the scene" : "a caption falls outside the scene" });
  const sorted = [...captions].every((caption, i, all) => i === 0 || caption.atMs >= all[i - 1].atMs);
  checks.push({ ok: sorted, text: sorted ? "caption timings are in order" : "caption timings are out of order" });
  checks.push({ ok: Boolean(scene.label) || ["arch-graph", "ember-timeline", "hud-telemetry", "warp-reveal"].includes(scene.mode),
    text: scene.label ? `disclosure shown: “${scene.label}”` : "no illustration label — this scene only states product behaviour" });
  return { facts, checks };
}
