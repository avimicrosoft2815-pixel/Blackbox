# Narration: script, timing and the voice asset

## What the film actually uses

Seven generated clips, one per scene, in `studio/assets/narration/scene-1.mp3` … `scene-7.mp3`. They are real audio, mixed
into the export at each scene's measured offset and verified present in the muxed file — not a placeholder, and not
`speechSynthesis` (which browsers do not capture).

- **Voice:** one neutral, masculine, unhurried read; advertising cadence, level delivery, no upward inflection on claims.
- **Language:** English.
- **File format:** MP3, mono, speech only. No music bed: there is no licensed track, and using one would mean shipping
  media this project has no right to.
- **Length:** 46.7 s of speech across 55.0 s of film.

If you want a different voice, replace the seven files and set each scene's `narration.audioMs` to the new length; `npm
test` then fails until the scene is long enough to hold it. That is the intended workflow, and it is why the audio length
is stored next to the script rather than only in the MP3.

## Script, with the boundaries the captions use

Boundaries were measured with `ffmpeg -af silencedetect=noise=-32dB:d=0.18`, not estimated; the caption times below are
those boundaries shifted by the 500 ms lead-in.

### Scene 1 — The problem · 7.99 s

> An AI application can fail between a request, a response and the next action. When the context is scattered, finding the
> cause takes time.

| Caption | Starts | Text |
|---|---|---|
| 1 | 0.50 s | An AI application can fail between a request, a response and the next action. |
| 2 | 5.40 s | When the context is scattered, finding the cause takes time. |

### Scene 2 — Meet BlackBox · 6.07 s

> BlackBox records selected activity for supported integrations, helping you inspect the evidence when something goes
> wrong.

| Caption | Starts | Text |
|---|---|---|
| 1 | 0.50 s | BlackBox records selected activity for supported integrations, |
| 2 | 4.20 s | helping you inspect the evidence when something goes wrong. |

### Scene 3 — Scattered clues · 5.75 s

> Instead of starting with disconnected clues, review what was recorded in one place, and see where the record ends.

| Caption | Starts | Text |
|---|---|---|
| 1 | 0.50 s | Instead of starting with disconnected clues, |
| 2 | 2.94 s | review what was recorded in one place, and see where the record ends. |

### Scene 4 — See it unfold · 6.39 s

> Follow the sequence through a request, response and interruption. BlackBox shows what it observed, and what it cannot
> establish.

| Caption | Starts | Text |
|---|---|---|
| 1 | 0.50 s | Follow the sequence through a request, response and interruption. |
| 2 | 3.87 s | BlackBox shows what it observed, and what it cannot establish. |

### Scene 5 — Diagnosis · 7.03 s

> The diagnosis connects findings to the evidence, separating observed facts from possible explanations and unanswered
> questions.

| Caption | Starts | Text |
|---|---|---|
| 1 | 0.50 s | The diagnosis connects findings to the evidence, |
| 2 | 3.17 s | separating observed facts from possible explanations and unanswered questions. |

### Scene 6 — You decide · 7.03 s

> Prepare a brief for your coding assistant. Review a proposed change, approve it explicitly, and see the result of the
> checks you choose.

| Caption | Starts | Text |
|---|---|---|
| 1 | 0.50 s | Prepare a brief for your coding assistant. |
| 2 | 2.81 s | Review a proposed change, approve it explicitly, and see the result of the checks you choose. |

### Scene 7 — See the evidence · 6.39 s

> When the next incident happens, start with evidence. BlackBox helps you see more clearly, and decide what comes next.

| Caption | Starts | Text |
|---|---|---|
| 1 | 0.50 s | When the next incident happens, start with evidence. |
| 2 | 3.32 s | BlackBox helps you see more clearly, and decide what comes next. |

## What the narration does not say

No percentage, no duration saved, no "enterprise", no comparative claim about another tool, no date, no promise about
coverage. The two places where the script could have overreached are handled on screen instead: "supported integrations"
in scene 2 (the diagram draws the unsupported case as dashed and grey) and "what it cannot establish" in scene 4 (the
timeline ends with a dashed `UNKNOWN`).

## Subtitles

`export/blackbox-film.srt` is generated from the same caption list that the renderer burns into the frame
(`tools/render_film.mjs` → `buildSrt`), so the two cannot disagree. It is UTF-8, CRLF-terminated, with `-->` timing.
