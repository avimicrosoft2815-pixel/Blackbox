# Onboarding test — three tasks for a person who has never seen this repository

This page exists so that the human-only item in `docs/STATUS.md` ("3-person stranger test") is **one command plus
three tasks**, not a plan. Nothing here has been run by anyone outside this session, and the STATUS row says so.

## Setup (one command, from the repository root)

```
python3 -m venv .venv && .venv/bin/pip install -e . pytest && .venv/bin/blackbox --version
```

Expected: `blackbox 0.1.0.dev0 (format v2)`. Do not read the rest of the repository first — that is the point of
the test.

## Task 1 — run a program under the tape and kill it

```
.venv/bin/blackbox run -- sh -c 'echo working; kill -9 $$'
```

Then answer, in your own words, without looking at any other file:

1. What was written down, and where is it?
2. The program was killed with signal 9. What did the tool do about the run that was in flight?
3. Compare what the tool printed with what actually happened. Did it claim anything it cannot know?

## Task 2 — read a run back

```
ls capsules/ && .venv/bin/blackbox verify capsules/*.bbx
.venv/bin/blackbox view capsules/
.venv/bin/blackbox diagnose capsules/ --brief repair-brief.md
```

Answer: does the tool tell you *which* step was interrupted, or only that something was? Is the answer specific
enough to act on?

Then read the brief, and check it against the standard this build holds itself to:

1. Does it claim anything the capsule cannot support — a file and line, a cause with no alternative, "the tool did not
   run"? It should not: it prints `observed` / `likely` / `cannot-tell`, and it says which check would settle a `likely`.
2. Does it show you the exact command it cannot reproduce? It should say the command line was **not recorded** rather
   than guessing one.
3. Does anything in it look like a credential? It should not: every quoted string is scrubbed with the same pattern
   table the writer redacts with.
4. Did it change anything? It should not have. `git status` in your repository is the check, and the brief is a
   document — applying it is your edit, made with your tests.

## Task 3 — do it again and break it on purpose

Run Task 1 twice more, once with `--stall-ms 500` on a program that sleeps, and once after `chmod 000` on the tape
directory. Answer:

1. Did it ever fail *open* (your program kept running, the recorder said nothing useful)?
2. Was any message about the failure wrong, vague, or addressed to the wrong audience?

## What to write down

One paragraph per task: what you expected, what happened, and the one sentence in the output you would change.
Send it with the commit you ran (`git rev-parse --short HEAD`). A task is failed if the tool **claimed** durability
it cannot prove, or if a failure left you unable to say what state the tape was in.
