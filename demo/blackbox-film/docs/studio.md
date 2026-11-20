# Director Studio

`npm run studio` → <http://localhost:8181/studio/> (or `/` on the same server).

The studio is the film's review surface. It renders the stage with `studio/js/frame.js` — the same module the encoder
imports — so what is on the stage is what lands in the MP4, at whatever size the window gives it.

It is laid out on the reference integration's shell: one centred column, the transport as a row of pills in the header
with the scene tabs beside them, drawers for the copy, the blueprint and the script directly under that, the stage where
the eye already is, and the seven scrub bars under the film with the live caption and the words being spoken beneath them.
The dressing is BlackBox's own — near-black ground, graphite surfaces, gold as the only pointer — and the stage is still a
canvas, not a second implementation, because that is the property the whole demo rests on.

## What is on screen

One centred column, drawn the way the reference integration draws its showcase — identity and transport in the header,
one drawer at a time directly under it, the stage where the eye already is, and the seven bars in the footer:

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ BLACKBOX · DIRECTOR STUDIO · pre-alpha · 00:00 – 00:09 · 55.0 s                       │
│ ▶ Play  1×  sound on  captions on  full motion │ Edit  Blueprint  Script │ PNG keys  │
│ 01 · The problem  02 · …  03 · …  04 · …  05 · …  06 · …  07 · …                     │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ drawer (Edit / Blueprint / Script), open one at a time, closed by default             │
├──────────────────────────────────────────────────────────────────────────────────────┤
│                            stage: canvas, 16:9, PAUSED                               │
│                     ◀ scene   scene ▶   download film   .srt   storyboard            │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ ▁▂▃▄▅▆▇   seven bars, one per scene, gold fill on the one you are in                  │
│ LIVE CAPTION // …                          scene 03 · 42% · SFX …                    │
│ the words being spoken, verbatim                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **Scene tabs** — seven, in film order, each showing its number and title; the active one is marked for the tab role.
- **Seven bars** — one per scene, equal width, labelled with the scene's number and carrying the gold fill for progress
  inside the active scene. Click a bar to jump into that scene; drag inside it to scrub that scene and nothing else. The
  gaps between bars are the cuts, which is the one thing a single proportional track cannot show.
- **Transport** — play/pause, speed (0.5×, 1×, 1.5×, 2× — a real four-value select, not a three-state cycle), sound,
  captions, and reduced motion. Timecode (`mm:ss.cc`) sits on the stage, not in a row of numbers.
- **Draft / spec notices** — a banner when a stored draft was discarded or applied, and a header chip counting any
  structural or per-scene check that fails right now. The shipped blueprint shows none.
- **Script** — every scene's narration and every caption with its absolute timecode; click a heading to go there.
- **Blueprint** — what the current frame is made of: mode, layout, duration, share of the film, narration file, clip
  length, tail after speech, caption count, accent, disclosure, and the headline size the layout actually chose. Below it,
  four checks re-measured on the stage's own canvas, and the active scene's JSON, re-serialised at most twice a second.
- **Edit** — eyebrow, both headline lines, accent line, body, duration. Validated on submit; saved to this browser only.
- **frame PNG** — renders the frame you are looking at, at export size (1920 × 1080), through the same renderer, and hands
  it to the browser as a file. It is the current frame, not the poster.

## Keys

| Key | Does |
|---|---|
| `Space` | play / pause |
| `←` `→` | previous / next scene |
| `Shift` + `←` `→` | nudge one percent |
| `A` `D` | move the before/after divider (scene 3) |
| `C` | captions on / off |
| `M` | sound on / off |
| `E` | edit the copy |
| `I` | blueprint and checks |
| `T` | the script |
| `R` | revert edits to the film's own copy |
| `Esc` | close the open drawer |

The same list is in the **keys** pill, next to the shortcut that opens it, so it cannot drift from the handler.

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

Prefers the system setting, and can be toggled. The only ambient motion in the film is the background glow's slow drift;
reduced motion parks it (`options.motionReduced` in `studio/js/frame.js`) and leaves every other frame identical. The
exported film is always full motion — a video has no `prefers-reduced-motion` — and the button's tooltip says so, because a
toggle that quietly did nothing would be the same claim in a quieter voice.

## Audio

One `<audio>` element, one clip per scene, loaded when the scene starts and seeked to `elapsed − sceneStart − 500 ms`.
The clock is the animation frame's own timestamp — one clock, one origin — not the audio: if a browser blocks autoplay, the
film still runs, the captions still land on time, and the transport says *sound blocked* (or *no audio* if the element
cannot be constructed at all). Drift over 260 ms triggers a re-seek. Mixing the frame timestamp with `performance.now()`
froze the clock outright; the test that plays at 1× and 2× and compares the two readings is what found it.

## Export

The row under the stage links to the film, the subtitles, the storyboard, the tour (`/tour`, the page that argues for the
product with numbers it measured) and the page that lists what is different. The header carries the pills that render the
two masters:

```bash
npm run film        # 1080p + .srt
npm run film:4k     # 3840×2160
```

Those links point at `../export/…`, served by `serve.mjs`. `/film.mp4` returns the 1080p file as an attachment, which is
the link to hand to someone who just wants the video.

## What is tested, and what is not

Tested, in jsdom, against the real `index.html` and the real `app.js` (14 checks — `npm test`):

- boot: seven tabs, seven segments, seven transcript entries, the blueprint fetched rather than hardcoded;
- navigation: next scene moves the caption, the active tab, the scrub label and the deep link;
- editor: a valid edit is applied and stored; a headline that cannot fit is refused with the field marked; a duration
  shorter than the narration is refused with the measured end time;
- storage: a corrupted draft is discarded with a message, and the studio still opens; a stored draft is applied on load;
  revert clears it and restores the film's copy;
- inspector: facts and checks render, and the shipped blueprint passes all of them;
- keyboard: space, captions, inspector, arrow navigation;
- deep link: opens on the named scene;
- reduced motion: the toggle reports its state;
- drawers: one at a time, a second click closes it, `Esc` leaves none open;
- bars and speed: half of the third bar reads half of scene three, at 2× the clock outruns 1×;
- export: `frame PNG` renders at 1920 × 1080 and asks for `image/png`; with no clipboard path the copy pill says so.

Not tested: a real browser. There is no browser automation in this environment, so layout, fonts and pointer dragging are
verified by eye and by rendering the same frames in Node — where two renders of a frame are asserted byte-identical and a
non-blank frame is asserted for the end card. Treat the studio as *working in this checkout* and *not yet exercised in
Safari, Firefox or a real trackpad*.
