/**
 * The frame renderer: every pixel of the BlackBox film is drawn here.
 *
 * One renderer, two hosts. In the Director Studio this paints into a real `<canvas>`; in `tools/render_frames.mjs` it
 * paints into the same Canvas2D API provided by Skia in Node. The exported film is therefore not a re-creation of the
 * preview — it *is* the preview, frame by frame, which is the only way "what you see is what exports" can be true.
 *
 * Rules this file keeps:
 *  - pure: no DOM, no `window`, no `Date`, no randomness. A frame is a function of `(scene, pct)` plus the copy in the
 *    spec. Re-rendering the same percent gives the same pixels.
 *  - no external images. The wordmark is type, the motif is lines, the glow is gradients. Nothing to load, nothing to
 *    license separately.
 *  - no layout by hope. Headlines are fitted to their column, body copy is measured before anything is placed after it,
 *    and every panel reserves its own gutters. A string that is too long shrinks or wraps — it never lands on a card.
 *  - honest labels: `scene.label` is drawn wherever the scene is illustrative, so no frame can be screenshotted without
 *    the word on it.
 *  - the font set carries no arrow or multiplication glyphs, so the copy avoids them rather than drawing tofu.
 */

import {
  captionAt,
  clamp,
  easeInOutCubic,
  easeInOutSine,
  easeOutCubic,
  easeOutExpoSoft,
  easeOutQuint,
  progress,
  springPop,
  stagger,
} from "./motionMath.js";

export const FRAME_WIDTH = 1920;
export const FRAME_HEIGHT = 1080;

/** The type families the renderer asks for, each registered under its own name so both hosts resolve it identically. */
export const FONTS = {
  display: "InterBlack",
  bold: "InterBold",
  semi: "InterSemiBold",
  medium: "InterMedium",
  regular: "InterRegular",
  mono: "MonoRegular",
  monoBold: "MonoBold",
  monoMedium: "MonoMedium",
};

const GOLD = "#E3B341";
const GOLD_LIGHT = "#F2CD73";
const INK = "#F4F1EA";
const MUTED = "#9A958C";
const UNKNOWN = "#7E786F";
const BG = "#07070A";

const M = 96;            // outer margin
const HEADER_RULE = 112; // y of the hairline under the header
const FOOTER_RULE = 984; // y of the hairline above the footer
const PANEL = { x: 840, y: 150, w: 984, h: 660 };

/* ── primitives ─────────────────────────────────────────────────────────────────────────────────────────────────── */

function roundedPath(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  if (typeof ctx.roundRect === "function") {
    ctx.roundRect(x, y, w, h, radius);
    return;
  }
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
}

function alpha(hex, a) {
  const clean = hex.replace("#", "");
  const full = clean.length === 3 ? clean.split("").map((c) => c + c).join("") : clean;
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

function setFont(ctx, family, size, weight = 400) {
  ctx.font = `${weight} ${size}px ${family}`;
}

/** Letter-spaced text, drawn glyph by glyph: canvas `letterSpacing` is not in every host. */
function spacedText(ctx, text, x, y, spacing, { align = "left", color } = {}) {
  if (color) ctx.fillStyle = color;
  const chars = [...String(text)];
  const widths = chars.map((c) => ctx.measureText(c).width);
  const total = widths.reduce((sum, w) => sum + w, 0) + spacing * Math.max(0, chars.length - 1);
  if (spacing === 0) {
    ctx.textAlign = align;
    ctx.fillText(text, align === "center" ? x : align === "right" ? x : x, y);
    return total;
  }
  let cursor = align === "center" ? x - total / 2 : align === "right" ? x - total : x;
  ctx.textAlign = "left";
  chars.forEach((c, i) => {
    ctx.fillText(c, cursor, y);
    cursor += widths[i] + spacing;
  });
  return total;
}

function wrap(ctx, text, maxWidth) {
  const words = String(text).split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (ctx.measureText(candidate).width > maxWidth && line) {
      lines.push(line);
      line = word;
    } else {
      line = candidate;
    }
  }
  if (line) lines.push(line);
  return lines;
}

/** Draw a paragraph and return the y just below its last line, so callers can place the next block. */
function paragraph(ctx, text, x, y, { family, size, weight = 400, color = MUTED, maxWidth, lineHeight, maxLines }) {
  setFont(ctx, family, size, weight);
  ctx.fillStyle = color;
  ctx.textAlign = "left";
  let lines = wrap(ctx, text, maxWidth);
  if (maxLines && lines.length > maxLines) lines = lines.slice(0, maxLines);
  const lh = lineHeight ?? Math.round(size * 1.5);
  lines.forEach((line, i) => ctx.fillText(line, x, y + i * lh));
  return y + (lines.length - 1) * lh;
}

function hairline(ctx, x1, y1, x2, y2, color, width = 1) {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
}

/** The largest size at which every supplied headline line fits on one line — measured, never guessed. */
function fitHeadlineSize(ctx, lines, maxWidth, maxSize, minSize) {
  for (let size = maxSize; size >= minSize; size -= 2) {
    setFont(ctx, FONTS.display, size, 900);
    if (lines.every((line) => ctx.measureText(line).width <= maxWidth)) return size;
  }
  return minSize;
}

/* ── the frame's furniture ──────────────────────────────────────────────────────────────────────────────────────── */

function drawBackground(ctx, scene, W, H, pct, motionReduced = false) {
  ctx.fillStyle = scene.theme?.bg ?? BG;
  ctx.fillRect(0, 0, W, H);

  // One warm glow, drifting very slowly: the only ambient motion in the film. A viewer who asks for reduced motion gets
  // the same frame with the glow parked, and the studio says so rather than implying the export changed.
  const drift = motionReduced ? 0 : Math.sin((pct / 100) * Math.PI) * 40;
  const cx = W * 0.66 + drift;
  const cy = H * 0.42 - drift * 0.4;
  const glow = ctx.createRadialGradient(cx, cy, 40, cx, cy, W * 0.62);
  glow.addColorStop(0, alpha(GOLD, 0.12));
  glow.addColorStop(0.45, alpha(GOLD, 0.045));
  glow.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, W, H);

  // Graphite grid: 48 px, barely there, so the frame has a scale without looking like a videogame.
  for (let x = 0; x <= W; x += 48) hairline(ctx, x + 0.5, 0, x + 0.5, H, "rgba(255,255,255,0.026)");
  for (let y = 0; y <= H; y += 48) hairline(ctx, 0, y + 0.5, W, y + 0.5, "rgba(255,255,255,0.026)");

  const vignette = ctx.createRadialGradient(W / 2, H / 2, H * 0.35, W / 2, H / 2, W * 0.72);
  vignette.addColorStop(0, "rgba(0,0,0,0)");
  vignette.addColorStop(1, "rgba(0,0,0,0.58)");
  ctx.fillStyle = vignette;
  ctx.fillRect(0, 0, W, H);
}

