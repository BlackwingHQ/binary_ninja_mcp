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
import re
import subprocess
from pathlib import Path

import pytest
import requests

DEFAULT_URL = "http://localhost:9009"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_NAME = "constructs"
FIXTURE_PATH = REPO_ROOT / "tests" / "integration" / "fixtures" / FIXTURE_NAME


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


def _nm_symbols(path: Path) -> dict[str, str]:
    """Return ``{symbol_name: "0x..."}`` for every defined symbol in
    the binary, as reported by `nm -g`. Skips undefined symbols
    (those whose first column is blank, e.g. ``U _printf``)."""
    out = subprocess.run(["nm", "-g", str(path)], capture_output=True, text=True, check=True)
    syms: dict[str, str] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        addr, _kind, name = parts
        try:
            syms[name] = f"0x{int(addr, 16):x}"
        except ValueError:
            continue
    return syms


def _objdump_disasm(path: Path) -> str:
    """Disassemble the whole text section. `--no-show-raw-insn` keeps
    the lines short so the regex parsers below stay readable."""
    out = subprocess.run(
        ["objdump", "-d", "--no-show-raw-insn", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


_FUNC_HEADER_RE = re.compile(r"^[0-9a-f]+\s+<([^>]+)>:\s*$")
_INSN_RE = re.compile(r"^\s*([0-9a-f]+):\s*(.*)$")


def _find_call_site(disasm: str, caller: str, callee: str) -> str | None:
    """Address of the first `bl <addr> <callee...>` instruction inside
    `caller`'s body. objdump renders calls as e.g.
    `1000005c0:    bl    0x1000004f8 <_compute_secret>`."""
    in_caller = False
    for line in disasm.splitlines():
        m = _FUNC_HEADER_RE.match(line)
        if m:
            in_caller = m.group(1) == caller
            continue
        if not in_caller:
            continue
        m = _INSN_RE.match(line)
        if not m:
            continue
        addr_str, insn = m.groups()
        if "bl" in insn.split() and callee in insn:
            return f"0x{int(addr_str, 16):x}"
    return None


def _find_constant_load(disasm: str, func: str, value: int) -> str | None:
    """Address of the first `mov`/`movz` that loads the immediate
    `value` into a register inside `func`. Pattern (objdump arm64):
    `100000528:    mov    w9, #0x7   ; =7`."""
    in_func = False
    needle = f"#0x{value:x}"
    for line in disasm.splitlines():
        m = _FUNC_HEADER_RE.match(line)
        if m:
            in_func = m.group(1) == func
            continue
        if not in_func:
            continue
        m = _INSN_RE.match(line)
        if not m:
            continue
        addr_str, insn = m.groups()
        tokens = insn.split()
        if tokens and tokens[0] in {"mov", "movz", "movk"} and needle in insn:
            return f"0x{int(addr_str, 16):x}"
    return None


def _find_cstring_va(path: Path, needle: bytes, section: str = "__cstring") -> str | None:
    """Virtual address of `needle` in the binary's `__cstring`
    section (or any other read-only data section). Parses the dump
    format `objdump -s -j <section>` emits: a base address per line
    followed by 4 groups of 8 hex digits and the ASCII rendering."""
    out = subprocess.run(
        ["objdump", "-s", "-j", section, str(path)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    data = bytearray()
    addrs: list[int] = []
    line_re = re.compile(r"^\s*([0-9a-f]+)\s+((?:[0-9a-f]+\s+){1,4})")
    for line in out.stdout.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        line_addr = int(m.group(1), 16)
        hex_chunks = m.group(2).split()
        chunk_bytes = bytes.fromhex("".join(hex_chunks))
        addrs.extend(range(line_addr, line_addr + len(chunk_bytes)))
        data.extend(chunk_bytes)
    idx = data.find(needle)
    if idx < 0 or idx >= len(addrs):
        return None
    return f"0x{addrs[idx]:x}"


@pytest.fixture(scope="session")
def anchors() -> dict[str, str]:
    """Symbol-relative anchor addresses for the fixture binary, read
    directly from the binary file via `nm` and `objdump`.

    Independent of BN — if a server-side endpoint regresses, these
    anchors are still correct, which keeps test failures pinned to
    the actual bug instead of cascading through the whole suite.
    Tests using anchors implicitly require the binary to have been
    built first (see `build.sh`).

    Keys (all values are ``0x...``-prefixed hex strings):
      compute_secret              — start of `_compute_secret`
      compute_secret_str_w0       — 2nd insn (`str w0, [sp, #0xc]`)
      compute_secret_mul7         — `mov w?, #0x7` inside the loop
      main                        — start of the entry function
      main_name                   — `_main` / `_start` / etc.
      compute_secret_call_site    — `bl _compute_secret` site in main
      usage_string                — `"usage: %s <n>"` in __cstring
      default_task                — the `default_task` global
    """
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"fixture binary missing at {FIXTURE_PATH}. "
            f"Run `bash tests/integration/fixtures/build.sh`."
        )
    syms = _nm_symbols(FIXTURE_PATH)
    if "_compute_secret" not in syms:
        pytest.skip(f"_compute_secret missing from fixture symbols: {list(syms)}")

    cs_addr = syms["_compute_secret"]
    cs_int = int(cs_addr, 16)
    main_name = next((n for n in ("_main", "main", "_start") if n in syms), None)
    if not main_name:
        pytest.skip(f"no main-like symbol in fixture: {list(syms)}")

    disasm = _objdump_disasm(FIXTURE_PATH)

    return {
        "compute_secret": cs_addr,
        "compute_secret_str_w0": f"0x{cs_int + 4:x}",
        "compute_secret_mul7": _find_constant_load(disasm, "_compute_secret", 7),
        "main": syms[main_name],
        "main_name": main_name,
        "compute_secret_call_site": _find_call_site(disasm, main_name, "_compute_secret"),
        "usage_string": _find_cstring_va(FIXTURE_PATH, b"usage"),
        "default_task": syms.get("_default_task"),
    }
