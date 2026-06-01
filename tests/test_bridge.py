"""Tests for bridge/binja_mcp_bridge.py — pure formatters and the
thin response-shaping wrappers around `requests`.

The bridge re-reads its auth token file and calls `requests.{get,post,
delete}` directly. Tests patch the module-level `requests` attribute
with a fake that records calls and returns canned responses.
"""

import json

import pytest

# ---------- _read_auth_token ----------


def test_read_auth_token_missing_file(bridge_module, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "_TOKEN_FILE", str(tmp_path / "missing"))
    assert bridge_module._read_auth_token() is None


def test_read_auth_token_empty_file(bridge_module, tmp_path, monkeypatch):
    p = tmp_path / "tok"
    p.write_text("   \n")
    monkeypatch.setattr(bridge_module, "_TOKEN_FILE", str(p))
    assert bridge_module._read_auth_token() is None


def test_read_auth_token_returns_stripped_value(bridge_module, tmp_path, monkeypatch):
    p = tmp_path / "tok"
    p.write_text("  abc123\n")
    monkeypatch.setattr(bridge_module, "_TOKEN_FILE", str(p))
    assert bridge_module._read_auth_token() == "abc123"


def test_auth_headers_present_when_token_exists(bridge_module, tmp_path, monkeypatch):
    p = tmp_path / "tok"
    p.write_text("secret")
    monkeypatch.setattr(bridge_module, "_TOKEN_FILE", str(p))
    assert bridge_module._auth_headers() == {"Authorization": "Bearer secret"}


def test_auth_headers_empty_when_no_token(bridge_module, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "_TOKEN_FILE", str(tmp_path / "missing"))
    assert bridge_module._auth_headers() == {}


# ---------- _format_undo ----------


def test_format_undo_with_more_available(bridge_module):
    assert bridge_module._format_undo({"can_undo": True}) == "Undone (undo available)"


def test_format_undo_with_none_left(bridge_module):
    assert bridge_module._format_undo({"can_undo": False}) == "Undone (no undo left)"


def test_format_undo_drops_missing_field(bridge_module):
    assert bridge_module._format_undo({}) == "Undone"


def test_format_undo_ignores_non_bool_value(bridge_module):
    # The formatter only reacts to literal True / False — other values
    # (None, strings, numbers) produce no suffix.
    assert bridge_module._format_undo({"can_undo": None}) == "Undone"


# ---------- _format_tag ----------


def test_format_tag_full(bridge_module):
    tag = {"icon": "🐛", "kind": "user", "type": "Bug", "data": "off by one"}
    assert bridge_module._format_tag(tag) == "[user] 🐛  Bug: off by one"


def test_format_tag_without_data(bridge_module):
    tag = {"icon": "🔖", "kind": "auto", "type": "Bookmark"}
    assert bridge_module._format_tag(tag) == "[auto] 🔖  Bookmark"


def test_format_tag_with_missing_fields(bridge_module):
    assert bridge_module._format_tag({}) == "[?]   ?"


# ---------- _format_var_ref ----------


def test_format_var_ref_with_snippet(bridge_module):
    ref = {"address": "0x401020", "il_type": "hlil", "hlil": "x = y + 1"}
    assert bridge_module._format_var_ref(ref) == "0x401020  [hlil]  x = y + 1"


def test_format_var_ref_without_snippet(bridge_module):
    ref = {"address": "0x401020", "il_type": "mlil"}
    assert bridge_module._format_var_ref(ref) == "0x401020  [mlil]"


def test_format_var_ref_with_missing_fields(bridge_module):
    assert bridge_module._format_var_ref({}) == "?  [?]"


# ---------- _normalize_identifier_input ----------


def test_normalize_identifier_input_single(bridge_module):
    assert bridge_module._normalize_identifier_input("main") == ["main"]


def test_normalize_identifier_input_comma_separated(bridge_module):
    assert bridge_module._normalize_identifier_input("a, b ,c") == ["a", "b", "c"]


def test_normalize_identifier_input_semicolon_separated(bridge_module):
    assert bridge_module._normalize_identifier_input("a;b;c") == ["a", "b", "c"]


