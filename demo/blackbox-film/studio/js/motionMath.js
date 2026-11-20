/**
 * The motion engine: every position, opacity and clock value in the film comes from here.
 *
 * Ported from the reference Director Studio's `motionMath.ts` and kept deliberately pure — no DOM, no canvas, no time —
 * so the browser stage, the in-browser export and the Node frame renderer all agree by construction. A frame is a
 * function of `(scene, pct)` and nothing else, which is what makes the exported film byte-identical to a re-render.
 *
 * Additions over the reference:
 *  - `hold()` and `segment()` shape a scene into beats (enter, hold, exit) so a scene can *arrive* rather than simply be
 *    present at 0 %.
 *  - `stagger()` spreads a list of items across a window, which is how the collage cards and the timeline tracks enter.
 *  - `easeOutExpoSoft` and `easeInOutSine`, because the black-and-gold direction wants long, quiet decelerations.
 */

export function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function clamp01(t) {
  return clamp(t, 0, 1);
}

export function lerp(a, b, t) {
  return a + (b - a) * clamp01(t);
}

/** Maps `value` from [inMin, inMax] onto [0, 1], clamped. The primitive every beat is built from. */
export function progress(value, inMin, inMax) {
  if (inMax === inMin) return value >= inMax ? 1 : 0;
  return clamp01((value - inMin) / (inMax - inMin));
}

export function easeOutQuint(t) {
  const x = clamp01(t);
  return 1 - Math.pow(1 - x, 5);
}

export function easeOutCubic(t) {
  const x = clamp01(t);
  return 1 - Math.pow(1 - x, 3);
}

export function easeOutExpoSoft(t) {
  const x = clamp01(t);
  return x === 1 ? 1 : 1 - Math.pow(2, -9 * x);
}

export function easeInOutCubic(t) {
  const x = clamp01(t);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

export function easeInOutSine(t) {
  return -(Math.cos(Math.PI * clamp01(t)) - 1) / 2;
}

/** The reference's back-out spring: overshoots 1.0 mid-curve and settles exactly on 1.0. */
export function springPop(t) {
  const x = clamp01(t);
  const s = 1.70158;
  return 1 + (s + 1) * Math.pow(x - 1, 3) + s * Math.pow(x - 1, 2);
}

/** 0 → 1 over `[start, start + length]`, then 1. Used for anything that must not animate twice. */
export function hold(pct, start, length = 8) {
  return easeOutCubic(progress(pct, start, start + length));
}

/** A window inside the scene: `[from, to]` in percent. `t` is 0 at `from`, 1 at `to`, clamped outside. */
export function segment(pct, from, to) {
  return progress(pct, from, to);
}

/** Item `index` of `count`, entering between `from` and `to`: each item's own window is offset by one slot. */
export function stagger(pct, index, count, from, to) {
  if (count <= 0) return 1;
  const span = (to - from) / Math.max(1, count);
  return progress(pct, from + span * index, from + span * index + span * 1.6);
}

/**
 * The reference's card transform, unchanged in behaviour: a prop is hidden before `enterPct`, springs into place after
 * it, and settles without motion when the viewer asked for reduced motion.
 */
export function computePropTransform({ pct, enterPct, rotateDeg, scale, reducedMotion }) {
  const shown = pct >= enterPct;
  if (!shown) {
    return { shown: false, opacity: 0, scale: 0, rotateDeg, translate: { x: 0, y: 0 } };
  }
  if (reducedMotion) {
    return { shown: true, opacity: 1, scale, rotateDeg, translate: { x: 0, y: 0 } };
  }
  const localPct = Math.min(100, ((pct - enterPct) / 18) * 100);
  const easedScale = springPop(localPct / 100) * scale;
  const dampedAngle = rotateDeg * (1 - localPct / 160);
  return { shown: true, opacity: 1, scale: easedScale, rotateDeg: dampedAngle, translate: { x: 0, y: 0 } };
}

/** The caption that is on screen at `pct`, from a scene's caption list (which need not be sorted). */
export function captionAt(captions, pct) {
  if (!captions || captions.length === 0) return "";
  const sorted = [...captions].sort((a, b) => a.atPct - b.atPct);
  let current = sorted[0].text;
  for (const caption of sorted) {
    if (pct >= caption.atPct) current = caption.text;
  }
  return current;
}

/** Which caption is *active* (index into the sorted list), for the transcript and the live region. */
export function captionIndexAt(captions, pct) {
  if (!captions || captions.length === 0) return -1;
  const sorted = [...captions].sort((a, b) => a.atPct - b.atPct);
  let index = 0;
  sorted.forEach((caption, i) => {
    if (pct >= caption.atPct) index = i;
  });
  return index;
}

/** Total film length in milliseconds. */
export function totalDurationMs(spec) {
  return (spec.scenes || []).reduce((sum, scene) => sum + scene.durationMs, 0);
}

/** Global film time (ms) → the scene index and the percentage inside it. */
export function locate(spec, elapsedMs) {
  const scenes = spec.scenes || [];
  if (scenes.length === 0) return { index: 0, pct: 0, localMs: 0 };
  let remaining = clamp(elapsedMs, 0, totalDurationMs(spec));
  for (let i = 0; i < scenes.length; i += 1) {
    const duration = scenes[i].durationMs;
    if (remaining < duration || i === scenes.length - 1) {
      return { index: i, pct: duration === 0 ? 100 : clamp((remaining / duration) * 100, 0, 100), localMs: remaining };
    }
    remaining -= duration;
  }
  const last = scenes.length - 1;
  return { index: last, pct: 100, localMs: scenes[last].durationMs };
}

/** Global film time (ms) at the start of a scene. */
export function sceneStartMs(spec, index) {
  return (spec.scenes || []).slice(0, index).reduce((sum, scene) => sum + scene.durationMs, 0);
}

/** `73250` → `"01:13.25"` — the timecode shape the timeline and the footer use. */
export function timecode(ms) {
  const safe = Math.max(0, ms);
  const minutes = Math.floor(safe / 60000);
  const seconds = Math.floor((safe % 60000) / 1000);
  const hundredths = Math.floor((safe % 1000) / 10);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(hundredths).padStart(2, "0")}`;
}

/** The four speeds the studio actually offers. The reference's button flipped through three. */
export const SPEEDS = [0.5, 1, 1.5, 2];

export function nextSpeed(speed) {
  const index = SPEEDS.indexOf(speed);
  return SPEEDS[(index + 1) % SPEEDS.length];
}
