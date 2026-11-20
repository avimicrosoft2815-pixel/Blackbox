# Director Studio

`npm run studio` → <http://localhost:8181/studio/> (or `/` on the same server).

The studio is the film's review surface. It renders the stage with `studio/js/frame.js` — the same module the encoder
imports — so what is on the stage is what lands in the MP4, at whatever size the window gives it.

## What is on screen

```
┌───────────────┬──────────────────────────────────────────────┬───────────────────────┐
│ scene tabs    │  stage (canvas, 16:9)                        │ Editor / Inspector /  │
│ 01 … 07       │  caption line under it                       │ Transcript            │
│               │  transport: play, prev, next, time, speed,   │                       │
│               │  sound, captions, reduced motion             │                       │
│               │  segmented timeline with a playhead          │                       │
└───────────────┴──────────────────────────────────────────────┴───────────────────────┘
```

- **Scene tabs** — seven, in film order, each showing its number and mode.
- **Segmented timeline** — one segment per scene, width proportional to its real duration. Click anywhere in it to jump;
  drag to scrub. The gold fill shows progress inside the active segment.
- **Transport** — play/pause, previous/next scene, timecode (`mm:ss.cc`), speed (0.5×, 1×, 1.5×, 2× — four values, not a
  three-state cycle), sound, captions, and reduced motion.
- **Transcript** — every scene's narration and every caption with its absolute timecode; click a heading to go there.
- **Inspector** — what the current frame is made of: mode, layout, duration, share of the film, narration file, clip
  length, tail after speech, caption count, accent, disclosure, and the headline size the layout actually chose. Below it,
  five checks re-measured on the stage's own canvas.
- **Editor** — eyebrow, both headline lines, accent line, body, duration. Validated on submit; saved to this browser only.

## Keys

| Key | Does |
|---|---|
| `Space` | play / pause |
| `←` `→` | previous / next scene |
| `Shift` + `←` `→` | nudge one percent |
| `A` `D` | move the before/after divider (scene 3) |
| `C` | captions on / off |
| `M` | sound on / off |
| `E` | editor |
| `I` | inspector |
| `R` | revert edits to the film's own copy |

## Deep links

`/studio/#scene=4&pct=62` opens on scene 4, 62 % in. The hash is rewritten as the film plays (throttled), so any frame can
be copied out of the address bar and sent to someone else.

## The editor's rules

Three rules, all of them visible in the interface rather than implied:

1. **The file is the source of truth.** `studio/spec.json` is what the encoder reads. The editor stores its draft in
   `localStorage` under `blackbox.studio.blueprint.v1` and says *saved in this browser · re-render the film to export it*.
   It never claims to have changed the export.
2. **A stored draft is untrusted.** It is parsed inside a `try`, shape-checked field by field, and discarded if a single
   field is the wrong type or out of range. A corrupt draft produces a banner — *the saved draft was not valid JSON, so
   the film's own copy was loaded* — and the studio still opens. The test suite boots the studio with deliberately broken
   JSON to prove it.
3. **Edits are validated against the film's constraints.** A headline has to fit its column at the smallest size the
   layout will drop to (44 px), measured on the stage's canvas; a duration has to be long enough to hold the scene's
   narration plus its lead-in; body copy is capped. A rejected edit marks the field, focuses it, and stores nothing.

`R` (or *Revert to film*) clears the draft and restores `spec.json`'s copy.

## Reduced motion

Prefers the system setting, and can be toggled. In the stage, reduced motion removes the spring and the drift rather than
the content: cards are simply present, the glow stops moving. The exported film is always full motion — a video has no
`prefers-reduced-motion` — which is stated here so nobody expects the export to change.

## Audio

One `<audio>` element, one clip per scene, loaded when the scene starts and seeked to `elapsed − sceneStart − 500 ms`.
The clock is `performance.now()`, not the audio: if a browser blocks autoplay, the film still runs, the captions still
land on time, and the transport says *sound blocked* (or *no audio* if the element cannot be constructed at all). Drift
over 260 ms triggers a re-seek.

## Export

The bottom bar shows the commands, and links to the film, the subtitles and the storyboard:

```bash
npm run film        # 1080p + .srt
npm run film:4k     # 3840×2160
```

Those links point at `../export/…`, served by `serve.mjs`. `/film.mp4` returns the 1080p file as an attachment, which is
the link to hand to someone who just wants the video.

## What is tested, and what is not

Tested, in jsdom, against the real `index.html` and the real `app.js` (11 checks — `npm test`):

- boot: seven tabs, seven segments, seven transcript entries, the blueprint fetched rather than hardcoded;
- navigation: next scene moves the caption, the active tab, the scrub label and the deep link;
- editor: a valid edit is applied and stored; a headline that cannot fit is refused with the field marked; a duration
  shorter than the narration is refused with the measured end time;
- storage: a corrupted draft is discarded with a message, and the studio still opens; a stored draft is applied on load;
  revert clears it and restores the film's copy;
- inspector: facts and checks render, and the shipped blueprint passes all of them;
- keyboard: space, captions, inspector, arrow navigation;
- deep link: opens on the named scene;
- reduced motion: the toggle reports its state.

Not tested: a real browser. There is no browser automation in this environment, so layout, fonts and pointer dragging are
verified by eye and by rendering the same frames in Node — where two renders of a frame are asserted byte-identical and a
non-blank frame is asserted for the end card. Treat the studio as *working in this checkout* and *not yet exercised in
Safari, Firefox or a real trackpad*.
