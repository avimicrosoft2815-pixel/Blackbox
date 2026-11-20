#!/usr/bin/env node
/**
 * Render film frames with the *studio's own* renderer.
 *
 *   node tools/render_frames.mjs --out frames/ [--fps 30] [--width 1920] [--scene 3 --pct 50]
 *
 * Why Node at all: a browser cannot be scripted reliably in this environment, and a second renderer would be a second
 * source of truth. `@napi-rs/canvas` provides the same Canvas2D API, so `studio/js/frame.js` runs unchanged here — the
 * encoded film is the studio's picture, not a re-creation of it.
 */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createCanvas, GlobalFonts } from "@napi-rs/canvas";
import { renderFrame } from "../studio/js/frame.js";
import { locate, totalDurationMs } from "../studio/js/motionMath.js";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

const FONT_FILES = [
  ["Inter-Regular.woff2", "InterRegular"],
  ["Inter-Medium.woff2", "InterMedium"],
  ["Inter-SemiBold.woff2", "InterSemiBold"],
  ["Inter-Bold.woff2", "InterBold"],
  ["Inter-ExtraBold.woff2", "InterExtraBold"],
  ["Inter-Black.woff2", "InterBlack"],
  ["JetBrainsMono-Regular.woff2", "MonoRegular"],
  ["JetBrainsMono-Medium.woff2", "MonoMedium"],
  ["JetBrainsMono-Bold.woff2", "MonoBold"],
];

export async function registerFonts() {
  for (const [file, family] of FONT_FILES) {
    const ok = GlobalFonts.registerFromPath(join(root, "studio/assets/fonts", file), family);
    if (!ok) throw new Error(`font did not register: ${file}`);
  }
  return GlobalFonts.families.length;
}

export async function loadSpec() {
  return JSON.parse(await readFile(join(root, "studio/spec.json"), "utf8"));
}

function stamp(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const mm = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const ss = String(totalSeconds % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function parseArgs(argv) {
  const args = { fps: 30, width: 1920, height: 1080, out: null, scene: null, pct: null, from: 0, to: null };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--fps") { args.fps = Number(value); i += 1; }
    else if (key === "--width") { args.width = Number(value); i += 1; }
    else if (key === "--height") { args.height = Number(value); i += 1; }
    else if (key === "--out") { args.out = value; i += 1; }
    else if (key === "--scene") { args.scene = Number(value); i += 1; }
    else if (key === "--pct") { args.pct = Number(value); i += 1; }
    else if (key === "--from") { args.from = Number(value); i += 1; }
    else if (key === "--to") { args.to = Number(value); i += 1; }
  }
  return args;
}

export function drawAt(canvas, ctx, spec, sceneIndex, pct, width, height, extra = {}) {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  renderFrame(ctx, spec, sceneIndex, pct, { width, height, ...extra });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  await registerFonts();
  const spec = await loadSpec();

  // Still mode: one PNG per (scene, pct) — used for posters, docs and the visual review loop.
  if (args.scene !== null && args.pct !== null) {
    const outDir = args.out ?? join(root, "frames");
    await mkdir(outDir, { recursive: true });
    const canvas = createCanvas(args.width, args.height);
    const ctx = canvas.getContext("2d");
    const scene = spec.scenes[args.scene - 1];
    drawAt(canvas, ctx, spec, args.scene - 1, args.pct, args.width, args.height, {
      timecode: `${stamp(args.pct)} · ${scene.shortTitle}`,
    });
    const path = join(outDir, `scene-${String(args.scene).padStart(2, "0")}-${String(Math.round(args.pct)).padStart(3, "0")}.png`);
    await writeFile(path, canvas.toBuffer("image/png"));
    console.log(path);
    return;
  }

  const outDir = args.out ?? join(root, "frames");
  await mkdir(outDir, { recursive: true });
  const total = totalDurationMs(spec);
  const frames = Math.round((total / 1000) * args.fps);
  const last = args.to ?? frames;
  const canvas = createCanvas(args.width, args.height);
  const ctx = canvas.getContext("2d");

  let wrote = 0;
  const started = Date.now();
  for (let frame = args.from; frame < last; frame += 1) {
    const elapsed = (frame * 1000) / args.fps;
    const { index, pct } = locate(spec, elapsed);
    const scene = spec.scenes[index];
    drawAt(canvas, ctx, spec, index, pct, args.width, args.height, {
      elapsedMs: elapsed,
      timecode: `${stamp(elapsed)} · ${scene.shortTitle}`,
    });
    const name = join(outDir, `frame-${String(frame).padStart(5, "0")}.png`);
    await writeFile(name, canvas.toBuffer("image/png"));
    wrote += 1;
    if (wrote % 150 === 0) {
      const rate = wrote / ((Date.now() - started) / 1000);
      process.stdout.write(`  ${wrote}/${last - args.from} frames (${rate.toFixed(1)}/s)\n`);
    }
  }
  console.log(`wrote ${wrote} frames at ${args.width}x${args.height} → ${outDir}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
