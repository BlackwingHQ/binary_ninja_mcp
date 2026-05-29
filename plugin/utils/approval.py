"""User-approval gating for sensitive MCP operations.

Two operations — patch and load — can mutate user state (binary contents on
disk, or the set of files Binary Ninja has loaded). Each request for these
operations is gated by:

  1. A persistent allow list, configured via Binary Ninja settings
     (`mcp.patchAllowList` and `mcp.loadAllowList`). Paths are normalized
     with `os.path.realpath` and matched exactly.
  2. An in-process session allow list, populated when the user clicks
     "Approve for this session" in the prompt. Cleared on plugin reload.
  3. A three-button modal dialog (Approve once / Approve for this session /
     Deny) shown on the UI thread.

In headless mode or when no Qt application is available, the prompt step
denies — only paths in the settings allow list are permitted.
"""

import os
from typing import Literal

import binaryninja as bn
from binaryninja.settings import Settings


_session_approved: dict[str, set[str]] = {
    "patch": set(),
    "load": set(),
}

_SETTING_KEYS = {
    "patch": "mcp.patchAllowList",
    "load": "mcp.loadAllowList",
}


def _normalize(path: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(path))
    except Exception:
        return path


def _allowlist(action: str) -> set[str]:
    key = _SETTING_KEYS.get(action)
    if not key:
        return set()
    try:
        raw = Settings().get_string_list(key) or []
    except Exception:
        raw = []
    return {_normalize(p) for p in raw if p}


def _prompt(
    action: str, path: str, details: str
) -> Literal["once", "session", "deny"]:
    """Show the three-button modal on the UI thread and return the choice.

    Returns "deny" if no UI is available (headless run, no QApplication,
    Qt import failure, or any error inside the dialog).
    """
    result: dict[str, str] = {"choice": "deny"}
    try:
        import binaryninjaui  # noqa: F401
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return "deny"

        def _show():
            try:
                box = QMessageBox()
                box.setWindowTitle(f"MCP: Approve {action}?")
                box.setIcon(QMessageBox.Warning)
                box.setText(
                    f"The MCP server is requesting a {action} operation."
                )
                box.setInformativeText(f"File: {path}\n\n{details}")
                once_btn = box.addButton(
                    "Approve once", QMessageBox.AcceptRole
                )
                session_btn = box.addButton(
                    "Approve for this session", QMessageBox.AcceptRole
                )
                deny_btn = box.addButton("Deny", QMessageBox.RejectRole)
                box.setDefaultButton(deny_btn)
                box.exec()
                clicked = box.clickedButton()
                if clicked is once_btn:
                    result["choice"] = "once"
                elif clicked is session_btn:
                    result["choice"] = "session"
                else:
                    result["choice"] = "deny"
            except Exception as e:
                bn.log_error(f"MCP approval dialog error: {e}")
                result["choice"] = "deny"

        bn.execute_on_main_thread_and_wait(_show)
    except Exception as e:
        bn.log_warn(
            f"MCP approval prompt unavailable ({e}); denying by default"
        )
        return "deny"
    return result["choice"]  # type: ignore[return-value]


def require_approval(action: str, path: str | None, details: str = "") -> bool:
    """Gate a sensitive operation. Returns True if the request may proceed.

    Resolution order:
      1. settings allow list
      2. in-process session approvals
      3. UI prompt (deny when unavailable)
    """
    if action not in ("patch", "load"):
        return False
    if not path:
        bn.log_warn(f"MCP {action} denied: no target path")
        return False

    norm = _normalize(path)

    if norm in _allowlist(action):
        bn.log_info(f"MCP {action} allowed by settings: {norm}")
        return True

    if norm in _session_approved[action]:
        bn.log_info(f"MCP {action} allowed by session approval: {norm}")
        return True

    choice = _prompt(action, norm, details)
    if choice == "once":
        bn.log_info(f"MCP {action} approved once for: {norm}")
        return True
    if choice == "session":
        _session_approved[action].add(norm)
        bn.log_info(f"MCP {action} approved for session: {norm}")
        return True
    bn.log_warn(f"MCP {action} denied for: {norm}")
    return False
