import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import binaryninja as bn

from ..api.endpoints import BinaryNinjaEndpoints
from ..core.binary_operations import BinaryOperations
from ..core.config import Config
from ..utils.address import (
    AddressParseError,
    is_address_literal,
    parse_address,
    parse_optional_address,
)
from ..utils.approval import require_approval
from ..utils.auth import matches as auth_matches
from ..utils.auth import read_token, token_file_path
from ..utils.number_utils import convert_number as util_convert_number
from ..utils.string_utils import parse_int_or_default


class MCPRequestHandler(BaseHTTPRequestHandler):
    binary_ops = None  # Will be set by the server
    _BINARY_OPTIONAL_PATH_PREFIXES: ClassVar[tuple[str, ...]] = (
        "/status",
        "/convertNumber",
        "/platforms",
        "/binaries",
        "/views",
        "/selectBinary",
    )
    _BINARY_OPTIONAL_POST_PATHS: ClassVar[set[str]] = {"/load"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def endpoints(self):
        # Create endpoints on demand to ensure binary_ops is set
        if not hasattr(self, "_endpoints"):
            if not self.binary_ops:
                raise RuntimeError("binary_ops not initialized")
            self._endpoints = BinaryNinjaEndpoints(self.binary_ops)
        return self._endpoints

    def log_message(self, format, *args):
        bn.log_info(format % args)

    def _set_headers(self, content_type="application/json", status_code=200):
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            # No Access-Control-Allow-Origin: the supported bridges talk to
            # this server via Python requests / Node axios, which don't enforce
            # CORS, so they don't need the header. Omitting it stops a browser
            # tab on an arbitrary site from reading responses out of the
            # server while a binary is loaded. If a real browser-based MCP
            # client ever ships, replace this with an explicit allowed origin
            # (e.g. echo back the request's Origin only when it matches a
            # value configured via a Binary Ninja setting) rather than `*`.
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, OSError):
            try:
                import binaryninja as _bn

                _bn.log_warn("Client disconnected while sending headers")
            except Exception:
                pass

    def _send_json_response(self, data: dict[str, Any], status_code: int = 200):
        try:
            self._set_headers(status_code=status_code)
            # If headers failed due to disconnect, avoid writing body
            try:
                body = json.dumps(data).encode("utf-8")
            except Exception:
                body = b"{}"
            try:
                self.wfile.write(body)
            except (BrokenPipeError, OSError):
                try:
                    import binaryninja as _bn

                    _bn.log_warn("Client disconnected while sending body")
                except Exception:
                    pass
        except Exception:
            # Last-resort swallow to avoid cascading errors on disconnects
            try:
                import binaryninja as _bn

                _bn.log_debug("Suppressed exception during response write")
            except Exception:
                pass

    def _parse_query_params(self) -> dict[str, str]:
        parsed_path = urllib.parse.urlparse(self.path)
        return dict(urllib.parse.parse_qsl(parsed_path.query))

    def _extract_identifiers(self, params: dict[str, str]) -> list[str]:
        """Normalize identifier-bearing query params into a list."""
        candidates: list[str] = []
        for key in (
            "identifiers",
            "identifier",
            "functions",
            "function",
            "names",
            "name",
            "addresses",
            "address",
        ):
            value = params.get(key)
            if value:
                candidates.append(value)
        identifiers: list[str] = []
        for raw in candidates:
            tokens = [tok.strip() for tok in raw.replace(";", ",").split(",")]
            identifiers.extend([tok for tok in tokens if tok])
        return identifiers

    def _requires_binary_loaded(self, path: str, method: str) -> bool:
        if any(path.startswith(prefix) for prefix in self._BINARY_OPTIONAL_PATH_PREFIXES):
            return False
        if method == "POST" and path in self._BINARY_OPTIONAL_POST_PATHS:
            return False
        return True

    def _parse_bool_param(self, params: dict[str, Any], *names: str, default: bool = False) -> bool:
        raw = None
        for name in names:
            if name in params and params[name] is not None:
                raw = params[name]
                break
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)

        value = str(raw).strip().lower()
        if value in ("true", "1", "yes", "on"):
            return True
        if value in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"Invalid {names[0]} value {raw!r}; use true/false/1/0/yes/no/on/off.")

    def _parse_post_params(self) -> dict[str, Any]:
        """Parse POST request parameters from various formats.

        Supports:
        - JSON data (application/json)
        - Form data (application/x-www-form-urlencoded)
        - Raw text (text/plain)

        Returns:
            Dictionary containing the parsed parameters
        """
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}

        content_type = self.headers.get("Content-Type", "")
        post_data = self.rfile.read(content_length).decode("utf-8")

        bn.log_info(f"Received POST data: {post_data}")
        bn.log_info(f"Content-Type: {content_type}")

        # Handle JSON data
        if "application/json" in content_type.lower():
            try:
                return json.loads(post_data)
            except json.JSONDecodeError as e:
                bn.log_error(f"Failed to parse JSON: {e}")
                return {"error": "Invalid JSON format"}

        # Handle form data
        if "application/x-www-form-urlencoded" in content_type.lower():
            try:
                return dict(urllib.parse.parse_qsl(post_data))
            except Exception as e:
                bn.log_error(f"Failed to parse form data: {e}")
                return {"error": "Invalid form data format"}

        # Handle raw text
        if "text/plain" in content_type.lower() or not content_type:
            return {"name": post_data.strip()}

    # ---------- Helpers ----------
    def _resolve_name_to_address(self, ident: str):
        """Resolve a symbol name or unambiguous address string to (address:int, label:str).

        Tries, in order:
        - Parse address (0x-prefixed hex or decimal)
        - get_symbol_by_raw_name
        - get_symbol_by_name
        - scan data_vars for matching symbol name/raw_name
        """
        bv = getattr(self.binary_ops, "current_view", None)
        if not bv:
            return None, None
        s = (ident or "").strip()
        address_error: AddressParseError | None = None
        if is_address_literal(s):
            try:
                return parse_address(s), s
            except AddressParseError as e:
                address_error = e
        # Raw name
        try:
            get_raw = getattr(bv, "get_symbol_by_raw_name", None)
            sym = get_raw(s) if callable(get_raw) else None
            if sym and hasattr(sym, "address"):
                return int(sym.address), getattr(sym, "name", s)
        except Exception:
            pass
        # Pretty name
        try:
            get_by_name = getattr(bv, "get_symbol_by_name", None)
            sym = get_by_name(s) if callable(get_by_name) else None
            if sym and hasattr(sym, "address"):
                return int(sym.address), getattr(sym, "name", s)
        except Exception:
            pass
        # Heuristic: BN auto-generated data labels like data_100003f66, byte_..., word_..., dword_..., qword_..., off_..., unk_...
        try:
            import re as _re

            m = _re.match(r"^(?i)(?:data|byte|word|dword|qword|off|unk)_(?:0x)?([0-9a-fA-F]+)$", s)
            if m:
                a = int(m.group(1), 16)
                return a, s
        except Exception:
            pass
        # Scan data vars
        try:
            for var in list(bv.data_vars):
                try:
                    sy = bv.get_symbol_at(var)
                    if not sy:
                        continue
                    if getattr(sy, "name", None) == s or getattr(sy, "raw_name", None) == s:
                        return int(var), getattr(sy, "name", s)
                except Exception:
                    continue
        except Exception:
            pass
        if address_error is not None:
            raise address_error
        return None, None

    def _c_escape(self, raw: bytes, limit: int | None = None) -> str:
        """Escape bytes as a C string literal."""
        try:
            b = raw if limit is None else raw[:limit]
            out = []
            for ch in b:
                if ch == 0x22:  # '"'
                    out.append('\\"')
                elif ch == 0x5C:  # '\\'
                    out.append("\\\\")
                elif 32 <= ch <= 126:
                    out.append(chr(ch))
                elif ch == 0x0A:
                    out.append("\\n")
                elif ch == 0x0D:
                    out.append("\\r")
                elif ch == 0x09:
                    out.append("\\t")
                else:
                    out.append(f"\\x{ch:02x}")
            return '"' + "".join(out) + '"'
        except Exception:
            return '""'

    def _check_binary_loaded(self):
        """Check if a binary is loaded and return appropriate error response if not"""
        if not self.binary_ops or not self.binary_ops.current_view:
            self._send_json_response({"error": "No binary loaded"}, 400)
            return False
        return True

    def _check_auth(self) -> bool:
        """Reject the request unless it carries the right Bearer token.

        Re-reads the token file each call so `setup_plugin.py --regen-token`
        takes effect without restarting Binary Ninja.
        """
        if auth_matches(self.headers.get("Authorization")):
            return True
        if read_token() is None:
            hint = (
                f"Auth token not configured. Run `python scripts/setup_plugin.py` "
                f"to generate {token_file_path()}."
            )
        else:
            hint = (
                "Send Authorization: Bearer <token> where <token> is the contents of "
                + token_file_path()
            )
        self._send_json_response({"error": "Unauthorized", "hint": hint}, 401)
        return False

    def _approve_patch(self, address, data, save_to_file) -> bool:
        bv_filename = None
        try:
            bv_filename = self.binary_ops.current_view.file.filename
        except Exception:
            pass
        nbytes: int | None = None
        try:
            if isinstance(data, list):
                nbytes = len(data)
            elif isinstance(data, bytes):
                nbytes = len(data)
            elif isinstance(data, str):
                s = data.strip()
                if s.startswith("0x") or s.startswith("0X"):
                    s = s[2:]
                s = s.replace(" ", "").replace("\n", "").replace("\t", "")
                if s and all(c in "0123456789abcdefABCDEF" for c in s) and len(s) % 2 == 0:
                    nbytes = len(s) // 2
        except Exception:
            pass
        details = (
            f"Address: {address}\n"
            f"Bytes: {nbytes if nbytes is not None else '?'}\n"
            f"Save to disk: {'yes' if save_to_file else 'no'}"
        )
        if require_approval("patch", bv_filename, details):
            return True
        self._send_json_response({"error": "User denied patch approval"}, 403)
        return False

    def _approve_load(self, filepath) -> bool:
        details = f"Load binary into Binary Ninja: {filepath}"
        if require_approval("load", filepath, details):
            return True
        self._send_json_response({"error": "User denied load approval"}, 403)
        return False

    def _delete_comment(self, params: dict[str, Any]):
        address = params.get("address")
        if not address:
            self._send_json_response(
                {
                    "error": "Missing address parameter",
                    "help": "Required parameter: address",
                    "received": params,
                },
                400,
            )
            return

        try:
            address_int = parse_address(address)
            success = self.binary_ops.delete_comment(address_int)
            if success:
                self._send_json_response(
                    {
                        "success": True,
                        "message": f"Successfully deleted comment at {hex(address_int)}",
                    }
                )
            else:
                self._send_json_response(
                    {
                        "error": "Failed to delete comment",
                        "message": "The comment could not be deleted at the specified address.",
                    },
                    500,
                )
        except ValueError as e:
            self._send_json_response({"error": str(e)}, 400)

    def _delete_function_comment(self, params: dict[str, Any]):
        function_name = params.get("name") or params.get("functionName")
        if not function_name:
            self._send_json_response(
                {
                    "error": "Missing function name parameter",
                    "help": "Required parameter: name (or functionName)",
                    "received": params,
                },
                400,
            )
            return

        success = self.binary_ops.delete_function_comment(function_name)
        if success:
            self._send_json_response(
                {
                    "success": True,
                    "message": f"Successfully deleted comment for function {function_name}",
                }
            )
        else:
            self._send_json_response(
                {
                    "error": "Failed to delete function comment",
                    "message": "The comment could not be deleted for the specified function.",
                },
                500,
            )

    def _handle_delete_request(self, path: str, params: dict[str, Any]) -> bool:
        if path == "/comment":
            self._delete_comment(params)
            return True
        if path == "/comment/function":
            self._delete_function_comment(params)
            return True
        return False

    def do_GET(self):
        try:
            if not self._check_auth():
                return
            path = urllib.parse.urlparse(self.path).path
            if self._requires_binary_loaded(path, "GET") and not self._check_binary_loaded():
                return

            params = self._parse_query_params()
            offset = parse_int_or_default(params.get("offset"), 0)
            # Support both `limit` and `count` (alias) for pagination
            if params.get("count") is not None:
                limit = parse_int_or_default(params.get("count"), 100)
            else:
                limit = parse_int_or_default(params.get("limit"), 100)

            if path == "/status":
                status = {
                    "loaded": self.binary_ops and self.binary_ops.current_view is not None,
                    "filename": self.binary_ops.current_view.file.filename
                    if self.binary_ops and self.binary_ops.current_view
                    else None,
                }
                self._send_json_response(status)

            elif path == "/functions" or path == "/methods":
                functions = self.binary_ops.get_function_names(offset, limit)
                bn.log_info(f"Found {len(functions)} functions")
                self._send_json_response({"functions": functions})

            elif path == "/classes":
                classes = self.binary_ops.get_class_names(offset, limit)
                self._send_json_response({"classes": classes})

            elif path == "/segments":
                segments = self.binary_ops.get_segments(offset, limit)
                self._send_json_response({"segments": segments})

            elif path == "/imports":
                imports = self.endpoints.get_imports(offset, limit)
                self._send_json_response({"imports": imports})

            elif path == "/binaries" or path == "/views":
                # List managed/open binaries
                self._send_json_response(self.endpoints.list_binaries())

            elif path == "/selectBinary":
                ident = (
                    params.get("view")
                    or params.get("binary")
                    or params.get("id")
                    or params.get("file")
                )
                if not ident:
                    self._send_json_response(
                        {
                            "error": "Missing parameter",
                            "help": "Use ?view=<id|filename>",
                        },
                        400,
                    )
                else:
                    self._send_json_response(self.endpoints.select_binary(ident))

            elif path == "/exports":
                exports = self.endpoints.get_exports(offset, limit)
                self._send_json_response({"exports": exports})
            elif path == "/sections":
                try:
                    sections = self.binary_ops.get_sections(offset, limit)
                    self._send_json_response({"sections": sections})
                except Exception as e:
                    bn.log_error(f"Error getting sections: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/entryPoints":
                try:
                    eps = self.endpoints.get_entry_points()
                    self._send_json_response({"entry_points": eps})
                except Exception as e:
                    bn.log_error(f"Error handling entryPoints: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/namespaces":
                namespaces = self.endpoints.get_namespaces(offset, limit)
                self._send_json_response({"namespaces": namespaces})

            elif path == "/data":
                try:
                    # length: desired byte count to read for preview; negative means "read exact defined size"
                    length_param = params.get("length")
                    preview_param = params.get("previewLen")
                    if length_param is not None:
                        read_len = parse_int_or_default(length_param, 32)
                    elif preview_param is not None:
                        read_len = parse_int_or_default(preview_param, 32)
                    else:
                        # Default: read exact defined size when available
                        read_len = -1
                    data_items = self.binary_ops.get_defined_data(offset, limit, read_len)
                    self._send_json_response({"data": data_items})
                except Exception as e:
                    bn.log_error(f"Error getting data items: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/localTypes":
                try:
                    include_libs = params.get("includeLibraries") in (
                        "1",
                        "true",
                        "True",
                    )
                    types = self.binary_ops.list_local_types(
                        offset, limit, include_libraries=include_libs
                    )
                    bn.log_info(
                        f"/localTypes returned {len(types)} entries (offset={offset}, limit={limit})"
                    )
                    self._send_json_response({"types": types})
                except Exception as e:
                    bn.log_error(f"Error listing local types: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/searchTypes":
                try:
                    term = params.get("query") or params.get("q")
                    if not term:
                        self._send_json_response(
                            {
                                "error": "Missing query parameter",
                                "help": "Required: query or q",
                            },
                            400,
                        )
                        return
                    # support count=-1 to return all
                    eff_limit = (
                        -1
                        if (params.get("count") == "-1" or params.get("limit") == "-1")
                        else limit
                    )
                    include_libs = params.get("includeLibraries") in (
                        "1",
                        "true",
                        "True",
                    )
                    # First compute total
                    all_matches = self.binary_ops.search_local_types(
                        term, 0, -1, include_libraries=include_libs
                    )
                    page = (
                        all_matches[offset:]
                        if eff_limit < 0
                        else all_matches[offset : offset + eff_limit]
                    )
                    self._send_json_response(
                        {
                            "types": page,
                            "query": term,
                            "total": len(all_matches),
                            "offset": offset,
                            "limit": eff_limit,
                            "includeLibraries": include_libs,
                        }
                    )
                except Exception as e:
                    bn.log_error(f"Error searching local types: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/strings":
                try:
                    bn.log_info(
                        f"/strings request: offset={offset}, limit={limit}, raw_params={params}"
                    )
                    strings = self.binary_ops.get_strings(offset, limit)
                    self._send_json_response({"strings": strings})
                except Exception as e:
                    bn.log_error(f"Error getting strings: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/allStrings":
                try:
                    # Return all strings without pagination
                    bn.log_info("/allStrings request received")
                    strings = self.binary_ops.get_strings(0, 2147483647)
                    self._send_json_response({"strings": strings})
                except Exception as e:
                    bn.log_error(f"Error getting all strings: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/hexdump":
                try:
                    address_str = params.get("address")
                    if not address_str:
                        self._set_headers(content_type="text/plain", status_code=400)
                        self.wfile.write(b"Missing address parameter\n")
                        return
                    try:
                        addr = parse_address(address_str)
                    except ValueError as e:
                        self._set_headers(content_type="text/plain", status_code=400)
                        self.wfile.write(f"{e}\n".encode())
                        return

                    # Determine length
                    length_param = params.get("length")
                    read_len = None
                    if length_param is not None:
                        try:
                            read_len = int(length_param)
                        except Exception:
                            read_len = None
                    # Default to exact defined size when available
                    if read_len is None:
                        read_len = -1

                    # If negative, try to use exact defined size at this address
                    if read_len < 0:
                        try:
                            inferred = self.binary_ops.infer_data_size(addr)
                            if inferred is not None and inferred > 0:
                                read_len = int(inferred)
                        except Exception:
                            pass
                    # Fallback default length
                    if read_len is None or read_len < 0:
                        read_len = 64

                    # Read bytes
                    try:
                        data = self.binary_ops.current_view.read(addr, read_len)
                        if data is None:
                            data = b""
                    except Exception:
                        data = b""

                    # Resolve symbol name for header label
                    label = None
                    try:
                        sym = self.binary_ops.current_view.get_symbol_at(addr)
                        if sym and hasattr(sym, "name"):
                            label = sym.name
                    except Exception:
                        label = None

                    # Build hexdump
                    def _printable(b: int) -> str:
                        try:
                            return chr(b) if 32 <= b <= 126 else "."
                        except Exception:
                            return "."

                    lines = []
                    addr_hex = format(addr, "x")
                    if label:
                        lines.append(f"{addr_hex}  {label}:")
                    else:
                        lines.append(f"{addr_hex}:")

                    total = len(data)
                    offset = 0
                    # First line may be unaligned
                    first_pad = addr % 16
                    if first_pad != 0 and total > 0:
                        take = min(16 - first_pad, total)
                        chunk = data[0:take]
                        hex_area = ("   " * first_pad) + "".join(f"{b:02x} " for b in chunk)
                        hex_area += "   " * (16 - first_pad - take)
                        ascii_area = (" " * first_pad) + "".join(_printable(b) for b in chunk)
                        ascii_area += " " * (16 - first_pad - take)
                        lines.append(f"{addr_hex}  {hex_area} {ascii_area}")
                        offset += take
                    # Full lines
                    while offset < total:
                        line_addr = addr + offset
                        take = min(16, total - offset)
                        chunk = data[offset : offset + take]
                        hex_area = "".join(f"{b:02x} " for b in chunk) + ("   " * (16 - take))
                        ascii_area = "".join(_printable(b) for b in chunk) + (" " * (16 - take))
                        lines.append(f"{format(line_addr, 'x')}  {hex_area} {ascii_area}")
                        offset += take

                    text = "\n".join(lines) + "\n"
                    self._set_headers(content_type="text/plain", status_code=200)
                    self.wfile.write(text.encode("utf-8", errors="replace"))
                except Exception as e:
                    bn.log_error(f"Error handling hexdump: {e}")
                    self._set_headers(content_type="text/plain", status_code=500)
                    self.wfile.write(f"Error: {e}\n".encode())

            elif path == "/hexdumpByName":
                try:
                    name = params.get("name") or params.get("symbol") or params.get("raw_name")
                    if not name:
                        self._set_headers(content_type="text/plain", status_code=400)
                        self.wfile.write(b"Missing name parameter\n")
                        return

                    try:
                        addr, label = self._resolve_name_to_address(name)
                    except AddressParseError as e:
                        self._set_headers(content_type="text/plain", status_code=400)
                        self.wfile.write(f"{e}\n".encode())
                        return
                    if addr is None:
                        self._set_headers(content_type="text/plain", status_code=404)
                        self.wfile.write(b"Symbol not found\n")
                        return

                    # Determine length
                    length_param = params.get("length")
                    try:
                        read_len = int(length_param) if length_param is not None else -1
                    except Exception:
                        read_len = -1
                    if read_len < 0:
                        try:
                            inferred = self.binary_ops.infer_data_size(addr)
                            if inferred is not None and inferred > 0:
                                read_len = int(inferred)
                        except Exception:
                            pass
                    if read_len is None or read_len < 0:
                        read_len = 64

                    # Read and format
                    try:
                        data = self.binary_ops.current_view.read(addr, read_len) or b""
                    except Exception:
                        data = b""

                    def _printable(b: int) -> str:
                        try:
                            return chr(b) if 32 <= b <= 126 else "."
                        except Exception:
                            return "."

                    lines = []
                    addr_hex = format(addr, "x")
                    lines.append(f"{addr_hex}  {label}:")

                    total = len(data)
                    offset = 0
                    first_pad = addr % 16
                    if first_pad != 0 and total > 0:
                        take = min(16 - first_pad, total)
                        chunk = data[0:take]
                        hex_area = ("   " * first_pad) + "".join(f"{b:02x} " for b in chunk)
                        hex_area += "   " * (16 - first_pad - take)
                        ascii_area = (" " * first_pad) + "".join(_printable(b) for b in chunk)
                        ascii_area += " " * (16 - first_pad - take)
                        lines.append(f"{addr_hex}  {hex_area} {ascii_area}")
                        offset += take
                    while offset < total:
                        line_addr = addr + offset
                        take = min(16, total - offset)
                        chunk = data[offset : offset + take]
                        hex_area = "".join(f"{b:02x} " for b in chunk) + ("   " * (16 - take))
                        ascii_area = "".join(_printable(b) for b in chunk) + (" " * (16 - take))
                        lines.append(f"{format(line_addr, 'x')}  {hex_area} {ascii_area}")
                        offset += take

                    text = "\n".join(lines) + "\n"
                    self._set_headers(content_type="text/plain", status_code=200)
                    self.wfile.write(text.encode("utf-8", errors="replace"))
                except Exception as e:
                    bn.log_error(f"Error handling hexdumpByName: {e}")
                    self._set_headers(content_type="text/plain", status_code=500)
                    self.wfile.write(f"Error: {e}\n".encode())

            elif path == "/getDataDecl":
                try:
                    ident = (
                        params.get("name")
                        or params.get("symbol")
                        or params.get("raw_name")
                        or params.get("address")
                    )
                    if not ident:
                        self._send_json_response(
                            {
                                "error": "Missing name/address parameter",
                                "help": "Provide name, symbol, raw_name, or address",
                            },
                            400,
                        )
                        return
                    try:
                        addr, label = self._resolve_name_to_address(ident)
                    except AddressParseError as e:
                        self._send_json_response({"error": str(e), "ident": ident}, 400)
                        return
                    if addr is None:
                        self._send_json_response({"error": "Symbol not found", "ident": ident}, 404)
                        return

                    # Determine exact size and type
                    size = None
                    type_text = None
                    try:
                        bv = self.binary_ops.current_view
                        dv = bv.get_data_var_at(addr) if hasattr(bv, "get_data_var_at") else None
                        typ_obj = (
                            dv.type
                            if (dv is not None and hasattr(dv, "type"))
                            else (bv.get_type_at(addr) if hasattr(bv, "get_type_at") else None)
                        )
                        if typ_obj is not None:
                            type_text = str(typ_obj)
                            if hasattr(typ_obj, "width") and typ_obj.width:
                                size = int(typ_obj.width)
                    except Exception:
                        pass
                    if size is None:
                        try:
                            inferred = self.binary_ops.infer_data_size(addr)
                            if inferred and inferred > 0:
                                size = int(inferred)
                        except Exception:
                            pass
                    if size is None:
                        size = 64

                    # Read bytes
                    try:
                        raw = self.binary_ops.current_view.read(addr, size) or b""
                    except Exception:
                        raw = b""

                    # Build a declaration string (best-effort)
                    decl = None
                    try:
                        # Prefer explicit char[] initialization when printable
                        is_char_array = (type_text or "").lower().startswith(
                            "char"
                        ) or "char [" in (type_text or "").lower()
                        if is_char_array and raw:
                            esc = self._c_escape(raw.rstrip(b"\x00"))
                            decl = f"{type_text} {label} = {esc};"
                        else:
                            if type_text:
                                decl = f"{type_text} {label};"
                            else:
                                decl = f"/* size={size} */ {label};"
                    except Exception:
                        decl = f"/* size={size} */ {label};"

                    # Also include a hexdump for convenience
                    # Reuse the hexdump generation above
                    def _printable(b: int) -> str:
                        try:
                            return chr(b) if 32 <= b <= 126 else "."
                        except Exception:
                            return "."

                    lines = []
                    addr_hex = format(addr, "x")
                    lines.append(f"{addr_hex}  {label}:")
                    total = len(raw)
                    offset = 0
                    first_pad = addr % 16
                    if first_pad != 0 and total > 0:
                        take = min(16 - first_pad, total)
                        chunk = raw[0:take]
                        hex_area = ("   " * first_pad) + "".join(f"{b:02x} " for b in chunk)
                        hex_area += "   " * (16 - first_pad - take)
                        ascii_area = (" " * first_pad) + "".join(_printable(b) for b in chunk)
                        ascii_area += " " * (16 - first_pad - take)
                        lines.append(f"{addr_hex}  {hex_area} {ascii_area}")
                        offset += take
                    while offset < total:
                        line_addr = addr + offset
                        take = min(16, total - offset)
                        chunk = raw[offset : offset + take]
                        hex_area = "".join(f"{b:02x} " for b in chunk) + ("   " * (16 - take))
                        ascii_area = "".join(_printable(b) for b in chunk) + (" " * (16 - take))
                        lines.append(f"{format(line_addr, 'x')}  {hex_area} {ascii_area}")
                        offset += take
                    hexdump_text = "\n".join(lines) + "\n"

                    self._send_json_response(
                        {
                            "address": hex(addr),
                            "name": label,
                            "size": size,
                            "type": type_text,
                            "decl": decl,
                            "hexdump": hexdump_text,
                        }
                    )
                except Exception as e:
                    bn.log_error(f"Error handling getDataDecl: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/strings/filter":
                try:
                    pattern = params.get("filter", "")
                    bn.log_info(
                        f"/strings/filter request: offset={offset}, limit={limit}, pattern={pattern}"
                    )
                    # Get all strings first, then filter and paginate
                    all_strings = self.binary_ops.get_strings(0, 2147483647)
                    if pattern:
                        pl = pattern.lower()
                        filtered = [
                            s
                            for s in all_strings
                            if isinstance(s.get("value"), str) and pl in s.get("value", "").lower()
                        ]
                    else:
                        filtered = all_strings
                    page = filtered[offset : offset + limit]
                    self._send_json_response({"strings": page, "total": len(filtered)})
                except Exception as e:
                    bn.log_error(f"Error filtering strings: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/searchFunctions":
                search_term = params.get("query", "")
                matches = self.endpoints.search_functions(search_term, offset, limit)
                self._send_json_response({"matches": matches})

            elif path == "/findBytes":
                pattern_str = params.get("pattern") or params.get("bytes") or params.get("data")
                if not pattern_str:
                    self._send_json_response(
                        {
                            "error": "Missing pattern parameter",
                            "help": (
                                "Use ?pattern=<hex> (e.g. 'deadbeef' or '90 90 90' or "
                                "'0xde 0xad'). Optional: start (hex/dec), end (hex/dec), "
                                "limit (default 100; 0 or negative = unlimited)."
                            ),
                        },
                        400,
                    )
                    return
                # Same hex-parsing rules as /patch: tolerate 0x prefixes, spaces, commas.
                normalized = pattern_str.strip()
                if normalized.startswith("0x") or normalized.startswith("0X"):
                    normalized = normalized[2:]
                for sep in (" ", "\n", "\t", ",", "0x", "0X"):
                    normalized = normalized.replace(sep, "")
                try:
                    pattern_bytes = bytes.fromhex(normalized)
                except ValueError as ve:
                    self._send_json_response({"error": f"Invalid hex pattern: {ve}"}, 400)
                    return

                try:
                    start_addr = parse_optional_address(params.get("start"), field="start")
                    end_addr = parse_optional_address(params.get("end"), field="end")
                except ValueError as ve:
                    self._send_json_response({"error": f"Invalid address: {ve}"}, 400)
                    return

                find_limit = parse_int_or_default(params.get("limit"), 100)

                try:
                    matches = self.binary_ops.find_bytes(
                        pattern_bytes,
                        start=start_addr,
                        end=end_addr,
                        limit=find_limit,
                    )
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                    return
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                    return
                except Exception as e:
                    bn.log_error(f"Error handling findBytes: {e}")
                    self._send_json_response({"error": str(e)}, 500)
                    return

                self._send_json_response(
                    {
                        "pattern": pattern_bytes.hex(),
                        "count": len(matches),
                        "matches": matches,
                    }
                )

            elif path == "/findText":
                text = params.get("text") or params.get("query") or params.get("q")
                if not text:
                    self._send_json_response(
                        {
                            "error": "Missing text parameter",
                            "help": (
                                "Use ?text=<string>. Optional: start (hex/dec), end "
                                "(hex/dec), limit (default 100; 0 or negative = unlimited), "
                                "caseSensitive (1/0; default 1)."
                            ),
                        },
                        400,
                    )
                    return

                try:
                    start_addr = parse_optional_address(params.get("start"), field="start")
                    end_addr = parse_optional_address(params.get("end"), field="end")
                except ValueError as ve:
                    self._send_json_response({"error": f"Invalid address: {ve}"}, 400)
                    return

                find_limit = parse_int_or_default(params.get("limit"), 100)
                cs_raw = params.get("caseSensitive") or params.get("case_sensitive")
                case_sensitive = True
                if cs_raw is not None:
                    case_sensitive = str(cs_raw).strip().lower() not in (
                        "0",
                        "false",
                        "no",
                        "off",
                    )

                try:
                    matches = self.binary_ops.find_text(
                        text,
                        start=start_addr,
                        end=end_addr,
                        limit=find_limit,
                        case_sensitive=case_sensitive,
                    )
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                    return
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                    return
                except Exception as e:
                    bn.log_error(f"Error handling findText: {e}")
                    self._send_json_response({"error": str(e)}, 500)
                    return

                self._send_json_response(
                    {
                        "text": text,
                        "case_sensitive": case_sensitive,
                        "count": len(matches),
                        "matches": matches,
                    }
                )

            elif path == "/findConstant":
                value_str = params.get("value") or params.get("constant")
                if not value_str:
                    self._send_json_response(
                        {
                            "error": "Missing value parameter",
                            "help": (
                                "Use ?value=<int> (hex like 0xCAFEBABE or decimal). "
                                "Optional: start, end, limit (default 100)."
                            ),
                        },
                        400,
                    )
                    return

                try:
                    s = value_str.strip()
                    if s.startswith("0x") or s.startswith("0X"):
                        value_int = int(s, 16)
                    elif any(c in "abcdefABCDEF" for c in s):
                        value_int = int(s, 16)
                    else:
                        value_int = int(s, 10)
                except ValueError as ve:
                    self._send_json_response({"error": f"Invalid integer value: {ve}"}, 400)
                    return

                try:
                    start_addr = parse_optional_address(params.get("start"), field="start")
                    end_addr = parse_optional_address(params.get("end"), field="end")
                except ValueError as ve:
                    self._send_json_response({"error": f"Invalid address: {ve}"}, 400)
                    return

                find_limit = parse_int_or_default(params.get("limit"), 100)

                try:
                    matches = self.binary_ops.find_constant(
                        value_int,
                        start=start_addr,
                        end=end_addr,
                        limit=find_limit,
                    )
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                    return
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                    return
                except Exception as e:
                    bn.log_error(f"Error handling findConstant: {e}")
                    self._send_json_response({"error": str(e)}, 500)
                    return

                self._send_json_response(
                    {
                        "value": hex(value_int),
                        "count": len(matches),
                        "matches": matches,
                    }
                )

            elif path == "/parseExpression":
                expr = params.get("expr") or params.get("expression") or params.get("e")
                here_str = params.get("here") or params.get("at")
                if not expr:
                    self._send_json_response(
                        {
                            "error": "Missing expression",
                            "help": (
                                "Use ?expr=<expression>. Optional: here=<address> "
                                "(hex or decimal) substituted for $here in the expression."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                here_val = 0
                if here_str:
                    try:
                        here_val = parse_address(here_str, field="here")
                    except ValueError as e:
                        self._send_json_response({"error": str(e)}, 400)
                        return
                try:
                    result = self.binary_ops.parse_expression(expr, here_val)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling parseExpression: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getCallers":
                identifiers = self._extract_identifiers(params)
                if not identifiers:
                    self._send_json_response(
                        {
                            "error": "Missing identifier parameter",
                            "help": "Provide ?identifier=<name|address> or comma-separated ?identifiers=a,b",
                        },
                        400,
                    )
                    return
                try:
                    payload = self.binary_ops.get_callers(identifiers)
                except Exception as e:
                    bn.log_error(f"Error handling getCallers: {e}")
                    self._send_json_response({"error": str(e)}, 500)
                else:
                    self._send_json_response(payload)

            elif path == "/getCallees":
                identifiers = self._extract_identifiers(params)
                if not identifiers:
                    self._send_json_response(
                        {
                            "error": "Missing identifier parameter",
                            "help": "Provide ?identifier=<name|address> or comma-separated ?identifiers=a,b",
                        },
                        400,
                    )
                    return
                try:
                    payload = self.binary_ops.get_callees(identifiers)
                except Exception as e:
                    bn.log_error(f"Error handling getCallees: {e}")
                    self._send_json_response({"error": str(e)}, 500)
                else:
                    self._send_json_response(payload)

            elif path == "/decompile":
                function_name = params.get("name") or params.get("functionName")
                if not function_name:
                    self._send_json_response(
                        {
                            "error": "Missing function name parameter. Use ?name=function_name or ?functionName=function_name"
                        },
                        400,
                    )
                    return

                self._handle_decompile(function_name)

            elif path == "/assembly":
                function_name = params.get("name") or params.get("functionName")
                if not function_name:
                    self._send_json_response(
                        {
                            "error": "Missing function name parameter. Use ?name=function_name or ?functionName=function_name"
                        },
                        400,
                    )
                    return

                try:
                    func_info = self.binary_ops.get_function_info(function_name)
                    if not func_info:
                        bn.log_error(f"Function not found: {function_name}")
                        self._send_json_response(
                            {
                                "error": "Function not found",
                                "requested_name": function_name,
                                "available_functions": self.binary_ops.get_function_names(0, 10),
                            },
                            404,
                        )
                        return

                    bn.log_info(f"Found function for assembly: {func_info}")
                    assembly = self.binary_ops.get_assembly_function(function_name)

                    if assembly is None:
                        self._send_json_response(
                            {
                                "error": "Assembly retrieval failed",
                                "function": func_info,
                                "reason": "Function assembly could not be retrieved. Check the Binary Ninja log for detailed error information.",
                            },
                            500,
                        )
                    else:
                        self._send_json_response({"assembly": assembly, "function": func_info})
                except Exception as e:
                    bn.log_error(f"Error handling assembly request: {e!s}")
                    import traceback

                    bn.log_error(traceback.format_exc())
                    self._send_json_response(
                        {
                            "error": "Assembly retrieval failed",
                            "requested_name": function_name,
                            "exception": str(e),
                        },
                        500,
                    )

            elif path == "/il":
                # Return IL by view (hlil/mlil/llil) and optional SSA form
                ident = params.get("name") or params.get("functionName") or params.get("address")
                if not ident:
                    self._send_json_response(
                        {
                            "error": "Missing function identifier",
                            "help": "Use ?name=<func> or ?address=<hex> with optional &view=hlil|mlil|llil&ssa=0|1",
                            "received": params,
                        },
                        400,
                    )
                    return

                view = (params.get("view") or params.get("il") or "hlil").strip().lower()
                if view not in ("hlil", "mlil", "llil"):
                    self._send_json_response(
                        {
                            "error": f"Unsupported IL view: {view!r}",
                            "supported_views": ["hlil", "mlil", "llil"],
                        },
                        400,
                    )
                    return
                ssa_param = (params.get("ssa") or params.get("isSSA") or "0").strip().lower()
                ssa = ssa_param in ("1", "true", "yes", "on")

                try:
                    func_info = self.binary_ops.get_function_info(ident)
                    if not func_info:
                        self._send_json_response(
                            {
                                "error": "Function not found",
                                "requested": ident,
                                "available_functions": self.binary_ops.get_function_names(0, 10),
                            },
                            404,
                        )
                        return

                    il_text = self.binary_ops.get_function_il(ident, view=view, ssa=ssa)
                    if il_text is None:
                        self._send_json_response(
                            {
                                "error": "Failed to get IL",
                                "function": func_info,
                                "view": view,
                                "ssa": ssa,
                                "reason": "Unsupported IL view or unavailable instructions",
                            },
                            500,
                        )
                        return

                    self._send_json_response(
                        {"il": il_text, "function": func_info, "view": view, "ssa": ssa}
                    )
                except Exception as e:
                    bn.log_error(f"Error handling IL request: {e!s}")
                    self._send_json_response(
                        {
                            "error": "IL retrieval failed",
                            "requested": ident,
                            "view": view,
                            "ssa": ssa,
                            "exception": str(e),
                        },
                        500,
                    )

            elif path == "/functionAt":
                address_str = params.get("address")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required parameter: address (in hex format, e.g., 0x41d100) the address of an insruction",
                            "received": params,
                        },
                        400,
                    )
                    return

                try:
                    offset = parse_address(address_str)
                    function_names = self.binary_ops.get_functions_containing_address(offset)

                    self._send_json_response({"address": hex(offset), "functions": function_names})
                except ValueError as e:
                    self._send_json_response(
                        {
                            "error": str(e),
                            "help": "Address must be a valid hexadecimal (0x...) or decimal number",
                            "received": address_str,
                        },
                        400,
                    )
                except Exception as e:
                    bn.log_error(f"Error handling function_at request: {e}")
                    self._send_json_response(
                        {
                            "error": str(e),
                            "address": address_str,
                        },
                        500,
                    )

            elif path == "/getUserDefinedType":
                type_name = params.get("name")
                if not type_name:
                    self._send_json_response(
                        {
                            "error": "Missing name parameter",
                            "help": "Required parameter: name (name of the user-defined type to retrieve)",
                            "received": params,
                        },
                        400,
                    )
                    return

                try:
                    # Get the user-defined type definition
                    type_info = self.binary_ops.get_user_defined_type(type_name)

                    if type_info:
                        self._send_json_response(type_info)
                    else:
                        # If type not found, list available types for reference
                        available_types = {}

                        try:
                            if (
                                hasattr(self.binary_ops._current_view, "user_type_container")
                                and self.binary_ops._current_view.user_type_container
                            ):
                                for (
                                    type_id
                                ) in self.binary_ops._current_view.user_type_container.types.keys():
                                    current_type = (
                                        self.binary_ops._current_view.user_type_container.types[
                                            type_id
                                        ]
                                    )
                                    available_types[current_type[0]] = (
                                        str(current_type[1].type)
                                        if hasattr(current_type[1], "type")
                                        else "unknown"
                                    )
                        except Exception as e:
                            bn.log_error(f"Error listing available types: {e}")

                        self._send_json_response(
                            {
                                "error": "Type not found",
                                "requested_type": type_name,
                                "available_types": available_types,
                            },
                            404,
                        )
                except Exception as e:
                    bn.log_error(f"Error handling getUserDefinedType request: {e}")
                    self._send_json_response(
                        {
                            "error": str(e),
                            "type_name": type_name,
                        },
                        500,
                    )

            elif path == "/comment":
                if self.command == "GET":
                    address = params.get("address")
                    if not address:
                        self._send_json_response(
                            {
                                "error": "Missing address parameter",
                                "help": "Required parameter: address",
                                "received": params,
                            },
                            400,
                        )
                        return

                    try:
                        address_int = parse_address(address)
                        comment = self.binary_ops.get_comment(address_int)
                        if comment is not None:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "address": hex(address_int),
                                    "comment": comment,
                                }
                            )
                        else:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "address": hex(address_int),
                                    "comment": None,
                                    "message": "No comment found at this address",
                                }
                            )
                    except ValueError as e:
                        self._send_json_response({"error": str(e)}, 400)
                elif self.command == "DELETE":
                    address = params.get("address")
                    if not address:
                        self._send_json_response(
                            {
                                "error": "Missing address parameter",
                                "help": "Required parameter: address",
                                "received": params,
                            },
                            400,
                        )
                        return

                    try:
                        address_int = parse_address(address)
                        success = self.binary_ops.delete_comment(address_int)
                        if success:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "message": f"Successfully deleted comment at {hex(address_int)}",
                                }
                            )
                        else:
                            self._send_json_response(
                                {
                                    "error": "Failed to delete comment",
                                    "message": "The comment could not be deleted at the specified address.",
                                },
                                500,
                            )
                    except ValueError as e:
                        self._send_json_response({"error": str(e)}, 400)
                else:  # POST
                    address = params.get("address")
                    comment = params.get("comment")
                    if not address or comment is None:
                        self._send_json_response(
                            {
                                "error": "Missing parameters",
                                "help": "Required parameters: address and comment",
                                "received": params,
                            },
                            400,
                        )
                        return

                    try:
                        address_int = parse_address(address)
                        success = self.binary_ops.set_comment(address_int, comment)
                        if success:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "message": f"Successfully set comment at {hex(address_int)}",
                                    "comment": comment,
                                }
                            )
                        else:
                            self._send_json_response(
                                {
                                    "error": "Failed to set comment",
                                    "message": "The comment could not be set at the specified address.",
                                },
                                500,
                            )
                    except ValueError as e:
                        self._send_json_response({"error": str(e)}, 400)

            elif path == "/comment/function":
                if self.command == "GET":
                    function_name = params.get("name") or params.get("functionName")
                    if not function_name:
                        self._send_json_response(
                            {
                                "error": "Missing function name parameter",
                                "help": "Required parameter: name (or functionName)",
                                "received": params,
                            },
                            400,
                        )
                        return

                    comment = self.binary_ops.get_function_comment(function_name)
                    if comment is not None:
                        self._send_json_response(
                            {
                                "success": True,
                                "function": function_name,
                                "comment": comment,
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "success": True,
                                "function": function_name,
                                "comment": None,
                                "message": "No comment found for this function",
                            }
                        )
                elif self.command == "DELETE":
                    function_name = params.get("name") or params.get("functionName")
                    if not function_name:
                        self._send_json_response(
                            {
                                "error": "Missing function name parameter",
                                "help": "Required parameter: name (or functionName)",
                                "received": params,
                            },
                            400,
                        )
                        return

                    success = self.binary_ops.delete_function_comment(function_name)
                    if success:
                        self._send_json_response(
                            {
                                "success": True,
                                "message": f"Successfully deleted comment for function {function_name}",
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "error": "Failed to delete function comment",
                                "message": "The comment could not be deleted for the specified function.",
                            },
                            500,
                        )
                else:  # POST
                    function_name = params.get("name") or params.get("functionName")
                    comment = params.get("comment")
                    if not function_name or comment is None:
                        self._send_json_response(
                            {
                                "error": "Missing parameters",
                                "help": "Required parameters: name (or functionName) and comment",
                                "received": params,
                            },
                            400,
                        )
                        return

                    success = self.binary_ops.set_function_comment(function_name, comment)
                    if success:
                        self._send_json_response(
                            {
                                "success": True,
                                "message": f"Successfully set comment for function {function_name}",
                                "comment": comment,
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "error": "Failed to set function comment",
                                "message": "The comment could not be set for the specified function.",
                            },
                            500,
                        )

            elif path == "/getComment":
                address = params.get("address")
                if not address:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required parameter: address",
                            "received": params,
                        },
                        400,
                    )
                    return

                try:
                    address_int = parse_address(address)
                    comment = self.binary_ops.get_comment(address_int)
                    if comment is not None:
                        self._send_json_response(
                            {
                                "success": True,
                                "address": hex(address_int),
                                "comment": comment,
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "success": True,
                                "address": hex(address_int),
                                "comment": None,
                                "message": "No comment found at this address",
                            }
                        )
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)

            elif path == "/getFunctionComment":
                function_name = params.get("name") or params.get("functionName")
                if not function_name:
                    self._send_json_response(
                        {
                            "error": "Missing function name parameter",
                            "help": "Required parameter: name (or functionName)",
                            "received": params,
                        },
                        400,
                    )
                    return

                comment = self.binary_ops.get_function_comment(function_name)
                if comment is not None:
                    self._send_json_response(
                        {
                            "success": True,
                            "function": function_name,
                            "comment": comment,
                        }
                    )
                else:
                    self._send_json_response(
                        {
                            "success": True,
                            "function": function_name,
                            "comment": None,
                            "message": "No comment found for this function",
                        }
                    )
            elif path == "/setFunctionPrototype":
                # Accept both GET and POST to support long prototypes via POST body
                address_str = (
                    params.get("address")
                    or params.get("functionAddress")
                    or params.get("addr")
                    or params.get("name")
                )
                proto = params.get("prototype") or params.get("signature") or params.get("type")
                if not address_str or proto is None:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": "Required: address (or functionAddress/addr) and prototype (or signature/type)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    # Do minimal validation here; the endpoint will resolve name or address
                    result = self.endpoints.set_function_prototype(address_str, proto)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling setFunctionPrototype request: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/makeFunctionAt":
                # Create a function at an address (idempotent if already exists)
                address_str = params.get("address") or params.get("addr")
                arch = params.get("platform") or params.get("arch") or params.get("architecture")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required: address (hex like 0x401000 or decimal). Optional: platform (e.g., linux-x86_64; use 'default' for view default)",
                        },
                        400,
                    )
                    return
                try:
                    res = self.endpoints.make_function_at(address_str, arch)
                    # If the endpoint signals an error, forward with 400 so clients can react properly
                    if isinstance(res, dict) and res.get("error"):
                        self._send_json_response(res, 400)
                    else:
                        self._send_json_response(res)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling makeFunctionAt: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/defineUserSymbol":
                address_str = params.get("address") or params.get("addr")
                name = params.get("name") or params.get("symbol")
                kind = params.get("kind") or params.get("type") or "data"
                if not address_str or not name:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                "Required: address (hex like 0x401000 or decimal), name. "
                                'Optional: kind ("data" default, or "function").'
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    result = self.binary_ops.define_user_symbol(addr_int, name, kind)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling defineUserSymbol: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/undefineUserSymbol":
                address_str = params.get("address") or params.get("addr")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required: address (hex like 0x401000 or decimal)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    result = self.binary_ops.undefine_user_symbol(addr_int)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling undefineUserSymbol: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/undefineUserType":
                type_name = params.get("name") or params.get("type") or params.get("typeName")
                if not type_name:
                    self._send_json_response(
                        {
                            "error": "Missing type name parameter",
                            "help": "Required: name (or type/typeName) — the user-defined type to remove.",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.undefine_user_type(type_name)
                    self._send_json_response(result)
                except ValueError as ve:
                    # "not found" and "BN refused" both come back as ValueError;
                    # 404 is the more useful signal for an agent.
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling undefineUserType: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/defineUserDataVar":
                address_str = params.get("address") or params.get("addr")
                type_str = params.get("type") or params.get("typeString") or params.get("dataType")
                if not address_str or not type_str:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                "Required: address (hex like 0x401000 or decimal), "
                                "type (C-style, e.g. 'int', 'struct Foo *', 'char[16]')."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    result = self.binary_ops.define_user_data_var(addr_int, type_str)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling defineUserDataVar: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path in ("/readInt", "/readPointer"):
                address_str = params.get("address") or params.get("addr") or params.get("at")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": (
                                "Required: address. For /readInt also size (1/2/4/8). "
                                "Optional for /readInt: signed (true/false; default false)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    if path == "/readInt":
                        size_str = params.get("size") or params.get("width")
                        if not size_str:
                            self._send_json_response(
                                {
                                    "error": "Missing size parameter",
                                    "help": "Required: size (1, 2, 4, or 8 bytes).",
                                },
                                400,
                            )
                            return
                        try:
                            size_int = int(size_str)
                        except ValueError:
                            self._send_json_response(
                                {"error": "Invalid size — must be an integer"}, 400
                            )
                            return
                        signed_raw = params.get("signed") or params.get("sign") or "false"
                        signed_bool = str(signed_raw).strip().lower() in (
                            "1",
                            "true",
                            "yes",
                            "on",
                        )
                        result = self.binary_ops.read_int(addr_int, size_int, signed_bool)
                    else:
                        result = self.binary_ops.read_pointer(addr_int)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling {path}: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/addTypeLibrary":
                tl_path = params.get("path") or params.get("file") or params.get("library")
                if not tl_path:
                    self._send_json_response(
                        {
                            "error": "Missing path parameter",
                            "help": "Required: path (absolute path to a .bntl file).",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.add_type_library(tl_path)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling addTypeLibrary: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/demangle":
                mangled = params.get("name") or params.get("mangled") or params.get("symbol")
                abi = (params.get("abi") or "auto").strip()
                if not mangled:
                    self._send_json_response(
                        {
                            "error": "Missing name parameter",
                            "help": (
                                "Required: name (the mangled C++ symbol). "
                                "Optional: abi (auto|gnu3|ms; default auto)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.demangle(mangled, abi)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling demangle: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getDataVarAt":
                address_str = params.get("address") or params.get("addr")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required: address (hex like 0x401000 or decimal).",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    result = self.binary_ops.get_data_var_at(addr_int)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling getDataVarAt: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/undefineUserDataVar":
                address_str = params.get("address") or params.get("addr")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required: address (hex like 0x401000 or decimal)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    result = self.binary_ops.undefine_user_data_var(addr_int)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling undefineUserDataVar: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/updateAnalysisAndWait":
                try:
                    result = self.binary_ops.update_analysis_and_wait()
                    self._send_json_response(result)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling updateAnalysisAndWait: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/reanalyzeFunction":
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                if not fn_ident:
                    self._send_json_response(
                        {
                            "error": "Missing function identifier",
                            "help": "Provide function (or functionName/address).",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.reanalyze_function(fn_ident)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling reanalyzeFunction: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path in ("/undo", "/redo"):
                count_raw = params.get("count") or "1"
                try:
                    count = int(count_raw)
                except (TypeError, ValueError):
                    self._send_json_response(
                        {"error": f"Invalid count {count_raw!r}; must be an integer >= 1"},
                        400,
                    )
                    return
                op = self.binary_ops.undo if path == "/undo" else self.binary_ops.redo
                try:
                    self._send_json_response(op(count))
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling {path}: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/tagTypes":
                try:
                    types = self.binary_ops.list_tag_types()
                    self._send_json_response({"count": len(types), "tag_types": types})
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling tagTypes: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/createTagType":
                tt_name = params.get("name") or params.get("tagType")
                icon = params.get("icon") or "🏷"
                if not tt_name:
                    self._send_json_response(
                        {
                            "error": "Missing name parameter",
                            "help": 'Required: name. Optional: icon (single grapheme/emoji, default "🏷").',
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.create_tag_type(tt_name, icon)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling createTagType: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/addTag":
                address_str = params.get("address") or params.get("addr")
                tt_name = params.get("tagType") or params.get("type") or params.get("name")
                data_payload = params.get("data") or ""
                kind = params.get("kind") or "auto"
                if not address_str or not tt_name:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                "Required: address, tagType. Optional: data (description), "
                                "kind (auto|address|function|data; default auto)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    result = self.binary_ops.add_tag(addr_int, tt_name, data_payload, kind)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling addTag: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path in ("/setFunctionCanReturn", "/setFunctionInline"):
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                # Single bool parameter under varying aliases per endpoint.
                if path == "/setFunctionCanReturn":
                    raw = params.get("canReturn") or params.get("can_return") or params.get("value")
                    arg_name = "canReturn"
                else:
                    raw = (
                        params.get("inline")
                        or params.get("inlineDuringAnalysis")
                        or params.get("value")
                    )
                    arg_name = "inline"
                if not fn_ident or raw is None:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                f"Required: function (or functionName/address) and "
                                f"{arg_name} (true/false/1/0/yes/no)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                bool_value = str(raw).strip().lower() in (
                    "true",
                    "1",
                    "yes",
                    "on",
                )
                try:
                    if path == "/setFunctionCanReturn":
                        result = self.binary_ops.set_function_can_return(fn_ident, bool_value)
                    else:
                        result = self.binary_ops.set_function_inline(fn_ident, bool_value)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling {path}: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/setFunctionReturnType":
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                type_str = (
                    params.get("type") or params.get("returnType") or params.get("typeString")
                )
                if not fn_ident or not type_str:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                "Required: function (or functionName/address) and "
                                "type (C-style, e.g. 'int', 'void', 'struct Foo *')."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.set_function_return_type(fn_ident, type_str)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling setFunctionReturnType: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getFunctionMetadata":
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                if not fn_ident:
                    self._send_json_response(
                        {
                            "error": "Missing function identifier",
                            "help": "Provide function (or functionName/address). Returns is_thunk, can_return, has_variable_arguments, is_pure, analysis_skipped, analysis_skip_reason, analysis_skip_override, auto, parameter_count.",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.get_function_metadata(fn_ident)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling getFunctionMetadata: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path in (
                "/getConstantsReferencedBy",
                "/getRegsReadBy",
                "/getRegsWrittenBy",
            ):
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("function")
                    or params.get("functionName")
                )
                address_str = params.get("address") or params.get("addr") or params.get("at")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": (
                                "Required: address (instruction address inside the function), "
                                "function (or functionName)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                # If the caller didn't name a function, auto-resolve via the
                # containing function so the agent doesn't have to wire that up
                # in two steps for the common case.
                if not fn_ident:
                    try:
                        bv = self.binary_ops.current_view
                        fns = (
                            list(bv.get_functions_containing(addr_int) or [])
                            if bv is not None
                            else []
                        )
                        if fns:
                            fn_ident = int(getattr(fns[0], "start", 0))
                    except Exception:
                        fn_ident = None
                if not fn_ident:
                    self._send_json_response(
                        {
                            "error": "Missing function identifier",
                            "help": (
                                "Pass function (name or address), or use an address "
                                "that lies inside a known function."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    if path == "/getConstantsReferencedBy":
                        result = self.binary_ops.get_constants_referenced_by(fn_ident, addr_int)
                    elif path == "/getRegsReadBy":
                        result = self.binary_ops.get_regs_read_by(fn_ident, addr_int)
                    else:
                        result = self.binary_ops.get_regs_written_by(fn_ident, addr_int)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling {path}: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getParameterAt":
                address_str = params.get("address") or params.get("addr") or params.get("at")
                index_str = params.get("index") or params.get("i") or params.get("paramIndex")
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("function")
                    or params.get("functionName")
                )
                if not address_str or index_str is None or index_str == "":
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                "Required: address (callsite), index (0-based). "
                                "Optional: function (auto-resolved when omitted)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    idx_int = int(index_str)
                except ValueError:
                    self._send_json_response(
                        {"error": "Invalid index — must be a non-negative integer"},
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.get_parameter_at(addr_int, idx_int, fn_ident or None)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling getParameterAt: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getSymbolsByType":
                sym_type = params.get("type") or params.get("symbolType") or params.get("kind")
                if not sym_type:
                    self._send_json_response(
                        {
                            "error": "Missing type parameter",
                            "help": (
                                "Required: type (e.g. 'function', 'data', 'import', "
                                "'external', or any BN SymbolType enum name). "
                                "Optional: start, end, limit (default 100; 0 or negative = unlimited)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return

                try:
                    start_addr = parse_optional_address(params.get("start"), field="start")
                    end_addr = parse_optional_address(params.get("end"), field="end")
                except ValueError as ve:
                    self._send_json_response({"error": f"Invalid address: {ve}"}, 400)
                    return

                lim = parse_int_or_default(params.get("limit"), 100)
                try:
                    result = self.binary_ops.get_symbols_by_type(
                        sym_type, start=start_addr, end=end_addr, limit=lim
                    )
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling getSymbolsByType: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path in ("/getSsaVarUses", "/getSsaVarDefinition"):
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                var_name = params.get("variableName") or params.get("variable") or params.get("var")
                version_raw = params.get("version") or params.get("ssaVersion") or "0"
                il_level = (
                    params.get("ilLevel") or params.get("il_level") or params.get("level") or "hlil"
                )
                if not fn_ident or not var_name:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                "Required: function (or functionName/address) and "
                                "variable (or variableName/var). Optional: version "
                                "(SSA version, default 0), ilLevel (hlil|mlil; default hlil)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    version_int = int(version_raw)
                except ValueError:
                    self._send_json_response(
                        {"error": "Invalid version — must be a non-negative integer"},
                        400,
                    )
                    return
                try:
                    if path == "/getSsaVarUses":
                        result = self.binary_ops.get_ssa_var_uses(
                            fn_ident, var_name, version_int, il_level
                        )
                    else:
                        result = self.binary_ops.get_ssa_var_definition(
                            fn_ident, var_name, version_int, il_level
                        )
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling {path}: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getVarUses" or path == "/getVarDefinitions":
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                var_name = params.get("variableName") or params.get("variable") or params.get("var")
                il_level = (
                    params.get("ilLevel") or params.get("il_level") or params.get("level") or "all"
                )
                if not fn_ident or not var_name:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": (
                                "Required: function (or functionName/address) and "
                                "variable (or variableName/var). Optional: ilLevel "
                                "(all|hlil|mlil|llil; default all)."
                            ),
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    if path == "/getVarUses":
                        result = self.binary_ops.get_var_uses(fn_ident, var_name, il_level)
                    else:
                        result = self.binary_ops.get_var_definitions(fn_ident, var_name, il_level)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 404)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling {path}: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getTagsAt":
                address_str = params.get("address") or params.get("addr")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required: address (hex like 0x401000 or decimal).",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr_int = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return
                try:
                    result = self.binary_ops.get_tags_at(addr_int)
                    self._send_json_response(result)
                except RuntimeError as re_err:
                    self._send_json_response({"error": str(re_err)}, 500)
                except Exception as e:
                    bn.log_error(f"Error handling getTagsAt: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/platforms":
                try:
                    self._send_json_response(self.endpoints.list_platforms())
                except Exception as e:
                    bn.log_error(f"Error listing platforms: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/setLocalVariableType":
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                var_name = (
                    params.get("variableName") or params.get("variable") or params.get("nameOrVar")
                )
                new_type = params.get("newType") or params.get("type") or params.get("signature")
                if not fn_ident or not var_name or new_type is None:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": "Required: functionAddress (or address/name), variableName, newType (or type/signature)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    res = self.endpoints.set_local_variable_type(fn_ident, var_name, new_type)
                    self._send_json_response(res)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling setLocalVariableType request: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/retypeVariable":
                function_name = params.get("functionName")
                if not function_name:
                    self._send_json_response({"error": "Missing function name parameter"}, 400)
                    return

                variable_name = params.get("variableName")
                if not variable_name:
                    self._send_json_response({"error": "Missing variable name parameter"}, 400)
                    return

                type_str = params.get("type")
                if not type_str:
                    self._send_json_response({"error": "Missing type parameter"}, 400)
                    return

                try:
                    self._send_json_response(
                        self.endpoints.retype_variable(function_name, variable_name, type_str)
                    )
                except Exception as e:
                    bn.log_error(f"Error handling retypeVariable request: {e}")
                    self._send_json_response(
                        {"error": str(e)},
                        500,
                    )
            elif path == "/renameVariable":
                function_name = params.get("functionName")
                if not function_name:
                    self._send_json_response({"error": "Missing function name parameter"}, 400)
                    return

                variable_name = params.get("variableName")
                if not variable_name:
                    self._send_json_response({"error": "Missing variable name parameter"}, 400)
                    return

                new_name = params.get("newName")
                if not new_name:
                    self._send_json_response({"error": "Missing new name parameter"}, 400)
                    return

                try:
                    self._send_json_response(
                        self.endpoints.rename_variable(function_name, variable_name, new_name)
                    )
                except Exception as e:
                    bn.log_error(f"Error handling renameVariable request: {e}")
                    self._send_json_response(
                        {"error": str(e)},
                        500,
                    )

            elif path == "/renameVariables":
                # Batch rename local variables in a function
                # Accept flexible identifiers and payload formats (GET/POST)
                fn_ident = (
                    params.get("functionAddress")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("name")
                )
                if not fn_ident:
                    self._send_json_response(
                        {
                            "error": "Missing function identifier",
                            "help": "Provide functionAddress/address or functionName/name",
                            "received": params,
                        },
                        400,
                    )
                    return

                raw_renames = None
                # Prefer explicit 'renames' in JSON when POSTed
                if isinstance(params, dict) and "renames" in params:
                    raw_renames = params.get("renames")
                # Or a JSON mapping under 'mapping'
                if raw_renames is None and "mapping" in params:
                    try:
                        m = params.get("mapping")
                        if isinstance(m, str):
                            raw_renames = json.loads(m)
                        else:
                            raw_renames = m
                    except Exception:
                        raw_renames = None
                # Or a compact 'pairs' string: old1:new1,old2:new2
                if raw_renames is None and "pairs" in params:
                    pairs_str = params.get("pairs") or ""
                    mapping = {}
                    try:
                        for item in pairs_str.split(","):
                            if not item.strip():
                                continue
                            if ":" in item:
                                o, n = item.split(":", 1)
                                mapping[o.strip()] = n.strip()
                    except Exception:
                        mapping = {}
                    raw_renames = mapping

                if raw_renames is None:
                    self._send_json_response(
                        {
                            "error": "Missing renames payload",
                            "help": "Provide 'renames' (array of {old,new}) or 'mapping' (JSON object old->new) or 'pairs' (old:new,...)",
                            "received": params,
                        },
                        400,
                    )
                    return

                try:
                    result = self.endpoints.rename_variables(fn_ident, raw_renames)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling renameVariables request: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getXrefsTo":
                address_str = params.get("address")
                if not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required parameter: address (hex like 0x401000 or decimal)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.binary_ops.get_xrefs_to_address(address_str)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling getXrefsTo request: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getXrefsToField":
                struct_name = params.get("struct") or params.get("structName")
                field_name = params.get("field") or params.get("fieldName")
                if not struct_name or not field_name:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": "Required: struct (or structName), field (or fieldName)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    refs = self.binary_ops.get_xrefs_to_field(struct_name, field_name)
                    self._send_json_response(
                        {"struct": struct_name, "field": field_name, "references": refs}
                    )
                except Exception as e:
                    bn.log_error(f"Error handling getXrefsToField: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getXrefsToStruct":
                struct_name = params.get("name") or params.get("struct") or params.get("structName")
                if not struct_name:
                    self._send_json_response(
                        {
                            "error": "Missing struct name parameter",
                            "help": "Required: name (or struct/structName)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    refs = self.binary_ops.get_xrefs_to_struct(struct_name)
                    self._send_json_response(refs)
                except Exception as e:
                    bn.log_error(f"Error handling getXrefsToStruct: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getXrefsToType":
                type_name = params.get("name") or params.get("type") or params.get("typeName")
                if not type_name:
                    self._send_json_response(
                        {
                            "error": "Missing type name parameter",
                            "help": "Required: name (or type/typeName)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    refs = self.binary_ops.get_xrefs_to_type(type_name)
                    self._send_json_response(refs)
                except Exception as e:
                    bn.log_error(f"Error handling getXrefsToType: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getTypeInfo":
                type_name = params.get("name") or params.get("type") or params.get("typeName")
                if not type_name:
                    self._send_json_response(
                        {
                            "error": "Missing type name parameter",
                            "help": "Required: name (or type/typeName)",
                        },
                        400,
                    )
                    return
                try:
                    info = self.binary_ops.get_type_info(type_name)
                    self._send_json_response(info)
                except Exception as e:
                    bn.log_error(f"Error handling getTypeInfo: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getXrefsToEnum":
                enum_name = params.get("name") or params.get("enum") or params.get("enumName")
                if not enum_name:
                    self._send_json_response(
                        {
                            "error": "Missing enum name parameter",
                            "help": "Required: name (or enum/enumName)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    refs = self.binary_ops.get_xrefs_to_enum(enum_name)
                    self._send_json_response(refs)
                except Exception as e:
                    bn.log_error(f"Error handling getXrefsToEnum: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            # '/displayAs' endpoint removed per request

            elif path == "/formatValue":
                # Compute representations and annotate BN at an address
                text = params.get("text")
                size_param = params.get("size")
                address_str = params.get("address")
                if not text or not address_str:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": "Required: address, text. Optional: size",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    addr = parse_address(address_str)
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)
                    return

                try:
                    conv = util_convert_number(text, size_param)
                    # Create a concise annotation
                    bases = conv.get("bases", {})
                    c_lit = conv.get("c_literal")
                    c_str = conv.get("c_string")
                    parts = []
                    if "hex" in bases:
                        parts.append(f"hex={bases['hex']}")
                    if "dec" in bases:
                        parts.append(f"dec={bases['dec']}")
                    if c_lit:
                        parts.append(f"char={c_lit}")
                    if c_str:
                        # Trim long strings for comments
                        s = c_str
                        if len(s) > 64:
                            s = s[:61] + '"…'
                        parts.append(f"str={s}")
                    annot = "Converted: " + ", ".join(parts) if parts else f"Converted: {conv}"

                    applied = self.binary_ops.set_comment(addr, annot)
                    self._send_json_response(
                        {
                            "address": hex(addr),
                            "converted": conv,
                            "applied_comment": bool(applied),
                            "comment": annot,
                        }
                    )
                except Exception as e:
                    bn.log_error(f"Error handling formatValue: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/convertNumber":
                # Compute number/string representations (bases, LE/BE, C literals)
                try:
                    text = params.get("text")
                    size_param = params.get("size")
                    if text is None:
                        self._send_json_response(
                            {
                                "error": "Missing text parameter",
                                "help": "Required: text. Optional: size (1,2,4,8 or 0 for auto)",
                                "received": params,
                            },
                            400,
                        )
                        return
                    conv = util_convert_number(text, size_param)
                    self._send_json_response(conv)
                except Exception as e:
                    bn.log_error(f"Error handling convertNumber: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getXrefsToUnion":
                union_name = params.get("name") or params.get("union") or params.get("unionName")
                if not union_name:
                    self._send_json_response(
                        {
                            "error": "Missing union name parameter",
                            "help": "Required: name (or union/unionName)",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    refs = self.binary_ops.get_xrefs_to_union(union_name)
                    self._send_json_response(refs)
                except Exception as e:
                    bn.log_error(f"Error handling getXrefsToUnion: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/getStackFrameVars":
                function_identifier = (
                    params.get("name")
                    or params.get("address")
                    or params.get("function")
                    or params.get("functionName")
                    or params.get("functionAddress")
                )
                if not function_identifier:
                    self._send_json_response(
                        {
                            "error": "Missing required parameter: name, address, or function",
                            "received": params,
                        },
                        400,
                    )
                    return
                try:
                    result = self.endpoints.get_stack_frame_vars(function_identifier)
                    self._send_json_response({"stack_frame_vars": result})
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling getStackFrameVars request: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/defineTypes":
                c_code = params.get("cCode")
                if not c_code:
                    self._send_json_response({"error": "Missing cCode parameter"}, 400)
                    return

                try:
                    self._send_json_response(self.endpoints.define_types(c_code))
                except Exception as e:
                    bn.log_error(f"Error handling defineTypes request: {e}")
                    self._send_json_response(
                        {"error": str(e)},
                        500,
                    )
            elif path == "/declareCType":
                c_decl = (
                    params.get("declaration")
                    or params.get("cDecl")
                    or params.get("cDeclaration")
                    or params.get("decl")
                )
                if not c_decl:
                    self._send_json_response(
                        {
                            "error": "Missing declaration parameter",
                            "help": "Use 'declaration' with a single C type declaration",
                        },
                        400,
                    )
                    return
                try:
                    self._send_json_response(self.endpoints.declare_c_type(c_decl))
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling declareCType request: {e}")
                    self._send_json_response({"error": str(e)}, 500)
            elif path == "/patch" or path == "/patchBytes":
                self._send_json_response(
                    {
                        "error": "Patch requests must use POST",
                        "help": (
                            "Send address, data, and optional save_to_file in a POST body. "
                            "save_to_file defaults to false."
                        ),
                    },
                    405,
                )
            else:
                self._send_json_response({"error": "Not found"}, 404)

        except Exception as e:
            bn.log_error(f"Error handling GET request: {e}")
            self._send_json_response({"error": str(e)}, 500)

    def _handle_decompile(self, function_name: str):
        """Handle function decompilation requests.

        Args:
            function_name: Name or address of the function to decompile

        Sends JSON response with either:
        - Decompiled function code and metadata
        - Error message with available functions list
        """
        try:
            func_info = self.binary_ops.get_function_info(function_name)
            if not func_info:
                bn.log_error(f"Function not found: {function_name}")
                self._send_json_response(
                    {
                        "error": "Function not found",
                        "requested_name": function_name,
                        "available_functions": self.binary_ops.get_function_names(0, 10),
                    },
                    404,
                )
                return

            bn.log_info(f"Found function for decompilation: {func_info}")
            decompiled = self.binary_ops.decompile_function(function_name)

            if decompiled is None:
                self._send_json_response(
                    {
                        "error": "Decompilation failed",
                        "function": func_info,
                        "reason": "Function could not be decompiled. This might be due to missing debug information or unsupported function type.",
                    },
                    500,
                )
            else:
                self._send_json_response({"decompiled": decompiled, "function": func_info})
        except Exception as e:
            bn.log_error(f"Error during decompilation: {e}")
            self._send_json_response(
                {
                    "error": f"Decompilation error: {e!s}",
                    "requested_name": function_name,
                },
                500,
            )

    def do_DELETE(self):
        try:
            if not self._check_auth():
                return
            if not self._check_binary_loaded():
                return

            params = self._parse_query_params()
            if int(self.headers.get("Content-Length", 0)):
                params.update(self._parse_post_params())
            path = urllib.parse.urlparse(self.path).path

            bn.log_info(f"DELETE {path} with params: {params}")

            if self._handle_delete_request(path, params):
                return

            self._send_json_response(
                {
                    "error": "Unsupported DELETE endpoint",
                    "path": path,
                    "supported": ["/comment", "/comment/function"],
                },
                404,
            )
        except Exception as e:
            bn.log_error(f"DELETE request error: {e}")
            self._send_json_response({"error": str(e)}, 500)

    def do_POST(self):
        try:
            if not self._check_auth():
                return
            path = urllib.parse.urlparse(self.path).path
            if self._requires_binary_loaded(path, "POST") and not self._check_binary_loaded():
                return

            params = self._parse_post_params()

            bn.log_info(f"POST {path} with params: {params}")

            method_override = str(params.get("_method", "")).strip().upper()
            if method_override:
                if method_override == "DELETE" and self._handle_delete_request(path, params):
                    return
                self._send_json_response(
                    {
                        "error": "Unsupported method override",
                        "method": method_override,
                        "path": path,
                    },
                    405,
                )
                return

            if path == "/load":
                filepath = params.get("filepath")
                if not filepath:
                    self._send_json_response({"error": "Missing filepath parameter"}, 400)
                    return

                if not self._approve_load(filepath):
                    return
                try:
                    self.binary_ops.load_binary(filepath)
                    self._send_json_response(
                        {"success": True, "message": f"Binary loaded: {filepath}"}
                    )
                except Exception as e:
                    self._send_json_response({"error": str(e)}, 500)

            elif path == "/rename/function" or path == "/renameFunction":
                old_name = params.get("oldName") or params.get("old_name")
                new_name = params.get("newName") or params.get("new_name")

                bn.log_info(
                    f"Rename request - old_name: {old_name}, new_name: {new_name}, params: {params}"
                )

                if not old_name or not new_name:
                    self._send_json_response(
                        {
                            "error": "Missing parameters",
                            "help": "Required parameters: oldName (or old_name) and newName (or new_name)",
                            "received": params,
                        },
                        400,
                    )
                    return

                # Handle address format (both 0x... and plain number)
                if isinstance(old_name, str):
                    if old_name.startswith("0x"):
                        try:
                            old_name = int(old_name, 16)
                        except ValueError:
                            pass
                    elif old_name.isdigit():
                        old_name = int(old_name)

                bn.log_info(f"Attempting to rename function: {old_name} -> {new_name}")

                # Get function info for validation
                func_info = self.binary_ops.get_function_info(old_name)
                if func_info:
                    bn.log_info(f"Found function: {func_info}")
                    success = self.binary_ops.rename_function(old_name, new_name)
                    if success:
                        self._send_json_response(
                            {
                                "success": True,
                                "message": f"Successfully renamed function from {old_name} to {new_name}",
                                "function": func_info,
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "error": "Failed to rename function",
                                "message": "The function was found but could not be renamed. This might be due to permissions or binary restrictions.",
                                "function": func_info,
                            },
                            500,
                        )
                else:
                    available_funcs = self.binary_ops.get_function_names(0, 10)
                    bn.log_error(f"Function not found: {old_name}")
                    self._send_json_response(
                        {
                            "error": "Function not found",
                            "requested": old_name,
                            "help": "Make sure the function exists. You can use either the function name or its address.",
                            "available_functions": available_funcs,
                        },
                        404,
                    )

            elif path == "/rename/data" or path == "/renameData":
                address = params.get("address")
                new_name = params.get("newName") or params.get("new_name")
                if not address or not new_name:
                    self._send_json_response({"error": "Missing parameters"}, 400)
                    return

                try:
                    address_int = parse_address(address)
                    success = self.binary_ops.rename_data(address_int, new_name)
                    self._send_json_response({"success": success})
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)

            elif path == "/comment":
                if self.command == "GET":
                    address = params.get("address")
                    if not address:
                        self._send_json_response(
                            {
                                "error": "Missing address parameter",
                                "help": "Required parameter: address",
                                "received": params,
                            },
                            400,
                        )
                        return

                    try:
                        address_int = parse_address(address)
                        comment = self.binary_ops.get_comment(address_int)
                        if comment is not None:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "address": hex(address_int),
                                    "comment": comment,
                                }
                            )
                        else:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "address": hex(address_int),
                                    "comment": None,
                                    "message": "No comment found at this address",
                                }
                            )
                    except ValueError as e:
                        self._send_json_response({"error": str(e)}, 400)
                elif self.command == "DELETE":
                    address = params.get("address")
                    if not address:
                        self._send_json_response(
                            {
                                "error": "Missing address parameter",
                                "help": "Required parameter: address",
                                "received": params,
                            },
                            400,
                        )
                        return

                    try:
                        address_int = parse_address(address)
                        success = self.binary_ops.delete_comment(address_int)
                        if success:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "message": f"Successfully deleted comment at {hex(address_int)}",
                                }
                            )
                        else:
                            self._send_json_response(
                                {
                                    "error": "Failed to delete comment",
                                    "message": "The comment could not be deleted at the specified address.",
                                },
                                500,
                            )
                    except ValueError as e:
                        self._send_json_response({"error": str(e)}, 400)
                else:  # POST
                    address = params.get("address")
                    comment = params.get("comment")
                    if not address or comment is None:
                        self._send_json_response(
                            {
                                "error": "Missing parameters",
                                "help": "Required parameters: address and comment",
                                "received": params,
                            },
                            400,
                        )
                        return

                    try:
                        address_int = parse_address(address)
                        success = self.binary_ops.set_comment(address_int, comment)
                        if success:
                            self._send_json_response(
                                {
                                    "success": True,
                                    "message": f"Successfully set comment at {hex(address_int)}",
                                    "comment": comment,
                                }
                            )
                        else:
                            self._send_json_response(
                                {
                                    "error": "Failed to set comment",
                                    "message": "The comment could not be set at the specified address.",
                                },
                                500,
                            )
                    except ValueError as e:
                        self._send_json_response({"error": str(e)}, 400)

            elif path == "/comment/function":
                if self.command == "GET":
                    function_name = params.get("name") or params.get("functionName")
                    if not function_name:
                        self._send_json_response(
                            {
                                "error": "Missing function name parameter",
                                "help": "Required parameter: name (or functionName)",
                                "received": params,
                            },
                            400,
                        )
                        return

                    comment = self.binary_ops.get_function_comment(function_name)
                    if comment is not None:
                        self._send_json_response(
                            {
                                "success": True,
                                "function": function_name,
                                "comment": comment,
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "success": True,
                                "function": function_name,
                                "comment": None,
                                "message": "No comment found for this function",
                            }
                        )
                elif self.command == "DELETE":
                    function_name = params.get("name") or params.get("functionName")
                    if not function_name:
                        self._send_json_response(
                            {
                                "error": "Missing function name parameter",
                                "help": "Required parameter: name (or functionName)",
                                "received": params,
                            },
                            400,
                        )
                        return

                    success = self.binary_ops.delete_function_comment(function_name)
                    if success:
                        self._send_json_response(
                            {
                                "success": True,
                                "message": f"Successfully deleted comment for function {function_name}",
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "error": "Failed to delete function comment",
                                "message": "The comment could not be deleted for the specified function.",
                            },
                            500,
                        )
                else:  # POST
                    function_name = params.get("name") or params.get("functionName")
                    comment = params.get("comment")
                    if not function_name or comment is None:
                        self._send_json_response(
                            {
                                "error": "Missing parameters",
                                "help": "Required parameters: name (or functionName) and comment",
                                "received": params,
                            },
                            400,
                        )
                        return

                    success = self.binary_ops.set_function_comment(function_name, comment)
                    if success:
                        self._send_json_response(
                            {
                                "success": True,
                                "message": f"Successfully set comment for function {function_name}",
                                "comment": comment,
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "error": "Failed to set function comment",
                                "message": "The comment could not be set for the specified function.",
                            },
                            500,
                        )

            elif path == "/getComment":
                address = params.get("address")
                if not address:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required parameter: address",
                            "received": params,
                        },
                        400,
                    )
                    return

                try:
                    address_int = parse_address(address)
                    comment = self.binary_ops.get_comment(address_int)
                    if comment is not None:
                        self._send_json_response(
                            {
                                "success": True,
                                "address": hex(address_int),
                                "comment": comment,
                            }
                        )
                    else:
                        self._send_json_response(
                            {
                                "success": True,
                                "address": hex(address_int),
                                "comment": None,
                                "message": "No comment found at this address",
                            }
                        )
                except ValueError as e:
                    self._send_json_response({"error": str(e)}, 400)

            elif path == "/getFunctionComment":
                function_name = params.get("functionName") or params.get("name")
                if not function_name:
                    self._send_json_response(
                        {
                            "error": "Missing function name parameter",
                            "help": "Required parameter: name (or functionName)",
                            "received": params,
                        },
                        400,
                    )
                    return

                comment = self.binary_ops.get_function_comment(function_name)
                if comment is not None:
                    self._send_json_response(
                        {
                            "success": True,
                            "function": function_name,
                            "comment": comment,
                        }
                    )
                else:
                    self._send_json_response(
                        {
                            "success": True,
                            "function": function_name,
                            "comment": None,
                            "message": "No comment found for this function",
                        }
                    )

            elif path == "/patch" or path == "/patchBytes":
                address = params.get("address") or params.get("addr")
                data = params.get("data") or params.get("bytes")
                try:
                    save_to_file = self._parse_bool_param(
                        params, "save_to_file", "saveToFile", default=False
                    )
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                    return

                if not address:
                    self._send_json_response(
                        {
                            "error": "Missing address parameter",
                            "help": "Required: address (hex like 0x401000 or decimal). Optional: data (hex string like '90 90' or '9090'), save_to_file (bool, default false)",
                            "received": params,
                        },
                        400,
                    )
                    return

                if not data:
                    self._send_json_response(
                        {
                            "error": "Missing data parameter",
                            "help": "Required: data (hex string like '90 90' or '9090', or list of integers)",
                            "received": params,
                        },
                        400,
                    )
                    return

                try:
                    # Parse data if it's a JSON string (for list format)
                    if isinstance(data, str):
                        try:
                            # Try to parse as JSON array
                            parsed = json.loads(data)
                            if isinstance(parsed, list):
                                data = parsed
                        except (json.JSONDecodeError, ValueError):
                            # Not JSON, treat as hex string
                            pass

                    if not self._approve_patch(address, data, save_to_file):
                        return
                    result = self.endpoints.patch_bytes(address, data, save_to_file)
                    self._send_json_response(result)
                except ValueError as ve:
                    self._send_json_response({"error": str(ve)}, 400)
                except Exception as e:
                    bn.log_error(f"Error handling patch request: {e}")
                    self._send_json_response({"error": str(e)}, 500)

            else:
                self._send_json_response({"error": "Not found"}, 404)
        except Exception as e:
            bn.log_error(f"Error handling POST request: {e}")
            self._send_json_response({"error": str(e)}, 500)


class MCPServer:
    """HTTP server for Binary Ninja MCP plugin.

    Provides REST API endpoints for:
    - Binary analysis and manipulation
    - Function decompilation
    - Symbol renaming
    - Data inspection
    """

    def __init__(self, config: Config):
        self.config = config
        self.server = None
        self.thread = None
        self.binary_ops = BinaryOperations(config.binary_ninja)

    def start(self):
        """Start the HTTP server in a background thread."""
        server_address = (self.config.server.host, self.config.server.port)

        # Create handler with access to binary operations
        handler_class = type(
            "MCPRequestHandlerWithOps",
            (MCPRequestHandler,),
            {"binary_ops": self.binary_ops},
        )

        self.server = HTTPServer(server_address, handler_class)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        bn.log_info(f"Server started on {self.config.server.host}:{self.config.server.port}")

    def stop(self):
        """Stop the HTTP server and clean up resources."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            if self.thread:
                self.thread.join()
            # Clear references so callers can reliably detect stopped state
            self.thread = None
            self.server = None
            bn.log_info("Server stopped")
