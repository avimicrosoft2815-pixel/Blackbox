/**
 * Tests for the film itself: the blueprint, the narration files it points at, the renderer, and the evidence the film
 * claims to show.
 *
 * These are the checks that keep the film honest rather than pretty:
 *  - every scene's duration really does hold its narration, measured from the MP3 rather than trusted from the spec;
 *  - the captions sit inside their scene and in order;
 *  - any scene that illustrates rather than records carries a label;
 *  - no frame is drawn with a word the repository bans, an absolute home path, or a leftover product name;
 *  - the renderer is a pure function of (scene, pct): same input, same bytes;
 *  - the incident the film animates is the incident in the evidence directory, frame for frame.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { createCanvas } from "@napi-rs/canvas";
import { renderFrame } from "../studio/js/frame.js";
import { registerFonts } from "./render_frames.mjs";
import { locate, sceneStartMs, totalDurationMs } from "../studio/js/motionMath.js";

const root = new URL("../", import.meta.url);
const path = (relative) => join(root.pathname, relative);
const spec = JSON.parse(await readFile(path("studio/spec.json"), "utf8"));

const FFMPEG = process.env.FFMPEG
  ?? join(root.pathname, "../../.venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v9.0.2");

function mp3DurationMs(file) {
  const probe = spawnSync(FFMPEG, ["-hide_banner", "-i", file], { encoding: "utf8" });
  const output = `${probe.stderr}${probe.stdout}`;
  const match = /Duration: (\d+):(\d+):(\d+\.\d+)/.exec(output);
  if (!match) return null;
  const [, h, m, s] = match;
  return Math.round((Number(h) * 3600 + Number(m) * 60 + Number(s)) * 1000);
}

/* ── the blueprint ─────────────────────────────────────────────────────────────────────────────────────────────── */

test("the blueprint has seven scenes in the reference's order", () => {
  assert.equal(spec.schema, "blackbox.demo-blueprint/1");
  assert.deepEqual(spec.scenes.map((scene) => scene.mode), [
    "split-collage", "arch-graph", "before-after", "ember-timeline", "hud-telemetry", "code-diff", "warp-reveal",
  ]);
  assert.deepEqual(spec.scenes.map((scene) => scene.step), [1, 2, 3, 4, 5, 6, 7]);
});

test("every scene's duration holds its narration with a lead-in and a tail", () => {
  for (const scene of spec.scenes) {
    const audio = scene.narration.audioMs;
    assert.ok(scene.durationMs >= audio + 500, `${scene.id}: ${scene.durationMs} ms cannot hold ${audio} ms of speech plus a 500 ms lead`);
    assert.ok(scene.durationMs - audio - 500 >= 250, `${scene.id}: needs at least 250 ms of air after the sentence`);
  }
});

test("caption timings are inside their scene, in order, and start at the 500 ms lead", () => {
  for (const scene of spec.scenes) {
    const captions = scene.narration.captions;
    assert.ok(captions.length >= 2, `${scene.id}: a scene needs at least two caption lines`);
    captions.forEach((caption, index) => {
      assert.ok(caption.atPct >= 0 && caption.atPct <= 100, `${scene.id}: caption ${index} outside 0–100`);
      assert.ok(caption.atMs >= 500 && caption.atMs < scene.durationMs, `${scene.id}: caption ${index} outside the scene`);
      if (index > 0) {
        assert.ok(caption.atPct > captions[index - 1].atPct, `${scene.id}: caption ${index} is not after ${index - 1}`);
        assert.ok(caption.atMs > captions[index - 1].atMs, `${scene.id}: caption ${index} time is not increasing`);
      }
    });
  }
});

test("narration files exist and the declared length matches the MP3", async () => {
  const probe = spawnSync(FFMPEG, ["-version"], { encoding: "utf8" });
  if (probe.error) {
    console.warn("ffmpeg not found — skipping the audio-length check");
    return;
  }
  for (const scene of spec.scenes) {
    const file = path(join("studio", scene.narration.audio));
    const info = await stat(file);
    assert.ok(info.size > 2000, `${scene.id}: ${scene.narration.audio} looks empty`);
    const measured = mp3DurationMs(file);
    assert.ok(measured, `${scene.id}: could not read a duration from ${scene.narration.audio}`);
    const delta = Math.abs(measured - scene.narration.audioMs);
    assert.ok(delta <= 120, `${scene.id}: spec says ${scene.narration.audioMs} ms, the file is ${measured} ms`);
  }
});

