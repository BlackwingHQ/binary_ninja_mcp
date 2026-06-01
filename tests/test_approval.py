"""Tests for plugin/utils/approval.py.

Approval gates three sources: settings allow-list, in-process session
approvals, and a UI prompt. Real BN `Settings` and Qt aren't usable in
unit tests, so these tests monkey-patch `_allowlist` (replaces the
settings lookup) and `_prompt` (replaces the Qt dialog).
"""

import os

import pytest

# ---------- _normalize ----------


def test_normalize_makes_absolute(approval_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "file.bin").write_bytes(b"")
    normalized = approval_module._normalize("file.bin")
    assert os.path.isabs(normalized)
    assert normalized.endswith("file.bin")


def test_normalize_resolves_symlink(approval_module, tmp_path):
    target = tmp_path / "real.bin"
    target.write_bytes(b"")
    link = tmp_path / "link.bin"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/user")

    normalized = approval_module._normalize(str(link))
    # realpath should follow the symlink to the target file
    assert normalized == os.path.realpath(str(target))


def test_normalize_collapses_dotdot(approval_module, tmp_path):
    inner = tmp_path / "sub"
    inner.mkdir()
    weird = str(inner / ".." / "file.bin")
    normalized = approval_module._normalize(weird)
    assert ".." not in normalized
    assert normalized == os.path.realpath(str(tmp_path / "file.bin"))


def test_normalize_handles_unresolvable_input_without_raising(approval_module):
    # _normalize swallows exceptions and returns the input. Hard to provoke
    # in-process; just verify a plain non-existent path still normalizes.
    out = approval_module._normalize("/nonexistent/path/here")
    assert isinstance(out, str)
    assert os.path.isabs(out)


# ---------- require_approval: unknown action / missing path ----------


def test_require_approval_rejects_unknown_action(approval_module, tmp_path):
    assert approval_module.require_approval("delete", str(tmp_path / "x")) is False


def test_require_approval_rejects_missing_path(approval_module, clear_session_approvals):
    assert approval_module.require_approval("patch", None) is False
    assert approval_module.require_approval("patch", "") is False


# ---------- require_approval: settings allow-list ----------


def test_settings_allowlist_permits(
    approval_module, tmp_path, clear_session_approvals, monkeypatch
):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")
    norm = approval_module._normalize(str(target))

    monkeypatch.setattr(approval_module, "_allowlist", lambda action: {norm})

    # Should NOT prompt — settings allow it directly
    monkeypatch.setattr(
        approval_module,
        "_prompt",
        lambda *args, **kwargs: pytest.fail("prompt should not run"),
    )

    assert approval_module.require_approval("patch", str(target)) is True


def test_settings_allowlist_isolated_per_action(
    approval_module, tmp_path, clear_session_approvals, monkeypatch
):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")
    norm = approval_module._normalize(str(target))

    monkeypatch.setattr(
        approval_module,
        "_allowlist",
        lambda action: {norm} if action == "patch" else set(),
    )
    monkeypatch.setattr(approval_module, "_prompt", lambda *args, **kwargs: "deny")

    assert approval_module.require_approval("patch", str(target)) is True
    assert approval_module.require_approval("load", str(target)) is False


# ---------- require_approval: session allow-list ----------


def test_session_approval_grants_subsequent_calls(
    approval_module, tmp_path, clear_session_approvals, monkeypatch
):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")

    monkeypatch.setattr(approval_module, "_allowlist", lambda action: set())

    prompt_calls = []

    def _prompt(action, path, details):
        prompt_calls.append((action, path))
        return "session"

    monkeypatch.setattr(approval_module, "_prompt", _prompt)

    # First call prompts, returns "session"
    assert approval_module.require_approval("patch", str(target)) is True
    assert len(prompt_calls) == 1

    # Subsequent calls hit the session cache — no further prompt
    assert approval_module.require_approval("patch", str(target)) is True
    assert len(prompt_calls) == 1


def test_session_approval_isolated_per_action(
    approval_module, tmp_path, clear_session_approvals, monkeypatch
):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")

    monkeypatch.setattr(approval_module, "_allowlist", lambda action: set())

    # First grant session approval for patch
    monkeypatch.setattr(approval_module, "_prompt", lambda *a, **k: "session")
    assert approval_module.require_approval("patch", str(target)) is True

    # Switch prompt to deny; load should still prompt since session is per-action
    monkeypatch.setattr(approval_module, "_prompt", lambda *a, **k: "deny")
    assert approval_module.require_approval("load", str(target)) is False
    # patch still allowed from session
    monkeypatch.setattr(
        approval_module,
        "_prompt",
        lambda *args, **kwargs: pytest.fail("session approval should bypass prompt"),
    )
    assert approval_module.require_approval("patch", str(target)) is True


# ---------- require_approval: deny ----------


def test_deny_returns_false(approval_module, tmp_path, clear_session_approvals, monkeypatch):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")
    monkeypatch.setattr(approval_module, "_allowlist", lambda action: set())
    monkeypatch.setattr(approval_module, "_prompt", lambda *a, **k: "deny")

    assert approval_module.require_approval("patch", str(target)) is False
    # Denial does NOT add to session cache
    norm = approval_module._normalize(str(target))
    assert norm not in approval_module._session_approved["patch"]


# ---------- require_approval: path normalization in checks ----------


def test_settings_check_uses_normalized_path(
    approval_module, tmp_path, clear_session_approvals, monkeypatch
):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")
    norm = approval_module._normalize(str(target))

    monkeypatch.setattr(approval_module, "_allowlist", lambda action: {norm})
    monkeypatch.setattr(
        approval_module,
        "_prompt",
        lambda *args, **kwargs: pytest.fail("prompt should not run"),
    )

    # Pass an un-normalized path — the function must still match it
    # against the normalized allow-list
    sub = tmp_path / "sub"
    sub.mkdir()
    weird = str(sub / ".." / "binary.bin")
    assert approval_module.require_approval("patch", weird) is True


def test_session_check_uses_normalized_path(
    approval_module, tmp_path, clear_session_approvals, monkeypatch
):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")
    monkeypatch.setattr(approval_module, "_allowlist", lambda action: set())

    # Prime the session cache by approving once
    monkeypatch.setattr(approval_module, "_prompt", lambda *a, **k: "session")
    assert approval_module.require_approval("patch", str(target)) is True

    # A differently-shaped path that normalizes to the same target
    # should match the session approval without prompting
    monkeypatch.setattr(
        approval_module,
        "_prompt",
        lambda *args, **kwargs: pytest.fail("session match should bypass prompt"),
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    weird = str(sub / ".." / "binary.bin")
    assert approval_module.require_approval("patch", weird) is True


# ---------- require_approval: action/path forwarded to prompt ----------


def test_prompt_receives_normalized_path(
    approval_module, tmp_path, clear_session_approvals, monkeypatch
):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"")
    monkeypatch.setattr(approval_module, "_allowlist", lambda action: set())

    captured = {}

    def _prompt(action, path, details):
        captured["action"] = action
        captured["path"] = path
        captured["details"] = details
        return "deny"

    monkeypatch.setattr(approval_module, "_prompt", _prompt)

    sub = tmp_path / "sub"
    sub.mkdir()
    weird = str(sub / ".." / "binary.bin")
    approval_module.require_approval("patch", weird, details="size=4 bytes")

    assert captured["action"] == "patch"
    assert captured["path"] == approval_module._normalize(str(target))
    assert captured["details"] == "size=4 bytes"
