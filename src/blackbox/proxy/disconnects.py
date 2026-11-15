"""§14.4a: the three ways a recorded call stops being a call, told apart by *which side* closed.

The proxy is the only part of this system that sees both ends of an exchange, so it is the only part that can tell these
apart — and telling them apart is the difference between a useful explanation and a guess:

    INTERRUPTED_CLIENT    the app died or cancelled; the model kept talking. Evidence: the write to the client failed
                          while the upstream was still producing. The cost of this class is *paid tokens the app never
                          saw*, which is exactly the thing a user opens a recording to find.
    INTERRUPTED_UPSTREAM  the provider failed while the client was still listening. Evidence: a short read or a reset on
                          the upstream socket, with a healthy client socket, and no terminal chunk. The client gets a
                          truncated stream and this is why.
    INTERRUPTED_PROXY     the recorder itself died. There is no frame saying so, because the process that would have
                          written it is gone: the evidence is an open call on the tape plus a writer whose lease is
                          dead. This is the only one of the three that recovery reasons about rather than reads.

The classes live here rather than in `server.py` because a *reader* has to name them too, and two spellings of a class
name — the one the proxy writes and the one a report prints — is how a database of classes rots.

Byte counts are per side and they are the point of §14.4a: when an SDK tape and a proxy tape cover the same call, the two
numbers should agree, and a difference is itself evidence. `client_received_bytes` is what the proxy managed to hand to
the client's socket, `proxy_received_bytes` is what it read from the upstream. Neither is "what the app processed" — the
app may have died with those bytes in a buffer, and this module does not pretend to know.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from blackbox import constants as C

INTERRUPTED_CLIENT = "INTERRUPTED_CLIENT"
INTERRUPTED_UPSTREAM = "INTERRUPTED_UPSTREAM"
INTERRUPTED_PROXY = "INTERRUPTED_PROXY"

CLASSES = (INTERRUPTED_CLIENT, INTERRUPTED_UPSTREAM, INTERRUPTED_PROXY)

# Which side stopped the exchange first. There is no third option: a call that both sides left is a call the client left,
# because the proxy stops reading the upstream the moment the client is gone — so "client" wins and the tape says so.
Side = Literal["client", "upstream"]

DETAIL = {
    INTERRUPTED_CLIENT: "the client socket went away while the upstream was still sending; the model may have produced "
                        "tokens nobody received",
    INTERRUPTED_UPSTREAM: "the upstream closed before a terminal event while the client was still connected; the client "
                          "received a truncated stream",
    INTERRUPTED_PROXY: "the recorder died mid-call: the tape has an open call and no terminal frame, and the writer's "
                       "lease is dead",
}


@dataclass(frozen=True)
class Disconnect:
    """One class, one detail, and the two byte counts §14.4a asks a reader to be able to compare."""

    klass: str
    status: int = 0
    client_received_bytes: int = 0
    proxy_received_bytes: int = 0
    detail: str = ""

    def as_error(self) -> str:
        """The single line the tape's `error` field carries, and the line `parse` reads back out of it."""
        return (f"{self.klass}: client received {self.client_received_bytes} B, proxy received "
                f"{self.proxy_received_bytes} B from the upstream; {self.detail or DETAIL.get(self.klass, '')}")

    def as_dict(self) -> dict[str, Any]:
        return {"class": self.klass, "status": self.status,
                "client_received_bytes": self.client_received_bytes,
                "proxy_received_bytes": self.proxy_received_bytes, "detail": self.detail}


def classify(*, closed_first: Side, status: int, client_received_bytes: int, proxy_received_bytes: int) -> Disconnect:
    """Which class a live proxy can prove, from the side that closed first and the counts it kept."""
    klass = INTERRUPTED_CLIENT if closed_first == "client" else INTERRUPTED_UPSTREAM
    return Disconnect(klass=klass, status=status, client_received_bytes=client_received_bytes,
                      proxy_received_bytes=proxy_received_bytes, detail=DETAIL[klass])


