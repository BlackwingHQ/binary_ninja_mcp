"""Shared pytest fixtures.

Setup (one-time, per workstation):

    python3 -m venv .venv
    .venv/bin/pip install -r tests/requirements.txt
    python3 scripts/install_binaryninja_pth.py .venv   # makes `import binaryninja` work
    .venv/bin/python -m pytest tests/

Tests must NOT be run with `pytest` (no path) from the repo root:
pytest would treat the repo as a Python package because of the
repo-root `__init__.py` and import it during collection, which runs
`bn.PluginCommand.register` — that only works inside the Binary Ninja
host. Pinning the config under `tests/pytest.ini` keeps the rootdir at
this directory and side-steps the issue when an explicit `tests/` path
is passed (or when run from inside this directory).

Helper modules under `plugin/utils` and `bridge/binja_mcp_bridge.py`
are loaded by file path rather than as part of the `plugin` / `bridge`
packages, for the same reason: bypassing the package init while still
giving each module its real `binaryninja` import.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _require_binaryninja() -> None:
    """Fail fast with a setup hint if the BN Python package isn't visible."""
    if importlib.util.find_spec("binaryninja") is not None:
        return
    raise RuntimeError(
        "`import binaryninja` failed in this interpreter "
        f"({sys.executable}).\n"
        "If you're running tests in a virtualenv, drop a `binaryninja.pth` "
        "into the venv's site-packages — see "
        "scripts/install_binaryninja_pth.py."
    )


_require_binaryninja()


def _load_module(name: str, relpath: str):
    """Import a single file as a top-level module, bypassing any package
    `__init__.py` that would otherwise be executed first."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def auth_module():
    return _load_module("_test_auth", "plugin/utils/auth.py")


@pytest.fixture(scope="session")
def address_module():
    return _load_module("_test_address", "plugin/utils/address.py")


@pytest.fixture(scope="session")
def string_module():
    return _load_module("_test_string_utils", "plugin/utils/string_utils.py")


@pytest.fixture(scope="session")
def approval_module():
    return _load_module("_test_approval", "plugin/utils/approval.py")


@pytest.fixture(scope="session")
def bridge_module():
    return _load_module("_test_bridge", "bridge/binja_mcp_bridge.py")


@pytest.fixture
def isolated_token_file(tmp_path, monkeypatch, auth_module):
    """Redirect auth.token_file_path() at a temp file so tests don't
    clobber the real `.mcp_auth_token` in the repo root."""
    token_path = tmp_path / ".mcp_auth_token"
    monkeypatch.setattr(auth_module, "token_file_path", lambda: str(token_path))
    return token_path


@pytest.fixture
def clear_session_approvals(approval_module):
    """Reset the in-process session approval sets so tests don't leak state."""
    for key in approval_module._session_approved:
        approval_module._session_approved[key].clear()
    yield approval_module._session_approved
    for key in approval_module._session_approved:
        approval_module._session_approved[key].clear()
