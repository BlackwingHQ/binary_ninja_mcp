"""Function-level read endpoints: /methods, /searchFunctions,
/getFunctionMetadata, /functionAt, /getCallers, /getCallees.

The fixture binary lays out a 1-level call graph: an entry function
(named `_start` by BN, with `_main` as an alias symbol) calls
`_compute_secret`, `_printf`, and `_atoi`. Those four are the entire
function inventory, which keeps assertions about lookups and call
edges deterministic.
"""

FIXTURE_FUNCTION_NAMES = {"_compute_secret", "_start", "_printf", "_atoi"}
COMPUTE_SECRET_ADDR = 0x100000460


# ---------- /methods ----------


def test_methods_includes_all_fixture_functions(binja_session, base_url):
    r = binja_session.get(f"{base_url}/methods", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    names = {fn["name"] for fn in r.json()["functions"]}
    assert FIXTURE_FUNCTION_NAMES <= names, (
        f"missing expected functions: {FIXTURE_FUNCTION_NAMES - names}"
    )


def test_methods_entries_have_name_and_address(binja_session, base_url):
    r = binja_session.get(f"{base_url}/methods", params={"limit": 100}, timeout=5)
    r.raise_for_status()
    for fn in r.json()["functions"]:
        assert fn.get("name"), fn
        assert fn.get("address", "").startswith("0x"), fn
        int(fn["address"], 16)


def test_methods_pagination_advances(binja_session, base_url):
    head = binja_session.get(f"{base_url}/methods", params={"limit": 2}, timeout=5).json()[
        "functions"
    ]
    skip = binja_session.get(
        f"{base_url}/methods", params={"offset": 2, "limit": 2}, timeout=5
    ).json()["functions"]
    assert len(head) == 2 and len(skip) == 2
    head_addrs = {fn["address"] for fn in head}
    skip_addrs = {fn["address"] for fn in skip}
    assert head_addrs.isdisjoint(skip_addrs), (
        f"second page overlapped first: {head_addrs & skip_addrs}"
    )


def test_methods_offset_past_end_yields_empty(binja_session, base_url):
    r = binja_session.get(f"{base_url}/methods", params={"offset": 9999, "limit": 5}, timeout=5)
    r.raise_for_status()
    assert r.json() == {"functions": []}


# ---------- /searchFunctions ----------


def test_search_functions_finds_substring_match(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/searchFunctions", params={"query": "compute", "limit": 20}, timeout=5
    )
    r.raise_for_status()
    matches = r.json()["matches"]
    assert [m["name"] for m in matches] == ["_compute_secret"]


def test_search_functions_is_case_insensitive(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/searchFunctions", params={"query": "COMPUTE", "limit": 20}, timeout=5
    )
    r.raise_for_status()
    names = [m["name"] for m in r.json()["matches"]]
    assert "_compute_secret" in names


def test_search_functions_empty_for_no_match(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/searchFunctions",
        params={"query": "this_string_does_not_appear_xyz", "limit": 5},
        timeout=5,
    )
    r.raise_for_status()
    assert r.json() == {"matches": []}


# ---------- /getFunctionMetadata ----------


METADATA_KEYS = {
    "function",
    "address",
    "is_thunk",
    "can_return",
    "has_variable_arguments",
    "is_pure",
    "analysis_skipped",
    "analysis_skip_reason",
    "analysis_skip_override",
    "auto",
    "parameter_count",
}


def test_function_metadata_by_name(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getFunctionMetadata", params={"function": "_compute_secret"}, timeout=5
    )
    r.raise_for_status()
    meta = r.json()
    assert meta["function"] == "_compute_secret"
    assert meta["parameter_count"] == 1


def test_function_metadata_by_address_matches_by_name(binja_session, base_url):
    by_name = binja_session.get(
        f"{base_url}/getFunctionMetadata", params={"function": "_compute_secret"}, timeout=5
    ).json()
    by_addr = binja_session.get(
        f"{base_url}/getFunctionMetadata",
        params={"function": f"0x{COMPUTE_SECRET_ADDR:x}"},
        timeout=5,
    ).json()
    assert by_name == by_addr


def test_function_metadata_exposes_all_documented_fields(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getFunctionMetadata", params={"function": "_compute_secret"}, timeout=5
    )
    r.raise_for_status()
    missing = METADATA_KEYS - r.json().keys()
    assert not missing, f"missing metadata fields: {missing}"


def test_function_metadata_unknown_function_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getFunctionMetadata",
        params={"function": "nope_does_not_exist"},
        timeout=5,
    )
    assert r.status_code == 404
    assert "error" in r.json()