test("scenes that illustrate rather than record say so on the frame", () => {
  const needsLabel = spec.scenes.filter((scene) => ["split-collage", "before-after", "code-diff"].includes(scene.mode));
  assert.equal(needsLabel.length, 3);
  needsLabel.forEach((scene) => {
    assert.match(scene.label ?? "", /illustrative|representative/i, `${scene.id} needs a visible label`);
  });
  assert.match(spec.film.closingNote, /illustrative/i);
  assert.match(spec.film.closingNote, /development/i);
});

/* ── honesty ─────────────────────────────────────────────────────────────────────────────────────────────────── */

async function walk(dir) {
  const { readdir } = await import("node:fs/promises");
  const { Dirent } = await import("node:fs");
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (entry.name === "node_modules" || entry.name === "export" || entry.name === "frames" || entry.name.startsWith(".")) continue;
    const full = join(dir, entry.name);
    if (entry instanceof Dirent && entry.isDirectory()) files.push(...await walk(full));
    else if (/\.(json|js|mjs|md|html|css|txt|srt|sh|py)$/.test(entry.name)) files.push(full);
  }
  return files;
}

test("no banned claim, home path or old product name appears in the film's own files", async () => {
  // Every file but this one: the scanner necessarily contains the patterns it looks for.
  const files = (await walk(root.pathname)).filter((file) => !file.endsWith("tools/test_film.mjs"));
  assert.ok(files.length > 15, "expected to scan the film's source files");
  // The repository's own banned-word test is the authority on copy; this list only covers what would make the film
  // dishonest if it slipped in. Spell the banned token with a character class so this file stays clean when scanned.
  const banned = [
    [/forge ?pulse/i, "the reference product's name"],
    [/\/home\/[a-z]+/i, "an absolute home path"],
    [/\bdeter\w*istic\b/i, "a word the repository bans in copy"],
    [/\b(100%|guaranteed|zero[- ]downtime|enterprise[- ]grade)\b/i, "an unearned superlative"],
    [/\b\d+%\s*(faster|cheaper|reduction)/i, "a performance claim"],
  ];
  for (const file of files) {
    const text = await readFile(file, "utf8");
    for (const [pattern, why] of banned) {
      assert.ok(!pattern.test(text), `${file.replace(root.pathname, "")} contains ${why}: ${pattern}`);
    }
  }
});

test("the evidence directory holds the incident the film animates", async () => {
  const run = JSON.parse(await readFile(path("evidence/run.json"), "utf8"));
  assert.equal(run.run_id, spec.evidence.runId, "the run the blueprint names is the run in the evidence");

  // The frame count is read from the capsule listing the CLI printed, not from the blueprint.
  const listing = await readFile(path("evidence/ls.txt"), "utf8");
  const rows = listing.split("\n")
    .map((line) => line.trim().split(/\s+/))
    .filter((cells) => cells.length >= 5 && cells[1] === spec.evidence.runId)
    .map((cells) => ({ capsule: cells[0], events: Number(cells[3]) }));
  assert.ok(rows.length >= 1, "the listing should contain the run's capsules");
  const counted = Math.max(...rows.map((row) => row.events));
  assert.ok(counted >= 10, `expected a recorded run, saw ${counted} events`);
  assert.deepEqual(rows.map((row) => row.capsule).sort(), [...spec.evidence.capsules].sort(),
    "the capsules the blueprint names are the capsules on disk");
  const diagnosis = JSON.parse(await readFile(path("evidence/diagnosis.json"), "utf8"));
  const findings = diagnosis.findings ?? [];
  assert.equal(findings.length, 2, "the film says two findings");
  assert.ok(findings.every((finding) => finding.confidence === "high"), "the film says both are high confidence");
  assert.ok(findings.every((finding) => finding.evidence && finding.evidence.length > 0), "each finding rests on frames");

  const timeline = spec.scenes[3].timelineTracks.map((track) => track.clip);
  ["RUN_START", "MODEL_REQUEST", "MODEL_CHUNK", "MODEL_ERROR"].forEach((kind) => {
    assert.ok(timeline.includes(kind), `the timeline track list should name ${kind}`);
  });
  assert.equal(spec.evidence.frames, counted, "the blueprint's frame count matches the capsule");
  assert.ok(spec.scenes[4].hudCounters.some((counter) => counter.code === "CANNOT TELL"), "the diagnosis keeps its unknowns");
});

/* ── the renderer ─────────────────────────────────────────────────────────────────────────────────────────────── */

test("the renderer is a pure function of scene and percent", async () => {
  await registerFonts();
  const canvas = createCanvas(640, 360);
  const ctx = canvas.getContext("2d");
  const paint = () => {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    renderFrame(ctx, spec, 3, 52, { width: 640, height: 360, showCaptions: true });
    return createHash("sha256").update(canvas.data()).digest("hex");
  };
  const first = paint();
  const second = paint();
  assert.equal(first, second, "two renders of the same frame must be byte-identical");

  renderFrame(ctx, spec, 3, 78, { width: 640, height: 360, showCaptions: true });
  const other = createHash("sha256").update(canvas.data()).digest("hex");
  assert.notEqual(first, other, "a different percent must produce a different frame");
});

