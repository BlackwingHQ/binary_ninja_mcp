"""Symbol- and type-related read endpoints: /getSymbolsByType,
/getUserDefinedType, /getTypeInfo, /searchTypes, /demangle.

The fixture binary defines a typedef'd struct (`task_t`), enum
(`priority_t`), and union (`value_view_t`), plus a global instance
(`default_task`). DWARF brings them into BN's view-local type pool
so /getTypeInfo and /searchTypes can resolve them by name.

`/getUserDefinedType` is *not* expected to find these: that endpoint
is narrowly scoped to BN's `user_type_container` (types created via
the UI or `define_types`). The DWARF-imported variants land in the
auto-type pool instead, which is what /getTypeInfo and /searchTypes
read from.
"""

# ---------- /getSymbolsByType ----------


def test_symbols_by_type_function_alias_returns_user_functions(binja_session, base_url):
    """The `function` alias maps to FunctionSymbol; the fixture has
    `_compute_secret` and the entry function (`_start` / `_main`)."""
    r = binja_session.get(
        f"{base_url}/getSymbolsByType",
        params={"type": "function", "limit": 50},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["type"] == "FunctionSymbol"
    names = {s["name"] for s in body["symbols"]}
    assert "_compute_secret" in names
    assert "_start" in names or "_main" in names


def test_symbols_by_type_import_alias_lists_libc_calls(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getSymbolsByType",
        params={"type": "import", "limit": 20},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["type"] == "ImportedFunctionSymbol"
    names = {s["name"].lstrip("_") for s in body["symbols"]}
    assert {"printf", "atoi"} <= names


def test_symbols_by_type_data_alias_non_empty(binja_session, base_url):
    """DataSymbol covers Mach-O / ELF header symbols and any defined
    data items; should always have at least one entry."""
    r = binja_session.get(
        f"{base_url}/getSymbolsByType",
        params={"type": "data", "limit": 5},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["type"] == "DataSymbol"
    assert body["symbols"]


def test_symbols_by_type_accepts_raw_enum_name(binja_session, base_url):
    """The endpoint accepts both friendly aliases and raw BN enum
    names — `FunctionSymbol` is equivalent to `function`."""
    alias = binja_session.get(
        f"{base_url}/getSymbolsByType", params={"type": "function", "limit": 50}, timeout=10
    ).json()
    raw = binja_session.get(
        f"{base_url}/getSymbolsByType",
        params={"type": "FunctionSymbol", "limit": 50},
        timeout=10,
    ).json()
    assert {s["name"] for s in alias["symbols"]} == {s["name"] for s in raw["symbols"]}


def test_symbols_by_type_missing_type_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/getSymbolsByType", params={"limit": 5}, timeout=5)
    assert r.status_code == 400
    assert "Missing type" in r.json().get("error", "")


def test_symbols_by_type_unknown_type_returns_400(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getSymbolsByType",
        params={"type": "zzz_not_a_symbol_type", "limit": 5},
        timeout=5,
    )
    assert r.status_code == 400
    body = r.json()
    assert "Unknown symbol type" in body.get("error", "")
    # The error message lists the valid aliases so the agent can
    # retry without having to consult docs.
    assert "function" in body["error"]


def test_symbols_by_type_respects_address_bounds(binja_session, base_url, anchors):
    """Bound the search to a tight window just past `_compute_secret`
    — only it should fall inside, and every entry in the answer must
    sit below the end bound."""
    cs_int = int(anchors["compute_secret"], 16)
    end_int = cs_int + 0x10  # just past the prologue, before any other function
    r = binja_session.get(
        f"{base_url}/getSymbolsByType",
        params={
            "type": "function",
            "start": "0x100000000",
            "end": f"0x{end_int:x}",
            "limit": 50,
        },
        timeout=10,
    )
    r.raise_for_status()
    addrs = {int(s["address"], 16) for s in r.json()["symbols"]}
    for addr in addrs:
        assert addr < end_int, f"symbol at {hex(addr)} leaked past end bound"
    assert cs_int in addrs, "expected _compute_secret to remain in range"


# ---------- /getUserDefinedType ----------


def test_user_defined_type_unknown_returns_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getUserDefinedType",
        params={"name": "does_not_exist"},
        timeout=5,
    )
    assert r.status_code == 404
    body = r.json()
    assert body["requested_type"] == "does_not_exist"
    assert "available_types" in body


def test_user_defined_type_does_not_resolve_dwarf_types(binja_session, base_url):
    """This endpoint is narrowly scoped to BN's `user_type_container`
    — types the user created via the UI or `define_types`. DWARF-
    imported types like `task_t` exist in the view but aren't
    "user-defined" by that definition, so a lookup returns 404.
    Callers wanting type info regardless of provenance should hit
    `/getTypeInfo` instead."""
    r = binja_session.get(f"{base_url}/getUserDefinedType", params={"name": "task_t"}, timeout=5)
    assert r.status_code == 404
    assert r.json()["requested_type"] == "task_t"


# ---------- /getTypeInfo ----------


def test_type_info_resolves_view_local_struct(binja_session, base_url):
    """`mach_header_64` is brought into the view automatically when BN
    loads a Mach-O binary, so it must resolve via the view-local
    lookup path with source='local' and a populated members list."""
    r = binja_session.get(f"{base_url}/getTypeInfo", params={"name": "mach_header_64"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["name"] == "mach_header_64"
    assert body["kind"] == "struct"
    assert body["source"] == "local"
    assert body["decl"]
    assert body["members"], "struct must report its members"


def test_type_info_resolves_libc_typedef_via_platform(binja_session, base_url):
    """`size_t` is a libc typedef known to the macOS platform but not
    imported into a fresh view; the platform-level lookup path must
    catch it."""
    r = binja_session.get(f"{base_url}/getTypeInfo", params={"name": "size_t"}, timeout=10)
    r.raise_for_status()
    body = r.json()
    assert body["name"] == "size_t"
    assert body["source"] in {"platform", "library"}
    assert body["kind"] != "unknown"
    assert body["decl"]


def test_type_info_fixture_struct_with_members(binja_session, base_url):
    """`task_t` is the fixture's three-field struct (DWARF-imported);
    every field must surface with its name, type, and byte offset.
    The HIGH-priority field comes after `id` (int, 4 bytes) so its
    offset is 4; the `label` pointer starts at 8."""
    r = binja_session.get(f"{base_url}/getTypeInfo", params={"name": "task_t"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["kind"] == "struct"
    assert body["source"] == "local"
    by_name = {m["name"]: m for m in body["members"]}
    assert set(by_name) == {"id", "pri", "label"}
    assert by_name["id"]["offset"] == 0
    assert by_name["pri"]["offset"] == 4
    assert by_name["label"]["offset"] == 8
    assert "int" in by_name["id"]["type"]
    assert "priority_t" in by_name["pri"]["type"]
    assert "char" in by_name["label"]["type"]


def test_type_info_fixture_enum_with_values(binja_session, base_url):
    """`priority_t` enumerates four named priorities. The integer
    values are pinned from the C source so a future fixture change
    that touches the enum is a visible failure."""
    r = binja_session.get(f"{base_url}/getTypeInfo", params={"name": "priority_t"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["kind"] == "enum"
    assert body["source"] == "local"
    by_name = {m["name"]: m["value"] for m in body["enum_members"]}
    assert by_name == {
        "PRIORITY_LOW": 0,
        "PRIORITY_NORMAL": 1,
        "PRIORITY_HIGH": 7,
        "PRIORITY_CRITICAL": 42,
    }


def test_type_info_fixture_union_members_overlap(binja_session, base_url):
    """`value_view_t` is the fixture's union — every member starts at
    offset 0 (that's what makes it a union, not a struct)."""
    r = binja_session.get(f"{base_url}/getTypeInfo", params={"name": "value_view_t"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["kind"] == "union"
    assert body["source"] == "local"
    names = {m["name"] for m in body["members"]}
    assert {"as_int", "bytes"} <= names
    for m in body["members"]:
        assert m["offset"] == 0, f"union member {m['name']!r} not at offset 0"


def test_type_info_unknown_type_pins_response_shape(binja_session, base_url):
    """Even for an unknown type, the response carries the full shape
    of fields with null values rather than 404-ing — so callers can
    parse the response uniformly."""
    r = binja_session.get(
        f"{base_url}/getTypeInfo",
        params={"name": "definitely_not_a_type_xyz"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["name"] == "definitely_not_a_type_xyz"
    assert body["kind"] == "unknown"
    assert body["decl"] is None
    assert body["members"] is None
    assert body["enum_members"] is None


# ---------- /searchTypes ----------


def test_search_types_no_match_returns_empty(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/searchTypes",
        params={"query": "zzz_definitely_not_a_type", "count": 5},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["types"] == []
    assert body["total"] == 0


def test_search_types_finds_fixture_struct(binja_session, base_url):
    """Without `includeLibraries=1`, the search must still cover the
    view's local types — that's the whole point of the default mode.
    `task_t` is defined in the fixture's DWARF info and should be
    found by a substring query."""
    r = binja_session.get(
        f"{base_url}/searchTypes", params={"query": "task_t", "count": 10}, timeout=10
    )
    r.raise_for_status()
    body = r.json()
    assert body["includeLibraries"] is False
    names = {t["name"] for t in body["types"]}
    assert "task_t" in names, f"task_t missing from local-type search: {names}"


def test_search_types_finds_fixture_enum(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/searchTypes", params={"query": "priority_t", "count": 10}, timeout=10
    )
    r.raise_for_status()
    names = {t["name"] for t in r.json()["types"]}
    assert "priority_t" in names


def test_search_types_default_does_not_pull_in_library_types(binja_session, base_url):
    """Without `includeLibraries=1`, library typedefs like `size_t`
    must NOT appear — that's the user's signal that the search is
    fast and scoped to the current view."""
    r = binja_session.get(
        f"{base_url}/searchTypes", params={"query": "size_t", "count": 10}, timeout=10
    )
    r.raise_for_status()
    body = r.json()
    assert body["includeLibraries"] is False
    names = {t["name"] for t in body["types"]}
    assert "size_t" not in names


def test_search_types_include_libraries_returns_matches(binja_session, base_url):
    """With `includeLibraries=1`, the BN type-library types become
    searchable; a common substring like 'int' must hit at least one."""
    r = binja_session.get(
        f"{base_url}/searchTypes",
        params={"query": "int", "count": 5, "includeLibraries": "1"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    assert body["includeLibraries"] is True
    assert body["total"] >= 1
    assert body["types"]


# ---------- /demangle ----------


def test_demangle_itanium_cpp_constructor(binja_session, base_url):
    """`_ZN3FooC1Ev` is `Foo::Foo()` in Itanium ABI — the lingua franca
    of every non-Windows toolchain."""
    r = binja_session.get(f"{base_url}/demangle", params={"name": "_ZN3FooC1Ev"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["mangled"] == "_ZN3FooC1Ev"
    assert body["demangled"] == "Foo::Foo"
    assert body["type"] == "void()"


def test_demangle_unmangled_c_symbol_returns_unchanged(binja_session, base_url):
    """A plain C symbol isn't really "mangled" — the demangler echoes
    it back unchanged with no type signature."""
    r = binja_session.get(f"{base_url}/demangle", params={"name": "_main"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["mangled"] == "_main"
    assert body["demangled"] == "_main"
    assert body["type"] is None


def test_demangle_missing_name_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/demangle", timeout=5)
    assert r.status_code == 400
    body = r.json()
    assert "Missing name" in body.get("error", "")
