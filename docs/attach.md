# Attach — one command per surface, and the version each row was actually run on

This file is the answer to *"can I attach this to what I already use?"* One row per surface, one of two verdicts in the
last column: **a version and a date on which the row was run**, or the word **unverified**. There is no third option and
no row without one of them; `tests/test_attach_doc.py` fails the suite if a row tries.

Two things this table is not. It is not a claim that a hosted provider accepted anything: every "verified" below means
*this client, on this version, today, honoured the base URL the launcher exported and the request arrived where the
provider's own documentation puts it* — the traffic went to a loopback origin in this repository, because there are no
provider keys here and no route off the sandbox. And it is not a plan: rows that are unverified say so rather than
turning into wishes (`docs/STATUS.md` keeps the same discipline).

## 1. The one command

```bash
blackbox run -- <your command>
```

That is the whole attach. It starts a loopback proxy, exports the base-URL variables below **only where you had not set
them yourself**, folds the coding-agent hooks you have installed (§3 — it does not install them for you), supervises the
child, and recovers a sealed capsule when the child exits or dies. A variable
you had already set is never overwritten; it is *adopted*, and its destination becomes a route, because a recorder that
silently moves your traffic is not a recorder.

| variable | what the child receives | pointed at |
|---|---|---|
| `OPENAI_BASE_URL` | `http://127.0.0.1:<port>/openai/v1` | the `openai` route, default upstream `https://api.openai.com` |
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:<port>/anthropic` | the `anthropic` route, default upstream `https://api.anthropic.com` |
| `OLLAMA_HOST` | `http://127.0.0.1:<port>/ollama` | the `ollama` route, default upstream `http://127.0.0.1:11434` — **unverified**: Ollama's own `OLLAMA_HOST` is a `host:port` without a scheme, so a client that parses it that way will not accept this value. Do not treat this row as working |
| `GOOGLE_GEMINI_BASE_URL` | `http://127.0.0.1:<port>/gemini` | the `gemini` route, default upstream `https://generativelanguage.googleapis.com` — **unverified**: the variable name is this repository's, not Google's; the `google-genai` client reads its own spelling, and nobody has run it here |
| `BLACKBOX_PROXY`, `BLACKBOX_PROXY_URL` | `1`, the proxy's URL | this is how a link is claimed as `exact` rather than inferred |

The version segment matters, and it is not the same for every client — that asymmetry is defect 39
(`docs/defects.md`): the OpenAI SDK's own default base is `https://api.openai.com/v1` and it appends only
`/chat/completions`, while the Anthropic SDK's default is `https://api.anthropic.com` and it appends `/v1/messages`.
Exporting one flat shape for both sent the OpenAI SDK to `/chat/completions` on the upstream, a path the provider does
not serve. The suffix per variable now lives in `BASE_URL_SUFFIX`, and
`tests/test_attach_sdks.py::test_the_exported_base_plus_the_clients_own_path_is_the_documented_provider_path` is what
keeps it honest.

## 2. Clients that take a base URL

"How to point it" is the value a user sets by hand when they are not using `blackbox run`; the verified column is what
`tests/test_attach_sdks.py` runs **through the proxy the launcher built**, with the export above and no hand-written
path in the test body.

| surface | client | how to point it | verified on | verdict |
|---|---|---|---|---|
| Python | `openai` SDK | `OPENAI_BASE_URL`, or `base_url=` | openai 3.51.0, Python 3.11.2, Linux, 2026-11-14 — `chat.completions.create` reached `/v1/chat/completions` | **verified** (loopback origin, not the hosted provider) |
| Python | `anthropic` SDK | `ANTHROPIC_BASE_URL`, or `base_url=` | anthropic 2.8.0, Python 3.11.2, Linux, 2026-11-14 — `messages.create` reached `/v1/messages` | **verified** (loopback origin) |
| any language | `curl` | `curl "$OPENAI_BASE_URL/chat/completions"` | curl 7.88.1, Linux, 2026-11-14 — POST reached `/v1/chat/completions` | **verified** (loopback origin) |
| Node / TS | built-in `fetch` | `fetch(process.env.OPENAI_BASE_URL + "/chat/completions")` | node v22.22.3, Linux, 2026-11-14 — POST reached `/v1/chat/completions` | **verified** (loopback origin) |
| Node / TS | `openai` npm package | `OPENAI_BASE_URL`, or `new OpenAI({baseURL})` | — | **unverified** |
| Go | `openai-go` | `OPENAI_BASE_URL`, or `option.WithBaseURL` | — | **unverified** |
| Java | `openai-java` | `OPENAI_BASE_URL`, or `.baseUrl(...)` | — | **unverified** |
| C# / .NET | `openai-dotnet` | `OPENAI_BASE_URL`, or `OpenAIClientOptions.Endpoint` | — | **unverified** |
| Rust | `async-openai` | `OPENAI_API_BASE` — a *different* variable name; `blackbox run` does not export it | — | **unverified** |
| Ruby | `ruby-openai` | `OPENAI_API_BASE` — as above | — | **unverified** |
| PHP | `openai-php` | `OPENAI_BASE_URL` is not read; the base URL is a constructor argument | — | **unverified** |
| Aider | `aider` | `OPENAI_API_BASE` / `--openai-api-base` — not exported by `blackbox run` | — | **unverified** |
| LangChain | `langchain-openai` | `base_url=` on the model | — | **unverified** |
| LiteLLM | `litellm` | `api_base=` per call, or `OPENAI_BASE_URL` | — | **unverified** |
| Cursor (own key) | Cursor's settings | base-URL override in Cursor's own settings; this repository does not know its shape | — | **unverified** |
| Claude Code | via `ANTHROPIC_BASE_URL` | `blackbox run -- claude` | Claude Code honours base URLs **[verified external, Round 1]** per the plan's §0.5; nothing in *this* repository has run it | **unverified here** |