test("every scene paints at several points without throwing, captions on and off", async () => {
  await registerFonts();
  const canvas = createCanvas(640, 360);
  const ctx = canvas.getContext("2d");
  for (let index = 0; index < spec.scenes.length; index += 1) {
    for (const pct of [0, 12, 35, 55, 78, 96, 100]) {
      for (const showCaptions of [true, false]) {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        renderFrame(ctx, spec, index, pct, { width: 640, height: 360, showCaptions });
      }
    }
  }
});

test("a frame is not blank: the stage actually draws", async () => {
  await registerFonts();
  const canvas = createCanvas(480, 270);
  const ctx = canvas.getContext("2d");
  renderFrame(ctx, spec, 6, 90, { width: 480, height: 270 });
  const data = canvas.data();
  let bright = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i] + data[i + 1] + data[i + 2] > 180) bright += 1;
  }
  assert.ok(bright > data.length / 4 / 40, `the end card should have lit pixels, found ${bright}`);
});

test("the film's own clock maps every scene boundary to the right scene", () => {
  const total = totalDurationMs(spec);
  assert.equal(total, 55000, "the film is 55 s: 46.7 s of narration plus leads and tails");
  spec.scenes.forEach((scene, index) => {
    const start = sceneStartMs(spec, index);
    assert.equal(locate(spec, start).index, index, `scene ${index + 1} should start at its own timecode`);
    assert.equal(locate(spec, start).pct, 0);
    assert.equal(locate(spec, start + scene.durationMs * 0.5).index, index);
  });
  assert.equal(locate(spec, total).index, spec.scenes.length - 1);
});

/* ── the sound design ───────────────────────────────────────────────────────────────────────────────────────────── */

test("every sound cue lands inside the film, on a file that exists, without piling up", async () => {
  const { cuesFor, SFX_DIR } = await import("./sfx.mjs");
  const cues = cuesFor(spec);
  const total = totalDurationMs(spec);

  assert.ok(cues.length >= 7, `expected a score, found ${cues.length} cues`);
  // A cut on every boundary except the first (nothing to cut from) and the last (the end card is entered on its swell).
  const cuts = cues.filter((cue) => cue.sound === "cut").length;
  assert.equal(cuts, spec.scenes.length - 2, "a cut at every interior boundary and none in the middle of a scene");

  for (const cue of cues) {
    const file = path(`${SFX_DIR}/${cue.sound}.wav`);
    const info = await stat(file).catch(() => null);
    assert.ok(info, `no sound file for ${cue.sound} (${file})`);
    assert.ok(info.size > 1000, `${cue.sound}.wav looks empty`);
    assert.ok(cue.atMs >= 0 && cue.atMs < total, `${cue.sound} at ${cue.atMs} ms is outside the film`);
    assert.ok(cue.gain > 0 && cue.gain <= 1, `${cue.sound} has a gain of ${cue.gain}`);
  }

  // Two effects on the same frame read as one louder effect, which is not the score anyone wrote.
  const sorted = [...cues].sort((a, b) => a.atMs - b.atMs);
  for (let i = 1; i < sorted.length; i += 1) {
    assert.ok(sorted[i].atMs - sorted[i - 1].atMs >= 150,
      `${sorted[i].sound} and ${sorted[i - 1].sound} are ${sorted[i].atMs - sorted[i - 1].atMs} ms apart`);
  }
});

test("each scene's sound notes describe the cues it actually has", async () => {
  const { cuesFor } = await import("./sfx.mjs");
  const cues = cuesFor(spec);
  spec.scenes.forEach((scene, index) => {
    const notes = scene.narration?.sfxNotes ?? [];
    assert.ok(notes.length > 0, `scene ${scene.step} declares no sound design`);
    const start = sceneStartMs(spec, index);
    const inside = cues.filter((cue) => cue.atMs >= start && cue.atMs < start + scene.durationMs);
    const sounds = new Set(inside.map((cue) => cue.sound));
    // The notes name what happens; the map below is the film's own vocabulary, not a copy of the prose.
    const expected = { rise: "swell", pulse: "tick", stamp: "stamp", cut: "cut" };
    for (const sound of sounds) {
      assert.ok(notes.some((note) => note.includes(expected[sound])),
        `scene ${scene.step} mixes ${sound} but its notes say ${JSON.stringify(notes)}`);
    }
  });
});
