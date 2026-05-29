"""Token-based auth for the MCP HTTP server.

The token lives in `<plugin_root>/.mcp_auth_token`. Both the in-BN HTTP
server and the bridge process read from that file directly:

  - The plugin re-reads on every request, so regenerating the token via
    `setup_plugin.py --regen-token` takes effect without restarting BN.
  - The bridge re-reads on every request too, so a regen only requires
    restarting the MCP client process (which respawns the bridge).

The MCP client config never carries the token — keeping it out of those
files avoids accidentally exposing the secret if a user shares a config.

The token file is created 0600 on Unix. Windows relies on the home
directory ACL.
"""

import hmac
import os
import secrets

_TOKEN_FILE_NAME = ".mcp_auth_token"
_BEARER_PREFIX = "Bearer "


def _plugin_root() -> str:
    # plugin/utils/auth.py -> plugin/utils -> plugin -> <plugin_root>
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def token_file_path() -> str:
    """Absolute path to the token file."""
    return os.path.join(_plugin_root(), _TOKEN_FILE_NAME)


def read_token() -> str | None:
    """Return the current token, or None if the file is missing/empty."""
    try:
        with open(token_file_path(), encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        return None
    return tok or None


def mint_token() -> str:
    """Generate a fresh token, write it atomically with 0600, and return it."""
    tok = secrets.token_urlsafe(32)
    path = token_file_path()
    tmp = path + ".tmp"
    # O_CREAT|O_TRUNC + 0600 so the secret never lands world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, tok.encode("utf-8"))
    finally:
        os.close(fd)
    # Atomic rename so a concurrent reader never sees a partial file.
    os.replace(tmp, path)
    return tok


def ensure_token() -> str:
    """Return the current token, minting one first if the file is missing."""
    existing = read_token()
    if existing:
        return existing
    return mint_token()


def matches(authorization_header: str | None) -> bool:
    """Constant-time check that the header carries the right Bearer token.

    Returns False on any of: no header, wrong prefix, no/empty token file,
    or value mismatch. Never raises.
    """
    expected = read_token()
    if not expected or not authorization_header:
        return False
    if not authorization_header.startswith(_BEARER_PREFIX):
        return False
    presented = authorization_header[len(_BEARER_PREFIX):].strip()
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)
