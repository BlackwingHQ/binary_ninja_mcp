import sys as _sys
import traceback as _tb


# Install a very-early excepthook so any ImportError at module import time is captured.
def _bridge_excepthook(exc_type, exc, tb):
    # Print to stderr for interactive runs
    _tb.print_exception(exc_type, exc, tb, file=_sys.stderr)


_sys.excepthook = _bridge_excepthook

import os as _os

import requests
from mcp.server.fastmcp import FastMCP

binja_server_url = "http://localhost:9009"
mcp = FastMCP("binja-mcp")

# Token file lives at <plugin_root>/.mcp_auth_token, two dirs up from this file.
# Re-read on every request so `setup_plugin.py --regen-token` takes effect on
# the next call without needing to restart the MCP client.
_TOKEN_FILE = _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".mcp_auth_token")
)


def _read_auth_token() -> str | None:
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        return None
    return tok or None


def _auth_headers() -> dict:
    tok = _read_auth_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _active_filename() -> str:
    """Return the currently active filename as known by the server."""
    try:
        st = get_json("status")
        if isinstance(st, dict) and st.get("filename"):
            return str(st.get("filename"))
    except Exception:
        pass
    return "(none)"


def safe_get(endpoint: str, params: dict | None = None, timeout: float | None = 5) -> list:
    """
    Perform a GET request. If 'params' is given, we convert it to a query string.
    """
    if params is None:
        params = {}
    url = f"{binja_server_url}/{endpoint}"

    try:
        if timeout is None:
            response = requests.get(url, params=params, headers=_auth_headers())
        else:
            response = requests.get(url, params=params, headers=_auth_headers(), timeout=timeout)
        response.encoding = "utf-8"
        if response.ok:
            return response.text.splitlines()
        else:
            return [f"Error {response.status_code}: {response.text.strip()}"]
    except Exception as e:
        return [f"Request failed: {e!s}"]


def get_json(endpoint: str, params: dict | None = None, timeout: float | None = 5):
    """
    Perform a GET and return parsed JSON.
    - On 2xx: returns parsed JSON.
    - On 4xx/5xx: attempts to parse JSON body and return it; if not JSON, returns {'error': 'Error <code>: <text>'}.
    Returns None only on transport errors.
    """
    if params is None:
        params = {}
    url = f"{binja_server_url}/{endpoint}"
    try:
        if timeout is None:
            response = requests.get(url, params=params, headers=_auth_headers())
        else:
            response = requests.get(url, params=params, headers=_auth_headers(), timeout=timeout)
        response.encoding = "utf-8"
        # Try to parse JSON regardless of status
        try:
            data = response.json()
        except Exception:
            data = None
        if response.ok:
            return data
        # Non-OK: return parsed error object if available; otherwise synthesize one
        if isinstance(data, dict):
            # Ensure at least an error field for LLMs
            if "error" not in data:
                data = {"error": str(data)}
            data.setdefault("status", response.status_code)
            return data
        text = (response.text or "").strip()
        return {"error": f"Error {response.status_code}: {text}"}
    except Exception as e:
        return {"error": f"Request failed: {e!s}"}


def get_text(endpoint: str, params: dict | None = None, timeout: float | None = 5) -> str:
    """Perform a GET and return raw text (or an error string)."""
    if params is None:
        params = {}
    url = f"{binja_server_url}/{endpoint}"
    try:
        if timeout is None:
            response = requests.get(url, params=params, headers=_auth_headers())
        else:
            response = requests.get(url, params=params, headers=_auth_headers(), timeout=timeout)
        response.encoding = "utf-8"
        if response.ok:
            return response.text
        else:
            return f"Error {response.status_code}: {response.text.strip()}"
    except Exception as e:
        return f"Request failed: {e!s}"


def safe_post(endpoint: str, data: dict | str) -> str:
    try:
        if isinstance(data, dict):
            response = requests.post(
                f"{binja_server_url}/{endpoint}",
                data=data,
                headers=_auth_headers(),
                timeout=5,
            )
        else:
            response = requests.post(
                f"{binja_server_url}/{endpoint}",
                data=data.encode("utf-8"),
                headers=_auth_headers(),
                timeout=5,
            )
        response.encoding = "utf-8"
        if response.ok:
            return response.text.strip()
        else:
            return f"Error {response.status_code}: {response.text.strip()}"
    except Exception as e:
        return f"Request failed: {e!s}"


@mcp.tool()
def list_methods(offset: int = 0, limit: int = 100) -> list:
    """
    List all function names in the program with pagination.
    """
    header = f"File: {_active_filename()}"
    body = safe_get("methods", {"offset": offset, "limit": limit})
    return [header] + (body or [])


@mcp.tool()
def get_entry_points() -> list:
    """
    List entry point(s) of the loaded binary.
    """
    data = get_json("entryPoints")
    if not data or "entry_points" not in data:
        return ["Error: no response"]
    out: list[str] = []
    for ep in data.get("entry_points", []) or []:
        addr = ep.get("address")
        name = ep.get("name") or "(unknown)"
        out.append(f"{addr}\t{name}")
    return out


