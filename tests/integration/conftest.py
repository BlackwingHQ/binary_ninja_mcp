"""Fixtures for live Binary Ninja integration tests.

These tests talk to the plugin's HTTP server over localhost, so they
require a running Binary Ninja with the plugin loaded and a fixture
binary open. Setup:

    bash tests/integration/fixtures/build.sh
    # Open `tests/integration/fixtures/constructs` in Binary Ninja
    # and start the MCP server (left-bottom corner button).
    .venv/bin/python -m pytest tests/integration/

The unit-test default run ignores this directory (see
`tests/pytest.ini`), so a missing Binary Ninja doesn't break the lint
+ unit-test pipeline.

If the server isn't reachable, the `binja_session` fixture skips the
suite rather than failing — that way an absent Binary Ninja is visibly
"not run" rather than "broken".
"""

import os
from pathlib import Path

import pytest
import requests

DEFAULT_URL = "http://localhost:9009"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_NAME = "constructs"


def _read_auth_token() -> str | None:
    path = REPO_ROOT / ".mcp_auth_token"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


@pytest.fixture(scope="session")
def base_url() -> str:
    """Override with `BINJA_MCP_URL=http://host:port` for non-default ports."""
    return os.environ.get("BINJA_MCP_URL", DEFAULT_URL).rstrip("/")


@pytest.fixture(scope="session")
def auth_token() -> str:
    tok = _read_auth_token()
    if not tok:
        pytest.skip(
            f"no auth token at {REPO_ROOT / '.mcp_auth_token'} — run "
            "`python scripts/setup_plugin.py` first."
        )
    return tok


def _current_filename(session: requests.Session, base_url: str) -> str:
    r = session.get(f"{base_url}/status", timeout=5)
    r.raise_for_status()
    return r.json().get("filename") or ""


def _select_fixture_if_open(session: requests.Session, base_url: str) -> str | None:
    """If the fixture binary is open in BN but not the active view, switch
    to it via /selectBinary. Returns a selector that worked, or None."""
    r = session.get(f"{base_url}/binaries", timeout=5)
    r.raise_for_status()
    for entry in r.json().get("binaries", []):
        basename = entry.get("basename") or ""
        if FIXTURE_NAME not in basename:
            continue
        # Try each selector the server offered until one sticks.
        for selector in entry.get("selectors") or [basename]:
            sel = session.get(f"{base_url}/selectBinary", params={"view": selector}, timeout=5)
            if sel.ok and sel.json().get("status") == "ok":
                return selector
    return None


@pytest.fixture(scope="session")
def binja_session(base_url: str, auth_token: str) -> requests.Session:
    """Authenticated `requests.Session` pinned to the live BN server,
    with the `constructs` fixture confirmed as the active view.

    BN's plugin keeps whatever binary was first selected as "current"
    even after the user switches tabs in the UI — `/selectBinary` is
    the explicit way to retarget. If the fixture is open but inactive,
    this switches to it; otherwise the suite is skipped with an
    actionable message. Skips (not failures) for: server unreachable,
    auth rejected, fixture not open.
    """
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {auth_token}"
    try:
        r = s.get(f"{base_url}/status", timeout=2)
    except requests.RequestException as e:
        pytest.skip(f"Binary Ninja MCP server not reachable at {base_url}: {e}")
    if r.status_code == 401:
        pytest.skip(
            "auth token rejected by the server — the value in "
            ".mcp_auth_token does not match what BN is using. "
            "Restart Binary Ninja or re-run `setup_plugin.py --regen-token`."
        )
    r.raise_for_status()

    if FIXTURE_NAME not in (r.json().get("filename") or ""):
        selector = _select_fixture_if_open(s, base_url)
        if selector is None:
            pytest.skip(
                f"the `{FIXTURE_NAME}` fixture binary is not open in Binary "
                f"Ninja. Open `tests/integration/fixtures/{FIXTURE_NAME}` in "
                "BN and retry."
            )
        active = _current_filename(s, base_url)
        if FIXTURE_NAME not in active:
            pytest.skip(
                f"tried to switch to `{FIXTURE_NAME}` via /selectBinary "
                f"(selector={selector!r}) but /status still reports "
                f"filename={active!r}."
            )
    return s
