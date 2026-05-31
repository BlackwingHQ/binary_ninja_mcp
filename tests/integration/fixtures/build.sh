#!/usr/bin/env bash
# Build the integration-test fixture binary from checked-in source.
# Compiled output is gitignored — re-run after editing the .c file.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -O0 -g keeps the call graph and locals legible in HLIL.
cc -O0 -g -o "$HERE/constructs" "$HERE/src/constructs.c"
echo "Built: $HERE/constructs"
