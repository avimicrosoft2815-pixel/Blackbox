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

## The tour page's claims

`start/index.html` argues for the product, so its sentences get the same treatment as the film's. Status vocabulary as
above; the numbers on the page are not repeated here — they are in `start/stats.json`, written by the command that ran
them, at the commit it names.

| # | Claim, as a reader takes it | Where | Backing | Status |
|---|---|---|---|---|
| 21 | One command puts a recorder in front of any OpenAI-compatible call, without changing the application | Tour hero, step 2 | `blackbox run --upstream NAME=URL[@/prefix]` points the client's base-URL variables at a loopback proxy; `incident/app.py` is an unmodified client and is what the recorded incident used | product |
| 22 | "a capsule you can verify" | Tour hero | `blackbox verify` on the recorded capsules: every recomputable part agrees, and the one it cannot recompute is printed as declared (`evidence/verify.txt`) | recorded |
| 23 | "a diagnosis that names what it cannot establish" | Tour hero | `evidence/diagnosis.json` → `cannot_know`, quoted in full on the showcase page | recorded |
| 24 | "a repair a person approves by token" | Tour hero, step 5 | `repair preview` prints the token and `apply` refuses without it; `tests/test_repair_workflow.py` exercises preview → apply → verify | product |
| 25 | Python 3.11 or newer, the standard library at run time | Step 1 | `pyproject.toml` `requires-python = ">=3.11"`; `tests/test_stdlib_only.py` forbids imports outside the standard library in `src/` | product |
| 26 | "the proxy can only reach the endpoints in its own table" | Proof card | `Routes.destinations()` is exactly that set, and `tests/test_no_egress.py` asserts it | product |
| 27 | "One corpus, every place a capsule can write, each asserted" | Proof card | `tests/test_redaction_every_sink.py`: the corpus and the sink list are in the file and the count is in `start/stats.json` | product |
| 28 | A tampered capsule is refused, not rendered | Proof card | `tests/test_capsule_attacks.py` | product |
| 29 | Markup cannot execute in the pages the viewer writes | Proof card | `tests/test_xss_corpus.py` | product |
| 30 | Verify does not count what it cannot recompute | Proof card, step 3 | `tests/test_verify_cli.py`, and the `----` line in `evidence/verify.txt` | product |
| 31 | The grid of numbers | Numbers section | `start/stats.json`, one card per fact, each naming the file or suite it came from | recorded |
| 32 | "eleven sound cues" | Tour hero caption, film | `tools/sfx.mjs` computes them from the blueprint; the encoder prints the count it mixed, and `npm test` fails if a cue has no sound file or lands outside the film | product |
| 33 | The page has its own sound design, off until asked for | Sound pill | `start/start.js` synthesises it; nothing plays before a click, so no autoplay policy is worked around | product |


## Not said anywhere

Checked mechanically by `tools/test_film.mjs` over the film's own files, and by the repository's
`tests/test_forbidden_words.py` over the whole tree:

| Not said | Why |
|---|---|
| Any percentage, factor or time saved | No measurement exists; a film is not a benchmark |
| The repository's own banned absolutes (marketing words about safety, certainty and precedence) | Banned by `tests/test_forbidden_words.py`; also untrue of a pre-alpha. That test is the authority and it enumerates them — repeating them here would fail it, which is why this row describes the list instead |
| A competitor's name or a comparison | Not needed to explain the product, and unverifiable here |
| That any of this makes a system safe, or that anything is 100 % safe | The tour page says the opposite in its own heading: a recorder records, and the limits are listed beside the tests |
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
