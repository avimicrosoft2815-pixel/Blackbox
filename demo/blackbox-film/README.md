# The BlackBox demo film

A 55-second film about one incident, recorded by this repository's own CLI, plus the Director Studio that plays, inspects
and re-times it. Everything here is self-contained: the picture is drawn by `studio/js/frame.js`, the narration is seven
generated clips, and the incident is a synthetic run you can reproduce with one command.

| | |
|---|---|
| **The film** | `export/blackbox-film-1080p.mp4` (1920×1080) and `export/blackbox-film-4k.mp4` (3840×2160) |
| **Subtitles** | `export/blackbox-film.srt` — built from the same captions burned into the picture |
| **The tour** | `npm run studio` → <http://localhost:8181/tour> — the sixty-second journey: install, point it at any OpenAI-compatible model, break it, read the evidence, approve a repair |
| **The studio** | `npm run studio` → <http://localhost:8181/studio/> |
| **Why this is different** | `docs/showcase.md` — ten things a demo usually does not show, each one re-measured by `npm run showcase` |
| **Download link** | `/film.mp4` on the same server, sent as an attachment |
| **The incident** | `incident/run_demo.sh` → writes `evidence/*` |
| **Tests** | `npm test` (32 checks: blueprint, narration, sound cues, renderer, studio behaviour, the tour page) |

## Watch it

```bash
cd demo/blackbox-film
npm install          # @napi-rs/canvas for the renderer, jsdom for the studio tests
npm run studio       # http://localhost:8181/studio/  →  the Director Studio
```

The **Director Studio** is the review surface: one centred column, a header of transport pills and scene tabs, and a
footer of seven scrub bars under a live caption and the words being spoken. It plays at 0.5–2×, shows a live stage
rendered by the same module that makes the export, and opens one drawer at a time — **Edit** (copy, validated against
the frame's own measurements), **Blueprint** (facts, re-measured checks, and the scene's JSON), **Script** (the full
transcript). `frame PNG` saves the frame you are looking at at 1920 × 1080; the keys drawer lists every shortcut
(`Space`, `←`/`→`, `Shift`+arrows, `A`/`D`, `C`, `M`, `E`, `I`, `T`, `R`, `Esc`). A deep link opens on a frame:
`/studio/#scene=4&pct=62`.

To download the rendered film without the studio, open `/film.mp4` on that server — the response is
`Blackbox-film-1080p.mp4` as an attachment. `/film-4k.mp4` is the same for the 4K file.

## The incident on screen

`incident/` holds a synthetic application, a mock model provider and a one-shot reproducer:

```bash
BLACKBOX=../../.venv/bin/blackbox PYTHON=../../.venv/bin/python bash incident/run_demo.sh /tmp/bbdemo
```

It starts a local mock that streams two SSE deltas in chunked framing and then closes the socket, runs the app through
`blackbox run`, then records the capsule, verifies it and asks for a diagnosis. The result is a 17-event run — request,
two 92-byte chunks, an interrupted upstream, a node closed with `outcome=error`, exit code 3 — and two findings, both
observed, both high confidence.

`evidence/` holds that run **scrubbed**: absolute paths became `<repo>` and `<tmp>`, the process id is gone, the
summary is split into machine-readable `run.json` and the app's own `run.out`. Run and capsule ids, sequence numbers,
byte counts, timings and the tool's own words are kept, because a film that shows evidence has to leave the evidence
checkable. `tools/scrub_evidence.py` does the scrubbing and refuses to finish if a path or a pid survives.

## What is recorded, what is illustrated

- **Recorded, then quoted:** the run summary, the capsule listing, the verify output, the diagnosis and its limits.
  These appear in scenes 3–5 with their real ids and numbers.
- **Illustrated, and labelled on the frame:** the collage in scene 1, the before/after comparison in scene 3, the diff in
  scene 6. The label reads *Illustrative scenario* or *Representative repair, run on the synthetic demo repo*.
- **Product behaviour, stated plainly:** the topology in scene 2 (a supported integration gets a node; one that isn't
  instrumented does not) and the end card.
