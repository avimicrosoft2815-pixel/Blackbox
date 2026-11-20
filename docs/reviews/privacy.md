# Review pack — privacy (this is not a gate file, and carries no verdict) — unsigned builder evidence

The privacy question has three parts, and they are checked in three different ways: **what is recorded at all**, **where
it can go** (nowhere), and **who on this machine can read it**. A row that only checks one of the three would let a
product claim the other two.

## 1. What is recorded — and what is not

| Row | Where it lives | The command | What this machine saw on 2026-11-20 |
|---|---|---|---|
| The product states what it captures, in the window, before the first run | `src/blackbox/ui/onboarding.py`, `src/blackbox/ui/app.py` (`onboarding_page`) | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_desktop_beginner_journey.py -q` | 3 pass; the first page asserts "Recorded" and "Not recorded" are both present |
| The same statement is in the docs a user reads before installing | `docs/privacy.md`, `docs/limitations.md` | read them; `tests/test_docs_links.py` fails if a link or a page is missing | passes: every `docs/…` path named in a user document exists |
| A run that records no model call says so in one sentence, with the flag that fixes it | `src/blackbox/rec/launcher.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_launcher_capture_warning.py -q` | passes |

## 2. Where it can go — nowhere

| Row | Where it lives | The command | What this machine saw |
|---|---|---|---|
| Nothing in the core can open a socket or name a URL | `src/blackbox/` (stdlib only), `tests/test_no_egress.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_no_egress.py -q` | 5 pass (static: imports and URL literals in `src`) |
| At run time, every reader command and a `run` whose child is its own process make **no** connection attempt, and the guard provably refuses one | `tests/test_netguard_reads.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_netguard_reads.py -q` | **9 collected, all pass** — `verify`, `view`, `diagnose`, `capsule verify`, `ls`, `serve`, `desktop --selftest`, a proxied `run`; the control proves the guard refuses |
| The proxy only reaches the upstream the launcher was told about, and the user's own base URL is not moved | `src/blackbox/proxy/routes.py`, `tests/test_proxy_routes.py`, `tests/test_launcher_proxy.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_proxy_routes.py tests/test_launcher_proxy.py -q` | passes; a variable the user already set is handed to the child untouched and becomes a route, not a redirect |
| Nothing in the product sends a capsule, a brief or a report anywhere | `docs/privacy.md` §"What leaves this machine: nothing"; the brief page says it in the window | `rg -n "send it anywhere" src/blackbox/ui/app.py` | present on the diagnosis page, and the journey test asserts the sentence |

## 3. Who can read it on this machine

| Row | Where it lives | The command | What this machine saw |
|---|---|---|---|
| The data directory is `0700` and every file a run writes is `0600` on POSIX | `src/blackbox/platform/perms.py`, `tests/test_permissions.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_permissions.py -q` | 5 collected here, with the Windows arm skipped **by name** (it runs `icacls` and skips only if that cannot run); the Windows CI cells are that measurement |
| `blackbox doctor` reports the permissions of the data directory it is pointed at | `src/blackbox/cli/cmd_doctor.py` (`probe_environment`) | `PYTHONPATH=src .venv/bin/python -m blackbox doctor --json` | `environment: ok`; the JSON carries the mode and the owner |

## What a reviewer should decide

1. **Is "nothing leaves this machine" a property of the code or of the current configuration?** Follow
   `tests/test_no_egress.py` (static) and `tests/test_netguard_reads.py` (runtime). If a reviewer can make the product
   open a connection from any documented command, that is the finding.
2. **Does the redaction-off control prove the sinks are real sinks?** It lives in the redaction pack
   ([`redaction.md`](redaction.md)); run it before deciding this one, because a sink that writes nothing would make both
   packs look clean.
3. **Windows.** The ACL arm and the `0700` equivalence are CI-measured, not measured here. Rule 3 in
   `docs/gates/README.md` applies: the reviewer records that the row is CI-measured.

## What this machine cannot do

* A Windows or macOS filesystem: the ACL rows are CI cells.
* A second machine on the same network to prove nothing is sent to it. The runtime guard is the substitute, and it is
  stronger than a packet capture in one way (it cannot miss a connection) and weaker in another (it only covers the
  commands the test runs).

## The one command that answers the most

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_no_egress.py tests/test_netguard_reads.py tests/test_permissions.py -q
```
