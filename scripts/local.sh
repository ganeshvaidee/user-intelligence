#!/bin/sh
# Run anything in this repo against the local open-weight model.
#
#   ./scripts/local.sh                  # standalone: interactive flow menu
#   ./scripts/local.sh check            # ~60s wiring check
#   ./scripts/local.sh serve            # orchestrator on :8000, for the client CLI
#   ./scripts/local.sh tests/test_flows.py --mode single
#
# Sets the env vars, starts LM Studio's server if it is down, then runs.
#
# TWO TOPOLOGIES, and the difference decides which command you want:
#
#   standalone — `./scripts/local.sh`
#       run_flow.py spawns its own MCP server over stdio. Nothing else to
#       start. This is the fast path for trying things out.
#
#   service — MCP server + orchestrator + client, three processes
#       Start the MCP server as usual, then `./scripts/local.sh serve`, then
#       the client. The orchestrator is the process that calls the model, so
#       it is the one that needs LLM_PROVIDER — setting it on the MCP server
#       or the client does nothing at all.

set -e
cd "$(dirname "$0")/.."

export LLM_PROVIDER=local
export LOCAL_BASE_URL="${LOCAL_BASE_URL:-http://127.0.0.1:1234/v1}"
export LOCAL_MODEL_ID="${LOCAL_MODEL_ID:-meta/muse-glimmer}"

# Prefer the project virtualenv, then an activated one, then whatever is on PATH.
if [ -x "path/to/venv/bin/python" ]; then
    PY="path/to/venv/bin/python"
elif [ -n "$VIRTUAL_ENV" ]; then
    PY="$VIRTUAL_ENV/bin/python"
else
    PY="python3"
fi

if ! "$PY" -c "import openai" 2>/dev/null; then
    echo "Installing the openai extra..."
    "$PY" -m pip install -q -r flows/requirements-local.txt
fi

if ! curl -sf -m 3 "$LOCAL_BASE_URL/models" >/dev/null 2>&1; then
    echo "Server not responding at $LOCAL_BASE_URL — starting LM Studio..."
    lms server start
    sleep 3
fi

case "$1" in
    check)
        exec "$PY" scripts/local_smoke.py
        ;;
    serve)
        shift
        # The orchestrator talks to an already-running MCP server over HTTP
        # rather than spawning one, so MCP_URL must be set. Default matches the
        # port mcp-server/server.py --transport http listens on.
        export MCP_URL="${MCP_URL:-http://localhost:8001}"
        # Any HTTP status means something is listening and speaking HTTP. Do not
        # use `curl -f` here: a healthy streamable-http MCP endpoint answers a
        # plain GET with 406 Not Acceptable, which -f reports as a failure and
        # turns a working server into a scary warning.
        MCP_HOST=$(printf '%s' "$MCP_URL" | sed -E 's#^https?://##; s#[:/].*##')
        MCP_PORT=$(printf '%s' "$MCP_URL" | sed -nE 's#^https?://[^:/]+:([0-9]+).*#\1#p')
        if ! nc -z "$MCP_HOST" "${MCP_PORT:-80}" 2>/dev/null; then
            echo "Warning: no MCP server answering at $MCP_URL"
            echo "  start it first:  python mcp-server/server.py --transport http --port 8001"
        fi
        echo "Orchestrator -> MCP $MCP_URL, model $LOCAL_MODEL_ID"
        exec "$PY" orchestrator/app.py "$@"
        ;;
    "")
        exec "$PY" flows/run_flow.py
        ;;
    *)
        exec "$PY" "$@"
        ;;
esac
