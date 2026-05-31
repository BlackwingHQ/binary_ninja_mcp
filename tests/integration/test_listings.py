"""Listing endpoints: segments, sections, imports, exports, strings,
namespaces, classes, data, local types, tag types, platforms.
"""

# ---------- segments / sections ----------


def test_segments_non_empty(binja_session, base_url):
    r = binja_session.get(f"{base_url}/segments", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    segments = r.json().get("segments", [])
    assert segments, "no segments reported"
    # Every segment must have a start/end with hex addresses.
    for seg in segments:
        assert seg.get("start", "").startswith("0x")
        assert seg.get("end", "").startswith("0x")


def test_segments_have_at_least_one_executable_range(binja_session, base_url):
    """The code segment must show up as r-x, otherwise something is
    very wrong with how BN loaded the binary."""
    r = binja_session.get(f"{base_url}/segments", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    segments = r.json()["segments"]
    assert any(s.get("executable") and s.get("readable") for s in segments), (
        f"no r-x segment: {segments}"
    )


def test_sections_include_text(binja_session, base_url):
    """Matches `__text` (Mach-O) and `.text` (ELF)."""
    r = binja_session.get(f"{base_url}/sections", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    names = [s.get("name", "") for s in r.json().get("sections", [])]
    assert any("text" in n.lower() for n in names), f"no text-bearing section: {names}"


# ---------- imports / exports ----------


def test_imports_includes_libc_calls(binja_session, base_url):
    """The fixture calls printf/atoi, so they must show up as imports."""
    r = binja_session.get(f"{base_url}/imports", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    names = {(i.get("name") or "").lstrip("_") for i in r.json().get("imports", [])}
    assert "printf" in names, f"printf missing from imports: {names}"
    assert "atoi" in names, f"atoi missing from imports: {names}"


def test_imports_entries_have_address(binja_session, base_url):
    r = binja_session.get(f"{base_url}/imports", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    for imp in r.json()["imports"]:
        assert imp.get("address", "").startswith("0x"), imp


def test_exports_non_empty(binja_session, base_url):
    r = binja_session.get(f"{base_url}/exports", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    assert r.json().get("exports"), "no exports reported"


# ---------- strings ----------


FIXTURE_STRINGS = ("usage: %s <n>", "result = %d")


def test_strings_includes_fixture_literals(binja_session, base_url):
    r = binja_session.get(f"{base_url}/strings", params={"limit": 1000}, timeout=10)
    r.raise_for_status()
    values = [s.get("value", "") for s in r.json().get("strings", [])]
    for needle in FIXTURE_STRINGS:
        assert any(needle in v for v in values), f"fixture string {needle!r} missing from /strings"


def test_strings_filter_finds_only_matching(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/strings/filter", params={"filter": "usage", "limit": 100}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    values = [s.get("value", "") for s in body.get("strings", [])]
    assert values, "filter='usage' returned no matches"
    assert all("usage" in v for v in values), values
    assert body.get("total") == len(values)


def test_strings_filter_empty_for_no_match(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/strings/filter",
        params={"filter": "this-string-cannot-possibly-appear-xyz", "limit": 10},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body.get("strings") == []
    assert body.get("total") == 0


def test_strings_pagination_advances(binja_session, base_url):
    """offset=N skips the first N entries; offset>=total yields []."""
    head = binja_session.get(f"{base_url}/strings", params={"limit": 3}, timeout=5).json()[
        "strings"
    ]
    assert len(head) == 3
    skip_one = binja_session.get(
        f"{base_url}/strings", params={"offset": 1, "limit": 3}, timeout=5
    ).json()["strings"]
    assert [s["value"] for s in skip_one[:2]] == [s["value"] for s in head[1:3]]

    past_end = binja_session.get(
        f"{base_url}/strings", params={"offset": 999_999, "limit": 5}, timeout=5
    ).json()["strings"]
    assert past_end == []


# ---------- namespaces / classes ----------


def test_namespaces_empty_for_c_binary(binja_session, base_url):
    r = binja_session.get(f"{base_url}/namespaces", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    # A vanilla C binary has no namespaces; pin the empty shape so a
    # change in representation (None vs [], missing key) is visible.
    assert r.json() == {"namespaces": []}


def test_classes_empty_for_c_binary(binja_session, base_url):
    r = binja_session.get(f"{base_url}/classes", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    assert r.json() == {"classes": []}


# ---------- data ----------


def test_data_items_non_empty(binja_session, base_url):
    r = binja_session.get(f"{base_url}/data", params={"limit": 50}, timeout=10)
    r.raise_for_status()
    items = r.json().get("data", [])
    assert items, "no data items reported"
    # Each item should carry an address, name, and bytes_hex.
    for item in items[:5]:
        assert item.get("address", "").startswith("0x")
        assert item.get("name"), item
        assert item.get("bytes_hex") is not None, item


def test_data_items_includes_named_global(binja_session, base_url, anchors):
    """The fixture exports `default_task` — a task_t global with
    `.id=99, .pri=PRIORITY_HIGH, .label="default"`. It must appear
    in the /data listing at the address `nm` reports for it, with
    the struct type and a non-empty bytes_hex."""
    r = binja_session.get(f"{base_url}/data", params={"limit": 500}, timeout=10)
    r.raise_for_status()
    items = r.json()["data"]
    entry = next((d for d in items if d.get("name") == "default_task"), None)
    assert entry is not None, "default_task missing from /data"
    assert entry["address"] == anchors["default_task"]
    assert "task_t" in entry.get("type", "")
    assert entry["bytes_hex"]


# ---------- local types ----------


def test_local_types_includes_fixture_types(binja_session, base_url):
    """The DWARF importer pulls task_t, priority_t, and value_view_t
    into the view's local type list. `/localTypes` (without library
    fallback) must surface all three."""
    r = binja_session.get(
        f"{base_url}/localTypes",
        params={"count": 500, "include_libraries": "false"},
        timeout=15,
    )
    r.raise_for_status()
    names = {t["name"] for t in r.json()["types"]}
    for expected in ("task_t", "priority_t", "value_view_t"):
        assert expected in names, f"{expected} missing from local types: {names}"


# ---------- tag types ----------


def test_tag_types_includes_binja_builtins(binja_session, base_url):
    r = binja_session.get(f"{base_url}/tagTypes", timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body.get("count", 0) > 0
    names = {t.get("name") for t in body.get("tag_types", [])}
    # These three ship with every BN install; if any one is missing the
    # response shape probably changed.
    for builtin in ("Bookmarks", "Bugs", "Important"):
        assert builtin in names, f"built-in tag type {builtin!r} missing; got {names}"


# ---------- platforms ----------


def test_platforms_lists_known_platforms(binja_session, base_url):
    """BN's `Platform.get_list()` always includes the major host
    platforms (linux-x86_64, mac-aarch64, windows-x86_64, ...). The
    endpoint should pass each through with name + arch + default
    calling-convention fields."""
    r = binja_session.get(f"{base_url}/platforms", timeout=5)
    r.raise_for_status()
    body = r.json()
    platforms = body.get("platforms", [])
    assert platforms, "empty platforms response"

    names = {p.get("name") for p in platforms}
    # At least one common host platform should always be present; pick
    # the widest set so the test passes on every BN install regardless
    # of which optional architectures the user has enabled.
    assert names & {
        "linux-x86_64",
        "linux-x86",
        "mac-x86_64",
        "mac-aarch64",
        "windows-x86_64",
        "windows-x86",
    }, f"no common host platform in {sorted(names)}"

    sample = platforms[0]
    for key in ("name", "arch", "default_calling_convention"):
        assert key in sample, f"missing key {key!r} in {sample}"
