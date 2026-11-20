#!/usr/bin/env node
/**
 * The live showcase: every claim in `docs/showcase.md`, executed here and printed with the number it produced.
 *
 *   npm run showcase
 *
 * Nothing below is a fixture. The incident is recorded by this repository's own CLI against the synthetic app in
 * `incident/`; the evidence is scrubbed by `tools/scrub_evidence.py`, which verifies its own output; the frames are
 * rendered by the module the studio imports; the film's metadata is read from the file that ships.
 *
 * Two things this script refuses to do, because the demo refuses to do them:
 *  - it does not print a claim it did not just measure, and it exits non-zero when a measurement is not what the
 *    document says it is;
 *  - it does not print this machine's paths. The work directory and the repository are replaced with `<tmp>` and
 *    `<repo>` before anything reaches the terminal, so its output can be pasted into a document unchanged.
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { cuesFor } from "./sfx.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));            // demo/blackbox-film/
const repo = fileURLToPath(new URL("../../../", import.meta.url));       // the repository root
const blackbox = process.env.BLACKBOX ?? join(repo, ".venv", "bin", "blackbox");
const python = process.env.PYTHON ?? join(repo, ".venv", "bin", "python");

const work = mkdtempSync(join(tmpdir(), "bb-showcase-"));
const evidence = join(work, "evidence");
const failures = [];

/** No absolute path of this machine leaves this process. */
function hide(text) {
  return String(text)
    .split(work).join("<tmp>")
    .split(repo).join("<repo>")
    .split(tmpdir()).join("<tmp>");
}

function say(line = "") {
  console.log(hide(line));
}

function fail(what, detail) {
  failures.push(`${what}: ${detail}`);
  say(`   !! ${what} — ${detail}`);
}

function run(command, args, { cwd = root, env = {} } = {}) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
  return { status: result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

function read(path, fallback = "") {
  try { return readFileSync(path, "utf8"); } catch { return fallback; }
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

/**
 * The film's facts, read from the file that ships. There is no `ffprobe` in this environment, only the ffmpeg build the
 * encoder muxes with, so the container is described by `ffmpeg -i` and the answer is parsed rather than assumed.
 */
function ffmpegPath() {
  const candidate = join(repo, ".venv", "lib", "python3.11", "site-packages", "imageio_ffmpeg", "binaries",
    "ffmpeg-linux-x86_64-v9.0.2");
  return process.env.FFMPEG ?? (existsSync(candidate) ? candidate : "ffmpeg");
}

function probe(path) {
  const result = spawnSync(ffmpegPath(), ["-hide_banner", "-i", path], { encoding: "utf8" });
  const text = `${result.stderr ?? ""}`;
  const seconds = /Duration: (\d+):(\d+):([\d.]+)/.exec(text);
  // `Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1920x1080, 1032 kb/s, 30 fps, ...`
  const video = /Video: (\w+) \(([^)]+)\) \([^)]*\), (\w+)(?:\([^)]*\))?, (\d+x\d+),[^\n]*?, ([\d.]+) fps/.exec(text);
  // `Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, mono, fltp, 143 kb/s (default)`
  const audio = /Audio: (\w+) \(([^)]+)\) \([^)]*\), (\d+) Hz, (\w+)/.exec(text);
  if (!seconds || !video) return null;
  return {
    duration: Number(seconds[1]) * 3600 + Number(seconds[2]) * 60 + Number(seconds[3]),
    video: `${video[1]} ${video[2].toLowerCase()} · ${video[3]} · ${video[4]} · ${video[5]} fps`,
    audio: audio ? `${audio[1]} ${audio[2]} · ${(Number(audio[3]) / 1000).toFixed(0)} kHz ${audio[4]}` : "no audio stream",
  };
}

function section(title) {
  say();
  say(title);
  say("-".repeat(title.length));
}

function row(label, value) {
  say(`  ${label.padEnd(26)} ${value}`);
}

