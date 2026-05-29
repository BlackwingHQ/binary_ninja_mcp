"""Shared helpers for the setup and per-client installer scripts.

Path layout assumes this file lives at <plugin_root>/plugin/utils/installer.py.
Both `scripts/setup_plugin.py` and `scripts/install_mcp_client.py` import from
here so the two scripts agree on where the venv lives, where the bridge
entrypoint is, and where each supported MCP client keeps its config.
"""

import os
import sys

from .python_detection import (
    copy_python_env,
    create_venv_with_system_python,
    get_python_executable,
)

# Key used inside each MCP client's `mcpServers` map.
MCP_SERVER_KEY = "binary_ninja_mcp"

# Default invocation timeout (seconds) written into the client config.
DEFAULT_TIMEOUT = 1800


def plugin_root() -> str:
    """Return the plugin repo root (two dirs up from this file)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def bridge_entrypoint() -> str:
    return os.path.join(plugin_root(), "bridge", "binja_mcp_bridge.py")


def requirements_file() -> str:
    return os.path.join(plugin_root(), "bridge", "requirements.txt")


def venv_dir() -> str:
    return os.path.join(plugin_root(), ".venv")


def venv_python() -> str:
    """Expected path to the venv's Python interpreter for this platform."""
    if sys.platform == "win32":
        # Always prefer python.exe over any Binary Ninja launcher copied into
        # Scripts/, which does not behave like a normal interpreter on stdio.
        return os.path.join(venv_dir(), "Scripts", "python.exe")
    return os.path.join(venv_dir(), "bin", "python3")


def venv_exists() -> bool:
    """True when the local venv looks usable for MCP stdio."""
    return os.path.exists(venv_python())


def ensure_venv() -> str:
    """Create the local venv if needed and return the path to its python.

    Falls back to a discovered system python if venv creation fails outright
    (matches the old auto-setup behavior, which avoided hard failures during
    plugin load).
    """
    req = requirements_file()
    try:
        py = create_venv_with_system_python(
            venv_dir(), req if os.path.exists(req) else None
        )
        return py if os.path.exists(py) else get_python_executable()
    except Exception:
        return get_python_executable()


def get_client_targets() -> dict[str, tuple[str, str]]:
    """Return the supported MCP client config locations for this platform.

    Each value is `(config_dir, config_filename)`. Clients whose config
    directory does not currently exist on disk are still returned — callers
    decide whether to skip or to create them.
    """
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return {
            "Cline": (
                os.path.join(
                    appdata,
                    "Code",
                    "User",
                    "globalStorage",
                    "saoudrizwan.claude-dev",
                    "settings",
                ),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                os.path.join(
                    appdata,
                    "Code",
                    "User",
                    "globalStorage",
                    "rooveterinaryinc.roo-cline",
                    "settings",
                ),
                "mcp_settings.json",
            ),
            "Claude": (
                os.path.join(appdata, "Claude"),
                "claude_desktop_config.json",
            ),
            "Cursor": (os.path.join(home, ".cursor"), "mcp.json"),
            "Windsurf": (
                os.path.join(home, ".codeium", "windsurf"),
                "mcp_config.json",
            ),
            "Claude Code": (home, ".claude.json"),
            "LM Studio": (os.path.join(home, ".lmstudio"), "mcp.json"),
        }
    if sys.platform == "darwin":
        return {
            "Cline": (
                os.path.join(
                    home,
                    "Library",
                    "Application Support",
                    "Code",
                    "User",
                    "globalStorage",
                    "saoudrizwan.claude-dev",
                    "settings",
                ),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                os.path.join(
                    home,
                    "Library",
                    "Application Support",
                    "Code",
                    "User",
                    "globalStorage",
                    "rooveterinaryinc.roo-cline",
                    "settings",
                ),
                "mcp_settings.json",
            ),
            "Claude": (
                os.path.join(home, "Library", "Application Support", "Claude"),
                "claude_desktop_config.json",
            ),
            "Cursor": (os.path.join(home, ".cursor"), "mcp.json"),
            "Windsurf": (
                os.path.join(home, ".codeium", "windsurf"),
                "mcp_config.json",
            ),
            "Claude Code": (home, ".claude.json"),
            "LM Studio": (os.path.join(home, ".lmstudio"), "mcp.json"),
        }
    if sys.platform == "linux":
        return {
            "Cline": (
                os.path.join(
                    home,
                    ".config",
                    "Code",
                    "User",
                    "globalStorage",
                    "saoudrizwan.claude-dev",
                    "settings",
                ),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                os.path.join(
                    home,
                    ".config",
                    "Code",
                    "User",
                    "globalStorage",
                    "rooveterinaryinc.roo-cline",
                    "settings",
                ),
                "mcp_settings.json",
            ),
            # Claude Desktop is not supported on Linux.
            "Cursor": (os.path.join(home, ".cursor"), "mcp.json"),
            "Windsurf": (
                os.path.join(home, ".codeium", "windsurf"),
                "mcp_config.json",
            ),
            "Claude Code": (home, ".claude.json"),
            "LM Studio": (os.path.join(home, ".lmstudio"), "mcp.json"),
        }
    return {}


def make_server_entry(
    command: str,
    bridge: str,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build the `mcpServers[binary_ninja_mcp]` dict to write into a config."""
    entry: dict[str, object] = {
        "command": command,
        "args": [bridge],
        "timeout": DEFAULT_TIMEOUT,
        "disabled": False,
    }
    if env:
        entry["env"] = dict(env)
    return entry


def collect_python_env() -> dict[str, str]:
    """Return PYTHON* env vars from the caller's environment, if any."""
    env: dict[str, str] = {}
    copy_python_env(env)
    return env
