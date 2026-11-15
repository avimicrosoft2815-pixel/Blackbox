"""What this recorder is allowed to look at (P0, from the reviewer's audit of the blanket patch).

Seam A patches `Client.__init__` in every importable client module, which is the right install strategy and the wrong
capture policy: the same process may also be talking to a vector database, a webhook or an internal service, and a
recorder that silently scoops all of it is a privacy incident waiting for a bug report. `ollama` on loopback and the
company gateway are model traffic; the webhook a CI job posts to is not.

So the scope is explicit, it is recorded in the seam's `detail`, and it has exactly three shapes:

* **default** — the provider hosts this build actually supports (`api.openai.com`, `api.anthropic.com`) plus loopback
  (`127.0.0.1`, `::1`, `localhost`), because that is where a local proxy, a mock provider and a self-hosted gateway
  answer. Nothing else. The directive's own words — "bind proxy locally by default; allow only explicit configured
  upstream forwarding" — are the same rule one layer up.
* **explicit** — `capture_hosts=[...]` or `BLACKBOX_CAPTURE_HOSTS="a.example,b.example"`, exact hosts or
  `*.suffix` wildcards.
* **wildcard** — `"*"`, which a user has to type on purpose, and which is the only way to capture everything.

A request outside the scope is not captured *and not counted as captured*: the hook returns before opening a call, so
it cannot leave a half-written call on the tape, and the skip is counted in `scope_stats["out_of_scope"]` so that "no
model calls in this capsule" can be told apart from "the recorder was not looking" (an absence is not evidence — the
evidence-boundary rule from the architecture, applied to a bug report).

Ports are ignored: `api.openai.com:443` and `api.openai.com` are the same host, and comparing them by string would be
a bug that only appears behind a corporate gateway.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "api.anthropic.com",
    "127.0.0.1",
    "::1",
    "localhost",
)

ENV_VAR = "BLACKBOX_CAPTURE_HOSTS"


def _normalise(host_or_url: str) -> str:
    """A URL or a host → the bare host: scheme dropped, path dropped, lower case, no port, no IPv6 brackets."""
    value = host_or_url.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):                      # IPv6 literal: [::1]:8080
        return value[1:value.find("]")] if "]" in value else value[1:]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


@dataclass(frozen=True)
class CaptureScope:
    """An allowlist of hosts. Frozen: a scope that mutates mid-run makes both the tape and the report a guess."""

    hosts: tuple[str, ...] = DEFAULT_HOSTS
    source: str = "argument"                       # "default" | "argument" | "environment" | "wildcard"

    def __post_init__(self) -> None:
        # Normalise here, not only in `from_config`: a scope built by hand is the one a test or an integration writes,
        # and `CaptureScope(hosts=("API.OpenAI.com",))` must mean what it looks like it means.
        object.__setattr__(self, "hosts", tuple(_normalise(h) for h in self.hosts))
    stats: dict[str, int] = field(default_factory=lambda: {"out_of_scope": 0}, compare=False, repr=False)

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def default(cls) -> CaptureScope:
        return cls(hosts=DEFAULT_HOSTS, source="default")

    @classmethod
    def from_config(cls, hosts: list[str] | tuple[str, ...] | None = None) -> CaptureScope:
        """Argument first, then the environment, then the default. A wildcard says so in `source`."""
        if hosts:
            cleaned = tuple(_normalise(h) for h in hosts if h and h.strip())
            return cls(hosts=cleaned, source="wildcard" if "*" in cleaned else "argument")
        env = os.environ.get(ENV_VAR, "").strip()
        if env:
            cleaned = tuple(_normalise(part) for part in env.split(",") if part.strip())
            return cls(hosts=cleaned, source="wildcard" if "*" in cleaned else "environment")
        return cls.default()

    # ── the question the seams ask ──────────────────────────────────────────
    def allows(self, host_or_url: str | None) -> bool:
        if not host_or_url:
            return False
        host = _normalise(host_or_url)
        if not host:
            return False
        for allowed in self.hosts:
            if allowed == "*":
                return True
            if allowed == host:
                return True
            if allowed.startswith("*.") and host.endswith(allowed[1:]):
                # `*.example.com` matches `api.example.com` and not `example.com` itself: a wildcard that also
                # matched the bare domain would be a quieter grant than the one the operator wrote.
                return host != allowed[2:] and host.endswith(allowed[1:])
        return False

    def skip(self) -> None:
        self.stats["out_of_scope"] = self.stats.get("out_of_scope", 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {"hosts": list(self.hosts), "source": self.source, "stats": dict(self.stats)}
