#!/usr/bin/env node
/**
 * Encode the film: frames piped straight from the studio renderer into ffmpeg, muxed with the seven narration clips.
 *
 *   node tools/render_film.mjs                      # 1920x1080 → export/blackbox-film-1080p.mp4
 *   node tools/render_film.mjs --height 2160        # 3840x2160 → export/blackbox-film-4k.mp4
 *   node tools/render_film.mjs --limit 60 --out /tmp/probe.mp4
 *
 * Frames go over a pipe, not to disk: a 4K render is several gigabytes of PNG otherwise, and the only reason to write
 * them would be to read them back immediately. The subtitles come from the same captions the renderer burns in, so the
 * .srt and the picture cannot disagree.
 */
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createCanvas } from "@napi-rs/canvas";
import { renderFrame } from "../studio/js/frame.js";
import { locate, sceneStartMs, timecode, totalDurationMs } from "../studio/js/motionMath.js";
import { registerFonts } from "./render_frames.mjs";
import { SFX_DIR, cueFilter, cuesFor } from "./sfx.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

const LEAD_MS = 500;

function ffmpegPath() {
  if (process.env.FFMPEG) return process.env.FFMPEG;
  const venv = resolve(root, "../../.venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries");
  return process.env.FFMPEG_FALLBACK ?? join(venv, "ffmpeg-linux-x86_64-v9.0.2");
}

function parseArgs(argv) {
  const args = { width: 1920, height: 1080, fps: 30, out: null, crf: 18, limit: null, audio: true, srt: null, label: "1080p" };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--width") { args.width = Number(value); i += 1; }
    else if (key === "--height") { args.height = Number(value); i += 1; }
    else if (key === "--fps") { args.fps = Number(value); i += 1; }
    else if (key === "--out") { args.out = value; i += 1; }
    else if (key === "--crf") { args.crf = Number(value); i += 1; }
    else if (key === "--limit") { args.limit = Number(value); i += 1; }
    else if (key === "--srt") { args.srt = value; i += 1; }
    else if (key === "--label") { args.label = value; i += 1; }
    else if (key === "--no-audio") { args.audio = false; }
  }
  if (args.height >= 2000 && args.width < 3000) args.width = 3840;
  if (args.height <= 1080) args.width = Math.round((args.height * 16) / 9);
  if (!args.out) args.out = join(root, "export", `blackbox-film-${args.label}.mp4`);
  return args;
}

function srtTime(ms) {
  const safe = Math.max(0, Math.round(ms));
  const h = String(Math.floor(safe / 3600000)).padStart(2, "0");
  const m = String(Math.floor((safe % 3600000) / 60000)).padStart(2, "0");
  const s = String(Math.floor((safe % 60000) / 1000)).padStart(2, "0");
  const msPart = String(safe % 1000).padStart(3, "0");
  return `${h}:${m}:${s},${msPart}`;
}

