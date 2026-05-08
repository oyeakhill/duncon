# dunc-connector

The connector SDK for the [Vicilus](https://api.vicilus.com) Agent Commerce Control Plane. Wrap any agent — a Python function, a CLI script, or a local HTTP service — and run it as a rentable service on Vicilus.

The folder name in this repo is `duncon` (short, easier to type). The installable package on PyPI / via `pip install` is `dunc-connector`. The Python import path is `dunc_connector`. The CLI command is `dunc-connector`.

## Install

Once published on PyPI:

```bash
pip install dunc-connector
# or
uv add dunc-connector
```

Until PyPI publish, install directly from this Git repo:

```bash
pip install "git+https://github.com/oyeakhil/duncon.git"
# or
uv add "git+https://github.com/oyeakhil/duncon.git"
```

That installs the `dunc_connector` Python package and the `dunc-connector` CLI.

## Three integration modes

| Mode | Best for | How |
|---|---|---|
| **Function** | Python agents you can `import` | `@svc.run` decorator + `svc.start()` |
| **Command** | Any CLI in any language (stdin/stdout JSON) | `dunc-connector command --command "python3 agent.py"` |
| **HTTP** | Already-running local API | `dunc-connector http --target-url http://localhost:9000/run` |

All three use the same Vicilus transport (long-polling). The connector never opens an inbound port. The seller's secrets (API keys, prompts, code) stay in the connector process; Vicilus only sees JSON inputs and JSON outputs.

## Operational defaults — read before going to production

The SDK is opinionated about reliability defaults. Sellers running paid services should keep these unless they know what they're trading off.

| Setting | Default | Why |
|---|---|---|
| `batch_limit` | **1** | Connector heartbeats between every dispatch. Long handlers won't make the connector look offline mid-batch. Raise only if your handlers complete in well under 60s. |
| `handler_timeout` | **90s** | Bounded under the platform's `RUN_TIMEOUT_SECONDS=120s`. If your handler exceeds this, the SDK fails the run cleanly — *before* the platform sweep refunds the buyer. Set to 0 to disable. |
| `poll_interval` | 2.0s | Long-poll cadence. |
| Transport backoff | exponential, cap 60s | Network/platform errors no longer hammer at 0.5 RPS. Resets after a successful heartbeat/fetch. |
| `User-Agent` | `dunc-connector/<version>` | Lets the platform correlate connector behavior to a specific SDK release. |

**Run the connector on a stable cloud server for paid services** (Railway, Fly, Render, EC2, GCE — anywhere with predictable uptime). Local-laptop mode is fine for testing but every Wi-Fi blip, lid close, or restart leaves runs stuck in `processing` until the platform sweep refunds the buyer.

## Quickstart — function mode

```python
from dunc_connector import DuncService

svc = DuncService(
    base_url="https://api.vicilus.com",
    connection_id="cnx_...",
    connection_token="cnxtok_...",  # shown ONCE when you mint the connection
)

@svc.run
def handle(input_json: dict) -> dict:
    # Your real agent goes here. SDK handles the rest:
    # auth, polling, output validation, error sanitization, timeouts.
    return {"answer": 42}

svc.start()
```

Run it:

```bash
python my_agent.py
```

You should see:

```
[connector] dunc connector starting: connection_id=cnx_... poll_interval=2.0s batch_limit=1 handler_timeout=90.0s
```

`Ctrl-C` or `kill` the process — it traps both `SIGINT` and `SIGTERM`, finishes the in-flight run, then exits cleanly.

## Quickstart — command mode (any language)

Your agent is a CLI script that reads JSON from stdin and writes JSON to stdout:

```python
# agent.py
import json, sys
data = json.load(sys.stdin)
out = {"echo": data}
sys.stdout.write(json.dumps(out))
```

**Recommended invocation** — token in env var, kept out of shell history / `ps aux` / journald:

```bash
export DUNC_CONNECTION_TOKEN="cnxtok_..."

dunc-connector \
    --base-url https://api.vicilus.com \
    --connection-id cnx_... \
    command \
    --command "python3 agent.py"
```

The token is also accepted via `--connection-token <secret>` for one-shot demos, but **the env var is strictly safer** — the CLI prints a warning when you use the flag.

For each queued run, the CLI:
1. Spawns the child process.
2. Pipes `input_json` to its stdin.
3. Reads stdout and parses as JSON.
4. POSTs the parsed object as `output_json` to Vicilus.

Non-zero exit, non-JSON stdout, or non-object JSON auto-fail the run with a sanitized message.

## Quickstart — HTTP mode

Your agent is already an HTTP service:

```bash
python my_agent_server.py     # listening on http://localhost:9000/run
```

Point the connector at it:

```bash
export DUNC_CONNECTION_TOKEN="cnxtok_..."

dunc-connector \
    --base-url https://api.vicilus.com \
    --connection-id cnx_... \
    http \
    --target-url http://localhost:9000/run
```

For each queued run, the connector POSTs `input_json` and parses the JSON response.

## What the SDK gives you for free

- **Token masking** in `repr()` and logs.
- **Token via env var** (`DUNC_CONNECTION_TOKEN`) keeps secrets out of shell history.
- **Output validation**: must be a JSON object, default 1 MiB cap, configurable.
- **Sanitized errors**: handler exceptions → `<Type>: <message>` (no traceback, no leaked args).
- **Handler timeout** (90s default): SDK fails the run cleanly before the platform sweep refunds the buyer.
- **Per-run heartbeats**: connector never looks offline mid-batch.
- **SIGTERM-aware shutdown**: redeploys finish the current run, then exit. No orphaned `processing` rows.
- **Transport backoff**: exponential with 60s cap; doesn't hammer the platform during outages.
- **Platform-finalized awareness**: if the platform already swept your run (or a buyer cancelled), the SDK logs *"run was already finalized by platform; result was not accepted"* instead of a generic transport error.
- **Structured logging** under `logging.getLogger("dunc_connector")`.

## Development

```bash
# In this repo
pip install -e .[dev]
pytest -q
```

46 unit tests cover the client, service, errors, CLI handlers, reliability hardening (timeout, backoff, shutdown, 422 awareness), and the env-var token flow. Tests are hermetic — no network calls; `httpx.MockTransport` is used to fake the platform.

## License

MIT.
