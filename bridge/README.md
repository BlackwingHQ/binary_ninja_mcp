# Binary Ninja MCP Bridge

The supported MCP bridge is `binja_mcp_bridge.py`.

It exposes the plugin's HTTP API over MCP stdio and reads the shared bearer
token from `<plugin_root>/.mcp_auth_token` on each request.

## Setup

Run the repository-level setup scripts from the plugin root:

```bash
python scripts/setup_plugin.py
python scripts/install_mcp_client.py --list-clients
python scripts/install_mcp_client.py --client "Claude Code"
```

For an unsupported MCP client, `scripts/setup_plugin.py` prints a ready-to-paste
MCP server entry that invokes this Python bridge with the local virtualenv.

## Files

- `binja_mcp_bridge.py`: primary MCP stdio bridge.
- `requirements.txt`: Python runtime dependencies installed into the local
  `.venv` by `scripts/setup_plugin.py`.
