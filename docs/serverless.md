# Serverless — `blackbox.serverless.wrap`, and what a frozen process actually costs you

A Lambda invocation is a process that starts, runs one request, and is then frozen or destroyed. BlackBox's ordinary
model is the opposite: a tape that stays open, a keeper that ticks, a recovery that runs later in the same process. Used
naively in a serverless function, that model leaves a run open at the freeze boundary — and an open run is a run that
never becomes a capsule.

`blackbox.serverless.wrap` changes the boundary and nothing else. It is stdlib-only and part of the package, not an extra.

```python
from blackbox import serverless

def ship(document):
    """Your code owns durability: upload the capsule, or log that there is none."""
    if document["capsule"]:
        upload(document["capsule"], key=f"bbx/{document['run']}.bbx")

@serverless.wrap(on_capsule=ship)
def handler(event, context):
    ...
```

`wrap` also works as a decorator factory (`@serverless.wrap(data_dir=..., on_capsule=...)`) and can be called directly
(`serverless.wrap(handler, on_capsule=ship)`).

## What it does, per invocation

1. **Recovers what the last invocation left.** Any tape under the data directory with no capsule is a previous
   invocation that was killed — a timeout, a freeze, an OOM. It is sealed and handed to `on_capsule` with
   `"recovered": true` and `"completeness": "unknown_serverless"`. The tape is then removed, because the capsule holds the
   same segment bytes and an integrity root over them.
2. **Writes a marker.** `invocation.json` appears before anything else and is removed at seal. A cold start that finds no
   marker knows nothing was lost at the previous boundary; one that finds a marker and no tape knows the previous
   invocation was destroyed outright. That is the only mechanism that can survive a sandbox teardown, and it is why the
   wrapper is honest about it instead of claiming otherwise.
3. **Opens one run** with `mode="serverless"`: 2 MiB segments, a sync at the end of the invocation rather than per batch.
4. **Installs the capture seams** for the invocation, so an SDK call the handler makes is this invocation's evidence.
5. **Seals at return or exception.** `RUN_END`, close, `recovery.recover`, then `on_capsule` with a document:

   ```json
   {"run": "r_…", "capsule": "/tmp/blackbox/capsules/c_….bbx", "root": "…", "state": "ARCHIVED",
    "completeness": "sealed", "invocation_error": null, "error": null}
   ```

   A handler that raised is still sealed, the exception is recorded in `invocation_error`, and it is **re-raised** — the
   recorder does not eat the application's failure.

## What it does not do

* **It cannot recover a destroyed sandbox.** If the platform tears the container down, the tape is gone with the rest of
  `/tmp`. The next cold start records that a boundary was crossed without a seal (`completeness="unknown_serverless"`)
  and makes no claim about what was in it — no event, no invented count.
* **It does not make the capsule durable.** `/tmp` survives warm invocations and nothing else. Shipping the capsule is
  `on_capsule`'s job, and a callback that raises is swallowed so a broken uploader cannot fail a request.
* **It does not remove the cost of sealing.** A seal is a scan, a capsule build and a run directory; measured here at
  **205.7 ms per invocation** over 1 000 invocations (`bench/results/2026-11-16-serverless-1000.json`). That is real
  request latency, and a user who cannot afford it should record fewer invocations (sampling) rather than discover it in
  production.
* **Fail-open is real but not silent.** If the data directory cannot be used — a file where the directory should be, a
  read-only `/tmp` — the handler runs and `on_capsule` receives `"completeness": "not_recorded"` with the reason.

## Measured

`tests/test_serverless.py` (10 tests) covers the boundary, the re-raised exception, a killed invocation recovered by the
next one, the defaults (`2 MiB`, `/tmp/blackbox`, `serverless` in the mode list), a model call recorded inside the
handler, a callback that raises, the fail-open path, and **250 invocations with random exceptions and timeouts leaving no
open run**.

`bench/serverless_1000.py` runs the acceptance's own number — **1 000 invocations**, 5 % of them killed outright — and
writes `bench/results/2026-11-16-serverless-1000.json`. Measured on this machine: **751 returned, 101 timeouts, 81 errors,
67 killed; 1 000 documents; 933 `sealed`, 67 `unknown_serverless`; 1 000 capsules, all verifying; 0 open runs; 205.7 s,
205.73 ms per invocation.** The bench asserts the property from the filesystem rather than from the documents the wrapper
returned: no tape left, every capsule verifying against its own root.

## A note on the platform's freeze

A platform may freeze the process *between* invocations without destroying it. Nothing in that state is running: the
keeper thread is stopped, no frames are appended, and a capsule cannot be built until the next warm invocation runs
`wrap` again — which is exactly when step 1 above seals it. A freeze is therefore visible to a reader as a run that is
sealed late, not as evidence that went missing.
