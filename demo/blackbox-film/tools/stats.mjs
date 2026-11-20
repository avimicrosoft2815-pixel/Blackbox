#!/usr/bin/env node
/**
 * The numbers on the tour page, measured rather than typed.
 *
 *   node tools/stats.mjs            # writes start/stats.json and prints what it measured
 *   node tools/stats.mjs --check    # re-measures and fails if the committed file is stale
 *
 * What is here is what this machine could run: the film and studio tests, a named set of Python suites, one real incident
 * recorded by the CLI, and the two exported files' own metadata. What could not be run here is listed as well, by name,
 * with the reason — a page that only lists its successes is a brochure.
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cuesFor } from "./sfx.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(root, "..", "..");
const blackbox = process.env.BLACKBOX ?? join(repo, ".venv", "bin", "blackbox");
const python = process.env.PYTHON ?? join(repo, ".venv", "bin", "python");
const outPath = join(root, "start", "stats.json");

const PYTHON_SUITES = [
  ["tests/test_forbidden_words.py", "the words this repository refuses to use, in the tree and in commit messages"],
  ["tests/test_redaction_every_sink.py", "one corpus of secrets, every place a capsule can write, each asserted"],
  ["tests/test_xss_corpus.py", "twenty payloads, checked against the same sinks"],
  ["tests/test_no_egress.py", "nothing leaves the machine while a run is recorded"],
  ["tests/test_capsule_attacks.py", "capsules that were tampered with are refused, not rendered"],
  ["tests/test_verify_cli.py", "verify refuses what it cannot recompute"],
  ["tests/test_diagnose.py", "the diagnosis separates observed facts from possible explanations"],
  ["tests/test_repair_workflow.py", "a repair is previewed, approved by token, then checked"],
  ["tests/test_docs_links.py", "the documents' cross-references and tables"],
];

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { cwd: repo, encoding: "utf8", env: { ...process.env }, ...options });
  return { status: result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

function ffmpeg() {
  const candidate = join(repo, ".venv", "lib", "python3.11", "site-packages", "imageio_ffmpeg", "binaries",
    "ffmpeg-linux-x86_64-v9.0.2");
  return process.env.FFMPEG ?? (existsSync(candidate) ? candidate : "ffmpeg");
}

function probeFilm(path) {
  if (!existsSync(path)) return null;
  const result = spawnSync(ffmpeg(), ["-hide_banner", "-i", path], { encoding: "utf8" });
  const text = `${result.stderr ?? ""}`;
  const seconds = /Duration: (\d+):(\d+):([\d.]+)/.exec(text);
  const size = readFileSync(path).length;
  return {
    megabytes: Number((size / 1024 / 1024).toFixed(1)),
    bytes: size,
    seconds: seconds ? Number(seconds[1]) * 3600 + Number(seconds[2]) * 60 + Number(seconds[3]) : null,
    sha256: createHash("sha256").update(readFileSync(path)).digest("hex").slice(0, 16),
  };
}

const work = mkdtempSync(join(tmpdir(), "bb-stats-"));
const facts = [];
const suites = [];
const notRun = [];

function fact(id, label, value, unit, source) {
  facts.push({ id, label, value, unit: unit ?? "", source });
}

/* ── the film and the studio's own tests ────────────────────────────────────────────────────────────────────── */

const filmTests = run(process.execPath,
  ["--test", "tools/test_film.mjs", "tools/test_studio.mjs", "tools/test_start.mjs"], { cwd: root });
