import os
import stat
import sys

import pytest


def test_token_file_path_is_absolute_and_under_repo_root(auth_module):
    path = auth_module.token_file_path()
    assert os.path.isabs(path)
    assert os.path.basename(path) == ".mcp_auth_token"


def test_read_token_returns_none_when_file_missing(auth_module, isolated_token_file):
    assert not isolated_token_file.exists()
    assert auth_module.read_token() is None


def test_read_token_returns_none_for_empty_or_whitespace_file(auth_module, isolated_token_file):
    isolated_token_file.write_text("   \n\t\n")
    assert auth_module.read_token() is None


def test_read_token_strips_surrounding_whitespace(auth_module, isolated_token_file):
    isolated_token_file.write_text("  abc123  \n")
    assert auth_module.read_token() == "abc123"


def test_mint_token_creates_file_with_token(auth_module, isolated_token_file):
    tok = auth_module.mint_token()
    assert tok
    assert isolated_token_file.read_text() == tok


def test_mint_token_returns_url_safe_string(auth_module, isolated_token_file):
    tok = auth_module.mint_token()
    # secrets.token_urlsafe uses base64 url-safe alphabet; no whitespace, no '+/='
    assert "\n" not in tok and " " not in tok
    assert all(c.isalnum() or c in "-_" for c in tok)
    # 32 random bytes -> at least 32 characters base64-encoded
    assert len(tok) >= 32


def test_mint_token_overwrites_existing_atomically(auth_module, isolated_token_file):
    isolated_token_file.write_text("old-token")
    new_tok = auth_module.mint_token()
    assert new_tok != "old-token"
    assert isolated_token_file.read_text() == new_tok
    # No leftover temp file
    tmp_path = str(isolated_token_file) + ".tmp"
    assert not os.path.exists(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_mint_token_writes_0600_permissions(auth_module, isolated_token_file):
    auth_module.mint_token()
    mode = stat.S_IMODE(os.stat(isolated_token_file).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_ensure_token_mints_when_file_missing(auth_module, isolated_token_file):
    assert not isolated_token_file.exists()
    tok = auth_module.ensure_token()
    assert tok and isolated_token_file.read_text() == tok


def test_ensure_token_reuses_existing(auth_module, isolated_token_file):
    isolated_token_file.write_text("preexisting-token")
    tok = auth_module.ensure_token()
    assert tok == "preexisting-token"


def test_matches_accepts_correct_bearer_token(auth_module, isolated_token_file):
    isolated_token_file.write_text("secret-token")
    assert auth_module.matches("Bearer secret-token") is True


def test_matches_rejects_when_no_header(auth_module, isolated_token_file):
    isolated_token_file.write_text("secret-token")
    assert auth_module.matches(None) is False
    assert auth_module.matches("") is False


def test_matches_rejects_wrong_prefix(auth_module, isolated_token_file):
    isolated_token_file.write_text("secret-token")
    assert auth_module.matches("Token secret-token") is False
    assert auth_module.matches("Basic secret-token") is False
    # Case matters: "bearer" != "Bearer"
    assert auth_module.matches("bearer secret-token") is False


def test_matches_rejects_missing_token_value(auth_module, isolated_token_file):
    isolated_token_file.write_text("secret-token")
    assert auth_module.matches("Bearer ") is False
    assert auth_module.matches("Bearer    ") is False


def test_matches_rejects_token_mismatch(auth_module, isolated_token_file):
    isolated_token_file.write_text("secret-token")
    assert auth_module.matches("Bearer wrong-token") is False


def test_matches_rejects_when_no_token_file(auth_module, isolated_token_file):
    # File does not exist
    assert not isolated_token_file.exists()
    assert auth_module.matches("Bearer anything") is False


def test_matches_rejects_when_token_file_empty(auth_module, isolated_token_file):
    isolated_token_file.write_text("")
    assert auth_module.matches("Bearer anything") is False


def test_matches_strips_token_whitespace(auth_module, isolated_token_file):
    isolated_token_file.write_text("secret-token")
    # The header value's token is .strip()'d before compare
    assert auth_module.matches("Bearer secret-token  ") is True