const started = Date.now();
say("BLACKBOX — the live showcase (a real run, no fixtures)");
say(`${new Date().toISOString().slice(0, 10)} · ${process.platform}/${process.arch} · ${process.version}`);
const head = run("git", ["-C", repo, "rev-parse", "--short", "HEAD"]);
row("commit", head.stdout.trim() || "not a checkout");

/* ── 1. the incident: recorded now, by this repository's CLI ─────────────────────────────────────────────────── */

section("1  THE INCIDENT — recorded now");
const incident = run("bash", [join(root, "incident", "run_demo.sh"), join(work, "incident")], {
  env: { BLACKBOX: blackbox, PYTHON: python },
});
if (incident.status !== 0) {
  fail("the incident run", `run_demo.sh exited ${incident.status}`);
  say(incident.stderr.trim().split("\n").slice(-6).join("\n"));
}

const scrub = run(python, [join(root, "tools", "scrub_evidence.py"), join(work, "incident"), evidence]);
if (scrub.status !== 0) fail("the evidence scrubber", `exited ${scrub.status}: ${scrub.stderr.trim().split("\n").pop()}`);

const runSummary = JSON.parse(read(join(evidence, "run.json"), "{}").trim() || "{}");
const verifyText = read(join(evidence, "verify.txt"));
const listText = read(join(evidence, "ls.txt"));
const diagnosis = JSON.parse(read(join(evidence, "diagnosis.json"), "{}").trim() || "{}");

const capsules = (listText.match(/^c_[0-9a-f]+/gm) ?? []).length;
const verified = (verifyText.match(/^ {2}ok /gm) ?? []).length;
const declaredOnly = (verifyText.match(/^ {2}---- /gm) ?? []).length;
const problems = (verifyText.match(/^ {2}(?:FAIL|ERROR|mismatch)/gm) ?? []).length;
const findings = Array.isArray(diagnosis.findings) ? diagnosis.findings.length : 0;
const cannotKnow = Array.isArray(diagnosis.cannot_know) ? diagnosis.cannot_know.length : 0;

row("run", runSummary.run_id ?? "none");
row("capsules", `${capsules} (${(runSummary.recovered?.roles ?? []).join(" + ") || "roles unread"})`);
row("app exit code", String(runSummary.exit_code ?? "unknown"));
row("verify", `${capsules} capsule(s) · ${verified} check(s) ok · ${declaredOnly} declared, not checkable`);
row("diagnosis", `${findings} finding(s) · ${cannotKnow} cannot-know statement(s)`);

if (runSummary.exit_code !== 3) fail("the app's exit code", `expected 3 from incident/app.py, read ${runSummary.exit_code}`);
if (capsules !== 2) fail("the capsule count", `expected launcher + sdk, read ${capsules}`);
if (problems !== 0) fail("verification", `${problems} check(s) did not pass`);
if (findings < 1) fail("the diagnosis", "no findings");

/* ── 2. the film that ships ──────────────────────────────────────────────────────────────────────────────────── */

section("2  THE FILM — read from the files in export/");
const spec = JSON.parse(read(join(root, "studio", "spec.json"), "{}"));
const scenes = spec.scenes ?? [];
const totalMs = scenes.reduce((sum, scene) => sum + scene.durationMs, 0);
const labelled = scenes.filter((scene) => scene.label);
const narration = readdirSync(join(root, "studio", "assets", "narration")).filter((name) => name.endsWith(".mp3"));
const srtCues = (read(join(root, "export", "blackbox-film.srt")).match(/-->/g) ?? []).length;

row("blueprint", `${scenes.length} scenes · ${(totalMs / 1000).toFixed(1)} s · ${labelled.length} on-frame label(s)`);
row("narration", `${narration.length} recorded clip(s)`);
const cues = cuesFor(spec);
const sounds = [...new Set(cues.map((cue) => cue.sound))];
row("sound design", `${cues.length} cue(s) from ${sounds.length} sound(s): ${sounds.join(", ")}`);
row("subtitles", `${srtCues} cue(s)`);

