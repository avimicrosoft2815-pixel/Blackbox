# The exported film

| File | Size | What it is |
|---|---|---|
| `blackbox-film-1080p.mp4` | 7.9 MB | 1920×1080, H.264 High, `yuv420p`, 30 fps, AAC 48 kHz mono, `+faststart`, 55.00 s |
| `blackbox-film-4k.mp4` | 15.1 MB | 3840×2160, same encode settings at CRF 20 |
| `blackbox-film.srt` | 1.5 KB | Subtitles, generated from the captions burned into the picture |

Both files are the output of `tools/render_film.mjs`, which renders with `studio/js/frame.js` — the studio's own module.
The narration is mixed in at each scene's measured offset; the captions are drawn in the picture *and* written to the
`.srt`, from one list.

Under the narration there are **10 effect cues** — a cut on each interior scene boundary, two pulses through the timeline
scene, a stamp where the diagnosis prints, a rise under the opening and another under the end card. They are not typed
into the renderer: `tools/sfx.mjs` derives them from `studio/spec.json`, so a scene that moves takes its sound with it,
and `npm test` fails if a cue lands on the frame the previous one is still decaying on. The four sounds themselves are
synthesised by `tools/make_sfx.mjs` from fixed seeds — reproducible WAVs in `studio/sfx/`, no downloaded assets.

## Downloading it

With `npm run studio` running:

- <http://localhost:8181/film.mp4> — the 1080p film, sent as an attachment (`Blackbox-film-1080p.mp4`)
- <http://localhost:8181/film-4k.mp4> — the 4K film, same way
- <http://localhost:8181/export/blackbox-film-1080p.mp4> — the same bytes, played inline, with byte-range support so
  seeking works

Behind the sandbox's preview proxy those paths are relative to the preview host, so the link is
`https://<preview-host>/film.mp4`.

## Re-rendering

```bash
cd demo/blackbox-film
npm run film        # 1080p, about 95 s on four cores
npm run film:4k     # 3840×2160, about eleven minutes
```

`--crf` trades size for quality (default 18 at 1080p, 20 at 4K); `--limit N` renders a probe of N frames instead of the
film; `--no-audio` produces a silent file.

## What is in Git, and what is not

Both films and the `.srt` are committed: 7.9 MB, 15.1 MB and 1.5 KB. That is deliberate rather than an oversight — they are
the deliverable, they are small (line art on black compresses well), and a copy that only exists in a working tree is a
copy that can be lost. If the repository ever decides otherwise, `tools/render_film.mjs` rebuilds either file from source
in one command.

Not committed: `frames/` (PNGs are streamed to ffmpeg, never written) and `node_modules/`.

## Checks run on the exports

- Duration is exactly 55.00 s, matching the blueprint's sum.
- Both streams are present: H.264 High `yuv420p` and AAC LC 48 kHz.
- The audio is not silence: `volumedetect` on the 1080p file reports mean −20.3 dB, max −0.6 dB, and the renderer prints
  `7 narration clip(s) and 10 effect cue(s)` when it mixes each master.
- The picture is not blank: frames are asserted non-blank in `npm test`, and the encode was spot-checked frame by frame
  during production.
- macOS/Safari playback is **unmeasured** — no macOS machine was available. The settings are the ones that format wants,
  but that is a statement about the container, not a test result.
