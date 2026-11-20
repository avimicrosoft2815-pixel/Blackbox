# Claims ledger

Every sentence in the film that could be read as a claim, what backs it, and where it appears. If a line is not in this
table, it is not a claim — it is a label, a timecode or the name of a thing.

Status vocabulary: **recorded** (an output of a real tool, in `evidence/`), **product** (behaviour of this repository's
code, visible in the source), **illustrative** (a drawn example, labelled as such on the frame), **banned** (not said
anywhere, on purpose).

| # | Claim, as the audience hears or reads it | Where | Backing | Status |
|---|---|---|---|---|
| 1 | "An AI application can fail between a request, a response and the next action." | Scene 1 body | The recorded run shows exactly this shape: request, two chunks, no terminal event | recorded |
| 2 | "…finding the cause takes time." | Scene 1 narration | No number is given. It is a description of the problem, not a measurement | product |
| 3 | BlackBox records selected activity for supported integrations | Scene 2 narration | `src/blackbox/proxy/server.py` is the capture point; coverage depends on instrumentation | product |
| 4 | "A program with no instrumentation gets no node, and the capsule says so." | Scene 2 graph caption | Same as 3: the diagram draws the unrecorded provider as dashed and grey | product |
| 5 | Records stay on this machine; no account | Scene 2 body | Loopback proxy, local tape, local capsules; `view` writes one offline HTML page | product |
| 6 | 17 frames, two capsules, exit code 3, 2 × 92 B, 776 B | Scenes 3–5 | `evidence/ls.txt`, `evidence/run.json`, `evidence/report.md` | recorded |
| 7 | Two findings, both observed, both high confidence | Scene 5 | `evidence/diagnosis.json` (`findings[].confidence`) | recorded |
| 8 | Three other explanations that fit the same frames | Scene 5 card 2 | `evidence/report.md` names two alternatives per finding; the card counts them | recorded |
| 9 | Content is stored as hashes, so wording is not claimed | Scene 5 card 3 | `evidence/report.md`: *content is stored as keyed or unkeyed hashes* | recorded |
| 10 | "Verify: every checkable part agrees" | Scene 3 | `evidence/verify.txt`: 2 capsules verified, 1 check declared and not checkable | recorded |
| 11 | The tool result is unknown, not guessed | Scene 3, scene 4 | The run ends before the tool call; the report says so explicitly | recorded |
| 12 | Nothing applies without the approval token | Scene 6 | `repair apply` refuses without a token and re-checks hashes at write time (source) | product |
| 13 | PASS, exit 0, 0.36 s, 2 passed | Scene 6 | The verify step run during production on the synthetic repo | recorded |
| 14 | "Unverified until you run `repair verify --test`" | Scene 6 caveat line | The tool's own output after applying | recorded |
| 15 | Local-first, no account | Scene 7 pillar | Same as 5 | product |
| 16 | "Evidence with its limits shown" | Scene 7 pillar | Every scene that could overreach ends in a stated limit (dashed `UNKNOWN`, `CANNOT TELL`) | product |
| 17 | "Coverage depends on the integration and capture settings." | Closing note | Says *depends*, names no numbers | product |
| 18 | "Product in development." / pre-alpha | Closing note, studio footer | `state` in the project; no version, date or launch hint is given | product |
| 19 | Illustrative scenario | Scenes 1, 3, 6 (on frame) | These scenes draw an example; the label is part of the picture and cannot be cropped away by a screenshot of the stage | illustrative |
| 20 | Representative repair, run on the synthetic demo repo | Scene 6 (on frame) | The repair in `incident/` really was applied and verified on `incident/repo/` | illustrative |

## Not said anywhere

Checked mechanically by `tools/test_film.mjs` over the film's own files, and by the repository's
`tests/test_forbidden_words.py` over the whole tree:

| Not said | Why |
|---|---|
| Any percentage, factor or time saved | No measurement exists; a film is not a benchmark |
| The repository's own banned absolutes (marketing words about safety, certainty and precedence) | Banned by `tests/test_forbidden_words.py`; also untrue of a pre-alpha. That test is the authority and it enumerates them — repeating them here would fail it, which is why this row describes the list instead |
| A competitor's name or a comparison | Not needed to explain the product, and unverifiable here |
| A launch date, waitlist, price, availability | None is decided |
| A confidence percentage | The tool reports `observed` + `high`; inventing a number would be worse than the word |
| Line numbers of a real bug | The displayed lines are a representative repair, labelled as such |
| Customer content, real incident, API key, hostname, username | None appears; `tools/scrub_evidence.py` enforces it for paths and pids |

## How the ledger is kept true

- `npm test` fails if a scene that illustrates lacks a label, if a caption leaves its scene, if a declared scene duration
  cannot hold its narration, or if the blueprint's frame count and capsule ids stop matching `evidence/`.
- The prose in these documents is scanned for the repository's banned words along with the rest of the tree.
- The narration script is in `docs/narration-script.md` and the audio is in the repository, so the spoken words can be
  checked against this table without watching the film.
