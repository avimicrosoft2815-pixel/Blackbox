#!/usr/bin/env node
/**
 * The film's sound design, synthesised here rather than borrowed or licensed.
 *
 *   node tools/make_sfx.mjs          # writes studio/sfx/*.wav (mono, 48 kHz, 16-bit)
 *
 * Four sounds, and no more: the film is a documentary about an incident, and a wall of effects would argue with the
 * narration. `cut` marks a scene boundary, `pulse` marks a beat in the diagnosis, `stamp` lands on the approval, and
 * `rise` opens the film and closes it.
 *
 * The waveforms are computed from a fixed seed, so re-running this writes the same bytes — a re-render of the film after
 * a copy change mixes exactly the effects that were there before, not a fresh roll of noise.
 */
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "studio", "sfx");
const RATE = 48000;

/** A small integer generator, so the noise in these files is reproducible. */
function random(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function seconds(duration) {
  return new Float32Array(Math.round(duration * RATE));
}

/** One-pole low-pass; `cutoff` may be a function of the sample index for a sweep. */
function lowpass(samples, cutoffAt) {
  let previous = 0;
  const out = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const cutoff = typeof cutoffAt === "function" ? cutoffAt(i / samples.length) : cutoffAt;
    const alpha = 1 - Math.exp((-2 * Math.PI * cutoff) / RATE);
    previous += alpha * (samples[i] - previous);
    out[i] = previous;
  }
  return out;
}

function highpass(samples, cutoff) {
  const low = lowpass(samples, cutoff);
  const out = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) out[i] = samples[i] - low[i];
  return out;
}

function mix(into, other, gain = 1) {
  for (let i = 0; i < into.length && i < other.length; i += 1) into[i] += other[i] * gain;
  return into;
}

function fade(samples, attackMs, releaseMs) {
  const attack = Math.round((attackMs / 1000) * RATE);
  const release = Math.round((releaseMs / 1000) * RATE);
  for (let i = 0; i < attack && i < samples.length; i += 1) samples[i] *= i / attack;
  for (let i = 0; i < release && i < samples.length; i += 1) {
    samples[samples.length - 1 - i] *= i / release;
  }
  return samples;
}

/** Peak-normalise to a headroom the mix can absorb. */
function normalise(samples, peak = 0.7) {
  let max = 0;
  for (const value of samples) max = Math.max(max, Math.abs(value));
  if (max === 0) return samples;
  const scale = peak / max;
  for (let i = 0; i < samples.length; i += 1) samples[i] *= scale;
  return samples;
}

function cut() {
  const randomNoise = random(0x5f3a);
  const length = 0.55;
  const noise = seconds(length);
  for (let i = 0; i < noise.length; i += 1) noise[i] = randomNoise() * 2 - 1;
  // A downward sweep: bright at the front, gone by the tail. Filtered noise reads as air moving, not as a tone.
  const swept = lowpass(noise, (p) => 6800 * Math.pow(1 - p, 2.2) + 420);
  const body = seconds(length);
  for (let i = 0; i < body.length; i += 1) {
    const t = i / RATE;
    const envelope = Math.max(0, 1 - t / length) ** 2.6;
    const sweep = Math.sin(2 * Math.PI * (300 * Math.pow(0.72, t / length)) * t);
    body[i] = (swept[i] * 0.85 + sweep * 0.18) * envelope;
  }
  return normalise(fade(body, 8, 90), 0.62);
}

function pulse() {
  const length = 0.42;
  const samples = seconds(length);
  const click = seconds(0.012);
  const randomNoise = random(0x1b7c);
  for (let i = 0; i < click.length; i += 1) click[i] = (randomNoise() * 2 - 1) * (1 - i / click.length);
  for (let i = 0; i < samples.length; i += 1) {
    const t = i / RATE;
    const envelope = Math.exp(-t / 0.075);
    samples[i] = Math.sin(2 * Math.PI * 72 * t) * envelope;
  }
  mix(samples, highpass(click, 1800), 0.35);
  return normalise(fade(samples, 2, 60), 0.66);
}

function stamp() {
  const length = 0.34;
  const samples = seconds(length);
  const randomNoise = random(0x77d1);
  const click = seconds(0.03);
  for (let i = 0; i < click.length; i += 1) click[i] = (randomNoise() * 2 - 1) * Math.exp(-i / (click.length * 0.3));
  for (let i = 0; i < samples.length; i += 1) {
    const t = i / RATE;
    const bell = Math.sin(2 * Math.PI * 1180 * t) * Math.exp(-t / 0.05)
      + Math.sin(2 * Math.PI * 1774 * t) * Math.exp(-t / 0.035) * 0.55;
    samples[i] = bell * 0.5;
  }
  mix(samples, lowpass(click, 5200), 0.55);
  return normalise(fade(samples, 1, 70), 0.6);
}

function rise() {
  const length = 1.9;
  const randomNoise = random(0x2c9e);
  const noise = seconds(length);
  for (let i = 0; i < noise.length; i += 1) noise[i] = randomNoise() * 2 - 1;
  const swept = lowpass(noise, (p) => 320 * Math.pow(16, p));
  const samples = seconds(length);
  for (let i = 0; i < samples.length; i += 1) {
    const p = i / samples.length;
    const swell = Math.sin(Math.PI * p) ** 1.6;
    const tone = Math.sin(2 * Math.PI * (110 + 40 * p) * (i / RATE)) * 0.25;
    samples[i] = (swept[i] * 0.9 + tone) * swell;
  }
  return normalise(fade(samples, 260, 320), 0.55);
}

function wav(samples) {
  const data = Buffer.alloc(samples.length * 2);
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    data.writeInt16LE(Math.round(value * 32767), i * 2);
  }
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);          // PCM
  header.writeUInt16LE(1, 22);          // mono
  header.writeUInt32LE(RATE, 24);
  header.writeUInt32LE(RATE * 2, 28);   // byte rate
  header.writeUInt16LE(2, 32);          // block align
  header.writeUInt16LE(16, 34);         // bits
  header.write("data", 36);
  header.writeUInt32LE(data.length, 40);
  return Buffer.concat([header, data]);
}

const sounds = { cut: cut(), pulse: pulse(), stamp: stamp(), rise: rise() };
await mkdir(out, { recursive: true });
for (const [name, samples] of Object.entries(sounds)) {
  const path = join(out, `${name}.wav`);
  await writeFile(path, wav(samples));
  console.log(`${name.padEnd(6)} ${(samples.length / RATE).toFixed(2)} s  ${(wav(samples).length / 1024).toFixed(0)} KiB → studio/sfx/${name}.wav`);
}
