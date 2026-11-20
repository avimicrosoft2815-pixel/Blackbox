# Storyboard

Fifty-five seconds, seven scenes, one incident. The copy below is the copy in `studio/spec.json`; if the two ever
disagree, the JSON is what renders, which is why `npm test` reads it rather than this page.

Each scene is a *split stage*: editorial half on the left (eyebrow, headline, body), a visual half on the right whose
mode changes scene by scene. The frame's furniture — wordmark, scene chip, caption box, gold progress line — is
identical in all seven, which is what makes the film read as one piece rather than seven slides.

| # | Mode | Scene | Narration | On screen |
|---|---|---|---|---|
| 1 | `split-collage` | 0.0–9.1 s | 7.99 s | An AI application can fail between a request, a response and the next action. When the context is scattered, finding the cause takes time. |
| 2 | `arch-graph` | 9.1–16.2 s | 6.07 s | BlackBox records selected activity for supported integrations, helping you inspect the evidence when something goes wrong. |
| 3 | `before-after` | 16.2–23.0 s | 5.75 s | Instead of starting with disconnected clues, review what was recorded in one place, and see where the record ends. |
| 4 | `ember-timeline` | 23.0–30.4 s | 6.39 s | Follow the sequence through a request, response and interruption. BlackBox shows what it observed, and what it cannot establish. |
| 5 | `hud-telemetry` | 30.4–38.5 s | 7.03 s | The diagnosis connects findings to the evidence, separating observed facts from possible explanations and unanswered questions. |
| 6 | `code-diff` | 38.5–46.7 s | 7.03 s | Prepare a brief for your coding assistant. Review a proposed change, approve it explicitly, and see the result of the checks you choose. |
| 7 | `warp-reveal` | 46.7–55.0 s | 6.39 s | When the next incident happens, start with evidence. BlackBox helps you see more clearly, and decide what comes next. |

---

## 01 · The problem — `split-collage`

> **When an AI app fails,** / *what happened?*

Chips: request, partial response, the next action that never happened, two logs that share no id. A gold rail runs across
the top of the panel and **breaks** at 30 % of the scene — the motif the rest of the film inherits. The hero card asks the
question the film answers.

- Label on frame: *Illustrative scenario.*
- Cards enter on a spring (4, 14, 26, 38, 50 %), the rail draws from 4 % and snaps at 34 %.
- Captions: 0.5 s and 5.4 s (measured sentence boundary at 4.90 s, plus the 0.5 s lead).

## 02 · Meet BlackBox — `arch-graph`

> **A flight recorder** / *for AI applications.*

Six nodes, five edges, signals travelling along them. The upstream provider is dashed and grey — *not recorded* is part of
the diagram, not a footnote. The caption under the graph: *supported integrations only — a program with no instrumentation
gets no node, and the capsule says so.*

- No illustration label: this scene states product behaviour and nothing else.
- Edge labels only appear where they fit in the gap between two boxes; nothing overlaps a node.

## 03 · Scattered clues — `before-after`

> **Scattered clues,** / *one evidence-led view.*

A wipe between two readings of the same failure. Left, four clues that cannot be joined. Right, the same failure as
frames: `MODEL_REQUEST seq 5`, two `MODEL_CHUNK` frames, `MODEL_ERROR seq 11`, node closed with the exit code, and the
tool result marked **unknown**. The divider completes by 38 % and holds, so the narration lands on a readable frame.

- Label on frame: *Illustrative scenario.*
- In the studio the divider is draggable and moves with `A`/`D`.

## 04 · See it unfold — `ember-timeline`

> **Follow the sequence.** / *Keep the gaps.*

Six tracks — app, model request, response, interruption, recovery, review — with a playhead sweeping the segment area.
Each row carries an evidence state, word first: `OBSERVED`, `RECOVERED`, `UNKNOWN`. The review track ends with a dashed
`UNKNOWN` chip and the line *an interrupted call has no established outcome*.

- The track list is the recorded run: `RUN_START`, `MODEL_REQUEST`, `MODEL_CHUNK` ×2, `MODEL_ERROR`, `NODE_CLOSE`,
  `RUN_END`. `npm test` checks that the blueprint's track names match the capsule's.
- No illustration label: every number is from `evidence/`.

## 05 · Diagnosis — `hud-telemetry`

> **Evidence first.** / *Uncertainty visible.*

Four telemetry cards: 2 findings (both high confidence), 3 named alternatives, **2** things it cannot tell, 17 frames.
The third card is drawn dashed and dim on purpose, and its tick meter stops short of full: *unknown is a recorded answer,
not a blank*. Under the copy: *read-only — writes a report, not a patch*.

## 06 · You decide — `code-diff`

> **Review the proposal.** / *You decide.*

Four diff lines over a five-step approval ladder: brief, proposed change, diff and risk, explicit approval, verification.
Bottom of the panel holds the approval token (its first twelve characters) and the verification line: `PASS · exit 0 ·
0.36 s · 2 passed`, with the tool's own caveat under it — *nothing was run: this change is unverified until you run
`blackbox repair verify --test …`*. The chip in the corner: *nothing applies without the token*.

- Label on frame: *Representative repair, run on the synthetic demo repo.*
- The numbers are the transcript of the repair that was actually run during production; see `docs/evidence.md`.

## 07 · See the evidence — `warp-reveal`

> **BLACKBOX** — *See the evidence. Decide what comes next.*

Sixty-four radial streaks surge once and stop. Wordmark, rule, subtitle, three pillars (local-first, no account; evidence
with its limits shown; approval-gated repair), one call to action: *Follow development*. The closing note is on the frame
for the last six seconds: *Illustrative scenario. Coverage depends on the integration and capture settings. Product in
development.*

---

## Pacing rules

- Narration is the clock. Scene length = clip + 500 ms lead + tail (0.25–1.4 s). Nothing is stretched to fit a round
  number.
- One idea per scene, one sentence per idea, and the sentence is on screen as a caption while it is spoken.
- Gold marks the thing being pointed at. Nothing else is gold.
- Content fades in over the first 3.2 % of a scene and out over the last 3 % — a dissolve, not a cut — except the end
  card, which holds.
- Every frame carries the wordmark, the scene number, the caption box and the film's progress line, so a single frame
  taken out of context still says what it is.