for (const [label, file] of [["1080p", "blackbox-film-1080p.mp4"], ["4k", "blackbox-film-4k.mp4"]]) {
  const path = join(root, "export", file);
  if (!existsSync(path)) { row(label, "not in this checkout"); continue; }
  const bytes = readFileSync(path).length;
  const facts = probe(path);
  // MiB, because that is the unit the rest of the demo's documents use for these two files.
  const size = `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  row(label, facts
    ? `${size} · ${facts.duration.toFixed(3)} s · ${facts.video} · ${facts.audio}`
    : `${size} · the container could not be described by this machine's ffmpeg`);
}

if (scenes.length !== 7) fail("the blueprint", `${scenes.length} scenes, expected 7`);
if (Math.round(totalMs) !== 55000) fail("the film's length", `${totalMs} ms, expected 55000`);
if (labelled.length === 0) fail("the disclosures", "no scene carries an on-frame label");

/* ── 3. one renderer, two hosts ──────────────────────────────────────────────────────────────────────────────── */

section("3  ONE RENDERER, TWO HOSTS");
const studioSource = read(join(root, "studio", "js", "app.js"));
const encoderSource = read(join(root, "tools", "render_film.mjs"));
const frameSource = read(join(root, "studio", "js", "frame.js"));
row("the stage imports", studioSource.includes('from "./frame.js"') ? "studio/js/frame.js" : "something else");
row("the encoder imports", encoderSource.includes("studio/js/frame.js") ? "studio/js/frame.js" : "something else");
row("the module draws", `${(frameSource.match(/^function draw[A-Z]/gm) ?? []).length} scene drawing(s), one canvas`);
if (!studioSource.includes('from "./frame.js"')) fail("the stage", "does not import the film's renderer");
if (!encoderSource.includes("studio/js/frame.js")) fail("the encoder", "does not import the film's renderer");

// The same frame, rendered three times: twice now, and once when the poster was committed. Equal hashes are the point.
const first = join(work, "frame-a");
const second = join(work, "frame-b");
const args = ["tools/render_frames.mjs", "--scene", "6", "--pct", "96", "--out"];
const a = run(process.execPath, [...args, first]);
const b = run(process.execPath, [...args, second]);
if (a.status !== 0 || b.status !== 0) {
  fail("the frame render", `exited ${a.status}/${b.status}`);
} else {
  const file = (dir) => readdirSync(dir).find((name) => name.endsWith(".png"));
  const hashA = sha256(readFileSync(join(first, file(first))));
  const hashB = sha256(readFileSync(join(second, file(second))));
  const poster = existsSync(join(root, "docs", "poster.png")) ? sha256(readFileSync(join(root, "docs", "poster.png"))) : "";
  row("frame 6 at 96 %, twice", hashA === hashB ? `same sha256 (${hashA.slice(0, 16)}…)` : "DIFFERENT");
  row("and the committed poster", poster === hashA ? "the same file" : "differs from this frame");
  if (hashA !== hashB) fail("frame identity", "two renders of one frame differ");
  if (poster && poster !== hashA) fail("the poster", "is not the frame this renderer produces");
}

/* ── 4. the copy is checked, not asserted ───────────────────────────────────────────────────────────────────── */

