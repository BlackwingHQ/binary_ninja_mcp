#!/usr/bin/env python3
"""One-time per-system setup for the Binary Ninja MCP plugin.

Creates the local virtualenv under `<plugin_root>/.venv` and installs the
bridge's runtime dependencies into it. Does NOT modify any MCP client config
files — use `install_mcp_client.py` for that.

Run once after cloning or upgrading the plugin. Safe to re-run; existing
venvs are reused unless `--force` is passed.
"""

import argparse
import json
import os
import shutil
import sys

# Allow running directly from the scripts/ directory without installing the
# package: add the plugin root to sys.path so `plugin.utils.installer` resolves.
_HERE = os.path.dirname(os.path.realpath(__file__))
_PLUGIN_ROOT = os.path.dirname(_HERE)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from plugin.utils.installer import (  # noqa: E402
    MCP_SERVER_KEY,
    bridge_entrypoint,
    collect_python_env,
    ensure_venv,
    make_server_entry,
    requirements_file,
    venv_dir,
    venv_exists,
    venv_python,
)


def _say(quiet: bool, msg: str) -> None:
    if not quiet:
        print(msg)


def run(force: bool = False, quiet: bool = False) -> int:
    """Create or refresh the venv and install requirements. Return exit code."""
    if force and os.path.isdir(venv_dir()):
        _say(quiet, f"Removing existing venv at {venv_dir()}")
        shutil.rmtree(venv_dir(), ignore_errors=True)

    if venv_exists() and not force:
        _say(quiet, f"Venv already present at {venv_dir()} (use --force to recreate)")
    else:
        _say(quiet, f"Creating venv at {venv_dir()} ...")
        ensure_venv()
        if not venv_exists():
            print(
                f"ERROR: venv creation did not produce {venv_python()}. "
                "Check that a system Python 3.10+ is installed.",
                file=sys.stderr,
            )
            return 1
        _say(quiet, "Venv ready.")

    req = requirements_file()
    if not os.path.exists(req):
        _say(quiet, f"No requirements file at {req}; skipping pip install.")

    py = venv_python()
    bridge = bridge_entrypoint()

    _say(quiet, "")
    _say(quiet, "Setup complete.")
    _say(quiet, f"  venv python : {py}")
    _say(quiet, f"  bridge      : {bridge}")
    _say(quiet, "")
    _say(quiet, "To register the bridge with a specific MCP client:")
    _say(quiet, "  python scripts/install_mcp_client.py --list-clients")
    _say(quiet, "  python scripts/install_mcp_client.py --client <Name>")
    _say(quiet, "")
    _say(quiet, "Or paste this snippet into an unsupported client's config:")
    snippet = {
        "mcpServers": {
            MCP_SERVER_KEY: make_server_entry(py, bridge, collect_python_env()),
        }
    }
    _say(quiet, json.dumps(snippet, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time system setup for the Binary Ninja MCP plugin.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recreate the venv even if it already exists.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress non-error output.",
    )
    args = parser.parse_args()
    return run(force=args.force, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
