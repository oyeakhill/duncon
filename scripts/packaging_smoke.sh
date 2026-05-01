#!/usr/bin/env bash
# Packaging smoke test: build the wheel, install it in a fresh venv,
# verify the CLI launches and the public API imports cleanly.
#
# Run from the repo root:
#   bash scripts/packaging_smoke.sh

set -euo pipefail

SCRATCH=$(mktemp -d)
trap "rm -rf $SCRATCH" EXIT

echo "[smoke] building wheel"
uv build > /dev/null
WHEEL=$(ls dist/dunc_connector-*-py3-none-any.whl | head -1)
echo "[smoke]   $WHEEL"

echo "[smoke] creating fresh venv at $SCRATCH/venv"
uv venv --python 3.10 "$SCRATCH/venv" > /dev/null

echo "[smoke] installing wheel"
uv pip install --python "$SCRATCH/venv/bin/python" "$WHEEL" > /dev/null

echo "[smoke] verifying CLI"
"$SCRATCH/venv/bin/dunc-connector" --help > /dev/null
echo "[smoke]   dunc-connector --help: OK"

echo "[smoke] verifying public imports"
"$SCRATCH/venv/bin/python" -c "
from dunc_connector import (
    DuncClient,
    DuncService,
    DuncConnectorError,
    DuncTransportError,
    DuncAuthError,
    DuncRunError,
    DuncValidationError,
)
assert DuncClient.__name__ == 'DuncClient'
assert DuncService.__name__ == 'DuncService'
print('[smoke]   imports: OK')
"

echo "[smoke] verifying CLI subprocess can spawn agent script"
cat > "$SCRATCH/agent.py" <<'PY'
import json, sys
data = json.load(sys.stdin)
sys.stdout.write(json.dumps({"echo": data}))
PY
"$SCRATCH/venv/bin/python" -c "
from dunc_connector.cli import build_command_handler
import sys
h = build_command_handler('python3 $SCRATCH/agent.py', timeout=5.0)
out = h({'k': 'v'})
assert out == {'echo': {'k': 'v'}}, out
print('[smoke]   command handler round-trip: OK')
"

echo "[smoke] DONE — package is installable + functional"