- **Not shown anywhere:** customer content, real incidents, API keys, hostnames, usernames, private source, performance
  numbers, competitor claims, dates, waitlists.

`docs/claims-ledger.md` lists every sentence that makes a claim and what backs it.

## The pipeline

```
studio/spec.json ──► studio/js/frame.js ──► canvas (browser, live stage)
                    tools/sfx.mjs ──► studio/sfx/*.wav ──► mixed under the narration
                                  └──────► canvas (Node) ──► ffmpeg ──► export/*.mp4
studio/assets/narration/scene-N.mp3 ─────────────┘  (mixed at each scene's measured offset)
```

One renderer, two hosts. `tools/render_frames.mjs` and `tools/render_film.mjs` import the *studio's* module, so the
export cannot drift from the preview — there is no second implementation to keep in step.

```bash
npm run film        # 1080p → export/blackbox-film-1080p.mp4 (+ the .srt)
npm run film:4k     # 3840×2160 → export/blackbox-film-4k.mp4
npm run still -- --scene 6 --pct 78 --out poster/    # one frame, for docs and thumbnails
npm test
```

Frames are piped straight into ffmpeg: a 4K render would otherwise be several gigabytes of PNG written and immediately
read back. Rendering costs roughly 1.5 minutes for 1080p and about ten for 4K on a 4-core machine.

## Timings come from the audio, not from taste

Each scene's `durationMs` is its narration clip plus a 500 ms lead-in and a tail:

| Scene | Mode | Audio | Scene |
|---|---|---|---|
| 1 The problem | split-collage | 7.99 s | 9.10 s |
| 2 Meet BlackBox | arch-graph | 6.07 s | 7.10 s |
| 3 Scattered clues | before-after | 5.75 s | 6.80 s |
| 4 See it unfold | ember-timeline | 6.39 s | 7.40 s |
| 5 Diagnosis | hud-telemetry | 7.03 s | 8.10 s |
| 6 You decide | code-diff | 7.03 s | 8.20 s |
| 7 See the evidence | warp-reveal | 6.39 s | 8.30 s |
| **Total** | | **46.7 s** | **55.0 s** |

`npm test` re-measures the MP3s with ffmpeg and fails if a declared length drifts by more than 120 ms — the table above
is checked, not remembered.

## Where things are

```
demo/blackbox-film/
├── studio/            index.html, css/studio.css, js/{app,frame,motionMath,blueprint}.js, spec.json, assets/
├── tools/             render_film.mjs, render_frames.mjs, scrub_evidence.py, test_*.mjs
├── incident/          app.py, mock_provider.py, proposal.json, repo/, run_demo.sh
├── evidence/          the scrubbed run: run.json, ls.txt, verify.txt, diagnosis.json, report.md, brief.md, logs
├── export/            the films and the .srt (see export/README.md for the git policy)
├── docs/              storyboard, narration script, claims ledger, evidence notes, studio guide
└── serve.mjs          static server for the studio and the downloads
```

## Honest status

- The film, the subtitles, the studio, the tour page and the 32 tests exist and run in this checkout.
- The studio is tested in jsdom (boot, navigation, validation, storage, keys, deep links, drawer exclusivity,
  the scrub bars, the speed select, the export-size PNG), **not** in a real browser:
  no browser automation is available in this environment. The canvas itself is tested by rendering frames in Node and
  asserting they are non-blank, identical across two runs, and different across two percents.
- The narration is generated speech, one clip per scene; it is in the export, mixed at each scene's offset and
  verified present (`mean_volume −20.3 dB`). If you replace the clips, change `audioMs` in `spec.json` — the tests will
  tell you if the scene is too short for the new audio.
- macOS playback of the exported H.264/AAC file is unmeasured here; the encode uses `yuv420p`, `high` profile and
  `+faststart`, which is the combination QuickTime and Safari expect, but that is a claim about a format, not a test
  result.
- Nothing is published anywhere. The film lives in this repository and on a local server.
