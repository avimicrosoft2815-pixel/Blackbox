# What is usually not there

A demo film normally asks you to believe it. The frames are an animation someone drew, the "incident" is a storyboard,
the diagnosis is a script, and the only thing you can check afterwards is whether the video played.

This demo is built the other way round: the failure was recorded by this repository's own CLI, the film is rendered from
that record by the same module the studio runs in your browser, and every property it claims can be re-measured in one
command that fails when it is wrong.

```bash
cd demo/blackbox-film
npm run showcase        # records the incident again and re-measures everything on this page
```

The product is **in development**, the incident is **synthetic**, and nothing below is a performance claim. What follows
is the list of things this demo has that a demo normally does not, where each one lives, and what it costs.

## Ten things you would not normally see in a demo

**1. The pictures are drawn from a recording, not from a screen.** No screen capture and no headless browser is involved:
`tools/render_film.mjs` paints 55 seconds frame by frame through `studio/js/frame.js`, and scenes 4 and 5 re-draw the
sequence numbers, node ids, byte counts and the `INTERRUPTED_UPSTREAM` error that `evidence/report.md` prints. If the
film and the evidence disagreed, you could diff them.
*Where:* `studio/js/frame.js`, `tools/render_film.mjs`, `evidence/report.md` · *Cost:* the film is 93 s of rendering, and
every copy change means re-rendering both masters.

**2. The pictures say they are pictures.** Three of the seven scenes carry an on-frame label — *Illustrative scenario*,
*Representative repair, run on the synthetic demo repo* — and the studio's Blueprint drawer re-checks that the disclosure
is still there after an edit. The other four scenes are diagrams of recorded frames or of the product's own behaviour,
which is why they carry none.
*Where:* `label` in `studio/spec.json`, the `disclosure` check in `studio/js/blueprint.js` · *Cost:* a frame that has to
qualify itself is a less confident-looking frame.

**3. The diagnosis publishes what it cannot know.** Every run ends with a list of limits instead of a confident story.
This is the line this demo's capsule produced:

> content is stored as keyed or unkeyed hashes: prompt and response text cannot be recovered from this capsule, and no
> claim here is about their wording

*Where:* `cannot_know` in `evidence/diagnosis.json` · *Cost:* a demo that says "we cannot tell" is a harder sell than one
that does not.

**4. Every finding carries its own rivals and the next check.** Each finding in the diagnosis arrives with the
alternatives it has not ruled out and the checks that would separate them. This is one of the two findings from the
recorded run, verbatim:

> **the step 'POST /v1/chat/completions' ended with an error** — basis: seq 13 NODE_CLOSE … `INTERRUPTED_UPSTREAM: client
> received 776 B, proxy received 776 B from the upstream; the upstream closed before a terminal event`
>
> *alternatives:* the application raised and the node's own close recorded it: the error text is the application's ·
> a recorder error closed the node early; the exception frame, when there is one, distinguishes them
>
> *checks:* read the application's handler for this step: the tape says the step failed, not why the code chose to ·
> if a model call inside this node failed first, that finding is the cause and this one is its shape

**5. Verification refuses to count what it cannot recompute.** One check per capsule cannot be re-derived from a capsule
at all, so `blackbox verify` prints it as a statement and keeps it out of the pass count:

```
data/capsules/c_75c1d97257dac250.bbx
  ok   integrity
  ok   chain_root
  ok   derived_parts
  ok   capture_seam
  ---- capture_vantage  a capsule carries no meta page: the vantage is declared and cannot be recomputed here, so it is printed as unverified rather than counted as a pass
  ok   links_summary
data/capsules/c_b3820c3ee321dd9d.bbx
  ok   integrity
  ok   chain_root
  ok   derived_parts
  ok   capture_seam
  ---- capture_vantage  a capsule carries no meta page: the vantage is declared and cannot be recomputed here, so it is printed as unverified rather than counted as a pass
  ok   links_summary
blackbox verify: 2 capsule(s) verified; 1 check(s) declared, not checkable from a capsule
```

*Where:* `evidence/verify.txt` · *Cost:* the summary line reads oddly until someone reads the `----` line above it — which
is the point.

**6. One module draws both the studio and the export.** The browser stage and the encoder are the same file. The showcase
renders the poster frame twice and compares both hashes with the committed `docs/poster.png`; all three are the same
file, so "what you scrub is what you export" is a measurement rather than a promise.
*Where:* `studio/js/frame.js` imported by `studio/js/app.js` **and** `tools/render_film.mjs` · *Cost:* the studio cannot
have a richer stage than the film, ever.

