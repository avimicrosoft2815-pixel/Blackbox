#!/usr/bin/env node
// bbx-read.mjs — an independent reader for BlackBox `.bbx` capsules.
//
// Written from `docs/format.md` (§4 frames, §5 segments, §8.1 capsule) and the `.bbx` validator table in
// `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §13.4 — **not** from `src/blackbox/cap/format.py`, and it imports nothing
// from the project. The only imports are Node's own: `node:zlib` (inflate + crc32), `node:fs`, `node:process`.
//
// Its job is to answer a question one implementation cannot answer about itself: is the *document* enough to read a
// capsule? If this file needs a detail the document does not state, that is a documentation defect, and the header
// comment at each such place says so instead of guessing silently.
//
//   node tools/readers/bbx-read.mjs <capsule.bbx> [--json] [--quiet]
//   node tools/readers/bbx-read.mjs --vectors tests/conformance/vectors/crc32.json
//
// Exit 0 verified, 3 refused (a named code, like `blackbox verify`), 4 an integrity claim that does not hold, 2 usage.

import { createHash } from 'node:crypto';
import { readFileSync, statSync } from 'node:fs';
import { crc32, inflateRawSync } from 'node:zlib';
import process from 'node:process';

// ── limits, from §13.4 ───────────────────────────────────────────────────────────────────────────────────────────
const MAX_ENTRIES = 5000;
const MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024;
const MAX_RATIO = 100;
const MAX_NESTING = 32;                    // §7: a decoder MUST reject nesting > 32
const SEGMENT_HEADER_BYTES = 4096;         // §5
const FRAME_HEADER_BYTES = 48;             // §4
const FRAME_TRAILER_BYTES = 8;

class Refusal extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

const refuse = (code, message) => {
  throw new Refusal(code, message);
};

// ── zip structure ────────────────────────────────────────────────────────────────────────────────────────────────
// §8.1 says "ZIP, STORED for raw segment bodies and DEFLATE for JSON". It does not say zip64 is out of scope, so this
// reader refuses an archive whose end-of-central-directory record is the zip64 one rather than half-reading it.
const EOCD_SIG = 0x06054b50;
const EOCD_ZIP64_LOCATOR_SIG = 0x07064b50;
const CDH_SIG = 0x02014b50;
const LFH_SIG = 0x04034b50;

function findEocd(buf) {
  // The record is 22 bytes plus a comment of up to 65535; scan backwards over the comment window.
  const lowest = Math.max(0, buf.length - (22 + 0xffff));
  for (let at = buf.length - 22; at >= lowest; at -= 1) {
    if (buf.readUInt32LE(at) === EOCD_SIG) return at;
  }
  refuse('BBX-E402', 'not a zip archive: no end-of-central-directory record');
}