/** The subtitle file, built from the captions that are burned into the picture. */
export function buildSrt(spec) {
  const cues = [];
  spec.scenes.forEach((scene, index) => {
    const start = sceneStartMs(spec, index);
    const captions = [...(scene.narration?.captions ?? [])].sort((a, b) => a.atMs - b.atMs);
    captions.forEach((caption, i) => {
      const from = start + caption.atMs;
      const to = i + 1 < captions.length ? start + captions[i + 1].atMs : start + scene.durationMs - 120;
      cues.push(`${cues.length + 1}\n${srtTime(from)} --> ${srtTime(to)}\n${caption.text}\n`);
    });
  });
  return `${cues.join("\n")}`;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  await registerFonts();
  const spec = JSON.parse(await readFile(join(root, "studio/spec.json"), "utf8"));
  await mkdir(dirname(args.out), { recursive: true });

  const total = totalDurationMs(spec);
  const frames = Math.min(Math.round((total / 1000) * args.fps), args.limit ?? Number.POSITIVE_INFINITY);
  const canvas = createCanvas(args.width, args.height);
  const ctx = canvas.getContext("2d");

  const narration = spec.scenes.map((scene, index) => ({
    path: join(root, "studio", scene.narration.audio),
    delayMs: sceneStartMs(spec, index) + LEAD_MS,
  }));
  // The sound design is a list computed from the blueprint (see tools/sfx.mjs), mixed under the narration rather than
  // on top of it: every gain here is small enough that the voice never has to compete.
  const cues = args.audio ? cuesFor(spec) : [];

  const ffmpeg = ffmpegPath();
  const inputArgs = [
    "-f", "rawvideo", "-pix_fmt", "rgba", "-s", `${args.width}x${args.height}`, "-r", String(args.fps), "-i", "-",
  ];
  const filterParts = [];
  const audioArgs = [];

  if (args.audio) {
    narration.forEach((clip, index) => {
      inputArgs.push("-i", clip.path);
      filterParts.push(`[${index + 1}:a]adelay=${Math.round(clip.delayMs)}|${Math.round(clip.delayMs)}[a${index}]`);
    });
    cues.forEach((cue, index) => {
      const input = narration.length + index + 1;
      inputArgs.push("-i", join(root, SFX_DIR, `${cue.sound}.wav`));
      filterParts.push(cueFilter(cue, input));
    });
    const mixed = [
      ...narration.map((_, index) => `[a${index}]`),
      ...cues.map((_, index) => `[s${narration.length + index + 1}]`),
    ].join("");
    const inputs = narration.length + cues.length;
    filterParts.push(`${mixed}amix=inputs=${inputs}:normalize=0:dropout_transition=0[voice]`);
    // A limiter, not a compressor: effects land at a fraction of the narration's level, and the ceiling is there so a
    // cue that coincides with a loud syllable cannot clip the AAC encoder.
    filterParts.push(`[voice]alimiter=limit=0.95[limit]`);
    filterParts.push(`[limit]apad=whole_dur=${(total / 1000).toFixed(3)},atrim=0:${(total / 1000).toFixed(3)}[aout]`);
    audioArgs.push("-filter_complex", filterParts.join(";"), "-map", "0:v:0", "-map", "[aout]",
      "-c:a", "aac", "-b:a", "192k", "-ar", "48000");
  } else {
    audioArgs.push("-an");
  }

  const encodeArgs = [
    ...inputArgs,
    ...audioArgs,
    "-c:v", "libx264", "-preset", "medium", "-crf", String(args.crf),
    "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "5.1",
    "-r", String(args.fps), "-g", String(args.fps * 2),
    "-movflags", "+faststart",
    "-metadata", `title=${spec.film.title}`,
    "-metadata", "comment=Synthetic incident, recorded by the BlackBox CLI. Illustrative scenario. Product in development.",
    "-y", args.out,
  ];

  console.log(`rendering ${frames} frames at ${args.width}x${args.height} → ${args.out}`);
  const child = spawn(ffmpeg, [...encodeArgs], { stdio: ["pipe", "inherit", "pipe"] });
  let stderrTail = "";
  child.stderr.on("data", (chunk) => {
    stderrTail = (stderrTail + chunk.toString()).slice(-4000);
  });

  // One error listener for the life of the pipe. Adding one per frame warns at frame 11 and hides real errors.
  let stdinError = null;
  child.stdin.on("error", (error) => { stdinError = error; });
  const write = (buffer) => new Promise((resolveWrite) => {
    if (stdinError) return resolveWrite();
    if (child.stdin.write(buffer)) resolveWrite();
    else child.stdin.once("drain", resolveWrite);
    return undefined;
  });

  const started = Date.now();
  for (let frame = 0; frame < frames; frame += 1) {
    const elapsed = (frame * 1000) / args.fps;
    const { index, pct } = locate(spec, elapsed);
    const scene = spec.scenes[index];
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    renderFrame(ctx, spec, index, pct, {
      width: args.width,
      height: args.height,
      elapsedMs: elapsed,
      timecode: `${timecode(elapsed)} · ${scene.shortTitle}`,
    });
    await write(canvas.data());
    if (stdinError) throw stdinError;
    if (frame > 0 && frame % 120 === 0) {
      const rate = frame / ((Date.now() - started) / 1000);
      process.stdout.write(`  ${frame}/${frames} frames · ${rate.toFixed(1)} fps · ${Math.round((frame / frames) * 100)}%\n`);
    }
  }

  child.stdin.end();
  const code = await new Promise((resolveExit) => child.on("close", resolveExit));
  if (code !== 0) {
    console.error(stderrTail);
    throw new Error(`ffmpeg exited ${code}`);
  }

  const srtPath = args.srt ?? join(dirname(args.out), "blackbox-film.srt");
  await writeFile(srtPath, buildSrt(spec), "utf8");

  const seconds = (Date.now() - started) / 1000;
  if (args.audio) console.log(`sound: ${narration.length} narration clip(s) and ${cues.length} effect cue(s)`);
  console.log(`done in ${seconds.toFixed(1)} s → ${args.out}`);
  console.log(`subtitles → ${srtPath}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