function drawHeader(ctx, spec, scene, timecode) {
  setFont(ctx, FONTS.bold, 21, 700);
  const wordmark = spacedText(ctx, "BLACKBOX", M, 70, 5.5, { color: INK });
  ctx.fillStyle = GOLD;
  ctx.beginPath();
  ctx.arc(M + wordmark + 22, 63, 3.4, 0, Math.PI * 2);
  ctx.fill();
  setFont(ctx, FONTS.mono, 13, 500);
  spacedText(ctx, "DEMO FILM", M + wordmark + 40, 69, 2.6, { color: MUTED });

  setFont(ctx, FONTS.mono, 13, 500);
  spacedText(ctx, `SCENE ${String(scene.step).padStart(2, "0")} / ${String(spec.scenes.length).padStart(2, "0")}`,
    FRAME_WIDTH - M, 69, 2.6, { color: GOLD, align: "right" });
  setFont(ctx, FONTS.mono, 12, 400);
  spacedText(ctx, timecode, FRAME_WIDTH - M, 92, 1.6, { color: UNKNOWN, align: "right" });

  hairline(ctx, M, HEADER_RULE, FRAME_WIDTH - M, HEADER_RULE, "rgba(255,255,255,0.08)");
}

function drawFooter(ctx, spec, sceneIndex, scene, pct, timecode) {
  hairline(ctx, M, FOOTER_RULE, FRAME_WIDTH - M, FOOTER_RULE, "rgba(255,255,255,0.09)");

  // The gold event line: the motif that runs through all seven scenes is also the film's own progress.
  const spans = spec.scenes.map((s) => s.durationMs);
  const total = spans.reduce((a, b) => a + b, 0) || 1;
  const trackWidth = FRAME_WIDTH - M * 2;
  let x = M;
  spans.forEach((span, i) => {
    const width = (trackWidth * span) / total;
    if (i < sceneIndex) {
      hairline(ctx, x, FOOTER_RULE, x + width, FOOTER_RULE, alpha(GOLD, 0.55), 2);
    } else if (i === sceneIndex) {
      hairline(ctx, x, FOOTER_RULE, x + width, FOOTER_RULE, "rgba(255,255,255,0.10)", 2);
      const head = x + (width * pct) / 100;
      hairline(ctx, x, FOOTER_RULE, head, FOOTER_RULE, GOLD, 2);
      ctx.fillStyle = alpha(GOLD, 0.22);
      ctx.beginPath();
      ctx.arc(head, FOOTER_RULE, 11, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = GOLD_LIGHT;
      ctx.beginPath();
      ctx.arc(head, FOOTER_RULE, 4.2, 0, Math.PI * 2);
      ctx.fill();
    } else {
      hairline(ctx, x, FOOTER_RULE, x + width, FOOTER_RULE, "rgba(255,255,255,0.10)", 2);
    }
    x += width;
  });

  setFont(ctx, FONTS.mono, 12, 500);
  ctx.textAlign = "left";
  spacedText(ctx, scene.shortTitle.toUpperCase(), M, FOOTER_RULE + 30, 1.6, { color: alpha(MUTED, 0.9) });
  setFont(ctx, FONTS.mono, 12, 400);
  ctx.textAlign = "right";
  ctx.fillStyle = alpha(UNKNOWN, 0.9);
  ctx.fillText("blackbox — a flight recorder for AI applications", FRAME_WIDTH - M, FOOTER_RULE + 30);
  ctx.textAlign = "left";
}

function drawCaption(ctx, scene, pct, showCaptions) {
  if (!showCaptions) return;
  const text = captionAt(scene.narration?.captions, pct);
  if (!text) return;
  const maxWidth = 1300;
  setFont(ctx, FONTS.medium, 29, 500);
  const lines = wrap(ctx, text, maxWidth);
  const lineHeight = 40;
  const boxW = maxWidth + 96;
  const boxH = lines.length * lineHeight + 32;
  const top = 862 - boxH;
  void top;
  const boxTop = FOOTER_RULE - 90 - boxH;
  const x = (FRAME_WIDTH - boxW) / 2;

  roundedPath(ctx, x, boxTop, boxW, boxH, 16);
  ctx.fillStyle = "rgba(4,4,7,0.78)";
  ctx.fill();
  ctx.strokeStyle = alpha(GOLD, 0.28);
  ctx.lineWidth = 1;
  ctx.stroke();

  setFont(ctx, FONTS.medium, 29, 500);
  ctx.fillStyle = INK;
  ctx.textAlign = "left";
  lines.forEach((line, i) => ctx.fillText(line, x + 48, boxTop + 40 + i * lineHeight));

  // Which caption of the scene this is: a dot per line, filled for the active one.
  const captions = [...(scene.narration?.captions ?? [])].sort((a, b) => a.atPct - b.atPct);
  const active = captions.filter((c) => pct >= c.atPct).length;
  captions.forEach((_, i) => {
    ctx.fillStyle = i + 1 === active ? GOLD : alpha(GOLD, 0.28);
    ctx.beginPath();
    ctx.arc(x + boxW - 34 - (captions.length - 1 - i) * 14, boxTop + 22, 3, 0, Math.PI * 2);
    ctx.fill();
  });
}

/** The disclosure chip. Top-right, under the header, where a viewer looking for "is this real?" will find it. */
function drawDisclosure(ctx, scene) {
  if (!scene.label) return;
  const text = scene.label.toUpperCase();
  setFont(ctx, FONTS.mono, 12, 500);
  const width = ctx.measureText(text).width + 32;
  const x = FRAME_WIDTH - M - width;
  const y = 128;
  roundedPath(ctx, x, y, width, 26, 5);
  ctx.fillStyle = alpha(UNKNOWN, 0.13);
  ctx.fill();
  ctx.strokeStyle = alpha(UNKNOWN, 0.38);
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.fillStyle = GOLD;
  ctx.fillRect(x + 11, y + 11, 4, 4);
  spacedText(ctx, text, x + 22, y + 17.5, 1.3, { color: MUTED });
}

/* ── shared pieces ─────────────────────────────────────────────────────────────────────────────────────────────── */

function panel(ctx, x, y, w, h, { border = "rgba(255,255,255,0.10)", radius = 26, tint = 0 } = {}) {
  roundedPath(ctx, x, y, w, h, radius);
  const gradient = ctx.createLinearGradient(x, y, x + w * 0.6, y + h);
  gradient.addColorStop(0, `rgba(${22 + tint},${21 + tint},${27 + tint},0.96)`);
  gradient.addColorStop(0.55, "rgba(16,16,20,0.72)");
  gradient.addColorStop(1, "rgba(9,9,12,0.92)");
  ctx.fillStyle = gradient;
  ctx.fill();
  ctx.strokeStyle = border;
  ctx.lineWidth = 1;
  ctx.stroke();
}

function pill(ctx, text, x, y, { color = GOLD, font = FONTS.mono, size = 12, padX = 14, height = 26, dashed = false, filled = false }) {
  setFont(ctx, font, size, 700);
  const width = ctx.measureText(text).width + padX * 2;
  roundedPath(ctx, x, y, width, height, height / 2);
  if (filled) {
    ctx.fillStyle = color;
    ctx.fill();
  } else {
    if (dashed) ctx.setLineDash([4, 4]);
    ctx.strokeStyle = alpha(color, 0.6);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = filled ? "#0A0A0C" : color;
  ctx.textAlign = "left";
  ctx.fillText(text, x + padX, y + height / 2 + size * 0.36);
  return width;
}

/* ── the seven stage modes ─────────────────────────────────────────────────────────────────────────────────────── */

function drawSplitCollage(ctx, scene, pct, layout) {
  const { x: px, y: py, w: pw, h: ph } = PANEL;
  panel(ctx, px, py, pw, ph, { border: alpha(GOLD, 0.16) });

  // The event rail sits above the cards, so it can break visibly without crossing any of them.
  const railY = py + 84;
  const railStart = px + 40;
  const railEnd = px + pw - 40;
  const breakAt = px + pw * 0.54;
  const reveal = easeOutCubic(progress(pct, 4, 40));
  const gap = 26;
  if (reveal > 0) {
    const right = railStart + (railEnd - railStart) * reveal;
    hairline(ctx, railStart, railY, Math.min(right, breakAt - gap), railY, alpha(GOLD, 0.5), 1.4);
    if (right > breakAt + gap) hairline(ctx, breakAt + gap, railY, Math.min(right, railEnd), railY, alpha(GOLD, 0.5), 1.4);
    if (right > breakAt - gap) {
      hairline(ctx, breakAt - gap, railY - 8, breakAt - gap, railY + 8, GOLD, 1.5);
      hairline(ctx, breakAt + gap, railY - 8, breakAt + gap, railY + 8, GOLD, 1.5);
      setFont(ctx, FONTS.mono, 11, 700);
      spacedText(ctx, "BREAK", breakAt, railY - 16, 2.2, { color: alpha(GOLD, 0.9), align: "center" });
    }
    const headX = railStart + (railEnd - railStart) * clamp(reveal, 0, 1);
    if (Math.abs(headX - breakAt) > gap) {
      ctx.fillStyle = GOLD_LIGHT;
      ctx.beginPath();
      ctx.arc(headX, railY, 3.4, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  if (scene.copy.overlayBadge) {
    setFont(ctx, FONTS.mono, 11, 500);
    spacedText(ctx, scene.copy.overlayBadge.toUpperCase(), px + 40, railY + 34, 1.6, { color: alpha(MUTED, 0.75) });
  }

  // Cards: percent of the panel, but the vertical band starts below the rail.
  const bandTop = py + 118;
  const bandHeight = ph - 118 - 40;
  for (const prop of scene.collageProps ?? []) {
    if (pct < prop.enterPct) continue;
    const local = Math.min(1, (pct - prop.enterPct) / 18);
    const scale = springPop(local) * prop.scale;
    const opacity = Math.min(1, local * 3);
    const rotate = prop.rotateDeg * (1 - local / 1.6);
    if (opacity <= 0.01 || scale <= 0.02) continue;

    const hero = Boolean(prop.hero);
    const cx = px + (pw * prop.xPct) / 100;
    const cy = bandTop + (bandHeight * prop.yPct) / 100;
    const w = hero ? 452 : 322;
    const h = hero ? 176 : 112;
    const padX = hero ? 24 : 16;

    ctx.save();
    ctx.globalAlpha = opacity;
    ctx.translate(cx, cy);
    ctx.rotate((rotate * Math.PI) / 180);
    // Scale the whole card, type included: a card that grows into place should not grow its box around stationary
    // text, which is what made the small ones look clipped mid-entrance.
    ctx.scale(scale, scale);
    roundedPath(ctx, -w / 2, -h / 2, w, h, hero ? 16 : 12);
    const fill = ctx.createLinearGradient(-w / 2, -h / 2, w / 2, h / 2);
    fill.addColorStop(0, hero ? "rgba(34,29,18,0.98)" : "rgba(21,21,26,0.97)");
    fill.addColorStop(1, hero ? "rgba(15,13,9,0.99)" : "rgba(12,12,15,0.98)");
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = hero ? prop.accent : alpha(prop.accent, 0.45);
    ctx.lineWidth = hero ? 1.6 : 1.1;
    ctx.stroke();
    if (hero) {
      ctx.strokeStyle = alpha(prop.accent, 0.12);
      ctx.lineWidth = 8;
      ctx.stroke();
    }

    const left = -w / 2 + padX;
    setFont(ctx, FONTS.mono, hero ? 12.5 : 11, 700);
    spacedText(ctx, prop.badge, left, -h / 2 + (hero ? 32 : 26), 1.7, { color: prop.accent });

    if (prop.metricTag) {
      setFont(ctx, FONTS.mono, hero ? 11.5 : 10, 500);
      const tw = ctx.measureText(prop.metricTag).width + 14;
      roundedPath(ctx, w / 2 - padX - tw, -h / 2 + (hero ? 18 : 16), tw, 18, 4);
      ctx.fillStyle = alpha(prop.accent, 0.16);
      ctx.fill();
      ctx.fillStyle = prop.accent;
      ctx.textAlign = "left";
      ctx.fillText(prop.metricTag, w / 2 - padX - tw + 7, -h / 2 + (hero ? 31 : 29));
    }

    setFont(ctx, FONTS.bold, hero ? 27 : 19, 700);
    ctx.fillStyle = INK;
    ctx.textAlign = "left";
    const titleWidth = w - padX * 2;
    const titleLines = wrap(ctx, prop.title, titleWidth).slice(0, hero ? 2 : 1);
    titleLines.forEach((line, i) => ctx.fillText(line, left, -h / 2 + (hero ? 70 : 52) + i * (hero ? 30 : 22)));

    setFont(ctx, FONTS.regular, hero ? 16 : 13, 400);
    ctx.fillStyle = alpha(MUTED, 0.95);
    const detailTop = hero ? -h / 2 + 70 + titleLines.length * 30 : -h / 2 + 74;
    wrap(ctx, prop.detail, titleWidth).slice(0, hero ? 2 : 1)
      .forEach((line, i) => ctx.fillText(line, left, detailTop + i * (hero ? 22 : 17)));
    ctx.restore();
  }
  void layout;
}

function drawArchGraph(ctx, scene, pct, layout) {
  const { x: px, y: py, w: pw, h: ph } = PANEL;
  panel(ctx, px, py, pw, ph, { border: alpha(GOLD, 0.16) });

  const graph = scene.archGraph ?? { nodes: [], edges: [] };
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const innerX = px + 56;
  const innerY = py + 118;
  const innerW = pw - 112;
  const innerH = ph - 118 - 96;
  const point = (node) => ({ x: innerX + (innerW * node.x) / 100, y: innerY + (innerH * node.y) / 100 });

  setFont(ctx, FONTS.mono, 11, 700);
  spacedText(ctx, "SYSTEM TOPOLOGY // SIGNAL FLOW", px + 24, py + 36, 1.8, { color: GOLD });

  const NODE_W = 196;
  const NODE_H = 68;

  // Edges first, so nodes always sit on top of the lines.
  for (const edge of graph.edges ?? []) {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) continue;
    const a = point(from);
    const b = point(to);
    const on = pct >= edge.activeFromPct;
    const dashed = edge.dashed || from.dashed || to.dashed;
    ctx.save();
    ctx.setLineDash(dashed || !on ? [7, 7] : []);
    ctx.strokeStyle = on ? alpha(GOLD, 0.6) : "rgba(255,255,255,0.13)";
    ctx.lineWidth = on ? 1.6 : 1;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    const mx = (a.x + b.x) / 2;
    ctx.bezierCurveTo(mx, a.y, mx, b.y, b.x, b.y);
    ctx.stroke();
    ctx.restore();

    if (on) {
      const t = (((pct - edge.activeFromPct) % 34) / 34);
      const ease = easeInOutSine(t);
      const dotX = a.x + (b.x - a.x) * ease;
      const dotY = a.y + (b.y - a.y) * ease;
      const glow = ctx.createRadialGradient(dotX, dotY, 0, dotX, dotY, 15);
      glow.addColorStop(0, alpha(GOLD_LIGHT, 0.85));
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(dotX - 15, dotY - 15, 30, 30);
      ctx.fillStyle = GOLD_LIGHT;
      ctx.beginPath();
      ctx.arc(dotX, dotY, 2.8, 0, Math.PI * 2);
      ctx.fill();

      // Edge label: it is only drawn where it actually fits. A horizontal edge gets the gap between two boxes; any
      // other edge needs its midpoint clear of every box by half the label's width. No label is better than a
      // label on top of a node.
      setFont(ctx, FONTS.mono, 10, 500);
      const midX = (a.x + b.x) / 2;
      const midY = (a.y + b.y) / 2;
      const labelW = ctx.measureText(edge.label).width + 14;
      const horizontal = Math.abs(a.y - b.y) < 18;
      const gapWidth = Math.abs(b.x - a.x) - NODE_W;
      const fits = horizontal
        ? gapWidth > labelW + 18
        : [...byId.values()].every((n) => {
          const p = point(n);
          return Math.abs(p.x - midX) > NODE_W / 2 + labelW / 2 + 8 ||
            Math.abs(p.y - midY) > NODE_H / 2 + 30;
        });
      if (fits) {
        setFont(ctx, FONTS.mono, 10, 500);
        setFont(ctx, FONTS.mono, 10, 500);
        const lw = ctx.measureText(edge.label).width + 14;
        roundedPath(ctx, midX - lw / 2, midY - 24, lw, 18, 4);
        ctx.fillStyle = "rgba(7,7,10,0.94)";
        ctx.fill();
        ctx.strokeStyle = alpha(GOLD, 0.22);
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.fillStyle = alpha(GOLD_LIGHT, 0.92);
        ctx.textAlign = "left";
        ctx.fillText(edge.label, midX - lw / 2 + 7, midY - 11);
      }
    }
  }

  graph.nodes.forEach((node, index) => {
    const { x, y } = point(node);
    const appear = easeOutCubic(progress(pct, 4 + index * 6, 22 + index * 6));
    ctx.save();
    ctx.globalAlpha = 0.22 + 0.78 * appear;
    roundedPath(ctx, x - NODE_W / 2, y - NODE_H / 2, NODE_W, NODE_H, 12);
    ctx.fillStyle = "rgba(11,11,15,0.97)";
    ctx.fill();
    ctx.strokeStyle = node.dashed ? alpha(UNKNOWN, 0.55) : alpha(node.accent, 0.5);
    ctx.lineWidth = 1.2;
    if (node.dashed) ctx.setLineDash([5, 5]);
    ctx.stroke();
    ctx.setLineDash([]);

    setFont(ctx, FONTS.mono, 10, 700);
    spacedText(ctx, node.role, x - NODE_W / 2 + 14, y - NODE_H / 2 + 22, 1.5, { color: node.dashed ? UNKNOWN : node.accent });
    setFont(ctx, FONTS.mono, 9, 400);
    ctx.textAlign = "right";
    ctx.fillStyle = alpha(MUTED, 0.8);
    ctx.fillText(node.latencyLabel, x + NODE_W / 2 - 14, y - NODE_H / 2 + 22);

    setFont(ctx, FONTS.semi, 18, 600);
    ctx.fillStyle = node.dashed ? alpha(INK, 0.62) : INK;
    ctx.textAlign = "left";
    ctx.fillText(node.label, x - NODE_W / 2 + 14, y + 16);
    ctx.restore();
  });

  if (graph.caption) {
    setFont(ctx, FONTS.mono, 11.5, 400);
    ctx.fillStyle = alpha(UNKNOWN, 0.95);
    ctx.textAlign = "left";
    wrap(ctx, graph.caption, pw - 48).slice(0, 2)
      .forEach((line, i) => ctx.fillText(line, px + 24, py + ph - 46 + i * 17));
  }
  void layout;
}

function drawBeforeAfter(ctx, scene, pct, layout, options) {
  const { x: px, y: py, w: pw, h: ph } = PANEL;
  const ba = scene.beforeAfter ?? { beforeBullets: [], afterBullets: [] };
  // The wipe completes early (by ~35 % of the scene) and then holds, so the narration lands on a readable frame
  // rather than on a divider still travelling across the words.
  const auto = 14 + easeOutCubic(clamp(pct / 38, 0, 1)) * 36;
  const divider = clamp(options.manualSplit ?? auto, 8, 92);

  panel(ctx, px, py, pw, ph, { border: alpha(GOLD, 0.18) });

  ctx.save();
  roundedPath(ctx, px, py, pw, ph, 26);
  ctx.clip();

  // AFTER: the evidence-led view, drawn full width, then covered on the left by the BEFORE layer.
  const afterFill = ctx.createLinearGradient(px, py, px + pw, py + ph);
  afterFill.addColorStop(0, "rgba(22,20,14,0.96)");
  afterFill.addColorStop(1, "rgba(9,9,13,0.97)");
  ctx.fillStyle = afterFill;
  ctx.fillRect(px, py, pw, ph);

  const afterX = px + pw * 0.52;
  const afterW = pw * 0.46;
  setFont(ctx, FONTS.mono, 11, 700);
  spacedText(ctx, ba.afterLabel ?? "AFTER", afterX, py + 42, 1.8, { color: GOLD });
  setFont(ctx, FONTS.display, 36, 900);
  ctx.fillStyle = INK;
  ctx.textAlign = "left";
  ctx.fillText(ba.afterMetric ?? "", afterX, py + 88);

  (ba.afterBullets ?? []).forEach((bullet, i) => {
    const y = py + 132 + i * 82;
    const appear = stagger(pct, i, ba.afterBullets.length, 10, 52);
    ctx.globalAlpha = 0.2 + 0.8 * appear;
    roundedPath(ctx, afterX - 10, y - 24, afterW + 20, 66, 10);
    ctx.fillStyle = alpha(GOLD, 0.13);
    ctx.fill();
    ctx.strokeStyle = alpha(GOLD, 0.24);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = GOLD;
    ctx.fillRect(afterX, y - 10, 3, 26);
    setFont(ctx, FONTS.regular, 18, 400);
    ctx.fillStyle = alpha(INK, 0.93);
    wrap(ctx, bullet, afterW - 40).slice(0, 2).forEach((line, j) => ctx.fillText(line, afterX + 16, y + 4 + j * 22));
    ctx.globalAlpha = 1;
  });

  // BEFORE: clipped to the left of the divider.
  ctx.save();
  ctx.beginPath();
  ctx.rect(px, py, (pw * divider) / 100, ph);
  ctx.clip();
  ctx.fillStyle = "rgba(8,8,11,0.985)";
  ctx.fillRect(px, py, pw, ph);
  const beforeX = px + 30;
  const beforeW = pw * 0.46;
  setFont(ctx, FONTS.mono, 11, 700);
  spacedText(ctx, ba.beforeLabel ?? "BEFORE", beforeX, py + 42, 1.8, { color: UNKNOWN });
  setFont(ctx, FONTS.display, 36, 900);
  ctx.fillStyle = alpha(INK, 0.5);
  ctx.fillText(ba.beforeMetric ?? "", beforeX, py + 88);
  (ba.beforeBullets ?? []).forEach((bullet, i) => {
    const y = py + 132 + i * 82;
    ctx.save();
    ctx.setLineDash([4, 6]);
    ctx.strokeStyle = alpha(UNKNOWN, 0.22);
    ctx.lineWidth = 1;
    roundedPath(ctx, beforeX - 10, y - 24, beforeW + 20, 66, 10);
    ctx.stroke();
    ctx.restore();
    setFont(ctx, FONTS.mono, 15, 400);
    ctx.fillStyle = alpha(MUTED, 0.72);
    wrap(ctx, bullet, beforeW - 20).slice(0, 2).forEach((line, j) => ctx.fillText(line, beforeX + 8, y + 2 + j * 20));
  });
  ctx.restore();

  // Divider + handle.
  const dx = px + (pw * divider) / 100;
  hairline(ctx, dx, py, dx, py + ph, alpha(GOLD_LIGHT, 0.9), 2);
  const knobY = py + ph / 2;
  ctx.fillStyle = BG;
  ctx.beginPath();
  ctx.arc(dx, knobY, 21, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = GOLD_LIGHT;
  ctx.lineWidth = 1.6;
  ctx.stroke();
  setFont(ctx, FONTS.monoBold, 11.5, 700);
  ctx.fillStyle = GOLD_LIGHT;
  ctx.textAlign = "center";
  ctx.fillText("VS", dx, knobY + 4);
  ctx.textAlign = "left";
  ctx.restore();

  if (scene.copy.interactionHint) {
    setFont(ctx, FONTS.mono, 11.5, 400);
    ctx.fillStyle = alpha(UNKNOWN, 0.85);
    ctx.fillText(scene.copy.interactionHint.toUpperCase(), px + 24, py + ph - 18);
  }
  void layout;
}

function drawTimeline(ctx, scene, pct, layout) {
  const { x: px, y: py, w: pw, h: ph } = PANEL;
  panel(ctx, px, py, pw, ph, { border: alpha(GOLD, 0.16) });

  const tracks = scene.timelineTracks ?? [];
  const gutter = 240;      // track name + clip
  const stateGutter = 132; // evidence state chip
  const segX = px + gutter;
  const segW = pw - gutter - stateGutter - 16;
  const rowH = 98;
  const top = py + 34;

  setFont(ctx, FONTS.mono, 11, 700);
  spacedText(ctx, "SEQUENCE // PLAYHEAD", px + 24, top + 8, 1.8, { color: GOLD });
  setFont(ctx, FONTS.mono, 11, 500);
  ctx.textAlign = "right";
  ctx.fillStyle = alpha(MUTED, 0.9);
  ctx.fillText(`PLAYHEAD ${String(Math.round(pct)).padStart(2, "0")}%`, px + pw - 24, top + 8);
  ctx.textAlign = "left";

  tracks.forEach((track, i) => {
    const y = top + 40 + i * rowH;
    const appear = stagger(pct, i, tracks.length, 4, 30);
    ctx.globalAlpha = 0.4 + 0.6 * appear;

    setFont(ctx, FONTS.mono, 11.5, 700);
    spacedText(ctx, track.track, px + 24, y + 16, 1.5, { color: track.color });
    setFont(ctx, FONTS.mono, 10, 400);
    ctx.fillStyle = alpha(MUTED, 0.78);
    ctx.fillText(`${track.clip} · ${track.timecode}`, px + 24, y + 34);

    const totalSpans = track.segments.reduce((sum, s) => sum + s.span, 0) || 1;
    let x = segX;
    track.segments.forEach((segment, j) => {
      const segWidth = (segW * segment.span) / totalSpans - 6;
      const lit = pct > (j / track.segments.length) * 92;
      roundedPath(ctx, x, y, segWidth, 44, 8);
      ctx.fillStyle = lit ? alpha(track.color, 0.15) : "rgba(255,255,255,0.035)";
      ctx.fill();
      ctx.strokeStyle = lit ? alpha(track.color, 0.5) : "rgba(255,255,255,0.09)";
      ctx.lineWidth = 1;
      ctx.stroke();
      setFont(ctx, FONTS.mono, 11, 500);
      ctx.fillStyle = lit ? alpha(INK, 0.94) : alpha(MUTED, 0.6);
      ctx.textAlign = "left";
      const label = wrap(ctx, segment.label, segWidth - 22)[0] ?? "";
      ctx.fillText(label, x + 11, y + 27);
      x += segWidth + 6;
    });

    // The evidence state, as a word first and a colour second.
    const state = track.state ?? "OBSERVED";
    const stateColor = state === "UNKNOWN" ? UNKNOWN : state === "RECOVERED" ? INK : GOLD;
    setFont(ctx, FONTS.mono, 9.5, 700);
    const stateWidth = ctx.measureText(state).width + 16;
    const sx = px + pw - 24 - stateWidth;
    roundedPath(ctx, sx, y + 4, stateWidth, 19, 4);
    if (state === "UNKNOWN") ctx.setLineDash([3, 3]);
    ctx.strokeStyle = alpha(stateColor, 0.62);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = stateColor;
    ctx.fillText(state, sx + 8, y + 17);
    ctx.globalAlpha = 1;
  });

  // Playhead across the segment area only, so it never sits on a label.
  const rowsTop = top + 34;
  const rowsBottom = top + 40 + tracks.length * rowH - 24;
  const headX = segX + (segW * pct) / 100;
  const beam = ctx.createLinearGradient(headX - 16, 0, headX + 16, 0);
  beam.addColorStop(0, alpha(GOLD, 0));
  beam.addColorStop(0.5, alpha(GOLD, 0.11));
  beam.addColorStop(1, alpha(GOLD, 0));
  ctx.fillStyle = beam;
  ctx.fillRect(headX - 16, rowsTop, 32, rowsBottom - rowsTop);
  hairline(ctx, headX, rowsTop, headX, rowsBottom, GOLD_LIGHT, 1.6);
  ctx.fillStyle = GOLD_LIGHT;
  ctx.beginPath();
  ctx.moveTo(headX - 6, rowsTop - 9);
  ctx.lineTo(headX + 6, rowsTop - 9);
  ctx.lineTo(headX, rowsTop);
  ctx.closePath();
  ctx.fill();
  void layout;
}

function drawHud(ctx, scene, pct, layout) {
  const cards = scene.hudCounters ?? [];
  const gap = 40;
  const cardW = (PANEL.w - gap) / 2;
  const cardH = (PANEL.h - gap) / 2;

  cards.forEach((cardSpec, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = PANEL.x + col * (cardW + gap);
    const y = PANEL.y + row * (cardH + gap);
    const appear = stagger(pct, i, cards.length, 6, 30);
    const eased = easeOutQuint(appear);
    const dashed = Boolean(cardSpec.dashed);

    ctx.save();
    ctx.globalAlpha = 0.25 + 0.75 * eased;
    roundedPath(ctx, x, y, cardW, cardH, 18);
    const fill = ctx.createLinearGradient(x, y, x + cardW, y + cardH);
    fill.addColorStop(0, dashed ? "rgba(15,15,18,0.96)" : "rgba(20,18,13,0.96)");
    fill.addColorStop(1, "rgba(9,9,12,0.97)");
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = dashed ? alpha(UNKNOWN, 0.5) : alpha(GOLD, 0.3);
    ctx.lineWidth = 1.2;
    if (dashed) ctx.setLineDash([6, 6]);
    ctx.stroke();
    ctx.setLineDash([]);

    setFont(ctx, FONTS.mono, 10.5, 700);
    spacedText(ctx, cardSpec.code, x + 22, y + 32, 1.6, { color: dashed ? UNKNOWN : GOLD });

    const value = cardSpec.from + (cardSpec.to - cardSpec.from) * eased;
    const shown = cardSpec.decimals ? value.toFixed(cardSpec.decimals) : Math.round(value).toString();
    setFont(ctx, FONTS.monoBold, 54, 700);
    ctx.fillStyle = dashed ? alpha(INK, 0.55) : GOLD_LIGHT;
    ctx.textAlign = "left";
    ctx.fillText(`${shown}${cardSpec.unit ?? ""}`, x + 20, y + 100);

    const badge = cardSpec.deltaBadge ?? "";
    setFont(ctx, FONTS.mono, 10, 700);
    const badgeW = ctx.measureText(badge).width + 14;
    roundedPath(ctx, x + cardW - 22 - badgeW, y + 74, badgeW, 19, 4);
    ctx.fillStyle = dashed ? alpha(UNKNOWN, 0.14) : alpha(GOLD, 0.15);
    ctx.fill();
    ctx.fillStyle = dashed ? UNKNOWN : GOLD;
    ctx.fillText(badge, x + cardW - 22 - badgeW + 7, y + 87);

    setFont(ctx, FONTS.semi, 17, 600);
    ctx.fillStyle = dashed ? alpha(INK, 0.72) : INK;
    wrap(ctx, cardSpec.label, cardW - 44).slice(0, 2).forEach((line, j) => ctx.fillText(line, x + 22, y + 136 + j * 23));

    setFont(ctx, FONTS.regular, 13, 400);
    ctx.fillStyle = alpha(MUTED, dashed ? 0.72 : 0.92);
    wrap(ctx, cardSpec.sublabel, cardW - 44).slice(0, 3).forEach((line, j) => ctx.fillText(line, x + 22, y + 204 + j * 19));

    // Tick meter. The CANNOT TELL card deliberately stops short of full: unknown is a recorded answer, not a blank.
    const ticks = cardSpec.ticks ?? 12;
    const lit = Math.round(eased * ticks * (dashed ? 0.45 : 1));
    const tickW = (cardW - 44 - (ticks - 1) * 3) / ticks;
    for (let t = 0; t < ticks; t += 1) {
      roundedPath(ctx, x + 22 + t * (tickW + 3), y + cardH - 40, tickW, 7, 2);
      ctx.fillStyle = t < lit ? (dashed ? alpha(UNKNOWN, 0.85) : GOLD) : "rgba(255,255,255,0.08)";
      ctx.fill();
    }
    ctx.restore();
  });
  void layout;
}

function drawCodeDiff(ctx, scene, pct, layout) {
  panel(ctx, PANEL.x, PANEL.y, PANEL.w, PANEL.h, { border: alpha(GOLD, 0.18) });
  const px = PANEL.x;
  const py = PANEL.y;
  const pw = PANEL.w;
  const ph = PANEL.h;

  setFont(ctx, FONTS.mono, 12, 500);
  ctx.fillStyle = alpha(MUTED, 0.9);
  ctx.textAlign = "left";
  ctx.fillText("incident/repo/app.py", px + 24, py + 36);

  const badge = (scene.copy.overlayBadge ?? "").toUpperCase();
  setFont(ctx, FONTS.mono, 10, 700);
  const badgeW = ctx.measureText(badge).width + 16;
  roundedPath(ctx, px + pw - 24 - badgeW, py + 21, badgeW, 20, 4);
  ctx.strokeStyle = alpha(GOLD, 0.5);
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.fillStyle = GOLD;
  ctx.fillText(badge, px + pw - 24 - badgeW + 8, py + 35);
  hairline(ctx, px, py + 54, px + pw, py + 54, "rgba(255,255,255,0.08)");

  // Lines are laid out from a fixed top and a fixed slot; the last slot never reaches the approval ledger.
  const lines = scene.codeLines ?? [];
  const slot = 84;
  lines.forEach((line, i) => {
    const y = py + 76 + i * slot;
    const active = pct >= line.highlightAtPct;
    const added = line.kind === "added";
    const removed = line.kind === "removed";
    roundedPath(ctx, px + 18, y, pw - 36, 70, 10);
    ctx.fillStyle = active ? (added ? alpha(GOLD, 0.16) : alpha(MUTED, 0.07)) : "rgba(255,255,255,0.028)";
    ctx.fill();
    if (active) {
      ctx.strokeStyle = added ? alpha(GOLD, 0.58) : alpha(MUTED, 0.26);
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    setFont(ctx, FONTS.mono, 11.5, 400);
    ctx.fillStyle = alpha(MUTED, 0.5);
    ctx.fillText(String(line.lineNumber), px + 34, y + 28);
    setFont(ctx, FONTS.mono, 16, 500);
    ctx.fillStyle = added ? GOLD_LIGHT : removed ? alpha(MUTED, 0.8) : active ? INK : alpha(INK, 0.6);
    ctx.fillText(line.code, px + 68, y + 28);
    if (active) {
      setFont(ctx, FONTS.regular, 13.5, 400);
      ctx.fillStyle = added ? alpha(GOLD_LIGHT, 0.9) : alpha(MUTED, 0.85);
      ctx.fillText(`// ${line.annotation}`, px + 68, y + 52);
    }
  });

  // The approval ledger: the token that was reviewed, then the check the human named.
  const approval = scene.approval ?? {};
  const verify = approval.verify ?? {};
  const baseY = py + ph - 116;
  hairline(ctx, px + 18, baseY, px + pw - 18, baseY, "rgba(255,255,255,0.08)");

  setFont(ctx, FONTS.mono, 10, 700);
  spacedText(ctx, "APPROVAL TOKEN", px + 24, baseY + 28, 1.6, { color: alpha(GOLD, 0.75) });
  setFont(ctx, FONTS.mono, 13, 500);
  ctx.fillStyle = alpha(INK, 0.88);
  ctx.fillText(`${String(approval.token ?? "").slice(0, 12)}…`, px + 24, baseY + 50);

  const verifyAppear = easeOutCubic(progress(pct, 52, 74));
  ctx.save();
  ctx.globalAlpha = verifyAppear;
  const chipX = px + 260;
  const chipW = pill(ctx, String(verify.verdict ?? ""), chipX, baseY + 14, { height: 26, size: 11.5 });
  setFont(ctx, FONTS.mono, 11.5, 500);
  ctx.fillStyle = alpha(INK, 0.85);
  ctx.textAlign = "left";
  ctx.fillText(`${verify.command ?? ""}  ·  exit ${verify.exit ?? "?"}  ·  ${verify.seconds ?? "?"} s  ·  ${verify.summary ?? ""}`,
    chipX + chipW + 14, baseY + 32);
  ctx.restore();

  // The line the tool actually printed after applying: unverified until a check is run. It stays on screen with the
  // PASS, so the frame cannot be read as "the fix is proven" without "by the check you named".
  if (approval.caveat) {
    setFont(ctx, FONTS.mono, 11, 400);
    ctx.fillStyle = alpha(UNKNOWN, 0.9);
    ctx.fillText(approval.caveat, px + 24, baseY + 78);
  }
  void layout;
}

function drawWarp(ctx, spec, scene, pct) {
  const cx = FRAME_WIDTH / 2;
  const cy = FRAME_HEIGHT * 0.38;
  const surge = easeOutExpoSoft(progress(pct, 0, 42));

  const count = 64;
  for (let i = 0; i < count; i += 1) {
    const angle = (Math.PI * 2 * i) / count + 0.06;
    const wave = 0.55 + 0.45 * Math.sin(i * 12.9898 + pct * 0.09);
    const inner = 130 + surge * 420 + wave * 40;
    const outer = inner + 80 + surge * 260 * wave;
    const fade = clamp(1 - inner / 940, 0, 1) * (0.18 + 0.3 * surge);
    hairline(ctx,
      cx + Math.cos(angle) * inner, cy + Math.sin(angle) * inner,
      cx + Math.cos(angle) * outer, cy + Math.sin(angle) * outer,
      i % 3 === 0 ? alpha(GOLD_LIGHT, fade) : alpha(INK, fade * 0.55), 1.2);
  }

  const ruleWidth = 640 * easeOutCubic(progress(pct, 8, 36));
  const ruleY = cy + 88;
  if (ruleWidth > 0) hairline(ctx, cx - ruleWidth / 2, ruleY, cx + ruleWidth / 2, ruleY, alpha(GOLD, 0.75), 1.6);

  ctx.save();
  ctx.globalAlpha = easeOutCubic(progress(pct, 6, 34));
  setFont(ctx, FONTS.display, 128, 900);
  spacedText(ctx, scene.warpTitle ?? "BLACKBOX", cx, cy + 40, 20, { align: "center", color: INK });
  ctx.restore();

  ctx.save();
  ctx.globalAlpha = easeOutCubic(progress(pct, 22, 48));
  setFont(ctx, FONTS.medium, 30, 500);
  ctx.textAlign = "center";
  ctx.fillStyle = alpha(INK, 0.86);
  ctx.fillText(scene.warpSubtitle ?? "", cx, cy + 152);
  ctx.restore();

  // Pillars are laid out once, measured once, then revealed one by one.
  const pillars = scene.warpPillars ?? [];
  setFont(ctx, FONTS.mono, 13.5, 500);
  const pillarWidths = pillars.map((p) => ctx.measureText(p).width + 40);
  const pillarsTotal = pillarWidths.reduce((a, b) => a + b, 0) + Math.max(0, pillars.length - 1) * 14;
  let pillarX = cx - pillarsTotal / 2;
  pillars.forEach((pillar, i) => {
    ctx.save();
    ctx.globalAlpha = stagger(pct, i, pillars.length, 34, 62);
    roundedPath(ctx, pillarX, cy + 188, pillarWidths[i], 40, 20);
    ctx.strokeStyle = alpha(GOLD, 0.4);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = alpha(INK, 0.82);
    ctx.textAlign = "left";
    ctx.fillText(pillar, pillarX + 20, cy + 214);
    ctx.restore();
    pillarX += pillarWidths[i] + 14;
  });

  ctx.save();
  ctx.globalAlpha = easeOutCubic(progress(pct, 52, 74));
  setFont(ctx, FONTS.semi, 19, 600);
  const cta = spec.product?.primaryCta ?? "Follow development";
  const ctaW = ctx.measureText(cta).width + 64;
  roundedPath(ctx, cx - ctaW / 2, cy + 248, ctaW, 56, 28);
  ctx.fillStyle = GOLD;
  ctx.fill();
  ctx.fillStyle = "#0A0A0C";
  ctx.textAlign = "center";
  ctx.fillText(cta, cx, cy + 284);
  ctx.restore();

  ctx.save();
  ctx.globalAlpha = easeOutCubic(progress(pct, 60, 84));
  // The disclosure sits between the caption box and the footer rule: present on every end-card frame, never on top of
  // the narration it qualifies.
  const note = spec.film?.closingNote ?? "";
  setFont(ctx, FONTS.mono, 13, 400);
  const noteWidth = ctx.measureText(note).width;
  roundedPath(ctx, cx - noteWidth / 2 - 18, 900, noteWidth + 36, 28, 6);
  ctx.fillStyle = "rgba(5,5,8,0.72)";
  ctx.fill();
  ctx.fillStyle = alpha(INK, 0.66);
  ctx.textAlign = "center";
  ctx.fillText(note, cx, 920);
  ctx.restore();
}

/* ── editorial column ───────────────────────────────────────────────────────────────────────────────────────────── */

/**
 * Eyebrow, headline, body — the half of every frame that does the talking. Returns where the copy ended so a mode can
 * put its own furniture below it instead of on top of it.
 */
function drawEditorial(ctx, scene, { width, headlineMax, headlineMin, bodySize = 24, bodyMaxWidth }) {
  setFont(ctx, FONTS.mono, 13, 700);
  spacedText(ctx, scene.copy.eyebrow, M, 214, 2.4, { color: GOLD });

  const lines = scene.copy.headlines ?? [];
  const size = fitHeadlineSize(ctx, lines, width, headlineMax, headlineMin);
  const lineHeight = Math.round(size * 1.16);
  let y = 296;
  lines.forEach((line, i) => {
    setFont(ctx, FONTS.display, size, 900);
    ctx.fillStyle = scene.copy.accentLine === i ? GOLD : INK;
    ctx.textAlign = "left";
    ctx.fillText(line, M, y);
    y += lineHeight;
  });

  let bodyBottom = y - lineHeight + size * 0.4;
  if (scene.copy.body) {
    bodyBottom = paragraph(ctx, scene.copy.body, M, y + 18, {
      family: FONTS.regular,
      size: bodySize,
      weight: 400,
      color: alpha(MUTED, 0.95),
      maxWidth: bodyMaxWidth ?? width,
      lineHeight: Math.round(bodySize * 1.5),
    });
  }
  return { bodyBottom, headlineSize: size };
}

/* ── the entry point ───────────────────────────────────────────────────────────────────────────────────────────── */

/**
 * Draw one frame.
 *
 * @param {CanvasRenderingContext2D} ctx
 * @param {object} spec      the blueprint (see studio/spec.json)
 * @param {number} sceneIndex
 * @param {number} pct       0…100 inside the scene
 * @param {object} [options] `{ width, height, showCaptions, manualSplit, elapsedMs, timecode }`
 */
export function renderFrame(ctx, spec, sceneIndex, pct, options = {}) {
  const width = options.width ?? FRAME_WIDTH;
  const height = options.height ?? FRAME_HEIGHT;
  const scene = spec.scenes[sceneIndex] ?? spec.scenes[0];
  const clamped = clamp(pct, 0, 100);

  ctx.save();
  ctx.setTransform(width / FRAME_WIDTH, 0, 0, height / FRAME_HEIGHT, 0, 0);
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";

  drawBackground(ctx, scene, FRAME_WIDTH, FRAME_HEIGHT, clamped, options.motionReduced === true);

  // Content fades in over the first few percent and out over the last few, so cuts read as dissolves. The final scene
  // holds instead of fading: it is the end card, and a fade there would look like a dropout.
  const isLast = sceneIndex === spec.scenes.length - 1;
  const contentAlpha = Math.min(progress(clamped, 0, 3.2), isLast ? 1 : 1 - progress(clamped, 97, 100));

  ctx.save();
  ctx.globalAlpha = contentAlpha;

  drawHeader(ctx, spec, scene, options.timecode ?? "");
  drawDisclosure(ctx, scene);

  const mode = scene.mode;
  const split = Boolean(scene.splitLayout);
  // The telemetry scene reads left-to-right too: copy on the left, its 2x2 panel on the right. Only the end card is
  // full-bleed, and it has no editorial block at all.
  const hasPanel = split || mode === "hud-telemetry";
  const columnWidth = hasPanel ? 660 : FRAME_WIDTH - M * 2;

  let layout = { bodyBottom: 296 };
  if (mode === "warp-reveal") {
    // The wordmark is the headline in the end card; the editorial block would only fight it.
    setFont(ctx, FONTS.mono, 13, 700);
    spacedText(ctx, scene.copy.eyebrow, M, 214, 2.4, { color: GOLD });
    drawWarp(ctx, spec, scene, clamped);
  } else {
    layout = drawEditorial(ctx, scene, {
      width: columnWidth,
      headlineMax: hasPanel ? 76 : 84,
      headlineMin: 44,
      bodySize: hasPanel ? 24 : 26,
      bodyMaxWidth: hasPanel ? 620 : 1080,
    });

    if (mode === "split-collage") drawSplitCollage(ctx, scene, clamped, layout);
    else if (mode === "arch-graph") drawArchGraph(ctx, scene, clamped, layout);
    else if (mode === "before-after") drawBeforeAfter(ctx, scene, clamped, layout, options);
    else if (mode === "ember-timeline") {
      drawTimeline(ctx, scene, clamped, layout);
      // The evidence vocabulary, shown as it is used in the panel: filled, outlined, and dashed-but-honest.
      setFont(ctx, FONTS.mono, 11, 700);
      const chipSpecs = [
        { label: "OBSERVED", color: GOLD, fill: true },
        { label: "RECOVERED", color: INK, fill: false },
        { label: "UNKNOWN", color: UNKNOWN, dashed: true, fill: false },
      ];
      const chipW = (label) => ctx.measureText(label).width + 30;
      const total = chipSpecs.reduce((sum, c) => sum + chipW(c.label), 0) + 12 * (chipSpecs.length - 1);
      let cx = M + Math.max(0, (620 - total) / 2);
      const y = Math.min(layout.bodyBottom + 74, 742);
      chipSpecs.forEach((chip, i) => {
        ctx.save();
        ctx.globalAlpha *= stagger(clamped, i, chipSpecs.length, 22, 46);
        const w = chipW(chip.label);
        roundedPath(ctx, cx, y, w, 28, 14);
        if (chip.fill) {
          ctx.fillStyle = alpha(chip.color, 0.16);
          ctx.fill();
          ctx.strokeStyle = alpha(chip.color, 0.65);
        } else {
          ctx.strokeStyle = alpha(chip.color, 0.5);
          if (chip.dashed) ctx.setLineDash([4, 4]);
        }
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = chip.color;
        ctx.textAlign = "center";
        ctx.fillText(chip.label, cx + w / 2, y + 18);
        ctx.textAlign = "left";
        ctx.restore();
        cx += w + 12;
      });
      ctx.save();
      ctx.globalAlpha *= stagger(clamped, chipSpecs.length - 1, chipSpecs.length, 22, 46);
      setFont(ctx, FONTS.mono, 11.5, 400);
      ctx.fillStyle = alpha(UNKNOWN, 0.92);
      ctx.fillText("an interrupted call has no established outcome", M, y + 56);
      ctx.restore();
    } else if (mode === "hud-telemetry") {
      drawHud(ctx, scene, clamped, layout);
      setFont(ctx, FONTS.mono, 11.5, 500);
      spacedText(ctx, "READ-ONLY · WRITES A REPORT, NOT A PATCH", M, Math.min(layout.bodyBottom + 64, 780), 1.5,
        { color: alpha(GOLD, 0.7) });
    } else if (mode === "code-diff") {
      drawCodeDiff(ctx, scene, clamped, layout);
      const steps = scene.approval?.steps ?? [];
      let y = Math.min(layout.bodyBottom + 60, 640);
      steps.forEach((step, i) => {
        ctx.save();
        ctx.globalAlpha *= stagger(clamped, i, steps.length, 14, 52);
        setFont(ctx, FONTS.mono, 12.5, 700);
        ctx.fillStyle = i === steps.length - 1 ? GOLD : alpha(MUTED, 0.9);
        ctx.fillText(`0${i + 1}`, M, y);
        setFont(ctx, FONTS.regular, 17, 400);
        ctx.fillStyle = alpha(INK, 0.86);
        ctx.fillText(step, M + 40, y);
        if (i < steps.length - 1) hairline(ctx, M + 11, y + 10, M + 11, y + 30, alpha(GOLD, 0.22));
        ctx.restore();
        y += 40;
      });
    }
  }

  drawCaption(ctx, scene, clamped, options.showCaptions !== false);
  drawFooter(ctx, spec, sceneIndex, scene, clamped, options.timecode ?? "");

  ctx.restore();
  ctx.restore();
}

/** A poster frame: the one image a README, a thumbnail or a still card can use. */
export const POSTER = { sceneIndex: 5, pct: 78 };
