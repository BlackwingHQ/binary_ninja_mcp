#!/usr/bin/env bash
# Run linter, verify the local test setup, then run the unit tests.
# Stops at the first failing stage so a contributor gets one clear error.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
VENV_PY="$VENV/bin/python"

heading() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
ok()      { printf '\033[1;32mOK\033[0m  %s\n' "$*"; }
fail()    { printf '\033[1;31mFAIL\033[0m  %s\n' "$*" >&2; }


# ---------- Stage 1: lint ----------

heading "Ruff lint and format check"

# Prefer the venv's ruff so contributors get a pinned version once it's
# in tests/requirements.txt; fall back to whatever's on PATH.
if [ -x "$VENV/bin/ruff" ]; then
    RUFF="$VENV/bin/ruff"
elif command -v ruff >/dev/null 2>&1; then
    RUFF="$(command -v ruff)"
else
    fail "ruff not found. Install it (e.g. \`pip install 'ruff>=0.8.4'\`) and re-run."
    exit 1
fi
echo "Using $RUFF ($($RUFF --version))"

"$RUFF" check .
"$RUFF" format --check .
ok "lint clean"


# ---------- Stage 2: verify test setup ----------

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

# Try to import each thing the tests need; collect failures so we can
# print one block of remediation steps instead of stopping at the first.
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


# ---------- Stage 3: tests ----------

heading "Running unit tests"

"$VENV_PY" -m pytest tests/ "$@"