**7. The blueprint refuses copy that will not fit the frame it lands in.** The editor measures a headline on the stage's
own canvas and refuses it below the smallest size the layout drops to; it refuses a duration shorter than that scene's
narration plus its lead-in; it stores nothing until both hold. A rejected edit marks the field and focuses it.
*Where:* `studio/js/blueprint.js`, two of the studio's tests · *Cost:* you cannot type any headline you like.

**8. The narration is the timing source, not a decoration.** Seven recorded clips; each scene's length is its clip plus a
500 ms lead-in and a tail, and `audioMs` in the blueprint comes from the file. The clock is the animation frame, and the
audio is nudged to it, so a browser that blocks autoplay still gets the film, the captions and the timings — and the
transport says *sound blocked* rather than pretending.
*Where:* `studio/spec.json` (`durationMs`, `audioMs`), `studio/js/app.js` · *Cost:* changing a word of narration means
re-recording and re-timing, not just re-rendering.

**9. Any frame is a link you can send.** `/studio/#scene=4&pct=62` opens on that frame, because the stage is the renderer
and the blueprint is the truth — the same hash the studio writes back into the address bar as the film plays.
*Where:* `readDeepLink` / `writeDeepLink` in `studio/js/app.js` · *Cost:* a deep link into a product that is still in
development is a link to something that may move.

**10. Nothing needs the network, and the demo can prove it.** The "provider" is a loopback mock that sends three SSE
deltas and closes without a terminal event; the studio's pages, blueprint and renderer contain zero URLs; the incident run
is reproducible offline, in about six seconds.
*Where:* `incident/mock_provider.py`, `incident/run_demo.sh`, the URL count in the showcase output · *Cost:* the demo
cannot show a real provider's quirks, and does not.

## Progress so far

| Built | Where | State |
|---|---|---|
| One incident, recorded by this repository's CLI against a synthetic app | `evidence/`, `incident/` | recorded, committed, reproduced on demand — app exit code 3, two capsules, one uncheckable vantage printed as such |
| A 55-second film, frame by frame, with a real recorded narration | `export/`, `studio/`, `tools/render_film.mjs` | 1080p 7.8 MB and 4K 15.0 MB, h264 high / yuv420p / 30 fps, AAC 48 kHz mono, 14 subtitle cues, poster in `docs/` |
| A studio that is the renderer, not a preview of it | `studio/` | seven scrub bars, one drawer at a time, blueprint editor with validation, export-size PNG, deep links |
| Tests for both halves | `tools/test_film.mjs`, `tools/test_studio.mjs` | 25 checks, all passing |
| CI | `.github/workflows/ci.yml` | every job green on this branch — `docs`, `go-reader`, and the 3 Python versions on ubuntu, macOS and Windows |
| Documents that carry their limits | `docs/` | storyboard, narration script, claims ledger, evidence, studio guide, this page |
| A reviewable change | PR #20 | open, not merged, not tagged |

**Measured here, and not measured.** Measured: the run, the capsules, the frames, the two masters, the subtitles, the 25
tests, the poster's identity. Not measured: the studio in a real browser (there is none in this environment, so the stage
is tested in jsdom and the layout by eye), playback of the exported file on macOS, and a trackpad drag. Those stay on the
"not measured" list until something here measures them.

## Live usage — one real run, pasted verbatim

This is the output of `npm run showcase` on this checkout, at commit `e40e593`, with nothing edited. Two of the numbers
change every time (the run id and the id-derived capsule list), because the incident is recorded again rather than
replayed from a fixture.

