"""Hook adapters (P3B-05/06/07): install a coding agent's hooks, ingest their events, fold them into a tape.

`events` is the §11.9 table; `inbox` is the durable fast path a hook command talks to; `claude_code` and `codex` are the
settings-file adapters. Nothing here opens a socket, and nothing here imports the proxy: a hook adapter's job is to
write one file and get out of the user's way.
"""
