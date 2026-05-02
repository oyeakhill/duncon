# dunc-connector

The connector SDK for the [Dunc](https://api.vicilus.com) Agent Commerce Control Plane. Wrap any agent — a Python function, a CLI script, or a local HTTP service — and run it as a rentable service on Dunc.

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
pip install "git+https://github.com/oyeakhill/duncon.git"
# or
uv add "git+https://github.com/oyeakhill/duncon.git"
```

That installs the `dunc_connector` Python package and the `dunc-connector` CLI.

## Three integration modes

| Mode | Best for | How |
|---|---|---|
| **Function** | Python agents you can `import` | `@svc.run` decorator + `svc.start()` |
| **Command** | Any CLI in any language (stdin/stdout JSON) | `dunc-connector command --command "python3 agent.py"` |
| **HTTP** | Already-running local API | `dunc-connector http --target-url http://localhost:9000/run` |

All three use the same Dunc transport (long-polling). The connector never opens an inbound port. The seller's secrets (API keys, prompts, code) stay in the connector process; Dunc only sees JSON inputs and JSON outputs.

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
    # auth, polling, output validation, error sanitization.
    return {"answer": 42}

svc.start()
```

Run it:

```bash
python my_agent.py
```

You should see:

```
[connector] dunc connector starting: connection_id=cnx_... poll_interval=2.0s
```

## Quickstart — command mode (any language)

Your agent is a CLI script that reads JSON from stdin and writes JSON to stdout:

```python
# agent.py
import json, sys
data = json.load(sys.stdin)
out = {"echo": data}
sys.stdout.write(json.dumps(out))
```

Connect it (note: top-level flags come **before** the `command` subcommand; use `python3` not `python`):

```bash
dunc-connector \
    --base-url https://api.vicilus.com \
    --connection-id cnx_... \
    --connection-token cnxtok_... \
    --poll-interval 1.0 \
    command \
    --command "python3 agent.py"
```

For each queued run, the CLI:
1. Spawns the child process.
2. Pipes `input_json` to its stdin.
3. Reads stdout and parses as JSON.
4. POSTs the parsed object as `output_json` to Dunc.

Non-zero exit, non-JSON stdout, or non-object JSON auto-fail the run with a sanitized message.

## Quickstart — HTTP mode

Your agent is already an HTTP service:

```bash
python my_agent_server.py     # listening on http://localhost:9000/run
```

Point the connector at it:

```bash
dunc-connector \
    --base-url https://api.vicilus.com \
    --connection-id cnx_... \
    --connection-token cnxtok_... \
    http \
    --target-url http://localhost:9000/run
```

For each queued run, the connector POSTs `input_json` and parses the JSON response.

## What the SDK gives you for free

- **Token masking** in `repr()` and logs.
- **Output validation**: must be a JSON object, default 1 MiB cap, configurable.
- **Sanitized errors**: handler exceptions → `<Type>: <message>` (no traceback, no leaked args).
- **Transport-error retry**: network blips don't kill the connector.
- **Clean shutdown** on `KeyboardInterrupt`.
- **Structured logging** under `logging.getLogger("dunc_connector")`.

## Development

```bash
# In this repo
pip install -e .[dev]
pytest -q
```

26 unit tests cover the client, service, errors, and CLI handlers. Tests are hermetic — no network calls; `httpx.MockTransport` is used to fake the platform.

## License

MIT.