The four verified rows are the four this sandbox can run: the two Python SDKs it has installed, `curl`, and `node`. The
rows below them are the ones a stranger will ask about first, and every one of them is a question this repository has
not answered. That is what the word means.

## 2a. No code change at all, for a Python program

`blackbox run` also arms the child's interpreter, so a program that never imports this package still gets the SDK
seam. A temporary `sitecustomize.py` goes first on the child's `PYTHONPATH`; it waits for `httpx`/`httpx2` to be
imported and only then installs the winning seam (D-044). The shim directory is removed when the child exits.

| surface | mechanism | verified on | verdict |
|---|---|---|---|
| a Python program that speaks HTTP through `httpx` / `httpx2` | temp `sitecustomize.py` + `PYTHONPATH` | `tests/test_autostart.py` (11), Python 3.11.2, Linux, 2026-11-16 — a child containing no import of ours produced `MODEL_REQUEST`…`MODEL_RESPONSE` on a vantage-1 tape, and the run recovered it beside its own | **verified** (loopback origin) |
| the same, on Windows | as above | — | **unverified** — the shim is standard CPython, but the tests that spawn children are POSIX-only and nothing here has run it on Windows |
| a client that speaks neither `httpx` nor `httpx2` (`requests`, `urllib3`, a native SDK) | not armed | — | **unverified** for the auto-import; the proxy and the hook adapters are the vantages that see those, and `attach/startup-<pid>.json` says the child imported no client library rather than leaving a silent gap |

The plan names a `.pth` file for this. A `.pth` is only executed from a directory `site` scans, so on `PYTHONPATH` it
does nothing — measured, not assumed, by the first test in `tests/test_autostart.py` — and the alternative would be
writing into your site-packages. That is why the mechanism above is a `sitecustomize` (D-044).

## 3. Coding agents through their own hooks

Hooks are the third vantage: they see the agent's turns and tool calls, not its HTTP. They are installed by
`blackbox hook install` and folded by the launcher into a tape of their own at `vantage 3`
(`docs/DECISIONS.md` D-039, D-040, D-041).

| agent | command | what is verified | verdict |
|---|---|---|---|
| Claude Code | `blackbox hook install --claude-code` | the settings round-trip, the ten-event mapping, the fold and the `exact` link — on 2026-11-14, by `tests/test_hook_claude_code.py` and `tests/test_hook_launcher.py` | **verified** as a *file adapter and mapping*; a real Claude Code session firing these hooks is **unverified here** (no Claude Code in this sandbox) |
| Codex | `blackbox hook install --codex` | the twelve-event mapping and the byte-identical round-trip, 2026-11-14, by `tests/test_hook_codex.py`; the settings path and hook-block shape are an **assumption**, printed by the adapter (`schema_verified: false`) | **unverified** — the product says so at the point of use |

`blackbox attach <pid>` is deliberately not offered: injecting into a running process needs ptrace or a debugger and is
platform-specific. The honest alternative is to restart the program under `blackbox run`, and that is what the CLI says.

## 4. What attach cannot see

- A closed consumer app that does not let you set a base URL (the ChatGPT desktop app, a phone app with a pinned
  endpoint, a browser extension calling its vendor's own backend).
- A client that signs its requests (SigV4-style) or speaks gRPC — the proxy is an HTTP/1.1 tee with an allowlist, not a
  key vault.
- A program built to refuse proxies: certificate pinning, or a client that insists on its own CA.
- Traffic that never leaves the machine's own process pair without a base-URL seam: a program with the endpoint compiled
  in and no override is not attachable, and no amount of hooking changes that.

## 5. Checking a row yourself

Every "verified" row above is one command away, and the command is the test:

```bash
PYTHONPATH=src python -m pytest tests/test_attach_sdks.py -v
```

The tests start a provider-shaped origin on loopback, build the route table the launcher would build, take the
base-URL value from `plan_exports()`, hand it to the real client, and assert on the path and body the **origin**
received. If a row in §2 is wrong, that test fails and the row is wrong; if a version moves, the test prints the version
it ran.

For the unverified rows, the recipe is the same with one substitution: set the variable your client reads to
`http://127.0.0.1:<port>/openai/v1` (or the path in §1), point it at a canary origin, and see whether the request
arrives. If it does, the row can become verified — with the version, the date, and a test beside it. Until then it says
**unverified**, which is a true sentence.
