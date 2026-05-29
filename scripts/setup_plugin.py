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

# Import the leaf modules under plugin/utils directly. Going through the
# `plugin` package would run `plugin/__init__.py`, which imports binaryninja
# and only works inside Binary Ninja itself.
_HERE = os.path.dirname(os.path.realpath(__file__))
_PLUGIN_ROOT = os.path.dirname(_HERE)
_UTILS = os.path.join(_PLUGIN_ROOT, "plugin", "utils")
if _UTILS not in sys.path:
    sys.path.insert(0, _UTILS)

from auth import ensure_token, mint_token, token_file_path  # noqa: E402
from installer import (  # noqa: E402
    MCP_SERVER_KEY,
    bn_plugins_folder,
    bridge_entrypoint,
    collect_python_env,
    ensure_venv,
    make_server_entry,
    plugin_root,
    requirements_file,
    venv_dir,
    venv_exists,
    venv_python,
)


def _say(quiet: bool, msg: str) -> None:
    if not quiet:
        print(msg)


def run(force: bool = False, regen_token: bool = False, quiet: bool = False) -> int:
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

    # Mint an auth token on first setup; --regen-token forces a fresh one.
    # The token file is read on every request by both the plugin and the
    # bridge, so a regen takes effect after the next MCP client restart.
    if regen_token:
        mint_token()
        _say(quiet, f"Regenerated auth token at {token_file_path()}")
    else:
        ensure_token()

    py = venv_python()
    bridge = bridge_entrypoint()

    _say(quiet, "")
    _say(quiet, "Setup complete.")
    _say(quiet, f"  venv python : {py}")
    _say(quiet, f"  bridge      : {bridge}")
    _say(quiet, f"  auth token  : {token_file_path()}")
    _say(quiet, "")
    bn_plugins = bn_plugins_folder()
    if bn_plugins:
        link_target = os.path.join(bn_plugins, "binary_ninja_mcp")
        _say(quiet, "If you haven't already, install this plugin into Binary Ninja's")
        _say(quiet, f"plugins folder ({bn_plugins}). For development, symlink:")
        _say(quiet, f"  mkdir -p {bn_plugins!s}")
        _say(quiet, f"  ln -s {plugin_root()} {link_target}")
        _say(quiet, "(In BN: Plugins -> Open Plugin Folder opens the same directory.)")
    else:
        _say(
            quiet,
            "Install this plugin into Binary Ninja's plugins folder for your platform "
            "(see https://docs.binary.ninja/guide/plugins.html).",
        )
    _say(quiet, "")
    _say(quiet, "To register the bridge with a specific MCP client:")
    _say(quiet, "  python scripts/install_mcp_client.py --list-clients")
    _say(quiet, "  python scripts/install_mcp_client.py --client <Name>")
    _say(quiet, "")
    _say(quiet, "To register only for a single project (or an unsupported client),")
    _say(quiet, "point --config-file at the project's MCP config file, e.g.:")
    _say(quiet, "  python scripts/install_mcp_client.py --config-file <project>/.mcp.json")
    _say(quiet, "")
    _say(quiet, "Or paste this snippet into the config by hand:")
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
        "--regen-token",
        action="store_true",
        help="Replace the existing auth token with a fresh one. "
        "Restart the MCP client(s) to pick up the new token.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress non-error output.",
    )
    args = parser.parse_args()
    return run(force=args.force, regen_token=args.regen_token, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
