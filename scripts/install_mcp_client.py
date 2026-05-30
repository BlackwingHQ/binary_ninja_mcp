#!/usr/bin/env python3
"""Add or remove the Binary Ninja MCP entry in a single MCP client config.

Run `scripts/setup_plugin.py` first — this script will refuse if the local
venv is missing.

Examples:
  python scripts/install_mcp_client.py --list-clients
  python scripts/install_mcp_client.py --client "Claude Code"
  python scripts/install_mcp_client.py --client Cursor --uninstall
  python scripts/install_mcp_client.py --config-file ~/some/mcp.json
"""

import argparse
import json
import os
import sys

# Import the leaf modules under plugin/utils directly. Going through the
# `plugin` package would run `plugin/__init__.py`, which imports binaryninja
# and only works inside Binary Ninja itself.
_HERE = os.path.dirname(os.path.realpath(__file__))
_PLUGIN_ROOT = os.path.dirname(_HERE)
_UTILS = os.path.join(_PLUGIN_ROOT, "plugin", "utils")
if _UTILS not in sys.path:
    sys.path.insert(0, _UTILS)

from installer import (  # noqa: E402
    MCP_SERVER_KEY,
    bridge_entrypoint,
    collect_python_env,
    get_client_targets,
    make_server_entry,
    venv_exists,
    venv_python,
)


def _load_config(path: str) -> dict:
    """Read an MCP config file, returning {} for missing/empty files.

    Raises ValueError for malformed JSON so the caller can refuse to clobber.
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = f.read().strip()
    if not data:
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"existing config at {path} is not valid JSON: {e}")


def _write_config(path: str, config: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def list_clients() -> int:
    targets = get_client_targets()
    if not targets:
        print(f"No supported MCP clients on platform {sys.platform!r}.")
        return 0
    name_width = max(len(n) for n in targets)
    print(f"{'Client'.ljust(name_width)}  Detected  Config path")
    print(f"{'-' * name_width}  --------  -----------")
    for name, (cfg_dir, cfg_file) in targets.items():
        cfg_path = os.path.join(cfg_dir, cfg_file)
        detected = "yes" if os.path.exists(cfg_dir) else "no "
        print(f"{name.ljust(name_width)}  {detected}       {cfg_path}")
    return 0


def _resolve_target(client: str | None, config_file: str | None) -> tuple[str, str]:
    """Map (--client | --config-file) into (label, absolute config path).

    Exits via SystemExit on bad input.
    """
    if bool(client) == bool(config_file):
        raise SystemExit("Specify exactly one of --client <name> or --config-file <path>.")

    if config_file:
        return ("custom", os.path.abspath(os.path.expanduser(config_file)))

    targets = get_client_targets()
    if client not in targets:
        known = ", ".join(sorted(targets)) or "(none on this platform)"
        raise SystemExit(
            f"Unknown client {client!r}. Known clients: {known}. "
            "Run with --list-clients for details."
        )
    cfg_dir, cfg_file = targets[client]
    return (client, os.path.join(cfg_dir, cfg_file))


def install(label: str, config_path: str, quiet: bool) -> int:
    if not venv_exists():
        print(
            "ERROR: local venv is missing. Run `python scripts/setup_plugin.py` first.",
            file=sys.stderr,
        )
        return 1

    try:
        config = _load_config(config_path)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not isinstance(config, dict):
        print(
            f"ERROR: top-level JSON in {config_path} is not an object; refusing to overwrite.",
            file=sys.stderr,
        )
        return 1

    config.setdefault("mcpServers", {})
    servers = config["mcpServers"]
    if not isinstance(servers, dict):
        print(
            f"ERROR: 'mcpServers' in {config_path} is not an object; refusing to overwrite.",
            file=sys.stderr,
        )
        return 1

    # Preserve any custom env overrides the user already had for our key.
    existing_env: dict[str, str] = {}
    if MCP_SERVER_KEY in servers and isinstance(servers[MCP_SERVER_KEY], dict):
        prior = servers[MCP_SERVER_KEY].get("env")
        if isinstance(prior, dict):
            existing_env = {str(k): str(v) for k, v in prior.items()}

    env = collect_python_env()
    env.update(existing_env)

    servers[MCP_SERVER_KEY] = make_server_entry(venv_python(), bridge_entrypoint(), env)

    _write_config(config_path, config)
    if not quiet:
        print(f"Installed Binary Ninja MCP server into {label}.")
        print(f"  Config: {config_path}")
        print("  Restart the client to pick up the change.")
    return 0


def uninstall(label: str, config_path: str, quiet: bool) -> int:
    if not os.path.exists(config_path):
        if not quiet:
            print(f"No config at {config_path}; nothing to uninstall.")
        return 0
    try:
        config = _load_config(config_path)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or MCP_SERVER_KEY not in servers:
        if not quiet:
            print(f"{MCP_SERVER_KEY} entry not present in {config_path}; nothing to do.")
        return 0
    del servers[MCP_SERVER_KEY]
    _write_config(config_path, config)
    if not quiet:
        print(f"Removed Binary Ninja MCP server from {label}.")
        print(f"  Config: {config_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add or remove the Binary Ninja MCP entry in one MCP client config.",
    )
    parser.add_argument(
        "--list-clients",
        action="store_true",
        help="List supported MCP clients and where their configs live on this system.",
    )
    parser.add_argument(
        "--client",
        metavar="NAME",
        help='Target a known client by name (e.g. "Claude Code", "Cursor"). '
        "Use --list-clients to see options.",
    )
    parser.add_argument(
        "--config-file",
        metavar="PATH",
        help="Target an arbitrary MCP-style JSON config file (for unsupported clients).",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the entry from the target config instead of adding it.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress non-error output.")
    args = parser.parse_args()

    if args.list_clients:
        return list_clients()

    label, config_path = _resolve_target(args.client, args.config_file)
    if args.uninstall:
        return uninstall(label, config_path, args.quiet)
    return install(label, config_path, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