def proxy_death(*, status: int = 0, client_received_bytes: int = 0, proxy_received_bytes: int = 0) -> Disconnect:
    """The class nobody writes on the tape: the proxy died, so there is no frame and no live counts to quote."""
    return Disconnect(klass=INTERRUPTED_PROXY, status=status, client_received_bytes=client_received_bytes,
                      proxy_received_bytes=proxy_received_bytes, detail=DETAIL[INTERRUPTED_PROXY])


def parse(error: str | None) -> str | None:
    """The class named by an error string, or `None` when the string is not one of ours.

    `None` is a real answer: an error frame may say `upstream-closed-uncertain`, which is a *different* kind of statement
    — an outcome the proxy could not determine — and a reader that filed it under a disconnect class would be inventing
    a fact.
    """
    if not error:
        return None
    for klass in CLASSES:
        if error.startswith(klass):
            return klass
    return None


_COUNTS = re.compile(r"client received (\d+) B, proxy received (\d+) B")


def counts(error: str | None) -> tuple[int, int] | None:
    """`(client_received_bytes, proxy_received_bytes)` read back off an error line, or `None` if it has none."""
    if not error:
        return None
    found = _COUNTS.search(error)
    return (int(found.group(1)), int(found.group(2))) if found else None


def error_of(frame: dict[str, Any]) -> str | None:
    """The error line inside a tape frame: the summary frames carry their JSON in the payload, not in a field."""
    if frame.get("error"):
        return str(frame["error"])
    payload = frame.get("payload")
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if isinstance(data, dict):
            found = data.get("error")
            if isinstance(found, str):
                return found
    return None


def from_tape(frames: Sequence[dict[str, Any]], *, writer_alive: bool) -> list[dict[str, Any]]:
    """Per-call classes from a recovered proxy tape, including the one that is only visible by its absence.

    `writer_alive` is the caller's lease check: a call that is still open on the tape of a writer that is still alive is
    an in-flight call, not an interrupted one, and is left out. A call that is still open on the tape of a dead writer is
    `INTERRUPTED_PROXY` — the proxy never got to write the terminal frame, which is the whole evidence.
    """
    if writer_alive:
        return []
    by_slot: dict[int, dict[str, Any]] = {}
    for frame in frames:
        kind = frame.get("kind")
        if kind not in (C.KIND_MODEL_REQUEST, C.KIND_MODEL_CHUNK, C.KIND_MODEL_RESPONSE, C.KIND_MODEL_ERROR):
            continue
        slot = int(frame.get("node_slot") or 0)
        entry = by_slot.setdefault(slot, {"node_slot": slot, "class": None, "terminal": False, "chunk_bytes": 0})
        if kind == C.KIND_MODEL_CHUNK:
            entry["chunk_bytes"] += len(frame.get("payload") or b"")
        elif kind in (C.KIND_MODEL_RESPONSE, C.KIND_MODEL_ERROR):
            line = error_of(frame)
            entry["terminal"] = True
            entry["class"] = parse(line)
            entry["counts"] = counts(line)
    out: list[dict[str, Any]] = []
    for slot, entry in sorted(by_slot.items()):
        if entry["terminal"]:
            if entry["class"] is None:
                continue                      # a completed call, or an error that is not a disconnect class
            client_bytes, proxy_bytes = entry.get("counts") or (0, entry["chunk_bytes"])
            out.append({"node_slot": slot, "class": entry["class"], "status": 0,
                        "client_received_bytes": client_bytes, "proxy_received_bytes": proxy_bytes,
                        "detail": DETAIL[entry["class"]]})
            continue
        out.append({"node_slot": slot, "class": INTERRUPTED_PROXY, "status": 0,
                    "client_received_bytes": None, "proxy_received_bytes": entry["chunk_bytes"],
                    "detail": DETAIL[INTERRUPTED_PROXY]})
    return out