def test_normalize_identifier_input_mixed_separators(bridge_module):
    assert bridge_module._normalize_identifier_input("a,b;c , d") == ["a", "b", "c", "d"]


def test_normalize_identifier_input_drops_empty_tokens(bridge_module):
    assert bridge_module._normalize_identifier_input(",,a,,,b,") == ["a", "b"]


def test_normalize_identifier_input_accepts_list(bridge_module):
    assert bridge_module._normalize_identifier_input(["a", "b,c"]) == ["a", "b", "c"]


def test_normalize_identifier_input_accepts_tuple_and_set(bridge_module):
    assert bridge_module._normalize_identifier_input(("a",)) == ["a"]
    # Sets are unordered, but a single-element set has a stable result
    assert bridge_module._normalize_identifier_input({"a"}) == ["a"]


def test_normalize_identifier_input_drops_none_in_list(bridge_module):
    assert bridge_module._normalize_identifier_input(["a", None, "b"]) == ["a", "b"]


def test_normalize_identifier_input_unsupported_type_returns_empty(bridge_module):
    assert bridge_module._normalize_identifier_input(42) == []
    assert bridge_module._normalize_identifier_input(None) == []


# ---------- HTTP-wrapping helpers ----------


class FakeResponse:
    def __init__(self, status=200, text="", json_data=None, raise_json=False):
        self.status_code = status
        self.text = text
        self._json = json_data
        self._raise_json = raise_json
        self.encoding = "utf-8"

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        if self._raise_json or (self._json is None and not self.text):
            raise ValueError("no json")
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class FakeRequests:
    """Records every call and returns the next queued response."""

    def __init__(self):
        self.calls = []
        self.responses = []
        self.exc = None

    def queue(self, *responses):
        self.responses.extend(responses)

    def _next(self):
        if self.exc is not None:
            raise self.exc
        return self.responses.pop(0)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers, timeout, None))
        return self._next()

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, None, headers, timeout, {"json": json, "data": data}))
        return self._next()

    def delete(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("DELETE", url, params, headers, timeout, None))
        return self._next()


@pytest.fixture
def fake_requests(bridge_module, tmp_path, monkeypatch):
    # Give the bridge a known token so headers are deterministic
    tok = tmp_path / "tok"
    tok.write_text("test-token")
    monkeypatch.setattr(bridge_module, "_TOKEN_FILE", str(tok))

    fake = FakeRequests()
    monkeypatch.setattr(bridge_module, "requests", fake)
    return fake


# ---------- safe_get ----------


def test_safe_get_returns_lines_on_ok(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, text="alpha\nbeta\n"))
    out = bridge_module.safe_get("methods", {"a": 1})
    assert out == ["alpha", "beta"]
    method, url, params, headers, timeout, _ = fake_requests.calls[0]
    assert method == "GET"
    assert url.endswith("/methods")
    assert params == {"a": 1}
    assert headers == {"Authorization": "Bearer test-token"}
    assert timeout == bridge_module.DEFAULT_HTTP_TIMEOUT_SECONDS


def test_safe_get_returns_error_string_on_non_ok(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(500, text="boom"))
    out = bridge_module.safe_get("methods")
    assert out == ["Error 500: boom"]


def test_safe_get_returns_request_failed_on_exception(bridge_module, fake_requests):
    fake_requests.exc = ConnectionError("connection refused")
    out = bridge_module.safe_get("methods")
    assert out == ["Request failed: connection refused"]


def test_safe_get_timeout_none_passes_no_timeout(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, text=""))
    bridge_module.safe_get("methods", timeout=None)
    _, _, _, _, timeout, _ = fake_requests.calls[0]
    assert timeout is None


# ---------- get_json ----------


def test_get_json_returns_parsed_dict_on_ok(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, json_data={"hello": "world"}))
    assert bridge_module.get_json("status") == {"hello": "world"}


def test_get_json_returns_error_with_status_on_non_ok_json(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(404, json_data={"detail": "not found"}))
    out = bridge_module.get_json("missing")
    # When the body lacks an "error" key, the wrapper replaces the dict
    # with `{"error": str(original)}` (and then adds the status), so the
    # original keys are NOT preserved.
    assert out["status"] == 404
    assert "detail" in out["error"]
    assert "not found" in out["error"]


