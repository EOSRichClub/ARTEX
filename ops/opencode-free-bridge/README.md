# OpenCode Free bridge for ARTEX

This deployment runs a minimal OpenAI-compatible adapter on `127.0.0.1:14059`.
Each request is executed through the pinned official `opencode run` CLI. OpenCode
1.19.0's standalone server currently loses the client attribution required by the
free tier, while the official non-interactive CLI path works correctly.

The adapter intentionally exposes only:

- `muse-spark-1.3-contributor-free`
- `mimo-v2.6-flash-free`

OpenAI `tools` are translated through the local `artex_proxy` OpenCode custom tool.
The bridge returns the resulting request as an ordinary OpenAI `tool_calls` block;
ARTEX remains the only process that executes the real tool. Keep the profiles out
of automatic failover because the extra OpenCode CLI hop is slower than a native
provider connection. The bridge also caps concurrent CLI runs at two to protect
the 4-core/8-GB host from bursts of subprocesses.

The production install pins `opencode-ai@1.19.0`. The listener is loopback-only and
requires a random bearer token stored in `/etc/artex/opencode-free-bridge.env`
(mode `0640`, group `artex`).