# ---------- /functionAt ----------


def test_function_at_function_start_hex(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/functionAt", params={"address": f"0x{COMPUTE_SECRET_ADDR:x}"}, timeout=5
    )
    r.raise_for_status()
    assert r.json()["functions"] == ["_compute_secret"]


def test_function_at_function_start_decimal(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/functionAt", params={"address": str(COMPUTE_SECRET_ADDR)}, timeout=5
    )
    r.raise_for_status()
    assert r.json()["functions"] == ["_compute_secret"]


def test_function_at_mid_function_returns_containing_function(binja_session, base_url):
    """An address inside a function (not at its start) should still
    resolve to that function."""
    mid_addr = COMPUTE_SECRET_ADDR + 0x10  # safely inside _compute_secret
    r = binja_session.get(
        f"{base_url}/functionAt", params={"address": f"0x{mid_addr:x}"}, timeout=5
    )
    r.raise_for_status()
    assert r.json()["functions"] == ["_compute_secret"]


def test_function_at_non_function_address_empty(binja_session, base_url):
    """Mach-O header address — definitely not inside any function.
    Server returns an empty `functions` list, not an error."""
    r = binja_session.get(f"{base_url}/functionAt", params={"address": "0x100000000"}, timeout=5)
    r.raise_for_status()
    assert r.json()["functions"] == []


# ---------- /getCallers ----------


def test_get_callers_finds_caller(binja_session, base_url):
    """`_compute_secret` is called from the entry function (which BN
    has named `_start`)."""
    r = binja_session.get(
        f"{base_url}/getCallers", params={"identifiers": "_compute_secret"}, timeout=5
    )
    r.raise_for_status()
    result = r.json()["results"][0]
    caller_names = [c["name"] for c in result["callers"]]
    assert "_start" in caller_names, f"missing _start in callers: {caller_names}"


def test_get_callers_includes_caller_site_addresses(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getCallers", params={"identifiers": "_compute_secret"}, timeout=5
    )
    r.raise_for_status()
    sites = r.json()["results"][0]["caller_sites"]
    assert sites, "no caller_sites returned"
    for s in sites:
        assert s.get("address", "").startswith("0x"), s
        int(s["address"], 16)


def test_get_callers_unknown_identifier_goes_to_errors(binja_session, base_url):
    """Mixed valid + invalid identifiers — server returns results for
    the valid ones and an `errors` array for the rest."""
    r = binja_session.get(
        f"{base_url}/getCallers",
        params={"identifiers": "_compute_secret,definitely_not_a_function"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert [res["function"]["name"] for res in body["results"]] == ["_compute_secret"]
    assert any("definitely_not_a_function" in e for e in body.get("errors", []))


# ---------- /getCallees ----------


def test_get_callees_lists_all_outgoing_calls(binja_session, base_url):
    """`_main` (an alias for `_start`) calls printf, atoi, and
    compute_secret. All three should appear in the callees list."""
    r = binja_session.get(f"{base_url}/getCallees", params={"identifiers": "_main"}, timeout=5)
    r.raise_for_status()
    callee_names = {c["name"] for c in r.json()["results"][0]["callees"]}
    assert {"_printf", "_atoi", "_compute_secret"} <= callee_names


def test_get_callees_includes_call_site_addresses(binja_session, base_url):
    r = binja_session.get(f"{base_url}/getCallees", params={"identifiers": "_main"}, timeout=5)
    r.raise_for_status()
    sites = r.json()["results"][0]["call_sites"]
    assert sites, "no call_sites returned"
    for s in sites:
        assert s.get("address", "").startswith("0x"), s
        int(s["address"], 16)


def test_get_callees_unknown_identifier_goes_to_errors(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getCallees",
        params={"identifiers": "definitely_not_a_function"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["results"] == []
    assert any("definitely_not_a_function" in e for e in body.get("errors", []))