const filmPass = Number((/# pass (\d+)/.exec(filmTests.stdout) ?? [0, 0])[1]);
const filmFail = Number((/# fail (\d+)/.exec(filmTests.stdout) ?? [0, 0])[1]);
suites.push({
  name: "the film and the studio",
  command: "npm test",
  files: ["tools/test_film.mjs", "tools/test_studio.mjs", "tools/test_start.mjs"],
  passed: filmPass,
  failed: filmFail,
  ok: filmTests.status === 0,
  what: "blueprint order, durations against the measured narration, caption bounds, on-frame labels, the banned-word rule, frame purity, clock mapping, and fourteen studio behaviour checks in jsdom",
});

/* ── the Python suites this machine can run ─────────────────────────────────────────────────────────────────── */

let pythonPassed = 0;
let pythonFailed = 0;
for (const [file, what] of PYTHON_SUITES) {
  if (!existsSync(join(repo, file))) {
    notRun.push({ what: file, why: "not in this checkout" });
    continue;
  }
  // pytest's prose summary is not printed at every verbosity on every version, so the count comes from its own XML
  // report rather than from a regular expression over the dots.
  const report = join(work, `${file.replace(/\W/g, "-")}.xml`);
  const result = run(python, ["-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-o", "junit_family=xunit2", `--junit-xml=${report}`, file]);
  let passed = 0;
  let failed = 0;
  if (existsSync(report)) {
    const xml = readFileSync(report, "utf8");
    // `<testsuite ` with the space: `<testsuites>` is the root wrapper and carries no counts of its own.
    const suite = /<testsuite\s[^>]*>/.exec(xml)?.[0] ?? "";
    const attr = (name) => Number((new RegExp(`${name}="(\\d+)"`).exec(suite) ?? [0, 0])[1]);
    const skipped = attr("skipped");
    passed = Math.max(0, attr("tests") - attr("failures") - attr("errors") - skipped);
    failed = attr("failures") + attr("errors");
  } else {
    failed = result.status === 0 ? 0 : 1;
  }
  pythonPassed += passed;
  pythonFailed += failed;
  suites.push({ name: file.replace(/^tests\//, ""), command: `pytest -q ${file}`, files: [file], passed, failed, ok: result.status === 0, what });
}

fact("tests.python", "Python checks in the named suites", pythonPassed, "checks", "start/stats.json · suites[]");
fact("tests.film", "film and studio checks", filmPass, "checks", "tools/test_film.mjs, tools/test_studio.mjs, tools/test_start.mjs");
fact("tests.suites", "suites summarised here", suites.length, "suites", "start/stats.json · suites[]");

/* ── one incident, recorded now ─────────────────────────────────────────────────────────────────────────────── */

const incident = run("bash", [join(root, "incident", "run_demo.sh"), join(work, "incident")], {
  cwd: root,
  env: { ...process.env, BLACKBOX: blackbox, PYTHON: python },
});
let incidentFacts = null;
if (incident.status === 0) {
  const scrubDir = join(work, "evidence");
  const scrub = run(python, [join(root, "tools", "scrub_evidence.py"), join(work, "incident"), scrubDir]);
  if (scrub.status === 0) {
    // The scrubber splits the app's stdout into run.out and leaves run.json as the summary object alone.
    const summary = JSON.parse(readFileSync(join(scrubDir, "run.json"), "utf8"));
    const verify = readFileSync(join(scrubDir, "verify.txt"), "utf8");
    const list = readFileSync(join(scrubDir, "ls.txt"), "utf8");
    const diagnosis = JSON.parse(readFileSync(join(scrubDir, "diagnosis.json"), "utf8"));
    incidentFacts = {
      runId: summary.run_id,
      exitCode: summary.exit_code,
      capsules: (list.match(/^c_[0-9a-f]+/gm) ?? []).length,
      verifyOk: (verify.match(/^ {2}ok /gm) ?? []).length,
      verifyDeclared: (verify.match(/^ {2}---- /gm) ?? []).length,
      findings: (diagnosis.findings ?? []).length,
      cannotKnow: (diagnosis.cannot_know ?? []).length,
      observed: (diagnosis.observed ?? []).length,
    };
    fact("incident.capsules", "capsules written by one run", incidentFacts.capsules, "capsules", "evidence/ls.txt");
    fact("incident.verifyOk", "verify checks that could be recomputed", incidentFacts.verifyOk, "checks", "evidence/verify.txt");
    fact("incident.verifyDeclared", "checks printed as declared, not counted", incidentFacts.verifyDeclared, "checks", "evidence/verify.txt");
    fact("incident.findings", "findings in the diagnosis", incidentFacts.findings, "findings", "evidence/diagnosis.json");
    fact("incident.cannotKnow", "limits the diagnosis states", incidentFacts.cannotKnow, "statements", "evidence/diagnosis.json");
    fact("incident.exitCode", "the app's own exit code, kept", incidentFacts.exitCode, "", "evidence/run.json");
  } else {
    notRun.push({ what: "the incident's evidence", why: `scrub_evidence.py exited ${scrub.status}` });
  }
} else {
  notRun.push({ what: "the incident", why: `run_demo.sh exited ${incident.status} on this machine` });
}

/* ── the film's own metadata ────────────────────────────────────────────────────────────────────────────────── */

const spec = JSON.parse(readFileSync(join(root, "studio", "spec.json"), "utf8"));
const totalMs = spec.scenes.reduce((sum, scene) => sum + scene.durationMs, 0);
const cues = cuesFor(spec);
const srtCues = (readFileSync(join(root, "export", "blackbox-film.srt"), "utf8").match(/-->/g) ?? []).length;
const narration = countNarration();

function countNarration() {
  try {
    return readdirSync(join(root, "studio", "assets", "narration")).filter((name) => name.endsWith(".mp3")).length;
  } catch { return 0; }
}

fact("film.scenes", "scenes", spec.scenes.length, "scenes", "studio/spec.json");
fact("film.seconds", "length", Number((totalMs / 1000).toFixed(1)), "seconds", "studio/spec.json");
fact("film.cues", "sound cues mixed under the narration", cues.length, "cues", "tools/sfx.mjs · studio/spec.json");
fact("film.narration", "recorded narration clips", narration, "clips", "studio/assets/narration/");
fact("film.subtitles", "subtitle cues in the .srt", srtCues, "cues", "export/blackbox-film.srt");

const hd = probeFilm(join(root, "export", "blackbox-film-1080p.mp4"));
const uhd = probeFilm(join(root, "export", "blackbox-film-4k.mp4"));
if (hd) fact("film.hd", "the 1080p master", hd.megabytes, "MB", "export/blackbox-film-1080p.mp4");
if (uhd) fact("film.uhd", "the 4K master", uhd.megabytes, "MB", "export/blackbox-film-4k.mp4");

/* ── what this machine could not run ───────────────────────────────────────────────────────────────────────── */

notRun.push(
  { what: "the rest of the Python suite", why: "this environment is missing pieces some suites need (an HTTP client, a browser, a Go toolchain); CI runs the whole thing" },
  { what: "the three operating systems", why: "CI runs ubuntu, macOS and Windows; this is one Linux machine" },
  { what: "the desktop viewer", why: "it needs a display, and this environment has none" },
  { what: "a real provider", why: "there is no outbound network here, so the incident uses a loopback OpenAI-compatible mock" },
);

const stats = {
  schema: "blackbox.demo-stats/1",
  commit: (run("git", ["rev-parse", "--short", "HEAD"], { cwd: repo }).stdout ?? "").trim(),
  measuredAt: new Date().toISOString(),
  machine: `${process.platform}/${process.arch} · node ${process.version}`,
  python: (run(python, ["-c", "import sys; print(sys.version.split()[0])"]).stdout ?? "").trim(),
  sources: {
    note: "every number above is written by tools/stats.mjs from a command it ran, at the commit named here",
    stats: "tools/stats.mjs",
  },
  facts,
  suites,
  incident: incidentFacts,
  notRun,
};

const rendered = `${JSON.stringify(stats, null, 2)}\n`;
const previous = existsSync(outPath) ? readFileSync(outPath, "utf8") : "";
mkdirSync(dirname(outPath), { recursive: true });
writeFileSync(outPath, rendered, "utf8");
rmSync(work, { recursive: true, force: true });

if (process.argv.includes("--check")) {
  if (previous === rendered) {
    console.log("stats.json matches a fresh measurement");
    process.exit(0);
  }
  console.error("stats.json is stale: re-run `node tools/stats.mjs`");
  process.exit(1);
}

console.log(`wrote start/stats.json · commit ${stats.commit} · ${stats.machine}`);
for (const suite of suites) {
  console.log(`  ${suite.passed.toString().padStart(4)} pass ${suite.failed} fail  ${suite.name}`);
}
for (const item of facts.filter((entry) => !entry.id.startsWith("tests."))) {
  console.log(`  ${String(item.value).padStart(6)} ${item.unit.padEnd(10)} ${item.label}  (${item.source})`);
}
console.log(`  not run here: ${notRun.length} item(s)`);
