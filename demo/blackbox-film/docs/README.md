# Film documents

| File | What it is for |
|---|---|
| `storyboard.md` | Scene by scene: mode, timing, copy, narration, what moves and why. Start here. |
| `narration-script.md` | The spoken script, the measured sentence boundaries, and what voice asset the film needs. |
| `claims-ledger.md` | Every sentence that could be read as a claim, and what backs it. |
| `evidence.md` | The incident the film animates: how to reproduce it, what was recorded, what the record cannot say. |
| `studio.md` | How to drive the Director Studio: controls, keys, deep links, the editor's rules, what is tested. |

The film itself is in `../export/`, the blueprint it renders from is `../studio/spec.json`, and the run behind it is in
`../evidence/`.

## The rule these documents follow

A number that appears on screen appears here with its source. A scene that illustrates rather than records says so on the
frame. A limit the tool reports — an uncheckable vantage, hashed content, an unknown tool result — is quoted, not
smoothed away. Where something has not been tested (a real browser, macOS playback), it says that instead of implying it
works.

## Poster frame

`poster.png` is scene 6 at 96 %, rendered by `tools/render_frames.mjs`:

```bash
cd demo/blackbox-film
node tools/render_frames.mjs --scene 6 --pct 96 --out docs/
```

It shows the moment the film is arguing for: a diff a person reviewed, a token that gated it, a check that passed, and
the tool's own caveat still visible underneath.