def test_get_json_preserves_existing_error_field(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(400, json_data={"error": "bad input"}))
    out = bridge_module.get_json("foo")
    assert out["error"] == "bad input"
    assert out["status"] == 400


def test_get_json_returns_synthesized_error_when_body_not_json(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(500, text="internal error", raise_json=True))
    assert bridge_module.get_json("foo") == {"error": "Error 500: internal error"}


def test_get_json_returns_error_on_request_exception(bridge_module, fake_requests):
    fake_requests.exc = TimeoutError("slow")
    assert bridge_module.get_json("foo") == {"error": "Request failed: slow"}


# ---------- post_json ----------


def test_post_json_sends_payload_as_json(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, json_data={"ok": True}))
    out = bridge_module.post_json("act", {"foo": "bar"})
    assert out == {"ok": True}
    method, url, _, headers, _, body = fake_requests.calls[0]
    assert method == "POST"
    assert url.endswith("/act")
    assert body["json"] == {"foo": "bar"}
    assert headers == {"Authorization": "Bearer test-token"}


def test_post_json_default_payload_is_empty_dict(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, json_data={}))
    bridge_module.post_json("act")
    _, _, _, _, _, body = fake_requests.calls[0]
    assert body["json"] == {}


def test_post_json_non_ok_returns_error_dict(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(422, json_data={"detail": "bad"}))
    out = bridge_module.post_json("act", {"x": 1})
    assert out["status"] == 422
    assert "error" in out


def test_post_json_exception_returns_request_failed(bridge_module, fake_requests):
    fake_requests.exc = OSError("nope")
    assert bridge_module.post_json("act", {}) == {"error": "Request failed: nope"}


# ---------- get_text ----------


def test_get_text_returns_raw_body_on_ok(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, text="alpha\nbeta"))
    assert bridge_module.get_text("dump") == "alpha\nbeta"


def test_get_text_returns_error_string_on_non_ok(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(500, text="oops"))
    assert bridge_module.get_text("dump") == "Error 500: oops"


def test_get_text_exception_returns_request_failed(bridge_module, fake_requests):
    fake_requests.exc = RuntimeError("net down")
    assert bridge_module.get_text("dump") == "Request failed: net down"


# ---------- safe_post ----------


def test_safe_post_sends_dict_as_form_data(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, text="ok\n"))
    out = bridge_module.safe_post("act", {"k": "v"})
    assert out == "ok"
    _, _, _, _, _, body = fake_requests.calls[0]
    assert body["data"] == {"k": "v"}
    assert body["json"] is None


def test_safe_post_sends_str_as_utf8_body(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, text=""))
    bridge_module.safe_post("act", "raw payload")
    _, _, _, _, _, body = fake_requests.calls[0]
    assert body["data"] == b"raw payload"


def test_safe_post_returns_error_on_non_ok(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(400, text="bad"))
    assert bridge_module.safe_post("act", {}) == "Error 400: bad"


def test_safe_post_exception(bridge_module, fake_requests):
    fake_requests.exc = ConnectionError("refused")
    assert bridge_module.safe_post("act", {}) == "Request failed: refused"


# ---------- safe_delete ----------


def test_safe_delete_returns_text_on_ok(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, text="deleted\n"))
    out = bridge_module.safe_delete("thing", {"id": "1"})
    assert out == "deleted"
    method, url, params, headers, _, _ = fake_requests.calls[0]
    assert method == "DELETE"
    assert url.endswith("/thing")
    assert params == {"id": "1"}
    assert headers == {"Authorization": "Bearer test-token"}


def test_safe_delete_default_params_is_empty_dict(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(200, text=""))
    bridge_module.safe_delete("thing")
    _, _, params, _, _, _ = fake_requests.calls[0]
    assert params == {}


def test_safe_delete_non_ok_returns_error_string(bridge_module, fake_requests):
    fake_requests.queue(FakeResponse(404, text="missing"))
    assert bridge_module.safe_delete("thing") == "Error 404: missing"


def test_safe_delete_exception(bridge_module, fake_requests):
    fake_requests.exc = OSError("net")
    assert bridge_module.safe_delete("thing") == "Request failed: net"
