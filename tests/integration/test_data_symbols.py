"""Data-symbol read endpoints: /hexdumpByName, /getDataDecl,
/getDataVarAt.

These complement the address-based reads in test_addresses.py —
here the input is a *name* (or an address that resolves to a named
data symbol) and the response includes type information BN already
derived for that symbol.

The fixture's `default_task` global is the canonical target: a
named `struct task_t` at the address `nm` reports for it. The
struct's binary layout on arm64 is:

    offset  field   value
    0x00    id      0x63 (=99)          int32
    0x04    pri     0x07 (PRIORITY_HIGH) int32 (enum)
    0x08    label   pointer to "default" in __cstring
    0x10    [padding to next 16-byte boundary]
"""


def test_hexdump_by_name_returns_global_bytes(binja_session, base_url, anchors):
    """The header line names the symbol; the body shows the bytes
    BN actually placed at its address. `id = 99` lives at offset 0,
    so the first four bytes are `63 00 00 00`."""
    r = binja_session.get(
        f"{base_url}/hexdumpByName",
        params={"name": "default_task", "length": 16},
        timeout=10,
    )
    r.raise_for_status()
    text = r.text
    assert "default_task" in text
    assert anchors["default_task"].removeprefix("0x") in text
    # First 4 bytes = id = 99 = 0x63, little-endian.
    assert "63 00 00 00" in text


def test_hexdump_by_name_unknown_symbol_returns_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/hexdumpByName", params={"name": "does_not_exist_xyz"}, timeout=5
    )
    assert r.status_code == 404


def test_get_data_decl_by_name_returns_struct_metadata(binja_session, base_url, anchors):
    """The declaration text and type field tell the agent what
    `default_task` actually is without it having to re-derive the
    type from the hexdump."""
    r = binja_session.get(
        f"{base_url}/getDataDecl",
        params={"name": "default_task", "length": -1},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["address"] == anchors["default_task"]
    assert body["name"] == "default_task"
    assert body["type"] == "struct task_t"
    assert "struct task_t" in body["decl"]
    assert body["decl"].endswith(";")
    assert body["size"] == 16  # sizeof(task_t) = int(4) + enum(4) + ptr(8)
    # Hexdump string includes the first four bytes (id=99).
    assert "63 00 00 00" in body["hexdump"]


def test_get_data_decl_by_address_resolves_same_data(binja_session, base_url, anchors):
    """An address resolves to the same data var; the type and size
    are identical even though the echoed `name` reflects the input."""
    by_name = binja_session.get(
        f"{base_url}/getDataDecl",
        params={"name": "default_task", "length": -1},
        timeout=10,
    ).json()
    by_addr = binja_session.get(
        f"{base_url}/getDataDecl",
        params={"address": anchors["default_task"], "length": -1},
        timeout=10,
    ).json()
    assert by_addr["address"] == by_name["address"]
    assert by_addr["type"] == by_name["type"]
    assert by_addr["size"] == by_name["size"]


def test_get_data_decl_unknown_name_returns_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getDataDecl", params={"name": "does_not_exist_xyz"}, timeout=5
    )
    assert r.status_code == 404
    assert r.json()["ident"] == "does_not_exist_xyz"


def test_get_data_var_at_returns_named_struct(binja_session, base_url, anchors):
    """At the global's address the response carries the name, type,
    and a stringified value rendering that exposes the field
    contents (id=99, pri=PRIORITY_HIGH, label=<ptr>)."""
    r = binja_session.get(
        f"{base_url}/getDataVarAt",
        params={"address": anchors["default_task"]},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["address"] == anchors["default_task"]
    assert body["name"] == "default_task"
    assert body["type"] == "struct task_t"
    # The value rendering is a stringified dict; check fields are visible.
    value = body["value"]
    assert "id" in value and "99" in value
    assert "pri" in value and "PRIORITY_HIGH" in value
    assert "label" in value


def test_get_data_var_at_code_address_returns_404(binja_session, base_url, anchors):
    """An address inside a function (not a data var) returns 404 —
    BN doesn't have a defined data variable there."""
    r = binja_session.get(
        f"{base_url}/getDataVarAt",
        params={"address": anchors["compute_secret"]},
        timeout=5,
    )
    assert r.status_code == 404
    assert "No data variable" in r.json().get("error", "")


def test_get_data_var_at_unmapped_address_returns_404(binja_session, base_url):
    r = binja_session.get(f"{base_url}/getDataVarAt", params={"address": "0xdeadbeef0"}, timeout=5)
    assert r.status_code == 404
    assert "No data variable" in r.json().get("error", "")
