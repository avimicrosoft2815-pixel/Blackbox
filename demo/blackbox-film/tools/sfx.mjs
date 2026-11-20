#!/usr/bin/env node
/**
 * Where the film's sound effects land, as one list that the encoder and the tests read.
 *
 * Two kinds of cue:
 *  - a `cut` at every scene boundary except the first, so a dissolve has a sound;
 *  - whatever the scene's own blueprint declares in `sfx`, in milliseconds from the start of that scene — the diagnosis
 *    beats, the approval stamp, the swell under the opening and closing cards.
 *
 * A cue list computed from the blueprint cannot drift from the picture: move a scene by 300 ms and its effects move
 * with it, because nobody wrote the absolute time down twice.
 */
import { sceneStartMs } from "../studio/js/motionMath.js";

export const SFX_DIR = "studio/sfx";

/** @returns {{sound: string, atMs: number, gain: number, why: string}[]} cues in film order */
export function cuesFor(spec, { cuts = true } = {}) {
  const cues = [];
  const last = spec.scenes.length - 1;
  spec.scenes.forEach((scene, index) => {
    const start = sceneStartMs(spec, index);
    // A cut on every boundary except the first and the last: the end card is entered on its own swell, and two effects on
    // one frame is one louder effect, not the score anyone wrote.
    if (cuts && index > 0 && index < last) {
      cues.push({ sound: "cut", atMs: start, gain: 0.13, why: `cut into scene ${scene.step}` });
    }
    for (const cue of scene.sfx ?? []) {
      cues.push({
        sound: cue.sound,
        atMs: start + Math.max(0, Number(cue.atMs ?? 0)),
        gain: Number(cue.gain ?? 0.12),
        why: `scene ${scene.step}`,
      });
    }
  });
  return cues.sort((a, b) => a.atMs - b.atMs);
}

/** The ffmpeg filter fragment for one cue, given the input index it was added as. */
export function cueFilter(cue, inputIndex) {
  const delay = Math.round(cue.atMs);
  return `[${inputIndex}:a]adelay=${delay}|${delay},volume=${cue.gain}[s${inputIndex}]`;
}