function parseCentralDirectory(buf) {
  const eocd = findEocd(buf);
  if (eocd >= 20 && buf.readUInt32LE(eocd - 20) === EOCD_ZIP64_LOCATOR_SIG) {
    refuse('BBX-E402', 'zip64 archive: this reader implements the v0 record only');
  }
  const diskNumber = buf.readUInt16LE(eocd + 4);
  const cdDisk = buf.readUInt16LE(eocd + 6);
  const countOnDisk = buf.readUInt16LE(eocd + 8);
  const count = buf.readUInt16LE(eocd + 10);
  const cdSize = buf.readUInt32LE(eocd + 12);
  const cdOffset = buf.readUInt32LE(eocd + 16);
  if (diskNumber !== 0 || cdDisk !== 0 || countOnDisk !== count) {
    refuse('BBX-E402', `multi-disk archive (disk ${diskNumber}, ${countOnDisk} of ${count} on this disk)`);
  }
  if (count > MAX_ENTRIES) refuse('BBX-E402', `${count} entries exceeds the ${MAX_ENTRIES}-entry limit`);
  if (cdOffset + cdSize > buf.length) refuse('BBX-E402', 'central directory runs past the end of the file');

  const entries = [];
  let at = cdOffset;
  for (let i = 0; i < count; i += 1) {
    if (at + 46 > buf.length || buf.readUInt32LE(at) !== CDH_SIG) {
      refuse('BBX-E402', `central directory entry ${i} is not a header`);
    }
    const flags = buf.readUInt16LE(at + 8);
    const method = buf.readUInt16LE(at + 10);
    const crc = buf.readUInt32LE(at + 16);
    const compressedSize = buf.readUInt32LE(at + 20);
    const size = buf.readUInt32LE(at + 24);
    const nameLen = buf.readUInt16LE(at + 28);
    const extraLen = buf.readUInt16LE(at + 30);
    const commentLen = buf.readUInt16LE(at + 32);
    const externalAttr = buf.readUInt32LE(at + 38);
    const localOffset = buf.readUInt32LE(at + 42);
    const name = buf.toString('utf8', at + 46, at + 46 + nameLen);
    entries.push({ name, flags, method, crc, compressedSize, size, externalAttr, localOffset });
    at += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

// ── §13.4: the refusals, in the order the table lists them ──────────────────────────────────────────────────────
const UNSAFE_PARTS = new Set(['', '.', '..']);
// §13.4 check 3: a member may carry a mode; a capsule is not a filesystem. 0o120000 symlink, 0o020000 char device,
// 0o060000 block device, 0o010000 fifo, 0o140000 socket.
const HOSTILE_MODES = new Set([0o120000, 0o020000, 0o060000, 0o010000, 0o140000]);

function checkName(name) {
  if (name.includes('\u0000')) refuse('BBX-E403', `entry name contains NUL: ${JSON.stringify(name)}`);
  if (name.includes('\\')) refuse('BBX-E403', `entry name contains a backslash: ${JSON.stringify(name)}`);
  if (name.startsWith('/')) refuse('BBX-E403', `absolute entry name: ${JSON.stringify(name)}`);
  if (name.length > 1 && name[1] === ':') refuse('BBX-E403', `drive-letter entry name: ${JSON.stringify(name)}`);
  for (const part of name.split('/')) {
    if (UNSAFE_PARTS.has(part)) refuse('BBX-E403', `entry name escapes its root: ${JSON.stringify(name)}`);
  }
}

function checkEntries(entries) {
  if (entries.length > MAX_ENTRIES) refuse('BBX-E402', `${entries.length} entries exceeds the ${MAX_ENTRIES}-entry limit`);
  const seen = new Map();
  let total = 0;
  for (const entry of entries) {
    checkName(entry.name);
    const folded = entry.name.toLowerCase();
    if (seen.has(folded)) refuse('BBX-E404', `duplicate entry name (case-folded): ${entry.name} and ${seen.get(folded)}`);
    seen.set(folded, entry.name);
    total += entry.size;
    if (total > MAX_TOTAL_BYTES) refuse('BBX-E405', `declared total exceeds ${MAX_TOTAL_BYTES} B`);
    if (entry.size > 0 && entry.compressedSize > 0 && entry.size / Math.max(entry.compressedSize, 1) > MAX_RATIO) {
      refuse('BBX-E405', `${entry.name} expands ${Math.round(entry.size / entry.compressedSize)}:1`);
    }
    if (entry.flags & 0x1) refuse('BBX-E406', `${entry.name} is encrypted`);
    if (entry.method !== 0 && entry.method !== 8) {
      refuse('BBX-E406', `${entry.name} uses compression method ${entry.method}, not STORED or DEFLATE`);
    }
    const mode = (entry.externalAttr >>> 16) & 0o170000;
    if (HOSTILE_MODES.has(mode)) refuse('BBX-E403', `${entry.name} is a mode-${mode.toString(8)} member`);
  }
}

// ── reading one member ──────────────────────────────────────────────────────────────────────────────────────────
function readMember(buf, entry) {
  const at = entry.localOffset;
  if (at + 30 > buf.length || buf.readUInt32LE(at) !== LFH_SIG) {
    refuse('BBX-E402', `${entry.name}: local header is not where the directory says`);
  }
  const nameLen = buf.readUInt16LE(at + 26);
  const extraLen = buf.readUInt16LE(at + 28);
  const start = at + 30 + nameLen + extraLen;
  const end = start + entry.compressedSize;
  if (end > buf.length) refuse('BBX-E402', `${entry.name}: member runs past the end of the file`);
  const raw = buf.subarray(start, end);
  let data;
  if (entry.method === 0) {
    data = raw;
  } else {
    try {
      data = inflateRawSync(raw, { maxOutputLength: Math.max(entry.size, 1) * 2 + 1024 });
    } catch (error) {
      refuse('BBX-E402', `${entry.name}: inflate failed (${error.message})`);
    }
  }
  if (data.length > entry.size) refuse('BBX-E409', `${entry.name} produced ${data.length} B, declared ${entry.size} B`);
  if (data.length !== entry.size) refuse('BBX-E402', `${entry.name} is ${data.length} B, declared ${entry.size} B`);
  if (crc32(data) !== entry.crc) {
    refuse('BBX-E402', `${entry.name}: CRC-32 ${crc32(data).toString(16)} does not match the recorded ${entry.crc.toString(16)}`);
  }
  return Buffer.from(data);
}

// ── §4 frames ───────────────────────────────────────────────────────────────────────────────────────────────────
function framesOf(body) {
  const frames = [];
  let at = 0;
  while (at + FRAME_HEADER_BYTES <= body.length) {
    if (body.toString('latin1', at, at + 4) !== 'BBXF') break;      // zero padding after the last frame
    const frameLen = body.readUInt32LE(at + 4);
    if (frameLen < FRAME_HEADER_BYTES + FRAME_TRAILER_BYTES || frameLen % 8 !== 0) {
      refuse('BBX-E402', `frame at ${at}: impossible length ${frameLen}`);
    }
    if (at + frameLen > body.length) refuse('BBX-E402', `frame at ${at}: runs past the segment body`);
    const payloadLen = body.readUInt32LE(at + 40);
    // §4: "payloads are zero-padded" so that `frame_len` is a multiple of 8 — the padding is 0 to 7 bytes, and a frame
    // whose numbers leave more room than that is lying about its own shape.
    const padding = frameLen - FRAME_HEADER_BYTES - payloadLen - FRAME_TRAILER_BYTES;
    if (padding < 0 || padding > 7) {
      refuse('BBX-E402', `frame at ${at}: payload ${payloadLen} B leaves ${padding} B of padding in a ${frameLen} B frame`);
    }
    // §4's write order ("payload+padding → trailer → header") puts the trailer at the end of the frame, so its offset
    // is a function of `frame_len` and not of `payload_len`: the *table's* row numbering (`48+payload_len … 52+payload_len`)
    // reads as though padding came after the trailer, and it does not.
    const trailerAt = at + frameLen - FRAME_TRAILER_BYTES;
    if (body.toString('latin1', trailerAt, trailerAt + 4) !== 'BBXT') refuse('BBX-E402', `frame at ${at}: no BBXT trailer`);
    if (body.readUInt32LE(trailerAt + 4) !== frameLen) refuse('BBX-E402', `frame at ${at}: trailer length disagrees`);
    const recordedCrc = body.readUInt32LE(at + 44);
    // §4: crc32 over header[0,44) ‖ payload ‖ trailer — the header's own crc field is excluded, and the padding travels
    // with the payload.
    const sliced = Buffer.concat([body.subarray(at, at + 44), body.subarray(at + FRAME_HEADER_BYTES, at + frameLen)]);
    if (crc32(sliced) !== recordedCrc) refuse('BBX-E402', `frame at ${at}: CRC-32 does not match`);
    frames.push({
      seq: Number(body.readBigUInt64LE(at + 8)),
      wall_ns: Number(body.readBigInt64LE(at + 16)),
      kind: body.readUInt16LE(at + 32),
      flags: body.readUInt16LE(at + 34),
      run_slot: body.readUInt16LE(at + 36),
      node_slot: body.readUInt16LE(at + 38),
      payload_len: payloadLen,
      offset: at,
    });
    at += frameLen;
  }
  return frames;
}

function segmentFrames(name, data) {
  if (data.length < SEGMENT_HEADER_BYTES) refuse('BBX-E402', `${name}: shorter than the 4096 B segment header`);
  if (data.toString('latin1', 0, 7) !== 'BBXSEG2' || data[7] !== 0) {
    refuse('BBX-E402', `${name}: not a BBXSEG2 segment`);
  }
  const version = data.readUInt16LE(8);
  if (version !== 2) refuse('BBX-E402', `${name}: segment version ${version}, this reader knows 2`);
  // §5 names the header checksum's function as of 2026-11-19 (before that it said only "profile", which is a document
  // defect a second implementation cannot work around): CRC-32/ISO-HDLC over bytes [0, 96).
  const recorded = data.readUInt32LE(96);
  if (crc32(data.subarray(0, 96)) !== recorded) refuse('BBX-E402', `${name}: segment header checksum does not match`);
  return framesOf(data.subarray(SEGMENT_HEADER_BYTES));
}

// ── INTEGRITY.sha256 and the root (§8.1) ───────────────────────────────────────────────────────────────────────
function parseIntegrity(text) {
  const lines = text.split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();     // final newline
  return lines.map((line) => {
    const match = /^([0-9a-f]{64}) {2}(.+)$/.exec(line);
    if (!match) refuse('BBX-E402', `INTEGRITY.sha256 line does not parse: ${JSON.stringify(line)}`);
    return { sha256: match[1], name: match[2] };
  });
}

// ── the read ───────────────────────────────────────────────────────────────────────────────────────────────────
function readCapsule(path) {
  const stat = statSync(path);
  if (stat.size > MAX_TOTAL_BYTES) refuse('BBX-E405', `${stat.size} B exceeds ${MAX_TOTAL_BYTES} B`);
  const buf = readFileSync(path);
  const entries = parseCentralDirectory(buf);
  checkEntries(entries);

  const byName = new Map(entries.map((entry) => [entry.name, entry]));
  const contents = new Map();
  for (const entry of entries) contents.set(entry.name, readMember(buf, entry));

  if (!byName.has('manifest.json')) refuse('BBX-E407', 'manifest.json is missing');
  let manifest;
  try {
    manifest = JSON.parse(contents.get('manifest.json').toString('utf8'));
  } catch (error) {
    refuse('BBX-E407', `manifest.json is not JSON: ${error.message}`);
  }
  if (manifest === null || typeof manifest !== 'object' || Array.isArray(manifest)) {
    refuse('BBX-E407', `manifest.json is a ${Array.isArray(manifest) ? 'list' : manifest === null ? 'null' : typeof manifest}, not an object`);
  }
  // `docs/format.md` §8.1 says the manifest carries "identity" but does not name these two values; they were read off a
  // capsule this build wrote, and the document was amended to state them (see the note in §8.1). A reader that guesses
  // them is a reader that cannot tell a newer writer from a corrupt one.
  if (manifest.format !== 'bbx.capsule') refuse('BBX-E407', `manifest format is ${JSON.stringify(manifest.format)}`);
  const version = Number(manifest.format_version ?? 0);
  if (version > 0) refuse('BBX-E407', `manifest format_version ${version}: this reader knows 0`);

  for (const part of Array.isArray(manifest.derived_parts) ? manifest.derived_parts : []) {
    if (part && typeof part.name === 'string' && !byName.has(part.name)) {
      refuse('BBX-E408', `manifest lists ${part.name}, which the archive does not contain`);
    }
  }

  if (!byName.has('INTEGRITY.sha256')) refuse('BBX-E402', 'INTEGRITY.sha256 is missing');
  const integrityBytes = contents.get('INTEGRITY.sha256');
  const lines = parseIntegrity(integrityBytes.toString('utf8'));
  const names = lines.map((line) => line.name);
  if (JSON.stringify(names) !== JSON.stringify([...names].sort())) {
    refuse('BBX-E402', 'INTEGRITY.sha256 is not sorted by name');
  }
  const mismatched = [];
  for (const line of lines) {
    const entry = byName.get(line.name);
    if (!entry) {
      mismatched.push(`${line.name}: declared but absent`);
      continue;
    }
    const digest = sha256(contents.get(line.name));
    if (digest !== line.sha256) mismatched.push(`${line.name}: ${digest.slice(0, 12)} != ${line.sha256.slice(0, 12)}`);
  }
  const listed = new Set(names);
  for (const entry of entries) {
    if (entry.name === 'manifest.json' || entry.name === 'INTEGRITY.sha256') continue;
    if (!listed.has(entry.name)) mismatched.push(`${entry.name}: present but not declared`);
  }
  const root = sha256(integrityBytes);
  const declaredRoot = manifest.integrity && typeof manifest.integrity.root === 'string' ? manifest.integrity.root : null;

  // Frames, and the one cross-check that makes the two halves of this file test each other.
  const segments = [...contents.keys()].filter((name) => /^events\/seg-\d{6}\.raw$/.test(name)).sort();
  const frames = [];
  for (const name of segments) frames.push(...segmentFrames(name, contents.get(name)));
  const seqs = frames.map((frame) => frame.seq);
  const nonIncreasing = seqs.some((seq, index) => index > 0 && seq <= seqs[index - 1]);
  const kinds = {};
  for (const frame of frames) kinds[frame.kind] = (kinds[frame.kind] ?? 0) + 1;

  return {
    capsule: path,
    bytes: buf.length,
    entries: entries.map((entry) => ({ name: entry.name, size: entry.size, method: entry.method })),
    manifest: {
      capsule_id: manifest.capsule_id ?? null,
      format_version: version,
      run_id: manifest.run?.run_id ?? null,
      run_status: manifest.run?.status ?? null,
      counts: manifest.counts ?? null,
      unknown_keys: unknownManifestKeys(manifest),
    },
    integrity: {
      lines: lines.length,
      root,
      declared_root: declaredRoot,
      root_ok: declaredRoot !== null && root === declaredRoot,
      mismatched,
    },
    frames: { segments: segments.length, count: frames.length, kinds, non_increasing_seq: nonIncreasing },
    ok: declaredRoot !== null && root === declaredRoot && mismatched.length === 0,
  };
}

// §8.1 lists the manifest's keys; anything else is tolerated, preserved and reported — a capsule from a newer writer
// must open here. The list is the document's, so a key the document gains without this file changing shows up as
// unknown, which is the honest failure mode for a second implementation.
const MANIFEST_KEYS = new Set([
  'capsule_id', 'format', 'format_version', 'min_reader', 'created_ns', 'tool', 'run', 'capture', 'redaction', 'gaps',
  'counts', 'chain_root', 'capture_vantage', 'capture_seam', 'links_summary', 'derived_parts', 'integrity', 'signature',
]);

function unknownManifestKeys(manifest) {
  return Object.keys(manifest).filter((key) => !MANIFEST_KEYS.has(key)).sort();
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

// ── main ──────────────────────────────────────────────────────────────────────────────────────────────────────
// ── the published vectors (§9 of format.md, `tests/conformance/vectors/`) ────────────────────────────────────
// The CRC-32 vectors exist so that a reader written from the document can be checked against numbers it did not
// produce. If `zlib`'s crc32 and this file's reading of §4 disagree, the vectors say so before a capsule does.
function checkVectors(path) {
  const spec = JSON.parse(readFileSync(path, 'utf8'));
  const failures = [];
  for (const vector of spec.vectors ?? []) {
    const bytes = vector.input_hex !== undefined
      ? Buffer.from(vector.input_hex, 'hex')
      : Buffer.from(vector.input_ascii ?? '', 'ascii');
    const expected = Number.parseInt(vector.crc32, 16);
    const actual = crc32(bytes);
    if (actual !== expected) {
      failures.push(`${JSON.stringify(vector.input_ascii ?? vector.input_hex)}: ${actual.toString(16)} != ${vector.crc32}`);
    }
  }
  return { algorithm: spec.algorithm ?? null, vectors: (spec.vectors ?? []).length, failures };
}

function main(argv) {
  const args = argv.slice(2);
  const vectorsAt = args.indexOf('--vectors');
  if (vectorsAt >= 0) {
    const vectorsPath = args[vectorsAt + 1];
    if (!vectorsPath) {
      process.stderr.write('usage: bbx-read.mjs --vectors <crc32.json>\n');
      return 2;
    }
    const report = checkVectors(vectorsPath);
    if (args.includes('--json')) process.stdout.write(JSON.stringify(report, null, 2) + '\n');
    else process.stdout.write(`${report.vectors} ${report.algorithm} vectors, ${report.failures.length} failures\n`);
    if (report.failures.length) {
      if (!args.includes('--json')) process.stderr.write(report.failures.join('\n') + '\n');
      return 4;
    }
    return 0;
  }
  const path = args.find((arg) => !arg.startsWith('--'));
  const json = args.includes('--json');
  const quiet = args.includes('--quiet');
  if (!path) {
    process.stderr.write('usage: bbx-read.mjs <capsule.bbx> [--json] [--quiet]\n');
    return 2;
  }
  let result;
  try {
    result = readCapsule(path);
  } catch (error) {
    if (error instanceof Refusal) {
      if (json) process.stdout.write(JSON.stringify({ ok: false, refused: error.code, reason: error.message }) + '\n');
      else process.stderr.write(`BBX refused: ${error.code} ${error.message}\n`);
      return 3;
    }
    if (error && error.code === 'ENOENT') {
      process.stderr.write(`cannot read ${path}: no such file\n`);
      return 2;
    }
    throw error;
  }
  if (json) process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  else if (!quiet) {
    const line = [
      `${result.capsule}: ${result.bytes} B, ${result.entries.length} entries`,
      `run ${result.manifest.run_id} (${result.manifest.run_status})`,
      `${result.frames.count} frames in ${result.frames.segments} segment(s)`,
      `integrity root ${result.integrity.root_ok ? 'matches' : 'DOES NOT MATCH'} the manifest (${result.integrity.root.slice(0, 16)}…)`,
      result.integrity.mismatched.length ? `problems: ${result.integrity.mismatched.join('; ')}` : 'no integrity problems',
    ];
    process.stdout.write(line.join('\n') + '\n');
  }
  return result.ok ? 0 : 4;
}

process.exitCode = main(process.argv);
