#!/usr/bin/env bash
# Run the live Binary Ninja integration tests.
#
# Prereqs the script checks for you:
#   - venv at .venv with test deps
#   - binaryninja importable in the venv
#   - the fixture binary built (rebuilds it from source if missing)
#
# Prereqs you need to set up yourself:
#   - Binary Ninja running with the constructs fixture open and the
#     MCP server started (bottom-left corner button)
#
# Extra args are forwarded to pytest, so e.g.
#   scripts/integration_test.sh -v -k stack_frame

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
VENV_PY="$VENV/bin/python"
FIXTURE_SRC="$REPO_ROOT/tests/integration/fixtures/src/constructs.c"
FIXTURE_BIN="$REPO_ROOT/tests/integration/fixtures/constructs"

heading() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
ok()      { printf '\033[1;32mOK\033[0m  %s\n' "$*"; }
fail()    { printf '\033[1;31mFAIL\033[0m  %s\n' "$*" >&2; }


# ---------- Stage 1: verify venv ----------

heading "Verifying test setup"

if [ ! -x "$VENV_PY" ]; then
    fail "no venv at $VENV. Create one and install test deps:"
    cat >&2 <<EOF
    python3 -m venv .venv
    .venv/bin/pip install -r tests/requirements.txt
    python3 scripts/install_binaryninja_pth.py .venv
EOF
    exit 1
fi
ok "venv present at $VENV"

missing=()
for mod in pytest requests mcp; do
    if ! "$VENV_PY" -c "import $mod" 2>/dev/null; then
        missing+=("$mod")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    fail "missing Python modules in venv: ${missing[*]}"
    echo "    .venv/bin/pip install -r tests/requirements.txt" >&2
    exit 1
fi
ok "pytest, requests, mcp importable"

if ! "$VENV_PY" -c "import binaryninja" 2>/dev/null; then
    fail "the venv's Python can't \`import binaryninja\`."
    echo "    python3 scripts/install_binaryninja_pth.py $VENV" >&2
    exit 1
fi
ok "binaryninja importable"


# ---------- Stage 2: ensure fixture binary is built ----------

heading "Checking fixture binary"

if [ ! -f "$FIXTURE_BIN" ] || [ "$FIXTURE_SRC" -nt "$FIXTURE_BIN" ]; then
    echo "Building fixture (source is newer or binary missing)..."
    bash "$REPO_ROOT/tests/integration/fixtures/build.sh"
    echo
    echo "Fixture rebuilt. If Binary Ninja already had it open, you'll" >&2
    echo "need to close and reopen the binary so BN re-analyses it." >&2
fi
ok "fixture at $FIXTURE_BIN"


# ---------- Stage 3: heads-up for the live half ----------

heading "Live Binary Ninja required"

cat <<EOF
The next stage talks to Binary Ninja over HTTP. Make sure:
  1. Binary Ninja is running
  2. tests/integration/fixtures/constructs is open and selected
  3. The MCP server is started (bottom-left corner button)

If any of those isn't ready, the tests will *skip* with a clear
message rather than fail — so it's safe to run anyway and check.
EOF


# ---------- Stage 4: run the tests ----------

heading "Running integration tests"

"$VENV_PY" -m pytest tests/integration/ "$@"