@mcp.tool()
def retype_variable(function_name: str, variable_name: str, type_str: str) -> str:
    """
    Retype a variable in a function.
    """
    data = get_json(
        "retypeVariable",
        {
            "functionName": function_name,
            "variableName": variable_name,
            "type": type_str,
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "status" in data:
        return data["status"]
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def rename_single_variable(function_name: str, variable_name: str, new_name: str) -> str:
    """
    Rename a variable in a function.
    """
    data = get_json(
        "renameVariable",
        {
            "functionName": function_name,
            "variableName": variable_name,
            "newName": new_name,
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "status" in data:
        return data["status"]
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def rename_multi_variables(
    function_identifier: str,
    mapping_json: str = "",
    pairs: str = "",
    renames_json: str = "",
) -> str:
    """
    Rename multiple local variables in one call.
    - function_identifier: function name or address (hex)
    - Provide either mapping_json (JSON object old->new), renames_json (JSON array of {old,new}), or pairs ("old1:new1,old2:new2").
    Returns per-item results and totals.
    """
    params: dict[str, object] = {}
    ident = (function_identifier or "").strip()
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["functionName"] = ident

    payload = None
    import json as _json

    if renames_json:
        try:
            payload = _json.loads(renames_json)
        except Exception:
            return "Error: renames_json is not valid JSON"
        params["renames"] = payload
    elif mapping_json:
        try:
            payload = _json.loads(mapping_json)
        except Exception:
            return "Error: mapping_json is not valid JSON"
        params["mapping"] = payload
    elif pairs:
        params["pairs"] = pairs
    else:
        return "Error: provide mapping_json, renames_json, or pairs"

    data = get_json("renameVariables", params)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    try:
        total = data.get("total")
        renamed = data.get("renamed")
        return f"Batch rename: {renamed}/{total} applied"
    except Exception:
        return str(data)


@mcp.tool()
def define_types(c_code: str) -> str:
    """
    Define types from a C code string.
    """
    data = get_json("defineTypes", {"cCode": c_code})
    if not data:
        return "Error: no response"
    # Expect a list of defined type names or a dict; normalize to string
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    if isinstance(data, (list, tuple)):
        return "Defined types: " + ", ".join(map(str, data))
    return str(data)


@mcp.tool()
def list_classes(offset: int = 0, limit: int = 100) -> list:
    """
    List all namespace/class names in the program with pagination.
    """
    return safe_get("classes", {"offset": offset, "limit": limit})


@mcp.tool()
def hexdump_address(address: str, length: int = -1) -> str:
    """
    Hexdump data starting at an address. When length < 0, reads the exact defined size if available.
    """
    params = {"address": address}
    if length is not None:
        params["length"] = length
    return get_text("hexdump", params, timeout=None)


@mcp.tool()
def hexdump_data(name_or_address: str, length: int = -1) -> str:
    """
    Hexdump a data symbol by name or address. When length < 0, reads the exact defined size if available.
    """
    ident = (name_or_address or "").strip()
    if ident.startswith("0x"):
        return hexdump_address(ident, length)
    return get_text("hexdumpByName", {"name": ident, "length": length}, timeout=None)


@mcp.tool()
def get_data_decl(name_or_address: str, length: int = -1) -> str:
    """
    Return a declaration-like string and a hexdump for a data symbol by name or address.
    LLM-friendly: includes both a C-like declaration (when possible) and text hexdump.
    """
    ident = (name_or_address or "").strip()
    params = {"name": ident} if not ident.startswith("0x") else {"address": ident}
    if length is not None:
        params["length"] = length
    data = get_json("getDataDecl", params, timeout=None)
    if not data:
        return "Error: no response"
    if "error" in data:
        return f"Error: {data.get('error')}"
    decl = data.get("decl") or "(no declaration)"
    hexdump = data.get("hexdump") or ""
    addr = data.get("address", "")
    name = data.get("name", ident)
    return f"Declaration ({addr} {name}):\n{decl}\n\nHexdump:\n{hexdump}"


@mcp.tool()
def decompile_function(name: str) -> str:
    """
    Decompile a specific function by name and return the decompiled C code.
    """
    file_line = f"File: {_active_filename()}\n\n"
    data = get_json("decompile", {"name": name}, timeout=None)
    if not data:
        return file_line + "Error: no response"
    if "decompiled" in data:
        return file_line + data["decompiled"]
    if "error" in data:
        return file_line + f"Error: {data.get('error')}"
    return file_line + str(data)


@mcp.tool()
def get_il(name_or_address: str, view: str = "hlil", ssa: bool = False) -> str:
    """
    Get IL for a function in the selected view.
    - view: one of hlil, mlil, llil
    - ssa: set True to request SSA form (MLIL/LLIL only)
    """
    file_line = f"File: {_active_filename()}\n\n"
    ident = (name_or_address or "").strip()
    params = {"view": view, "ssa": int(bool(ssa))}
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["name"] = ident
    data = get_json("il", params, timeout=None)
    if not data:
        return file_line + "Error: no response"
    if "il" in data:
        return file_line + data["il"]
    if "error" in data:
        import json as _json

        return file_line + _json.dumps(data, indent=2, ensure_ascii=False)
    return file_line + str(data)


@mcp.tool()
def fetch_disassembly(name: str) -> str:
    """
    Retrive the disassembled code of a function with a given name as assemby mnemonic instructions.
    """
    file_line = f"File: {_active_filename()}\n\n"
    data = get_json("assembly", {"name": name}, timeout=None)
    if not data:
        return file_line + "Error: no response"
    if "assembly" in data:
        return file_line + data["assembly"]
    if "error" in data:
        return file_line + f"Error: {data.get('error')}"
    return file_line + str(data)


@mcp.tool()
def rename_function(old_name: str, new_name: str) -> str:
    """
    Rename a function by its current name to a new user-defined name.
    The configured prefix (default "mcp_") will be automatically prepended if not present.
    """
    return safe_post("renameFunction", {"oldName": old_name, "newName": new_name})


@mcp.tool()
def rename_data(address: str, new_name: str) -> str:
    """
    Rename a data label at the specified address.
    """
    return safe_post("renameData", {"address": address, "newName": new_name})


@mcp.tool()
def set_comment(address: str, comment: str) -> str:
    """
    Set a comment at a specific address.
    """
    return safe_post("comment", {"address": address, "comment": comment})


@mcp.tool()
def set_function_comment(function_name: str, comment: str) -> str:
    """
    Set a comment for a function.
    """
    return safe_post("comment/function", {"name": function_name, "comment": comment})


@mcp.tool()
def get_comment(address: str) -> str:
    """
    Get the comment at a specific address.
    """
    return safe_get("comment", {"address": address})[0]


@mcp.tool()
def get_function_comment(function_name: str) -> str:
    """
    Get the comment for a function.
    """
    return safe_get("comment/function", {"name": function_name})[0]


@mcp.tool()
def list_segments(offset: int = 0, limit: int = 100) -> list:
    """
    List all memory segments in the program with pagination.
    """
    return safe_get("segments", {"offset": offset, "limit": limit})


@mcp.tool()
def list_sections(offset: int = 0, limit: int = 100) -> list:
    """
    List sections in the program with pagination.

    Returns one line per section with: start-end, size, name, and any semantics/type if available.
    """
    data = get_json("sections", {"offset": offset, "limit": limit})
    if not data or not isinstance(data, dict):
        return ["Error: no response"]
    if data.get("error"):
        return [f"Error: {data.get('error')}"]
    sections = data.get("sections", []) or []
    out: list[str] = [f"File: {_active_filename()}"]
    for s in sections:
        try:
            start = s.get("start") or ""
            end = s.get("end") or ""
            size = s.get("size")
            name = s.get("name") or "(unnamed)"
            sem = s.get("semantics") or s.get("type") or ""
            tail = f"\t{sem}" if sem else ""
            out.append(f"{start}-{end}\t{size}\t{name}{tail}")
        except Exception:
            continue
    return out


@mcp.tool()
def list_imports(offset: int = 0, limit: int = 100) -> list:
    """
    List imported symbols in the program with pagination.
    """
    return safe_get("imports", {"offset": offset, "limit": limit})


@mcp.tool()
def list_strings(offset: int = 0, count: int = 100) -> list:
    """
    List all strings in the database (paginated).
    """
    return safe_get("strings", {"offset": offset, "limit": count}, timeout=None)


@mcp.tool()
def list_strings_filter(offset: int = 0, count: int = 100, filter: str = "") -> list:
    """
    List matching strings in the database (paginated, filtered).
    """
    return safe_get(
        "strings/filter",
        {"offset": offset, "limit": count, "filter": filter},
        timeout=None,
    )


@mcp.tool()
def list_local_types(offset: int = 0, count: int = 200, include_libraries: bool = False) -> list:
    """
    List all local types in the database (paginated).
    """
    return safe_get(
        "localTypes",
        {
            "offset": offset,
            "limit": count,
            "includeLibraries": int(bool(include_libraries)),
        },
        timeout=None,
    )


@mcp.tool()
def search_types(
    query: str, offset: int = 0, count: int = 200, include_libraries: bool = False
) -> list:
    """
    Search local types whose name or declaration contains the substring.
    """
    return safe_get(
        "searchTypes",
        {
            "query": query,
            "offset": offset,
            "limit": count,
            "includeLibraries": int(bool(include_libraries)),
        },
        timeout=None,
    )


@mcp.tool()
def list_all_strings(batch_size: int = 500) -> list:
    """
    List all strings in the database (aggregated across pages).
    """
    results: list[str] = []
    offset = 0
    while True:
        data = get_json("strings", {"offset": offset, "limit": batch_size}, timeout=None)
        if not data or "strings" not in data:
            break
        items = data.get("strings", [])
        if not items:
            break
        for s in items:
            addr = s.get("address")
            length = s.get("length")
            stype = s.get("type")
            value = s.get("value")
            results.append(f"{addr}\t{length}\t{stype}\t{value}")
        if len(items) < batch_size:
            break
        offset += batch_size
    return results


@mcp.tool()
def list_exports(offset: int = 0, limit: int = 100) -> list:
    """
    List exported functions/symbols with pagination.
    """
    return safe_get("exports", {"offset": offset, "limit": limit})


@mcp.tool()
def list_namespaces(offset: int = 0, limit: int = 100) -> list:
    """
    List all non-global namespaces in the program with pagination.
    """
    return safe_get("namespaces", {"offset": offset, "limit": limit})


@mcp.tool()
def list_data_items(offset: int = 0, limit: int = 100) -> list:
    """
    List defined data labels and their values with pagination.
    """
    return safe_get("data", {"offset": offset, "limit": limit})


@mcp.tool()
def search_functions_by_name(query: str, offset: int = 0, limit: int = 100) -> list:
    """
    Search for functions whose name contains the given substring.
    """
    if not query:
        return ["Error: query string is required"]
    return safe_get("searchFunctions", {"query": query, "offset": offset, "limit": limit})


@mcp.tool()
def find_bytes(
    pattern: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list:
    """
    Find non-overlapping occurrences of a byte pattern in the current binary.

    Args:
        pattern: Hex string for the bytes to find. Spaces and 0x prefixes are
            tolerated, e.g. "deadbeef", "de ad be ef", "0xde 0xad 0xbe 0xef".
        start: Optional starting address (hex like "0x401000" or decimal).
            Defaults to the binary view's start.
        end: Optional ending address (exclusive). Defaults to view end.
        limit: Cap on results (default 100). 0 or negative means unlimited.

    Returns:
        List of "<address>\\t<function|->" lines, one per match, or an error /
        "(no matches)" sentinel.
    """
    if not pattern:
        return ["Error: pattern is required"]
    params: dict = {"pattern": pattern, "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    data = get_json("findBytes", params)
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    matches = data.get("matches", []) if isinstance(data, dict) else []
    if not matches:
        return ["(no matches)"]
    return [f"{m.get('address')}\t{m.get('function') or '-'}" for m in matches]


@mcp.tool()
def find_text(
    text: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    case_sensitive: bool = True,
) -> list:
    """
    Find non-overlapping occurrences of a text string anywhere in the binary.

    Distinct from `list_strings` / `list_all_strings`, which only see
    BN-defined string objects. `find_text` greps the raw bytes, so it
    surfaces text that BN didn't recognize as a string (embedded format
    specifiers, code-adjacent text, etc.).

    Args:
        text: Search string. Encoded as UTF-8 for the match.
        start: Optional starting address (hex like "0x401000" or decimal).
        end: Optional ending address (exclusive).
        limit: Cap on results (default 100). 0 or negative means unlimited.
        case_sensitive: When False, uses BN's case-insensitive match flag
            if the API exposes it.

    Returns:
        List of "<address>\\t<function|->" lines, or an error /
        "(no matches)" sentinel.
    """
    if not text:
        return ["Error: text is required"]
    params: dict = {
        "text": text,
        "limit": limit,
        "caseSensitive": "1" if case_sensitive else "0",
    }
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    data = get_json("findText", params)
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    matches = data.get("matches", []) if isinstance(data, dict) else []
    if not matches:
        return ["(no matches)"]
    return [f"{m.get('address')}\t{m.get('function') or '-'}" for m in matches]


@mcp.tool()
def parse_expression(expr: str, here: str = "0") -> str:
    """
    Evaluate a Binary Ninja expression to an address.

    BN's expression language accepts symbol names, arithmetic (`+`, `-`,
    `*`, `/`), hex (`0x...`) / decimal literals, and the `$here`
    placeholder. Use this whenever you'd otherwise compute an address by
    hand — `parse_expression("main+0x40")` returns the same string you'd
    pass to `decompile_function`, `find_bytes`, `add_tag`, etc.

    Args:
        expr: Expression to evaluate (e.g. `"main+0x40"`, `"sub_401000+8"`,
            `"&strtab"`).
        here: Optional address substituted for `$here`. Hex or decimal.
            Default `"0"`.

    Returns:
        Resolved hex address, or an error message.
    """
    if not expr:
        return "Error: expr is required"
    data = get_json("parseExpression", {"expr": expr, "here": here})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return data.get("address") or "Error: missing address"
    return str(data)


@mcp.tool()
def find_constant(
    value: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list:
    """
    Find non-overlapping occurrences of a numeric constant in instructions.

    Backed by BN's `find_next_constant`, which scans *instructions* for
    the literal value — different from `find_bytes`, which scans raw
    bytes. Use this for magic values that appear as immediates ("where is
    0xCAFEBABE loaded?", "where else is the polynomial 0xEDB88320 used?").

    Args:
        value: Integer constant. Hex (with or without `0x`) or decimal.
        start: Optional starting address (hex like "0x401000" or decimal).
        end: Optional ending address (exclusive).
        limit: Cap on results (default 100). 0 or negative means unlimited.

    Returns:
        List of "<address>\\t<function|->" lines, or an error /
        "(no matches)" sentinel.
    """
    if not value:
        return ["Error: value is required"]
    params: dict = {"value": value, "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    data = get_json("findConstant", params)
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    matches = data.get("matches", []) if isinstance(data, dict) else []
    if not matches:
        return ["(no matches)"]
    return [f"{m.get('address')}\t{m.get('function') or '-'}" for m in matches]


@mcp.tool()
def define_user_symbol(address: str, name: str, kind: str = "data") -> str:
    """
    Create a user symbol (label) at an address.

    Args:
        address: Target address (hex like "0x401000" or decimal).
        name: Symbol name to assign.
        kind: "data" (default) or "function". Selects the symbol type used
            inside Binary Ninja; "data" is the right choice for labeling
            globals, strings, jump tables, etc., while "function" declares
            a function name without forcing function creation.

    Returns:
        Status string from the server, or an error message.
    """
    if not address or not name:
        return "Error: address and name are required"
    data = get_json(
        "defineUserSymbol",
        {"address": address, "name": name, "kind": kind},
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return (
            f"Defined {data.get('kind', kind)} symbol "
            f"{data.get('name')!r} at {data.get('address')}"
        )
    return str(data)


@mcp.tool()
def undefine_user_symbol(address: str) -> str:
    """
    Remove the user symbol at an address.

    Args:
        address: Target address (hex like "0x401000" or decimal).

    Returns:
        Status string from the server, or an error message. The server
        rejects requests against auto-generated symbols and returns a clear
        error in that case.
    """
    if not address:
        return "Error: address is required"
    data = get_json("undefineUserSymbol", {"address": address})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        removed = data.get("removed") or "(unnamed)"
        return f"Removed symbol {removed!r} at {data.get('address')}"
    return str(data)


@mcp.tool()
def define_user_data_var(address: str, type: str) -> str:
    """
    Type a global at an address as a user data variable.

    Typing a global propagates the type through every cross-reference in
    decompilation — one of the highest-leverage RE actions. Use after the
    type itself is known to Binary Ninja (declared via `declare_c_type` or
    `define_types`, or from a stock type like "int" or "uint8_t").

    Args:
        address: Target address (hex like "0x401000" or decimal).
        type: C-style type string (e.g. "int", "uint8_t", "struct Foo *",
            "char[16]"). Parsed via BinaryView.parse_type_string.

    Returns:
        Status string from the server, or an error message.
    """
    if not address or not type:
        return "Error: address and type are required"
    data = get_json("defineUserDataVar", {"address": address, "type": type})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return (
            f"Defined data variable {data.get('type')!r} at {data.get('address')}"
        )
    return str(data)


@mcp.tool()
def read_int(address: str, size: int, signed: bool = False) -> str:
    """
    Read a typed integer at an address.

    Use as a primitive for any analysis that needs raw values without
    dumping bytes: inline immediates, length prefixes, header fields,
    structure members. Cheaper and more direct than `/hexdump` + parse.

    Args:
        address: Target address (hex like "0x401000" or decimal).
        size: 1, 2, 4, or 8 bytes.
        signed: True for two's-complement interpretation.

    Returns:
        Formatted line "value=<decimal> hex=<hex>" or an error message.
    """
    if not address:
        return "Error: address is required"
    if int(size) not in (1, 2, 4, 8):
        return "Error: size must be 1, 2, 4, or 8"
    data = get_json(
        "readInt",
        {
            "address": address,
            "size": str(int(size)),
            "signed": "true" if signed else "false",
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict):
        return (
            f"address={data.get('address')}  size={data.get('size')}  "
            f"signed={data.get('signed')}  value={data.get('value')}  "
            f"hex={data.get('hex')}"
        )
    return str(data)


@mcp.tool()
def read_pointer(address: str) -> str:
    """
    Read a pointer-sized integer at an address.

    Critical for following vtables, jump tables, function-pointer arrays,
    and any pointer-shaped data structure. The size is taken from the
    current view (4 bytes on a 32-bit binary, 8 bytes on a 64-bit one),
    so the caller never has to specify it. When the resulting value
    matches a known symbol, the symbol name is included for navigation.

    Args:
        address: Target address (hex like "0x401000" or decimal).

    Returns:
        Formatted line including the value and (when known) the symbol
        the pointer points at, or an error message.
    """
    if not address:
        return "Error: address is required"
    data = get_json("readPointer", {"address": address})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict):
        points_to = data.get("points_to")
        suffix = f"  -> {points_to}" if points_to else ""
        return (
            f"address={data.get('address')}  "
            f"value={data.get('value')}  hex={data.get('hex')}{suffix}"
        )
    return str(data)


@mcp.tool()
def add_type_library(path: str) -> str:
    """
    Load a .bntl type library and attach it to the current binary view.

    Highest-leverage typing action: one call types every matching import
    from the library at once. Typical pairings:

      - Windows malware: load `msvcrt.bntl`, `kernel32.bntl`, `user32.bntl`.
      - Linux malware / firmware: load `libc.bntl`.
      - Windows kernel drivers: load `ntoskrnl.bntl` or the WDK typelibs.

    Args:
        path: Absolute path to a .bntl file.

    Returns:
        Status string from the server, or an error.
    """
    if not path:
        return "Error: path is required"
    data = get_json("addTypeLibrary", {"path": path})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return (
            f"Attached type library {data.get('name')!r} "
            f"(arch: {data.get('arch')}) from {data.get('path')}"
        )
    return str(data)


@mcp.tool()
def demangle(name: str, abi: str = "auto") -> str:
    """
    Demangle a C++ symbol name to a human-readable form.

    Use whenever you see a mangled import or symbol — `_ZN5MyLib...` from
    Itanium ABI (Linux/macOS C++), `?Foo@Bar@@QEAA...` from Microsoft ABI
    (Windows C++). The agent's first action on a C++ binary's import list
    should usually be a demangle pass.

    Args:
        name: Mangled symbol string.
        abi: "auto" (default — tries Itanium then MSVC), "gnu3"/"itanium",
            or "ms"/"msvc".

    Returns:
        Demangled string with the ABI that worked, or an error message.
    """
    if not name:
        return "Error: name is required"
    data = get_json("demangle", {"name": name, "abi": abi})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        type_str = data.get("type")
        type_suffix = f"   :: {type_str}" if type_str else ""
        return (
            f"[{data.get('abi')}] {data.get('mangled')} -> "
            f"{data.get('demangled')}{type_suffix}"
        )
    return str(data)


@mcp.tool()
def get_data_var_at(address: str) -> str:
    """
    Read the data variable at an address.

    Returns the name (if any), C type, and a string representation of the
    stored value. Use this as the targeted-read companion to
    `define_user_data_var` / `undefine_user_data_var` — much cheaper than
    paginating `list_data_items` to find one address.

    Args:
        address: Target address (hex like "0x401000" or decimal).

    Returns:
        Formatted lines describing the data variable, or an error.
    """
    if not address:
        return "Error: address is required"
    data = get_json("getDataVarAt", {"address": address})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if not isinstance(data, dict):
        return str(data)
    name = data.get("name") or "(unnamed)"
    type_str = data.get("type") or "(unknown)"
    value = data.get("value") or "(no value)"
    return (
        f"address : {data.get('address')}\n"
        f"name    : {name}\n"
        f"type    : {type_str}\n"
        f"value   : {value}"
    )


@mcp.tool()
def undefine_user_data_var(address: str) -> str:
    """
    Remove a user data variable at an address.

    Args:
        address: Target address (hex like "0x401000" or decimal).

    Returns:
        Status string from the server, or an error message. The server
        returns 404 if no data variable exists at the address.
    """
    if not address:
        return "Error: address is required"
    data = get_json("undefineUserDataVar", {"address": address})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        prior = data.get("removed_type") or "(unknown type)"
        return f"Removed data variable (was {prior}) at {data.get('address')}"
    return str(data)


@mcp.tool()
def reanalyze_function(function: str) -> str:
    """
    Trigger reanalysis of a single function.

    Cheaper than `update_analysis` when only one function changed (e.g.
    after a single retype or prototype change). The call returns as soon
    as BN accepts the request; if you need the result fully settled
    before the next query, call `update_analysis` afterwards.

    Args:
        function: Function name or address.

    Returns:
        Status string from the server.
    """
    if not function:
        return "Error: function is required"
    data = get_json("reanalyzeFunction", {"function": function})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return (
            f"Reanalysis triggered for {data.get('function')!r} "
            f"at {data.get('address')}"
        )
    return str(data)


@mcp.tool()
def update_analysis() -> str:
    """
    Force a full Binary Ninja reanalysis and block until idle.

    Run after a batch of mutations (rename, retype, declare_c_type,
    define_user_symbol, define_user_data_var, etc.) when subsequent queries
    need to observe propagated state — type-through-xref inference, new
    callers/callees, updated decompilation, etc. May be slow on large
    binaries; no client-side timeout is applied.

    Returns:
        Status string including wall-clock duration of the analysis pass.
    """
    # Pass timeout=None so we wait as long as BN needs. Default 5s would
    # almost never let a real reanalysis finish.
    data = get_json("updateAnalysisAndWait", timeout=None)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        ms = data.get("duration_ms")
        info = data.get("analysis_info") or {}
        state = info.get("state") if isinstance(info, dict) else None
        if state:
            return f"Analysis settled in {ms} ms (state: {state})"
        return f"Analysis settled in {ms} ms"
    return str(data)


def _format_undo_redo(data: dict, action: str) -> str:
    can_undo = data.get("can_undo")
    can_redo = data.get("can_redo")
    pieces = []
    if can_undo is True:
        pieces.append("undo available")
    elif can_undo is False:
        pieces.append("no undo left")
    if can_redo is True:
        pieces.append("redo available")
    elif can_redo is False:
        pieces.append("no redo left")
    suffix = f" ({'; '.join(pieces)})" if pieces else ""
    verb = "Undone" if action == "undo" else "Redone"
    return f"{verb}{suffix}"


@mcp.tool()
def undo() -> str:
    """
    Undo the most recent Binary Ninja action.

    Useful when an experimental mutation didn't have the intended effect —
    e.g. `define_user_data_var` propagated a wrong type through xrefs, or
    a `rename_function` decision should be reverted. One call rolls the
    last action back without manually reconstructing the prior state.

    Returns:
        Status string including whether further undo / redo is available.
    """
    data = get_json("undo")
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return _format_undo_redo(data, "undo")
    return str(data)


@mcp.tool()
def redo() -> str:
    """
    Redo the most recently undone Binary Ninja action.

    Returns:
        Status string including whether further undo / redo is available.
    """
    data = get_json("redo")
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return _format_undo_redo(data, "redo")
    return str(data)


@mcp.tool()
def list_tag_types() -> list:
    """
    List all tag types known to Binary Ninja for the current binary.

    Includes BN built-ins (Important, Bug, Bookmark, ...) and any
    user-created categories. Use this before `add_tag` if you want to know
    what categories already exist; `create_tag_type` will set one up
    on the fly otherwise.

    Returns:
        List of strings, one per tag type, formatted as "<icon>  <name>".
    """
    data = get_json("tagTypes")
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    types = data.get("tag_types", []) if isinstance(data, dict) else []
    if not types:
        return ["(no tag types defined)"]
    return [f"{t.get('icon') or ' '}  {t.get('name')}" for t in types]


@mcp.tool()
def create_tag_type(name: str, icon: str = "🏷") -> str:
    """
    Create a tag type, or no-op if one with that name already exists.

    Tag types are categories like "Crypto", "Syscall", "TODO", "Reviewed".
    Use them to mark progress without polluting decompilation with
    comments — comments are for *explaining* code, tags are for *marking*
    locations.

    Args:
        name: Tag-type name (e.g. "Crypto").
        icon: Short string used as BN's icon, typically a single emoji.
            Defaults to a generic tag glyph.

    Returns:
        Status string indicating whether the type was created or already
        existed.
    """
    if not name:
        return "Error: name is required"
    data = get_json("createTagType", {"name": name, "icon": icon})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        verb = "Created" if data.get("created") else "Already existed:"
        return f"{verb} tag type {data.get('name')!r} (icon: {data.get('icon')})"
    return str(data)


@mcp.tool()
def add_tag(
    address: str,
    tag_type: str,
    data: str = "",
    kind: str = "auto",
) -> str:
    """
    Attach a tag to an address, function, or data location.

    Tags are the right tool for marking progress and findings during RE
    work — "checked this", "calls crypto here", "TODO: verify size". Use
    them instead of comments when you want a *marker* the user can browse
    in BN's tags pane, rather than a *note* embedded in the disassembly.

    Args:
        address: Target address (hex like "0x401000" or decimal).
        tag_type: Tag-type name. Auto-created (with the default icon) if it
            doesn't already exist.
        data: Optional description / payload text.
        kind: One of:
            - "auto" (default): the server picks "function" if the address
              is the start of a function, "address" if it's inside a
              function body, "data" otherwise.
            - "address": code-address tag (must be inside a function).
            - "function": tags the whole function containing the address.
            - "data": data-section tag.

    Returns:
        Status string with the chosen kind and the address tagged.
    """
    if not address or not tag_type:
        return "Error: address and tag_type are required"
    data_payload = data or ""
    resp = get_json(
        "addTag",
        {
            "address": address,
            "tagType": tag_type,
            "data": data_payload,
            "kind": kind,
        },
    )
    if not resp:
        return "Error: no response"
    if isinstance(resp, dict) and resp.get("error"):
        return f"Error: {resp['error']}"
    if isinstance(resp, dict) and resp.get("status") == "ok":
        payload = resp.get("data") or ""
        suffix = f": {payload}" if payload else ""
        return (
            f"Added {resp.get('kind')} tag {resp.get('tag_type')!r} "
            f"at {resp.get('address')}{suffix}"
        )
    return str(resp)


@mcp.tool()
def get_tags_at(address: str) -> list:
    """
    List all tags at an address (data, in-function address, and the
    containing function's tags).

    Args:
        address: Target address (hex like "0x401000" or decimal).

    Returns:
        List of human-readable strings, one per tag, or "(no tags)" /
        an error message.
    """
    if not address:
        return ["Error: address is required"]
    resp = get_json("getTagsAt", {"address": address})
    if not resp:
        return ["Error: no response"]
    if isinstance(resp, dict) and resp.get("error"):
        return [f"Error: {resp['error']}"]
    if not isinstance(resp, dict):
        return [str(resp)]
    if (resp.get("total") or 0) == 0:
        return ["(no tags)"]
    out: list = []
    for tag in resp.get("data_tags", []) or []:
        out.append(_format_tag(tag))
    for tag in resp.get("address_tags", []) or []:
        out.append(_format_tag(tag))
    for tag in resp.get("function_tags", []) or []:
        out.append(_format_tag(tag))
    return out


def _format_tag(tag: dict) -> str:
    icon = tag.get("icon") or ""
    kind = tag.get("kind") or "?"
    name = tag.get("type") or "?"
    data = tag.get("data") or ""
    base = f"[{kind}] {icon}  {name}"
    return f"{base}: {data}" if data else base


@mcp.tool()
def get_parameter_at(address: str, index: int, function: str = "") -> str:
    """
    Resolve what value is being passed as a callsite's i-th argument.

    Answers "what does this strcpy/memcpy/syscall get called with?" using
    the MLIL call instruction's params attribute — gives the lifted
    expression that's actually being passed, not raw register names.

    Args:
        address: Call instruction address (hex like "0x401080" or decimal).
        index: Zero-based parameter index.
        function: Optional containing-function name or address. When
            omitted the server auto-resolves it from the address.

    Returns:
        Status string describing the resolved expression, callee name (when
        determinable), and total parameter count. Returns 404-style error
        if there's no call at the address or the index is out of range.
    """
    if not address:
        return "Error: address is required"
    if index is None or int(index) < 0:
        return "Error: index must be a non-negative integer"
    params: dict = {"address": address, "index": str(int(index))}
    if function:
        params["function"] = function
    data = get_json("getParameterAt", params)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        callee = data.get("callee") or "(unknown callee)"
        return (
            f"At {data.get('address')} in {data.get('function')!r}, "
            f"call to {callee} arg[{data.get('index')}] = "
            f"{data.get('expression')} (of {data.get('param_count')} total)"
        )
    return str(data)


@mcp.tool()
def get_constants_referenced_by(address: str, function: str = "") -> list:
    """
    List immediate constants referenced by an instruction.

    Use to answer "what magic values does this instruction touch?" without
    parsing disassembly text. Composes with `find_constant` for tracing
    where a value flows.

    Args:
        address: Instruction address (hex like "0x401080" or decimal).
        function: Optional function name or address. If omitted, the
            server auto-resolves the containing function.

    Returns:
        List of "<value>  size=<n>  pointer=<bool>  intermediate=<bool>"
        lines, or "(no constants)" / an error message.
    """
    if not address:
        return ["Error: address is required"]
    params: dict = {"address": address}
    if function:
        params["function"] = function
    data = get_json("getConstantsReferencedBy", params)
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    consts = data.get("constants", []) or []
    if not consts:
        return ["(no constants)"]
    return [
        f"{c.get('value')}  size={c.get('size')}  "
        f"pointer={c.get('pointer')}  intermediate={c.get('intermediate')}"
        for c in consts
    ]


@mcp.tool()
def get_regs_read_by(address: str, function: str = "") -> list:
    """
    List registers read by an instruction.

    Args:
        address: Instruction address (hex like "0x401080" or decimal).
        function: Optional function name or address. If omitted, the
            server auto-resolves the containing function.

    Returns:
        List of register name strings, or "(no registers)" / an error message.
    """
    if not address:
        return ["Error: address is required"]
    params: dict = {"address": address}
    if function:
        params["function"] = function
    data = get_json("getRegsReadBy", params)
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    regs = data.get("registers", []) or []
    if not regs:
        return ["(no registers)"]
    return list(regs)


@mcp.tool()
def get_regs_written_by(address: str, function: str = "") -> list:
    """
    List registers written by an instruction.

    Args:
        address: Instruction address (hex like "0x401080" or decimal).
        function: Optional function name or address. If omitted, the
            server auto-resolves the containing function.

    Returns:
        List of register name strings, or "(no registers)" / an error message.
    """
    if not address:
        return ["Error: address is required"]
    params: dict = {"address": address}
    if function:
        params["function"] = function
    data = get_json("getRegsWrittenBy", params)
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    regs = data.get("registers", []) or []
    if not regs:
        return ["(no registers)"]
    return list(regs)


def _format_var_ref(ref: dict) -> str:
    addr = ref.get("address") or "?"
    il_type = ref.get("il_type") or "?"
    snippet = ref.get("hlil") or ""
    base = f"{addr}  [{il_type}]"
    return f"{base}  {snippet}" if snippet else base


@mcp.tool()
def get_function_metadata(function: str) -> list:
    """
    Return diagnostic flags for a function: a quick read of why
    decompilation might look sparse or weird before retrying.

    Bundles BN's `Function.is_thunk`, `can_return`,
    `has_variable_arguments`, `is_pure`, `analysis_skipped`,
    `analysis_skip_reason`, `analysis_skip_override`, `auto`, plus the
    parameter count.

    Args:
        function: Function name or address (hex like "0x401000" or decimal).

    Returns:
        List of "<field>: <value>" lines, or an error message.
    """
    if not function:
        return ["Error: function is required"]
    data = get_json("getFunctionMetadata", {"function": function})
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    keys = (
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
    )
    width = max(len(k) for k in keys)
    out: list = []
    for k in keys:
        v = data.get(k)
        if isinstance(v, bool):
            v_str = "true" if v else "false"
        elif v is None:
            v_str = "-"
        else:
            v_str = str(v)
        out.append(f"{k.ljust(width)} : {v_str}")
    return out


@mcp.tool()
def set_function_can_return(function: str, can_return: bool) -> str:
    """
    Override BN's no-return inference for a function.

    Use this after `get_function_metadata` reveals BN guessed wrong about
    whether a function returns — e.g. for custom abort/panic wrappers that
    BN doesn't recognize. Wrong `can_return` corrupts the CFG of every
    caller, so this is one of the highest-impact corrections available.

    Args:
        function: Function name or address (hex like "0x401000" or decimal).
        can_return: True to mark as returning, False as never-returning.

    Returns:
        Status string from the server.
    """
    if not function:
        return "Error: function is required"
    data = get_json(
        "setFunctionCanReturn",
        {"function": function, "canReturn": "true" if can_return else "false"},
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return (
            f"Set {data.get('function')!r} can_return={data.get('can_return')} "
            f"at {data.get('address')}"
        )
    return str(data)


@mcp.tool()
def set_function_return_type(function: str, type: str) -> str:
    """
    Set just a function's return type without rewriting the prototype.

    Args:
        function: Function name or address.
        type: C-style type string (e.g. "int", "void *", "struct Foo *").

    Returns:
        Status string from the server.
    """
    if not function or not type:
        return "Error: function and type are required"
    data = get_json(
        "setFunctionReturnType",
        {"function": function, "type": type},
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return (
            f"Set {data.get('function')!r} return_type={data.get('return_type')!r} "
            f"at {data.get('address')}"
        )
    return str(data)


@mcp.tool()
def set_function_inline(function: str, inline: bool) -> str:
    """
    Force or un-force BN's inline-during-analysis behavior for a function.

    Useful for tiny helpers where inlining cleans up decompilation, or
    when BN's auto-inline heuristic made the wrong call.

    Args:
        function: Function name or address.
        inline: True to force inlining, False to disable.

    Returns:
        Status string from the server.
    """
    if not function:
        return "Error: function is required"
    data = get_json(
        "setFunctionInline",
        {"function": function, "inline": "true" if inline else "false"},
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return (
            f"Set {data.get('function')!r} inline_during_analysis="
            f"{data.get('inline_during_analysis')} at {data.get('address')}"
        )
    return str(data)


@mcp.tool()
def get_symbols_by_type(
    type: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list:
    """
    List symbols of a given type, optionally bounded by address range.

    Use to get a focused slice of the symbol table without paginating
    through `list_imports` / `list_exports` and filtering yourself.

    Args:
        type: Agent-friendly alias (`function`, `data`, `import`,
            `import_data`, `import_address`, `external`, `library_function`,
            `symbolic_function`, `label`) or a raw BN `SymbolType` enum
            name (e.g. `"FunctionSymbol"`).
        start: Optional starting address (hex or decimal).
        end: Optional ending address (exclusive).
        limit: Cap on results (default 100; 0 or negative = unlimited).

    Returns:
        List of "<address>\\t<name>" lines, or "(no symbols)" / an error.
    """
    if not type:
        return ["Error: type is required"]
    params: dict = {"type": type, "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    data = get_json("getSymbolsByType", params)
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    syms = data.get("symbols", []) or []
    if not syms:
        return ["(no symbols)"]
    return [f"{s.get('address')}\t{s.get('name')}" for s in syms]


@mcp.tool()
def get_ssa_var_uses(
    function: str,
    variable: str,
    version: int = 0,
    il_level: str = "hlil",
) -> list:
    """
    SSA-precise use sites of a local variable inside a function.

    Distinct from `get_var_uses`: that one returns all references to the
    variable in any form; this one filters to a specific SSA version,
    which lets you reason about one logical "version" of a value through
    its uses (e.g. after a known definition you care about).

    Args:
        function: Function name or address.
        variable: Local variable name.
        version: SSA version (default 0 = the first definition).
        il_level: "hlil" (default) or "mlil".

    Returns:
        List of "<address>  [<il_type>]  <expression>" lines, or
        "(no uses)" / an error.
    """
    if not function or not variable:
        return ["Error: function and variable are required"]
    data = get_json(
        "getSsaVarUses",
        {
            "function": function,
            "variable": variable,
            "version": str(int(version)),
            "ilLevel": il_level,
        },
    )
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    uses = data.get("uses", []) or []
    if not uses:
        return ["(no uses)"]
    return [
        f"{u.get('address')}  [{u.get('il_type')}]  {u.get('expression')}"
        for u in uses
    ]


@mcp.tool()
def get_ssa_var_definition(
    function: str,
    variable: str,
    version: int = 0,
    il_level: str = "hlil",
) -> str:
    """
    SSA-precise definition site of a local variable inside a function.

    SSA semantics guarantee at most one definition per (variable,
    version), so the response is a single line (not a list).

    Args:
        function: Function name or address.
        variable: Local variable name.
        version: SSA version (default 0).
        il_level: "hlil" (default) or "mlil".

    Returns:
        "<address>  [<il_type>]  <expression>" line, or
        "(no definition)" / an error.
    """
    if not function or not variable:
        return "Error: function and variable are required"
    data = get_json(
        "getSsaVarDefinition",
        {
            "function": function,
            "variable": variable,
            "version": str(int(version)),
            "ilLevel": il_level,
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if not isinstance(data, dict):
        return str(data)
    d = data.get("definition")
    if d is None:
        return "(no definition)"
    return f"{d.get('address')}  [{d.get('il_type')}]  {d.get('expression')}"


@mcp.tool()
def get_var_uses(function: str, variable: str, il_level: str = "all") -> list:
    """
    Find every use site of a local variable inside a function.

    Use this before renaming or retyping a local to confirm the new name
    or type fits everywhere the variable appears, or for taint-style
    reasoning ("where else does this argument flow?").

    Args:
        function: Function name or address (hex like "0x401000" or decimal).
        variable: Local variable name (as shown by `get_stack_frame_vars` or
            in the decompilation).
        il_level: Filter — "all" (default), "hlil", "mlil", or "llil".
            Case-insensitive substring match.

    Returns:
        List of strings, one per use site, formatted as
        "<address>  [<il_type>]  <hlil snippet>". Returns "(no uses)" if
        the variable isn't referenced.
    """
    if not function or not variable:
        return ["Error: function and variable are required"]
    data = get_json(
        "getVarUses",
        {"function": function, "variable": variable, "ilLevel": il_level},
    )
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    uses = data.get("uses", []) or []
    if not uses:
        return ["(no uses)"]
    return [_format_var_ref(u) for u in uses]


@mcp.tool()
def get_var_definitions(
    function: str, variable: str, il_level: str = "all"
) -> list:
    """
    Find every definition (write) site of a local variable inside a function.

    Use this to understand where a value comes from — e.g. "this argument
    looks like a length, where is it computed?" — without re-parsing the
    decompilation text.

    Args:
        function: Function name or address (hex like "0x401000" or decimal).
        variable: Local variable name.
        il_level: Filter — "all" (default), "hlil", "mlil", or "llil".

    Returns:
        List of strings, one per definition site, formatted as
        "<address>  [<il_type>]  <hlil snippet>". Returns "(no definitions)"
        if the variable is never written.
    """
    if not function or not variable:
        return ["Error: function and variable are required"]
    data = get_json(
        "getVarDefinitions",
        {"function": function, "variable": variable, "ilLevel": il_level},
    )
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [f"Error: {data['error']}"]
    if not isinstance(data, dict):
        return [str(data)]
    defs = data.get("definitions", []) or []
    if not defs:
        return ["(no definitions)"]
    return [_format_var_ref(d) for d in defs]


@mcp.tool()
def get_binary_status() -> str:
    """
    Get the current status of the loaded binary.
    """
    return safe_get("status")[0]


@mcp.tool()
def list_binaries() -> list:
    """
    List managed/open binaries known to the server with ids and active flag.
    """
    data = get_json("binaries")
    if not data:
        return ["Error: no response"]
    if isinstance(data, dict) and data.get("error"):
        return [data.get("error")]
    items = data.get("binaries", [])
    out = []
    for it in items:
        vid = it.get("id")
        view_id = it.get("view_id")
        fn = it.get("filename")
        basename = it.get("basename") or ""
        selectors = it.get("selectors") or []
        active = it.get("active")
        label = basename or fn or "(unknown)"
        full = fn or "(no filename)"
        selector_text = ", ".join(str(s) for s in selectors if s)
        mark = " *active*" if active else ""
        view_part = f" view={view_id}" if view_id else ""
        out.append(
            f"{vid}. {label}{view_part}{mark}\n    path: {full}\n    selectors: {selector_text}"
        )
    return out


@mcp.tool()
def select_binary(view: str) -> str:
    """
    Select which binary to analyze by ordinal, internal view id, full path, or basename.
    Call this after listing binaries whenever you need to switch analysis targets.
    """
    data = get_json("selectBinary", {"view": view})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        import json as _json

        return _json.dumps(data, indent=2, ensure_ascii=False)
    sel = data.get("selected") if isinstance(data, dict) else None
    if sel:
        ordinal = sel.get("id") or "?"
        view_id = sel.get("view_id") or ""
        fn = sel.get("filename") or ""
        basename = sel.get("basename") or ""
        selectors = sel.get("selectors") or []
        selector_text = ", ".join(str(s) for s in selectors if s)
        display_name = basename or fn or "(unknown)"
        view_part = f" (view {view_id})" if view_id else ""
        path_part = f"\nFull path: {fn}" if fn else ""
        return (
            f"Selected {ordinal}: {display_name}{view_part}{path_part}\nSelectors: {selector_text}"
        )
    return str(data)


@mcp.tool()
def delete_comment(address: str) -> str:
    """
    Delete the comment at a specific address.
    """
    return safe_post("comment", {"address": address, "_method": "DELETE"})


@mcp.tool()
def delete_function_comment(function_name: str) -> str:
    """
    Delete the comment for a function.
    """
    return safe_post("comment/function", {"name": function_name, "_method": "DELETE"})


@mcp.tool()
def function_at(address: str) -> str:
    """
    Retrive the name of the function the address belongs to. Address must be in hexadecimal format 0x00001
    """
    return safe_get("functionAt", {"address": address})


@mcp.tool()
def get_user_defined_type(type_name: str) -> str:
    """
    Retrive definition of a user defined type (struct, enumeration, typedef, union)
    """
    return safe_get("getUserDefinedType", {"name": type_name})


@mcp.tool()
def undefine_user_type(name: str) -> str:
    """
    Remove a user-defined type by name.

    Args:
        name: Type name as it appears in `list_local_types` /
            `get_user_defined_type`. Auto-generated and library types are
            rejected (only user types can be removed via this tool).

    Returns:
        Status string describing the removal, or an error. The server
        returns 404 if the name does not name a user-defined type.
    """
    if not name:
        return "Error: type name is required"
    data = get_json("undefineUserType", {"name": name})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "ok":
        prior = data.get("removed_declaration")
        if prior:
            return f"Removed user type {data.get('name')!r} (was: {prior})"
        return f"Removed user type {data.get('name')!r}"
    return str(data)


@mcp.tool()
def get_xrefs_to(address: str) -> list:
    """
    Get all cross references (code and data) to the given address.
    Address can be hex (e.g., 0x401000) or decimal.
    """
    return safe_get("getXrefsTo", {"address": address})


@mcp.tool()
def get_xrefs_to_field(struct_name: str, field_name: str) -> list:
    """
    Get all cross references to a named struct field (member).
    """
    return safe_get("getXrefsToField", {"struct": struct_name, "field": field_name})


@mcp.tool()
def get_xrefs_to_struct(struct_name: str) -> list:
    """
    Get cross references/usages related to a struct name.
    """
    return safe_get("getXrefsToStruct", {"name": struct_name})


@mcp.tool()
def get_xrefs_to_type(type_name: str) -> list:
    """
    Get xrefs/usages related to a struct or type name.
    Includes global instances, code refs to those, HLIL matches, and functions whose signature mentions the type.
    """
    return safe_get("getXrefsToType", {"name": type_name})


@mcp.tool()
def get_xrefs_to_enum(enum_name: str) -> list:
    """
    Get usages/xrefs of an enum by scanning for member values and matches.
    """
    return safe_get("getXrefsToEnum", {"name": enum_name})


@mcp.tool()
def get_xrefs_to_union(union_name: str) -> list:
    """
    Get cross references/usages related to a union type by name.
    """
    return safe_get("getXrefsToUnion", {"name": union_name})


@mcp.tool()
def get_stack_frame_vars(function_identifier: str) -> list:
    """
    Get stack frame variable information for a function by name or address.
    Returns names, offsets, sizes, and types of local variables.
    """
    ident = (function_identifier or "").strip()
    params = {}
    # Choose param name based on identifier format
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["name"] = ident
    data = get_json("getStackFrameVars", params)
    if not data:
        return []
    if isinstance(data, dict) and data.get("error"):
        return []
    if isinstance(data, dict) and data.get("stack_frame_vars"):
        return data["stack_frame_vars"]
    return []


@mcp.tool()
def format_value(address: str, text: str, size: int = 0) -> list:
    """
    Convert and annotate a value at an address in Binary Ninja.
    Adds a comment with hex/dec and C literal/string so you can see the change.
    """
    return safe_get("formatValue", {"address": address, "text": text, "size": size}, timeout=None)


@mcp.tool()
def convert_number(text: str, size: int = 0) -> str:
    """
    Convert a number or string to multiple representations (hex/dec/bin, LE/BE, C char/string literals).
    Accepts decimal (e.g., 123), hex (0x7b or 7Bh), binary (0b1111011), octal (0o173),
    char ('A'), or string ("ABC" with escapes like \x41).
    """
    data = get_json("convertNumber", {"text": text, "size": size}, timeout=None)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {data['error']}"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def get_type_info(type_name: str) -> str:
    """
    Resolve a type name and return its declaration and details (kind, members, enum values).
    """
    data = get_json("getTypeInfo", {"name": type_name}, timeout=None)
    if not data:
        return "Error: no response"
    if "error" in data:
        return f"Error: {data.get('error')}"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


def _normalize_identifier_input(value: str | list[str]) -> list[str]:
    tokens: list[str] = []
    if isinstance(value, str):
        raw = value.replace(";", ",").split(",")
        tokens.extend([tok.strip() for tok in raw if tok.strip()])
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if item is None:
                continue
            tokens.extend(_normalize_identifier_input(str(item)))
    return tokens


@mcp.tool()
def get_callers(identifiers: str) -> str:
    """
    List callers and caller sites for one or more function identifiers (name or address).
    Provide comma-separated identifiers like "sub_401000,main".
    """
    items = _normalize_identifier_input(identifiers)
    if not items:
        return "Error: provide at least one identifier"
    data = get_json("getCallers", {"identifiers": ",".join(items)}, timeout=None)
    if not data:
        return "Error: no response"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def get_callees(identifiers: str) -> str:
    """
    List callees and call sites for one or more function identifiers (name or address).
    Provide comma-separated identifiers like "sub_401000,main".
    """
    items = _normalize_identifier_input(identifiers)
    if not items:
        return "Error: provide at least one identifier"
    data = get_json("getCallees", {"identifiers": ",".join(items)}, timeout=None)
    if not data:
        return "Error: no response"
    import json as _json

    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def set_function_prototype(name_or_address: str, prototype: str) -> str:
    """
    Set a function's prototype by name or address.
    """
    # Use GET like other endpoints (server accepts complex prototypes)
    ident = (name_or_address or "").strip()
    params = {"prototype": prototype}
    # Choose param name based on identifier format
    if ident.lower().startswith("0x") or ident.isdigit():
        params["address"] = ident
    else:
        params["name"] = ident
    data = get_json("setFunctionPrototype", params)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and "status" in data:
        return f"Applied prototype at {data.get('address')}: {data.get('applied_type')}"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def make_function_at(address: str, platform: str = "") -> str:
    """
    Create a function at the given address. Platform is optional (e.g., "linux-x86_64").
    Use "default" to explicitly select the BinaryView/platform default.
    Returns status and function info; no-op if the function already exists.
    """
    params = {"address": address}
    if platform:
        params["platform"] = platform
    data = get_json("makeFunctionAt", params)
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        import json as _json

        return _json.dumps(data, indent=2, ensure_ascii=False)
    if isinstance(data, dict) and data.get("status") == "exists":
        return f"Function already exists at {data.get('address')}: {data.get('name')}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return f"Created function at {data.get('address')}: {data.get('name')}"
    return str(data)


@mcp.tool()
def list_platforms() -> str:
    """
    List all available platform names from Binary Ninja.
    """
    data = get_json("platforms")
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("error"):
        import json as _json

        return _json.dumps(data, indent=2, ensure_ascii=False)
    plats = data.get("platforms") if isinstance(data, dict) else None
    if not plats:
        return "(no platforms)"
    return "\n".join(plats)


@mcp.tool()
def declare_c_type(c_declaration: str) -> str:
    """
    Create or update a local type from a C declaration.
    """
    data = get_json("declareCType", {"declaration": c_declaration})
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("defined_types"):
        names = ", ".join(data["defined_types"].keys())
        return f"Declared types ({data.get('count', 0)}): {names}"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def set_local_variable_type(function_address: str, variable_name: str, new_type: str) -> str:
    """
    Set a local variable's type.
    """
    data = get_json(
        "setLocalVariableType",
        {
            "functionAddress": function_address,
            "variableName": variable_name,
            "newType": new_type,
        },
    )
    if not data:
        return "Error: no response"
    if isinstance(data, dict) and data.get("status") == "ok":
        return f"Retyped {data.get('variable')} in {data.get('function')} to {data.get('applied_type')}"
    if isinstance(data, dict) and "error" in data:
        return f"Error: {data['error']}"
    return str(data)


@mcp.tool()
def patch_bytes(address: str, data: str, save_to_file: bool = True) -> str:
    """
    Patch bytes at a given address in the binary.
    - address: Address to patch (hex string like "0x401000" or decimal)
    - data: Hex string of bytes to write (e.g., "90 90" or "9090" or "0x90 0x90")
    - save_to_file: If True (default), save patched binary to disk and re-sign on macOS.
                    If False, only modify in memory without affecting the original file.

    Returns status with original and patched bytes.
    On macOS, automatically re-signs the binary after patching to avoid execution errors.
    """
    # Handle boolean type conversion (MCP may pass as string)
    if isinstance(save_to_file, str):
        save_to_file = save_to_file.lower() not in ("false", "0", "no")

    params = {"address": address, "data": data, "save_to_file": save_to_file}
    result = get_json("patch", params)
    if not result:
        return "Error: no response"

    status = result.get("status") if isinstance(result, dict) else None
    if status in ("ok", "partial"):
        orig = result.get("original_bytes", "")
        patched = result.get("patched_bytes", "")
        written = result.get("bytes_written", 0)
        requested = result.get("bytes_requested", 0)
        addr = result.get("address", address)
        saved = result.get("saved_to_file", False)
        saved_path = result.get("saved_path", "")
        save_error = result.get("save_error", "")
        codesign = result.get("codesign", {})
        warning = result.get("warning", "")

        msg = f"Patched {written}/{requested} bytes at {addr}"
        if status == "partial":
            msg += " (PARTIAL WRITE)"
        if warning:
            msg += f"\nWarning: {warning}"
        if orig:
            msg += f"\nOriginal: {orig}"
        if patched:
            msg += f"\nPatched:  {patched}"
        if saved:
            msg += f"\nSaved to file: {saved_path}"
        elif save_error:
            msg += f"\nWarning: File not saved - {save_error}"

        # Show codesign status for macOS
        if codesign:
            if codesign.get("success"):
                msg += f"\nCode signing: {codesign.get('message', 'Re-signed successfully')}"
            elif codesign.get("attempted"):
                msg += f"\nCode signing: Failed - {codesign.get('error', 'Unknown error')}"

        return msg
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"
    return str(result)


if __name__ == "__main__":
    # Important: write any logs to stderr to avoid corrupting MCP stdio JSON-RPC
    print("Starting MCP bridge service...", file=_sys.stderr)
    try:
        mcp.run()
    except Exception as _e:
        # Ensure any runtime exception is captured in the log file
        _bridge_excepthook(type(_e), _e, _e.__traceback__)
        raise