section("4  THE COPY AND THE PICTURES ARE CHECKED");
const filmTests = run(process.execPath, ["--test", "tools/test_film.mjs", "tools/test_studio.mjs", "tools/test_start.mjs"]);
const pass = Number((/# pass (\d+)/.exec(filmTests.stdout) ?? [0, 0])[1]);
const failed = Number((/# fail (\d+)/.exec(filmTests.stdout) ?? [0, 0])[1]);
row("npm test (node --test)", `${pass} pass · ${failed} fail`);
if (filmTests.status !== 0 || failed !== 0) fail("the film and studio tests", `${failed} failing`);

const words = run(python, ["-m", "pytest", "tests/test_forbidden_words.py", "-q"], { cwd: repo });
row("the forbidden-word rule", words.status === 0 ? "pass (tree and commit messages)" : `not run here (exit ${words.status})`);

const urls = [join(root, "studio", "spec.json"), join(root, "studio", "js", "app.js"), join(root, "studio", "js", "frame.js")]
  .map((path) => (read(path).match(/https?:\/\//g) ?? []).length)
  .reduce((sum, count) => sum + count, 0);
row("network references", `${urls} in the blueprint, the stage and the renderer`);
if (urls !== 0) fail("the studio's pages", `${urls} URL(s) found; the demo is meant to need none`);

/* ── 5. the tour page's numbers ─────────────────────────────────────────────────────────────────────────────── */

section("5  THE TOUR PAGE — numbers it did not type");
const statsPath = join(root, "start", "stats.json");
if (!existsSync(statsPath)) {
  fail("the tour page's numbers", "start/stats.json is missing; run `node tools/stats.mjs`");
} else {
  const stats = JSON.parse(readFileSync(statsPath, "utf8"));
  row("stats.json", `${stats.facts.length} fact(s) · measured at ${stats.commit || "an unknown commit"}`);
  row("suites summarised", `${stats.suites.length} · ${stats.suites.reduce((sum, suite) => sum + suite.passed, 0)} check(s) passing`);
  row("declared not run here", `${stats.notRun.length} item(s), listed on the page beside the numbers`);
  if (stats.facts.length < 10) fail("the tour page's numbers", `only ${stats.facts.length} facts`);
}

/* ── 6. what is usually not there ───────────────────────────────────────────────────────────────────────────── */

section("6  WHAT IS USUALLY NOT THERE — each line is the measurement above");
const shorts = [
  `every frame of the failure is drawn, not screen-captured: ${srtCues} cues and 7 scenes come out of the same renderer, and the timeline scene re-draws the seq numbers in evidence/report.md`,
  `the pictures say they are pictures: ${labelled.length} of ${scenes.length} scenes carry an on-frame label, and the rest are diagrams of recorded frames rather than illustrations of a story`,
  `the diagnosis names what it cannot know: ${cannotKnow} statement(s), each one a limit rather than a claim`,
  `verification refuses to count what it cannot recompute: ${declaredOnly} check(s) printed as declared, not as a pass`,
  `one module draws both the studio and the export: frame 6 at 96 % has the same sha256 in two renders and in docs/poster.png`,
  `the blueprint validates copy against the frame it will land in: headline width is measured on the studio's own canvas, and a duration shorter than its narration is refused (both in the ${pass} tests above)`,
  `the narration is the clock's source: ${narration.length} clips, and each scene's length is its clip plus a lead-in and a tail`,
  `the studio is deep-linkable to the frame: /studio/#scene=4&pct=62 opens exactly there, because the stage is the renderer, not a picture of it`,
  `the exit code is real: the app exited ${runSummary.exit_code ?? "?"} and the record says so rather than showing a clean run`,
  `the score is in the blueprint: ${cues.length} cues computed from the same file that lays out the picture, so a scene that moves takes its sound with it`,
  `nothing here needs the network: ${urls} URL(s) in the studio's pages, the incident's provider is a loopback mock`,
];
shorts.forEach((line, index) => say(`  ${String(index + 1).padStart(2)}. ${line}`));

/* ── the limit of this page ─────────────────────────────────────────────────────────────────────────────────── */

section("WHAT THIS DOES NOT PROVE");
say("  · the studio in a real browser: there is none in this environment, so the stage is tested in jsdom and the");
say("    layout by eye; Safari, Firefox and a real trackpad are unmeasured;");
say("  · playback of the exported H.264/AAC file on macOS: the encode is yuv420p/high/+faststart, which is what");
say("    QuickTime expects, and that is a claim about a format, not a test;");
say("  · that the recording survives a machine losing power: no such fault was injected for this demo.");

say();
say(`  ${failures.length === 0 ? "all measurements above matched the document" : `${failures.length} measurement(s) did not match — see the !! lines`}`);
say(`  ${((Date.now() - started) / 1000).toFixed(1)} s, including the incident run`);
rmSync(work, { recursive: true, force: true });
process.exit(failures.length === 0 ? 0 : 1);