```
BLACKBOX — the live showcase (a real run, no fixtures)
2026-11-20 · linux/x64 · v22.22.3
  commit                     e40e593

1  THE INCIDENT — recorded now
------------------------------
  run                        r_78aff7de71b656b5
  capsules                   2 (launcher + sdk)
  app exit code              3
  verify                     2 capsule(s) · 10 check(s) ok · 2 declared, not checkable
  diagnosis                  2 finding(s) · 1 cannot-know statement(s)

2  THE FILM — read from the files in export/
--------------------------------------------
  blueprint                  7 scenes · 55.0 s · 3 on-frame label(s)
  narration                  7 recorded clip(s)
  subtitles                  14 cue(s)
  1080p                      7.8 MB · 55.000 s · h264 high · yuv420p · 1920x1080 · 30 fps · aac LC · 48 kHz mono
  4k                         15.0 MB · 55.000 s · h264 high · yuv420p · 3840x2160 · 30 fps · aac LC · 48 kHz mono

3  ONE RENDERER, TWO HOSTS
--------------------------
  the stage imports          studio/js/frame.js
  the encoder imports        studio/js/frame.js
  the module draws           13 scene drawing(s), one canvas
  frame 6 at 96 %, twice     same sha256 (35af0af43915dd5e…)
  and the committed poster   the same file

4  THE COPY AND THE PICTURES ARE CHECKED
----------------------------------------
  npm test (node --test)     25 pass · 0 fail
  the forbidden-word rule    pass (tree and commit messages)
  network references         0 in the blueprint, the stage and the renderer

5  WHAT IS USUALLY NOT THERE — each line is the measurement above
-----------------------------------------------------------------
   1. every frame of the failure is drawn, not screen-captured: 14 cues and 7 scenes come out of the same renderer, and the timeline scene re-draws the seq numbers in evidence/report.md
   2. the pictures say they are pictures: 3 of 7 scenes carry an on-frame label, and the rest are diagrams of recorded frames rather than illustrations of a story
   3. the diagnosis names what it cannot know: 1 statement(s), each one a limit rather than a claim
   4. verification refuses to count what it cannot recompute: 2 check(s) printed as declared, not as a pass
   5. one module draws both the studio and the export: frame 6 at 96 % has the same sha256 in two renders and in docs/poster.png
   6. the blueprint validates copy against the frame it will land in: headline width is measured on the studio's own canvas, and a duration shorter than its narration is refused (both in the 25 tests above)
   7. the narration is the clock's source: 7 clips, and each scene's length is its clip plus a lead-in and a tail
   8. the studio is deep-linkable to the frame: /studio/#scene=4&pct=62 opens exactly there, because the stage is the renderer, not a picture of it
   9. the exit code is real: the app exited 3 and the record says so rather than showing a clean run
  10. nothing here needs the network: 0 URL(s) in the studio's pages, the incident's provider is a loopback mock

WHAT THIS DOES NOT PROVE
------------------------
  · the studio in a real browser: there is none in this environment, so the stage is tested in jsdom and the
    layout by eye; Safari, Firefox and a real trackpad are unmeasured;
  · playback of the exported H.264/AAC file on macOS: the encode is yuv420p/high/+faststart, which is what
    QuickTime expects, and that is a claim about a format, not a test;
  · that the recording survives a machine losing power: no such fault was injected for this demo.

  all measurements above matched the document
  11.4 s, including the incident run
```

## A sixty-second walkthrough

Open `npm run studio` → <http://localhost:8181/studio/> and, in this order:

1. **Press Space.** The clock on the stage runs, the caption under it changes per line, the gold fill moves inside the
   first bar and nothing else does — because one bar is one scene, and the gaps between bars are the cuts.
2. **Drag inside the fourth bar.** The timeline scene scrubs only within its own 7.4 s; `#scene=4&pct=…` appears in the
   address bar as you go, so any frame you land on is a link you can send.
3. **Press `I`.** The Blueprint drawer lists what the frame is made of — mode, duration, share of the film, the narration
   clip, the headline size the layout actually chose — the four checks re-measured on the stage's canvas, and the scene's
   own JSON.
4. **Press `E`, then type a headline that cannot fit** (hold a key for a second). Apply: the field is marked, focused and
   nothing is stored. Type one that fits and apply: it is kept in this browser only, and the drawer says *re-render the
   film to export it* — because the file on disk did not change.
5. **Press `frame PNG`.** The frame you are looking at, rendered at 1920 × 1080 by the same module that made the film.
6. **Press `R`.** The film's own copy comes back, the draft is cleared.

The studio's own tests drive steps 1–6 in jsdom (`tools/test_studio.mjs`, 14 of the 25 checks); the pointer drags in steps
1–2 are the part a real browser would have to confirm, and none was available here.

## Run it yourself

```bash
cd demo/blackbox-film
npm install                                        # @napi-rs/canvas, jsdom
npm run showcase                                   # the block above, re-measured
npm run studio                                     # http://localhost:8181/studio/
npm run film                                       # 1080p + .srt, about 93 s
npm run film:4k                                    # 3840x2160, about 5.5 minutes
```

The incident alone, without the film or the studio:

```bash
BLACKBOX=../../.venv/bin/blackbox PYTHON=../../.venv/bin/python bash incident/run_demo.sh /tmp/bbdemo
python3 tools/scrub_evidence.py /tmp/bbdemo evidence/
```

## What this does not prove

- **The studio in a real browser.** There is no browser in the environment this was built in, so the stage is tested in
  jsdom and the layout by eye; Safari, Firefox and a real trackpad are unmeasured.
- **macOS playback.** The export is yuv420p / high profile / `+faststart`, which is the combination QuickTime expects —
  a claim about a format, not a test of one.
- **Durability against a machine losing power.** No such fault was injected for this demo; the recorder's own documents,
  not this page, are where that ground is argued.
