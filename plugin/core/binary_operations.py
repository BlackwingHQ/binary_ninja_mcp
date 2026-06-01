import os
import platform
import re
import subprocess
import time
import weakref
from typing import Any, ClassVar

import binaryninja as bn
from binaryninja.enums import StructureVariant, TypeClass

from ..utils.address import is_address_literal, parse_address
from ..utils.string_utils import escape_non_ascii
from .config import BinaryNinjaConfig


class BinaryOperations:
    def __init__(self, config: BinaryNinjaConfig):
        self.config = config
        self._current_view: bn.BinaryView | None = None
        # Multi-binary support
        # Store weak references so closed views are auto-pruned
        self._views_by_id: dict[str, weakref.ReferenceType] = {}
        self._next_view_id: int = 1
        self._id_by_filename: dict[str, str] = {}

    @property
    def current_view(self) -> bn.BinaryView | None:
        return self._current_view

    @current_view.setter
    def current_view(self, bv: bn.BinaryView | None):
        self._current_view = bv
        if bv:
            bn.log_info(f"Set current binary view: {bv.file.filename}")
            try:
                self._register_view(bv)
            except Exception:
                pass
        else:
            bn.log_info("Cleared current binary view")

    def load_binary(self, filepath: str) -> bn.BinaryView:
        """Open a binary file and register the resulting view.

        Idempotent: if the file is already tracked, returns the existing
        view and makes it current. Calling bn.load on an already-loaded
        file creates a duplicate headless view, and our dedup-by-filename
        logic would then orphan the original (UI-held) view.
        """
        canonical = os.path.realpath(os.path.abspath(filepath))
        existing_vid = self._id_by_filename.get(canonical)
        if existing_vid:
            w = self._views_by_id.get(existing_vid)
            existing = w() if w else None
            if existing is not None:
                self._current_view = existing
                return existing

        try:
            bv = bn.load(filepath)
            if bv is None:
                raise RuntimeError(f"bn.load returned None for: {filepath}")
            # Register BEFORE assigning to _current_view: _register_view calls
            # _prune_views, which clears _current_view if it's not in the
            # tracked set yet. Assigning first would self-clobber to None.
            self._register_view(bv)
            self._current_view = bv
            return bv
        except Exception as e:
            bn.log_error(f"Failed to load binary: {e}")
            raise

    # ---------------- Multi-binary helpers ----------------
    def _prune_views(self) -> None:
        """Remove entries for BinaryViews that no longer exist and rebuild filename map."""
        alive: dict[str, weakref.ReferenceType] = {}
        new_fn_map: dict[str, str] = {}
        alive_objs: list[object] = []
        for vid, w in list(self._views_by_id.items()):
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is None:
                continue
            alive[vid] = w
            alive_objs.append(vb)
            try:
                fn = str(getattr(vb.file, "filename", None)) if getattr(vb, "file", None) else None
            except Exception:
                fn = None
            if fn and fn not in new_fn_map:
                new_fn_map[fn] = vid
        self._views_by_id = alive
        self._id_by_filename = new_fn_map
        # If current_view no longer exists among alive views, clear it
        try:
            if self._current_view is not None and all(
                obj is not self._current_view for obj in alive_objs
            ):
                self._current_view = None
        except Exception:
            self._current_view = None

    def _register_view(self, bv: bn.BinaryView) -> str:
        """Add a view to the managed list if not present, return its id."""
        self._prune_views()
        # Reuse existing id if the exact object is already tracked
        for vid, w in list(self._views_by_id.items()):
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is bv:
                return vid
        # Prefer deduplication by canonical filename
        fn = None
        try:
            fn = str(getattr(bv.file, "filename", None)) if getattr(bv, "file", None) else None
        except Exception:
            fn = None
        if fn:
            # If a view for this filename already exists, reuse its id and update the view
            existing_id = self._id_by_filename.get(fn)
            if existing_id and existing_id in self._views_by_id:
                # Always store weak references so closed views can be pruned
                self._views_by_id[existing_id] = weakref.ref(bv)
                return existing_id
        # Assign a new id
        vid = str(self._next_view_id)
        self._next_view_id += 1
        self._views_by_id[vid] = weakref.ref(bv)
        if fn:
            self._id_by_filename[fn] = vid
        return vid

    def register_view(self, bv: bn.BinaryView) -> str:
        """Public wrapper to register a BinaryView and return its id."""
        return self._register_view(bv)

    def unregister_by_filename(self, filename: str) -> int:
        """Remove all tracked views that match the given absolute filename.

        Returns number of entries removed.
        """
        if not filename:
            return 0
        self._prune_views()
        to_delete: list[str] = []
        for vid, w in list(self._views_by_id.items()):
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is None:
                continue
            try:
                fn = getattr(vb.file, "filename", None)
            except Exception:
                fn = None
            if fn == filename:
                to_delete.append(vid)
        for vid in to_delete:
            self._views_by_id.pop(vid, None)
        # Rebuild filename map and clear current_view if it matched
        try:
            cur_fn = None
            if self._current_view and getattr(self._current_view, "file", None):
                cur_fn = getattr(self._current_view.file, "filename", None)
            if cur_fn == filename:
                self._current_view = None
        except Exception:
            self._current_view = None
        self._prune_views()
        return len(to_delete)

    def list_open_binaries(self) -> list[dict[str, str]]:
        """Return a list of managed/open binaries with ids.

        Note: Tracks binaries opened via this plugin or explicitly registered as current_view.
        """
        items: list[dict[str, str]] = []
        # Cleanup first
        self._prune_views()
        # Do NOT auto-register current_view here; UI monitor handles discovery.
        # This avoids re-introducing closed views via a stale strong reference.
        # Deduplicate by canonical filename; prefer the id mapped in _id_by_filename
        entries: list[tuple[str, str, bool]] = []  # (id, filename, active)
        seen: set[str] = set()
        for vid, w in self._views_by_id.items():
            try:
                vb = w()
            except Exception:
                vb = None
            if vb is None:
                continue
            try:
                fn = vb.file.filename
            except Exception:
                fn = "(unknown)"
            key = fn
            if key in seen:
                continue
            seen.add(key)
            # Resolve canonical id for this filename when available
            canonical_id = self._id_by_filename.get(fn, vid)
            try:
                vb_canon_ref = self._views_by_id.get(canonical_id)
                vb_canon = vb_canon_ref() if vb_canon_ref else vb
            except Exception:
                vb_canon = vb
            entries.append((canonical_id, fn, bool(vb_canon is self._current_view)))
        # Sort by filename for stable ordering
        entries.sort(key=lambda t: t[1] or "")
        for cid, fn, active in entries:
            items.append({"id": cid, "filename": fn, "active": active})
        return items

    def select_view(self, ident: str) -> dict[str, str] | None:
        """Select active BinaryView by id or filename/basename.

        Returns selection info on success, None on failure.
        """
        s = (ident or "").strip()
        if not s:
            return None
        self._prune_views()
        # Try id
        w = self._views_by_id.get(s)
        vb = None
        if w is not None:
            try:
                vb = w()
            except Exception:
                vb = None
        # If user passed a 1-based ordinal (from /binaries), map it to filename
        if vb is None and s.isdigit():
            try:
                idx = int(s)
                if idx >= 1:
                    lst = self.list_open_binaries()  # sorted order
                    if 1 <= idx <= len(lst):
                        fname = lst[idx - 1].get("filename")
                        if fname:
                            map_id = self._id_by_filename.get(fname)
                            if map_id:
                                wmap = self._views_by_id.get(map_id)
                                vb = wmap() if wmap else None
            except Exception:
                vb = None
        # Try direct filename mapping
        if vb is None:
            try:
                # Exact filename
                map_id = self._id_by_filename.get(s)
                if map_id:
                    wmap = self._views_by_id.get(map_id)
                    vb = wmap() if wmap else None
            except Exception:
                vb = None
        if vb is None:
            # Try match by full filename or basename
            for vid, w2 in self._views_by_id.items():
                try:
                    v = w2()
                except Exception:
                    v = None
                if v is None:
                    continue
                try:
                    fn = v.file.filename
                except Exception:
                    fn = None
                if not fn:
                    continue
                import os as _os

                if s == fn or s == _os.path.basename(fn):
                    vb = v
                    break
        if vb is None:
            return None
        self.current_view = vb
        vid = None
        for k, wv in self._views_by_id.items():
            try:
                vv = wv()
            except Exception:
                vv = None
            if vv is vb:
                vid = k
                break
        return {"id": vid or "", "filename": getattr(vb.file, "filename", "(unknown)")}

    def get_function_by_name_or_address(self, identifier: str | int) -> bn.Function | None:
        """Get a function by either its name or address.

        Args:
            identifier: Function name or address (can be int, hex string, or decimal string)

        Returns:
            Function object if found, None otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        address_error: ValueError | None = None
        if is_address_literal(identifier):
            try:
                addr = parse_address(identifier, field="function identifier")
                func = self._current_view.get_function_at(addr)
                if func:
                    bn.log_info(f"Found function at address {hex(addr)}: {func.name}")
                    return func
            except ValueError as e:
                address_error = e

        # Handle name-based lookup with case sensitivity
        for func in self._current_view.functions:
            if func.name == identifier:
                bn.log_info(f"Found function by name: {func.name}")
                return func

        # Try case-insensitive match as fallback
        for func in self._current_view.functions:
            if func.name.lower() == str(identifier).lower():
                bn.log_info(f"Found function by case-insensitive name: {func.name}")
                return func

        # Try symbol table lookup as last resort
        symbol = self._current_view.get_symbol_by_raw_name(str(identifier))
        if symbol and symbol.address:
            func = self._current_view.get_function_at(symbol.address)
            if func:
                bn.log_info(f"Found function through symbol lookup: {func.name}")
                return func

        if address_error is not None:
            raise address_error

        bn.log_error(f"Could not find function: {identifier}")
        return None

    def _normalize_identifier_list(self, identifiers: Any) -> list[Any]:
        """Normalize comma-delimited strings or iterables into a list of identifiers."""
        if identifiers is None:
            return []
        if isinstance(identifiers, (list, tuple, set)):
            raw_items = list(identifiers)
        else:
            raw_items = [identifiers]
        normalized: list[Any] = []
        for item in raw_items:
            if item is None:
                continue
            if isinstance(item, str):
                # Allow comma or semicolon separation for convenience
                tokens = [tok.strip() for tok in item.replace(";", ",").split(",")]
                normalized.extend([tok for tok in tokens if tok])
            else:
                normalized.append(item)
        return normalized

    def _format_function_reference(self, func: bn.Function | None) -> dict[str, Any] | None:
        if not func:
            return None
        try:
            return {
                "name": getattr(func, "name", None),
                "address": hex(int(func.start)) if hasattr(func, "start") else None,
            }
        except Exception:
            return {
                "name": getattr(func, "name", None),
                "address": None,
            }

    def _collect_related_functions(
        self, func: bn.Function, relation_attr: str
    ) -> list[dict[str, Any]]:
        related: list[dict[str, Any]] = []
        seen: set[int] = set()
        try:
            rel_iter = getattr(func, relation_attr, None)
        except Exception:
            rel_iter = None
        if rel_iter is None:
            return related
        try:
            for rel_func in list(rel_iter):
                if not rel_func:
                    continue
                addr = None
                try:
                    addr = int(rel_func.start)
                except Exception:
                    addr = None
                if addr is not None and addr in seen:
                    continue
                if addr is not None:
                    seen.add(addr)
                ref = self._format_function_reference(rel_func)
                if ref:
                    related.append(ref)
        except Exception:
            pass
        return related

    def _summarize_call_sites(self, func: bn.Function, relation: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        attr = "caller_sites" if relation == "callers" else "call_sites"
        try:
            sites = getattr(func, attr, None)
        except Exception:
            sites = None
        if not sites:
            return entries

        def _extract_function(site: Any, names: tuple[str, ...]) -> bn.Function | None:
            for name in names:
                try:
                    value = getattr(site, name, None)
                except Exception:
                    value = None
                if value:
                    return value
            return None

        for site in list(sites):
            try:
                entry: dict[str, Any] = {}
                addr = getattr(site, "address", None)
                if isinstance(addr, int):
                    entry["address"] = hex(addr)
                elif isinstance(addr, str) and addr:
                    entry["address"] = addr

                if relation == "callers":
                    caller_func = _extract_function(site, ("function", "source_function", "caller"))
                    ref = self._format_function_reference(caller_func)
                    if ref:
                        entry["caller"] = ref
                else:
                    callee_func = _extract_function(
                        site, ("callee", "dest_function", "target_function")
                    )
                    ref = self._format_function_reference(callee_func)
                    if ref:
                        entry["callee"] = ref
                    else:
                        # Fall back to raw destination address when available
                        dest = None
                        for attr_name in ("dest", "target", "constant"):
                            try:
                                dest = getattr(site, attr_name)
                            except Exception:
                                dest = None
                            if dest is not None:
                                break
                        if isinstance(dest, int):
                            entry["callee"] = {"name": None, "address": hex(dest)}

                # Attach textual representation for quick context
                summary_text = None
                for attr_name in ("hlil", "il"):
                    try:
                        val = getattr(site, attr_name, None)
                    except Exception:
                        val = None
                    if val is not None:
                        summary_text = str(val)
                        break
                if summary_text is None:
                    summary_text = str(site)
                entry["il"] = summary_text

                entries.append(entry)
            except Exception:
                continue
        return entries

    def get_callers(self, identifiers: Any) -> dict[str, Any]:
        """Collect caller information for the given function identifiers."""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        items = self._normalize_identifier_list(identifiers)
        if not items:
            raise ValueError("No function identifiers provided")

        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for ident in items:
            try:
                func = self.get_function_by_name_or_address(ident)
            except Exception as exc:
                func = None
                errors.append(f"{ident}: {exc}")
            if not func:
                errors.append(f"Function not found: {ident}")
                continue
            entry = {
                "identifier": str(ident),
                "function": self._format_function_reference(func),
                "callers": self._collect_related_functions(func, "callers"),
                "caller_sites": self._summarize_call_sites(func, "callers"),
            }
            results.append(entry)

        return {"results": results, "errors": errors}

    def get_callees(self, identifiers: Any) -> dict[str, Any]:
        """Collect callee information for the given function identifiers."""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        items = self._normalize_identifier_list(identifiers)
        if not items:
            raise ValueError("No function identifiers provided")

        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for ident in items:
            try:
                func = self.get_function_by_name_or_address(ident)
            except Exception as exc:
                func = None
                errors.append(f"{ident}: {exc}")
            if not func:
                errors.append(f"Function not found: {ident}")
                continue
            entry = {
                "identifier": str(ident),
                "function": self._format_function_reference(func),
                "callees": self._collect_related_functions(func, "callees"),
                "call_sites": self._summarize_call_sites(func, "callees"),
            }
            results.append(entry)

        return {"results": results, "errors": errors}

    def get_function_names(self, offset: int = 0, limit: int = 100) -> list[dict[str, str]]:
        """Get list of function names with addresses"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        functions = []
        for func in self._current_view.functions:
            functions.append(
                {
                    "name": func.name,
                    "address": hex(func.start),
                    "raw_name": func.raw_name if hasattr(func, "raw_name") else func.name,
                }
            )

        return functions[offset : offset + limit]

    def get_class_names(self, offset: int = 0, limit: int = 100) -> list[str]:
        """Get list of class names with pagination"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        class_names = set()

        try:
            # Try different methods to identify classes
            for type_obj in self._current_view.types.values():
                try:
                    # Skip None or invalid types
                    if not type_obj or not hasattr(type_obj, "name"):
                        continue

                    # Method 1: Check type_class attribute
                    if hasattr(type_obj, "type_class"):
                        class_names.add(type_obj.name)
                        continue

                    # Method 2: Check structure attribute
                    if hasattr(type_obj, "structure") and type_obj.structure:
                        structure = type_obj.structure

                        # Check various attributes that indicate a class
                        if any(
                            hasattr(structure, attr)
                            for attr in [
                                "vtable",
                                "base_structures",
                                "members",
                                "functions",
                            ]
                        ):
                            class_names.add(type_obj.name)
                            continue

                        # Check type attribute if available
                        if hasattr(structure, "type"):
                            type_str = str(structure.type).lower()
                            if "class" in type_str or "struct" in type_str:
                                class_names.add(type_obj.name)
                                continue

                except Exception as e:
                    bn.log_debug(
                        f"Error processing type {getattr(type_obj, 'name', '<unknown>')}: {e}"
                    )
                    continue

            bn.log_info(f"Found {len(class_names)} classes")
            sorted_names = sorted(list(class_names))
            return sorted_names[offset : offset + limit]

        except Exception as e:
            bn.log_error(f"Error getting class names: {e}")
            return []

    def get_segments(self, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Get list of segments with pagination"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        segments = []
        for segment in self._current_view.segments:
            segment_info = {
                "start": hex(segment.start),
                "end": hex(segment.end),
                "name": "",
                "flags": [],
            }

            # Try to get segment name if available
            if hasattr(segment, "name"):
                segment_info["name"] = segment.name
            elif hasattr(segment, "data_name"):
                segment_info["name"] = segment.data_name

            # Try to get segment flags safely
            if hasattr(segment, "flags"):
                try:
                    if isinstance(segment.flags, (list, tuple)):
                        segment_info["flags"] = list(segment.flags)
                    else:
                        segment_info["flags"] = [str(segment.flags)]
                except (AttributeError, TypeError, ValueError):
                    pass

            # Add segment permissions if available
            if hasattr(segment, "readable"):
                segment_info["readable"] = bool(segment.readable)
            if hasattr(segment, "writable"):
                segment_info["writable"] = bool(segment.writable)
            if hasattr(segment, "executable"):
                segment_info["executable"] = bool(segment.executable)

            segments.append(segment_info)

        return segments[offset : offset + limit]

    def get_sections(self, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Get list of sections with pagination.

        Returns per-section fields when available:
        - name: section name
        - start/end: hex strings
        - size: integer number of bytes (end - start)
        - type: stringified section type (if exposed by BN)
        - semantics: stringified semantics (if exposed by BN)
        - linked_section: related/paired section name if exposed
        - alignment: alignment in bytes if exposed
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        results: list[dict[str, Any]] = []

        # Binary Ninja has exposed sections across versions either as an
        # iterable of Section objects or a dict-like object. Handle both.
        try:
            sec_container = getattr(self._current_view, "sections", None)
        except Exception:
            sec_container = None
        if not sec_container:
            return []

        def _iter_sections(container):
            try:
                # If it's a dict-like {name: Section}
                if hasattr(container, "items"):
                    for _name, _sec in list(container.items()):
                        yield _sec
                    return
            except Exception:
                pass
            # Otherwise assume it's iterable of Section objects
            try:
                for _sec in list(container):
                    yield _sec
            except Exception:
                return

        for sec in _iter_sections(sec_container):
            try:
                start = getattr(sec, "start", None)
                end = getattr(sec, "end", None)
                if start is None or end is None:
                    continue
                name = None
                try:
                    name = getattr(sec, "name", None)
                except Exception:
                    name = None
                try:
                    size = int(end) - int(start)
                except Exception:
                    size = None

                entry: dict[str, Any] = {
                    "name": name or "",
                    "start": hex(int(start)),
                    "end": hex(int(end)),
                    "size": size,
                }

                # Optional attributes: type, semantics, linked_section, alignment
                for attr, key in (
                    ("type", "type"),
                    ("semantics", "semantics"),
                    ("linked_section", "linked_section"),
                    ("align", "alignment"),
                    ("alignment", "alignment"),
                ):
                    try:
                        val = getattr(sec, attr, None)
                        if val is not None:
                            entry[key] = str(val)
                    except Exception:
                        pass

                results.append(entry)
            except Exception:
                continue

        return results[offset : offset + limit]

    def rename_function(self, old_name: str, new_name: str) -> bool:
        """Rename a function using multiple fallback methods.

        Args:
            old_name: Current function name or address
            new_name: New name for the function

        Returns:
            True if rename succeeded, False otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(old_name)
            if not func:
                bn.log_error(f"Function not found: {old_name}")
                return False

            bn.log_info(f"Found function to rename: {func.name} at {hex(func.start)}")

            if not new_name or not isinstance(new_name, str):
                bn.log_error(f"Invalid new name: {new_name}")
                return False

            if not hasattr(func, "name") or not hasattr(func, "__setattr__"):
                bn.log_error(f"Function {func.name} cannot be renamed (read-only)")
                return False

            try:
                # Try direct name assignment first
                old_name = func.name
                func.name = new_name

                if func.name == new_name:
                    bn.log_info(f"Successfully renamed function from {old_name} to {new_name}")
                    return True

                # Try symbol-based renaming if direct assignment fails
                if hasattr(func, "symbol") and func.symbol:
                    try:
                        new_symbol = bn.Symbol(
                            func.symbol.type,
                            func.start,
                            new_name,
                            namespace=func.symbol.namespace
                            if hasattr(func.symbol, "namespace")
                            else None,
                        )
                        self._current_view.define_user_symbol(new_symbol)
                        bn.log_info("Successfully renamed function using symbol table")
                        return True
                    except Exception as e:
                        bn.log_error(f"Symbol-based rename failed: {e}")

                # Try function update method as last resort
                if hasattr(self._current_view, "update_function"):
                    try:
                        func_copy = func
                        func_copy.name = new_name
                        self._current_view.update_function(func)
                        bn.log_info("Successfully renamed function using update method")
                        return True
                    except Exception as e:
                        bn.log_error(f"Function update rename failed: {e}")

                bn.log_error(f"All rename methods failed - function name unchanged: {func.name}")
                return False

            except Exception as e:
                bn.log_error(f"Error during rename operation: {e}")
                return False

        except Exception as e:
            bn.log_error(f"Error in rename_function: {e}")
            return False

    def get_function_info(self, identifier: str | int) -> dict[str, Any] | None:
        """Get detailed information about a function"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        func = self.get_function_by_name_or_address(identifier)
        if not func:
            return None

        bn.log_info(f"Found function: {func.name} at {hex(func.start)}")

        info = {
            "name": func.name,
            "raw_name": func.raw_name if hasattr(func, "raw_name") else func.name,
            "address": hex(func.start),
            "symbol": None,
        }

        if func.symbol:
            info["symbol"] = {
                "type": str(func.symbol.type),
                "full_name": func.symbol.full_name
                if hasattr(func.symbol, "full_name")
                else func.symbol.name,
            }

        return info

    def decompile_function(self, identifier: str | int) -> str | None:
        """Decompile a function and include addresses per statement.

        Args:
            identifier: Function name or address

        Returns:
            Decompiled HLIL-like code with address prefixes per line
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        func = self.get_function_by_name_or_address(identifier)
        if not func:
            return None

        # analyze func in case it was skipped
        func.analysis_skipped = False
        self._current_view.update_analysis_and_wait()

        try:
            il = getattr(func, "hlil", None)
            if il and hasattr(il, "instructions"):
                lines: list[str] = []
                last_addr: int | None = None
                for ins in il.instructions:
                    try:
                        addr = getattr(ins, "address", None)
                    except Exception:
                        addr = None
                    if addr is None:
                        addr = last_addr if last_addr is not None else func.start
                    last_addr = addr
                    addr_str = f"{int(addr):08x}"
                    text = str(ins)
                    lines.append(f"{addr_str}        {text}")
                return "\n".join(lines)
            # Fall back to MLIL with addresses
            mil = getattr(func, "mlil", None)
            if mil and hasattr(mil, "instructions"):
                lines: list[str] = []
                last_addr: int | None = None
                for ins in mil.instructions:
                    try:
                        addr = getattr(ins, "address", None)
                    except Exception:
                        addr = None
                    if addr is None:
                        addr = last_addr if last_addr is not None else func.start
                    last_addr = addr
                    addr_str = f"{int(addr):08x}"
                    text = str(ins)
                    lines.append(f"{addr_str}        {text}")
                return "\n".join(lines)
            # Last resort
            return str(func)
        except Exception as e:
            bn.log_error(f"Error decompiling function: {e!s}")
            return None

    def get_function_il(
        self, identifier: str | int, view: str = "hlil", ssa: bool = False
    ) -> str | None:
        """Return IL for a function with selectable view and optional SSA form.

        Args:
            identifier: Function name or address
            view: One of 'hlil', 'mlil', 'llil' (case-insensitive). Aliases: 'il' -> 'llil'.
            ssa: When True, use SSA form if available (MLIL/LLIL only)

        Returns:
            Concatenated string with one instruction per line prefixed by address.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        func = self.get_function_by_name_or_address(identifier)
        if not func:
            return None

        # Ensure analysis has run for this function
        try:
            func.analysis_skipped = False
            self._current_view.update_analysis_and_wait()
        except Exception:
            pass

        v = (view or "").strip().lower()
        if v in ("il", "llil", "low", "lowlevel", "low-level", "low_level"):
            prop = "llil"
        elif v in ("mlil", "medium", "mediumlevel", "medium-level", "medium_level"):
            prop = "mlil"
        else:
            # Default to HLIL when unknown
            prop = "hlil"

        try:
            il_func = getattr(func, prop, None)
            if il_func is None:
                return None

            # Only MLIL/LLIL support SSA form in practice
            if ssa and hasattr(il_func, "ssa_form") and il_func.ssa_form is not None:
                il_func = il_func.ssa_form

            if not hasattr(il_func, "instructions"):
                # As a last resort, stringify the object
                return str(il_func)

            lines: list[str] = []
            last_addr: int | None = None
            for ins in il_func.instructions:
                try:
                    addr = getattr(ins, "address", None)
                except Exception:
                    addr = None
                if addr is None:
                    addr = last_addr if last_addr is not None else func.start
                last_addr = addr
                addr_str = f"{int(addr):08x}"
                text = str(ins)
                lines.append(f"{addr_str}        {text}")
            return "\n".join(lines)
        except Exception as e:
            bn.log_error(
                f"Error getting {prop}{' SSA' if ssa else ''} for function {identifier}: {e!s}"
            )
            return None

    def rename_data(self, address: int, new_name: str) -> bool:
        """Rename data at a specific address"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if self._current_view.is_valid_offset(address):
                self._current_view.define_user_symbol(
                    bn.Symbol(bn.SymbolType.DataSymbol, address, new_name)
                )
                return True
        except Exception as e:
            bn.log_error(f"Failed to rename data: {e}")
        return False

    def make_function_at(
        self, address: str | int, architecture: str | None = None
    ) -> dict[str, Any]:
        """Create a function at the given address (no-op if it already exists).

        Args:
            address: Hex string (e.g., 0x401000) or integer address.
            architecture: Optional architecture name (e.g., "x86_64", "x86", "armv7").

        Returns:
            Dict with keys: status (ok|exists), address, name (if found), architecture (if resolved).

        Raises:
            RuntimeError if no binary is loaded.
            ValueError on invalid address or creation failure.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            addr = parse_address(address)
        except ValueError as e:
            raise ValueError(str(e)) from None

        bv = self._current_view

        # If a function already exists, return info
        try:
            existing = bv.get_function_at(addr)
            if existing:
                return {
                    "status": "exists",
                    "address": hex(addr),
                    "name": existing.name,
                    "architecture": str(getattr(existing, "arch", getattr(bv, "arch", ""))) or None,
                }
        except Exception:
            pass

        # Resolve platform if provided; otherwise use view/platform default.
        # Note: BinaryView.create_user_function expects a Platform, not an Architecture.
        plat_obj = None
        arch_token = None
        if isinstance(architecture, str):
            arch_token = architecture.strip().lower()
        if architecture and arch_token not in (None, "", "default", "auto", "platform"):
            try:
                P = getattr(__import__("binaryninja", fromlist=["Platform"]), "Platform", None)
            except Exception:
                P = None
            if P is not None:
                try:
                    plat_obj = P[architecture]
                except Exception:
                    try:
                        getp = getattr(P, "get_by_name", None)
                        if callable(getp):
                            plat_obj = getp(architecture)
                    except Exception:
                        plat_obj = None
            # If user explicitly provided an architecture/platform name and we couldn't resolve it,
            # return an error with suggestions instead of silently using the default.
            if plat_obj is None:
                import re as _re
                from difflib import get_close_matches as _gcm

                names: list[str] = []
                # Prefer dynamic enumeration via binaryninja.Platform
                try:
                    import binaryninja as _bn  # type: ignore

                    try:
                        names = [
                            str(getattr(p, "name", str(p))) for p in list(getattr(_bn, "Platform"))
                        ]
                    except Exception:
                        names = []
                except Exception:
                    names = []
                # Fallback: try iterating via imported P if available
                if not names and P is not None:
                    try:
                        names = [str(getattr(p, "name", str(p))) for p in list(P)]
                    except Exception:
                        names = []
                # Last resort: static catalog (kept up-to-date best-effort)
                if not names:
                    names = [
                        "decree-x86",
                        "efi-x86",
                        "efi-windows-x86",
                        "efi-x86_64",
                        "efi-windows-x86_64",
                        "efi-aarch64",
                        "efi-windows-aarch64",
                        "efi-armv7",
                        "efi-thumb2",
                        "freebsd-x86",
                        "freebsd-x86_64",
                        "freebsd-aarch64",
                        "freebsd-armv7",
                        "freebsd-thumb2",
                        "ios-aarch64",
                        "ios-armv7",
                        "ios-thumb2",
                        "ios-kernel-aarch64",
                        "ios-kernel-armv7",
                        "ios-kernel-thumb2",
                        "linux-ppc32",
                        "linux-ppcvle32",
                        "linux-ppc64",
                        "linux-ppc32_le",
                        "linux-ppc64_le",
                        "linux-rv32gc",
                        "linux-rv64gc",
                        "linux-x86",
                        "linux-x86_64",
                        "linux-x32",
                        "linux-aarch64",
                        "linux-armv7",
                        "linux-thumb2",
                        "linux-armv7eb",
                        "linux-thumb2eb",
                        "linux-mipsel",
                        "linux-mips",
                        "linux-mips3",
                        "linux-mipsel3",
                        "linux-mips64",
                        "linux-cnmips64",
                        "linux-mipsel64",
                        "mac-x86",
                        "mac-x86_64",
                        "mac-aarch64",
                        "mac-armv7",
                        "mac-thumb2",
                        "mac-kernel-x86",
                        "mac-kernel-x86_64",
                        "mac-kernel-aarch64",
                        "mac-kernel-armv7",
                        "mac-kernel-thumb2",
                        "windows-x86",
                        "windows-x86_64",
                        "windows-aarch64",
                        "windows-armv7",
                        "windows-thumb2",
                        "windows-kernel-x86",
                        "windows-kernel-x86_64",
                        "windows-kernel-windows-aarch64",
                    ]
                # Build ranked suggestions
                tl = (arch_token or "").lower()

                def _score(n: str) -> float:
                    nl = n.lower()
                    s = 0.0
                    if tl and tl in nl:
                        s += 2.0
                    # remove non-alnum for loose matching
                    tlr = _re.sub(r"[^a-z0-9]", "", tl)
                    nlr = _re.sub(r"[^a-z0-9]", "", nl)
                    if tlr and tlr in nlr:
                        s += 1.0
                    return s

                base = sorted(names)
                # Start with substring matches, then extend with close matches
                substr = [n for n in base if tl in n.lower()]
                # Use difflib for additional candidates if needed
                extra = _gcm(tl, base, n=10, cutoff=0.3) if tl else []
                cand = []
                seen = set()
                for n in substr + extra:
                    if n not in seen:
                        seen.add(n)
                        cand.append(n)
                cand.sort(key=_score, reverse=True)
                cand[:10]
                raise ValueError(f"Unknown platform/architecture '{architecture}'")
        # Default/platform fallback when no explicit architecture provided
        if plat_obj is None:
            try:
                plat_obj = getattr(bv, "platform", None)
            except Exception:
                plat_obj = None

        # Pre-flight: address must be in a mapped, executable segment.
        # Without this, `bv.create_user_function` happily "succeeds"
        # at unmapped addresses (no function actually appears) and
        # overlays functions on top of data symbols (clobbering them
        # silently). Both produce misleading "ok" responses.
        seg = None
        try:
            seg_getter = getattr(bv, "get_segment_at", None)
            if callable(seg_getter):
                seg = seg_getter(addr)
        except Exception:
            seg = None
        if seg is None:
            raise ValueError(
                f"Address {hex(addr)} is not in any mapped segment; "
                "no function can be created there."
            )
        if not getattr(seg, "executable", False):
            raise ValueError(
                f"Address {hex(addr)} lies in a non-executable segment "
                f"({hex(int(seg.start))}-{hex(int(seg.end))}); refusing to "
                "create a function over data. If the address truly contains "
                "code that BN misclassified, fix the segment permissions "
                "first."
            )

        # Create the function
        try:
            if hasattr(bv, "create_user_function"):
                if plat_obj is not None:
                    bv.create_user_function(addr, plat_obj)
                else:
                    bv.create_user_function(addr)
            elif hasattr(bv, "add_function"):
                if plat_obj is not None:
                    bv.add_function(addr, plat_obj)
                else:
                    bv.add_function(addr)
            else:
                raise ValueError("BinaryView does not support function creation")
        except Exception as e:
            raise ValueError(f"Failed to create function: {e!s}")

        # Post-flight: BN's `create_user_function` doesn't always
        # raise on failure — confirm the function actually exists
        # before reporting success.
        try:
            fn = bv.get_function_at(addr)
        except Exception:
            fn = None
        if fn is None:
            raise ValueError(
                f"BN accepted the request but no function exists at "
                f"{hex(addr)} after analysis; the address may not contain "
                "valid instructions."
            )
        return {
            "status": "ok",
            "address": hex(addr),
            "name": fn.name if fn else None,
            "platform": str(plat_obj) if plat_obj is not None else None,
            "architecture": str(getattr(plat_obj, "arch", None))
            if plat_obj is not None
            else (
                str(getattr(bv, "arch", None)) if getattr(bv, "arch", None) is not None else None
            ),
        }

    def get_defined_data(
        self, offset: int = 0, limit: int = 100, read_len: int = 32
    ) -> list[dict[str, Any]]:
        """Get list of defined data variables with lightweight previews and sizes.

        Returns per-item fields:
        - address: hex string
        - name/raw_name: label info if available
        - type: string if available
        - size: exact defined size in bytes if known (from BN type)
        - width: alias of size for backward compatibility
        - value: small integer value when width<=8 and readable; otherwise None
        - bytes_hex: hex string of up to preview_len bytes
        - ascii_preview: printable ASCII representation for the same bytes
        - repr: concise, human-friendly summary for LLMs (value/ASCII/hex)
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        data_items = []
        for var in self._current_view.data_vars:
            data_type = None  # may be a BN Type or a DataVariable
            value = None
            width = None
            bytes_hex = None
            ascii_preview = None
            typ_obj = None

            try:
                # Prefer DataVariable (carries underlying Type)
                dv = None
                if hasattr(self._current_view, "get_data_var_at"):
                    try:
                        dv = self._current_view.get_data_var_at(var)
                    except Exception:
                        dv = None
                if dv is not None and hasattr(dv, "type") and dv.type is not None:
                    typ_obj = dv.type
                    data_type = dv  # keep for fallback string formatting
                else:
                    # Fall back to direct type lookup
                    if hasattr(self._current_view, "get_type_at"):
                        try:
                            typ_obj = self._current_view.get_type_at(var)
                            data_type = typ_obj
                        except Exception:
                            typ_obj = None

                # Exact defined size if available
                if typ_obj is not None and hasattr(typ_obj, "width"):
                    try:
                        width = int(typ_obj.width)
                    except Exception:
                        width = None

                # Best-effort numeric read for small integers (<= 8 bytes)
                if width is not None and width <= 8:
                    try:
                        value = str(self._current_view.read_int(var, width))
                    except (ValueError, RuntimeError):
                        value = None

                # Provide bytes + ASCII preview for all cases
                # Determine effective read length
                try:
                    requested = int(read_len)
                except Exception:
                    requested = 32
                # If requested < 0 and width known, treat as "read exact size"
                if requested < 0 and width is not None:
                    eff_len = max(0, int(width))
                else:
                    eff_len = max(0, requested if requested >= 0 else 32)
                if width is not None:
                    eff_len = min(eff_len, int(width))

                try:
                    raw = self._current_view.read(var, eff_len)
                    if raw is not None:
                        try:
                            bytes_hex = raw.hex()
                        except Exception:
                            bytes_hex = None
                        try:
                            ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in raw)
                        except Exception:
                            ascii_preview = None
                except (ValueError, RuntimeError, TypeError):
                    pass
            except (AttributeError, TypeError, ValueError, RuntimeError):
                value = None
                data_type = None
                typ_obj = None

            # If BN doesn't expose a width, try to infer size from call sites
            if width is None:
                try:
                    inferred = self.infer_data_size(int(var))
                    if isinstance(inferred, int) and inferred > 0:
                        width = inferred
                except Exception:
                    pass

            # Get symbol information
            sym = self._current_view.get_symbol_at(var)
            # Choose a concise repr for LLMs
            if value is not None:
                short_repr = f"int:{value}"
            elif ascii_preview:
                short_repr = f'ascii:"{ascii_preview}"'
            elif bytes_hex:
                short_repr = f"hex:{bytes_hex}"
            else:
                short_repr = None

            data_items.append(
                {
                    "address": hex(var),
                    "name": sym.name if sym else "(unnamed)",
                    "raw_name": sym.raw_name if sym and hasattr(sym, "raw_name") else None,
                    # Prefer clean type string (avoid "<var ...>" envelope when possible)
                    "type": (
                        str(typ_obj)
                        if typ_obj is not None
                        else (str(data_type) if data_type else None)
                    ),
                    "size": width,
                    "width": width,
                    "value": value,
                    "bytes_hex": bytes_hex,
                    "ascii_preview": ascii_preview,
                    "bytes_read": len(bytes_hex) // 2 if bytes_hex else 0,
                    "repr": short_repr,
                }
            )

        return data_items[offset : offset + limit]

    def infer_data_size(self, address: int) -> int | None:
        """Infer size for data at address when BN hasn't defined a type width.

        Strategy:
        - Prefer BN's DataVariable.type.width or get_type_at().width if available.
        - Otherwise scan HLIL for calls like memcmp/strncmp/memcpy/strncpy where
          an argument equals this address and extract the last numeric argument
          as a best-effort length. Returns the maximum constant seen.
        """
        if not self._current_view:
            return None

        # 1) BN-provided width if available
        try:
            dv = None
            if hasattr(self._current_view, "get_data_var_at"):
                dv = self._current_view.get_data_var_at(address)
            t = None
            if dv is not None and hasattr(dv, "type"):
                t = dv.type
            elif hasattr(self._current_view, "get_type_at"):
                t = self._current_view.get_type_at(address)
            if t is not None and hasattr(t, "width") and t.width:
                return int(t.width)
        except Exception:
            pass

        # 2) HLIL heuristic
        try:
            addr_hex = hex(address)
            candidates: list[int] = []
            names = ("memcmp", "strncmp", "memcpy", "strncpy")
            for func in list(self._current_view.functions):
                try:
                    il = getattr(func, "hlil", None)
                    if not il:
                        continue
                    for ins in il.instructions:
                        try:
                            text = str(ins)
                            if addr_hex not in text:
                                continue
                            if not any(n in text for n in names):
                                continue
                            # Extract all numeric constants
                            nums = re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", text)
                            vals: list[int] = []
                            for n in nums:
                                try:
                                    v = int(n, 16) if n.startswith("0x") else int(n)
                                    vals.append(v)
                                except Exception:
                                    continue
                            if vals:
                                # Heuristic: last constant in call string is likely the size
                                candidates.append(vals[-1])
                        except Exception:
                            continue
                except Exception:
                    continue
            if candidates:
                # Use the maximum plausible size
                best = max(c for c in candidates if c > 0)
                if best > 0:
                    return best
        except Exception:
            pass
        return None

    def list_local_types(
        self, offset: int = 0, limit: int = 100, include_libraries: bool = False
    ) -> list[dict[str, Any]]:
        """List local types (Types view) in the current database.

        Returns a list of dictionaries with:
        - name: type name
        - kind: struct/union/class/enum/typedef/unknown
        - decl: string form of the type (when available)
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        results: list[dict[str, Any]] = []
        seen_keys = set()
        try:

            def add_type_entry(name, tobj):
                # Normalize name to string to avoid BN QualifiedName in JSON
                try:
                    name_str = str(name) if name is not None else None
                except Exception:
                    name_str = None
                if not name_str:
                    return
                # Fallback: try to resolve missing type object by querying BV / libraries
                if tobj is None:
                    try:
                        if hasattr(self._current_view, "get_type_by_name"):
                            t2 = self._current_view.get_type_by_name(name_str)
                            if t2 is not None:
                                tobj = t2
                    except Exception:
                        pass
                    if tobj is None:
                        try:
                            plat = getattr(self._current_view, "platform", None)
                            libs = list(getattr(plat, "type_libraries", []) or []) if plat else []
                            for lib in libs:
                                get_t = getattr(lib, "get_type_by_name", None)
                                if not callable(get_t):
                                    continue
                                t3 = None
                                try:
                                    # Try QualifiedName if available
                                    try:
                                        t3 = get_t(bn.QualifiedName(name_str))
                                    except Exception:
                                        t3 = get_t(name_str)
                                except Exception:
                                    t3 = None
                                if t3 is not None:
                                    tobj = t3
                                    break
                        except Exception:
                            pass

                tc = getattr(tobj, "type_class", None)
                kind = "unknown"
                if tc == TypeClass.VoidTypeClass:
                    kind = "void"
                elif tc == TypeClass.BoolTypeClass:
                    kind = "bool"
                elif tc == TypeClass.IntegerTypeClass:
                    kind = "int"
                elif tc == TypeClass.FloatTypeClass:
                    kind = "float"
                elif tc == TypeClass.StructureTypeClass:
                    try:
                        if getattr(tobj, "type", None) == StructureVariant.StructStructureType:
                            kind = "struct"
                        elif getattr(tobj, "type", None) == StructureVariant.UnionStructureType:
                            kind = "union"
                        elif getattr(tobj, "type", None) == StructureVariant.ClassStructureType:
                            kind = "class"
                        else:
                            kind = "struct"
                    except Exception:
                        kind = "struct"
                elif tc == TypeClass.EnumerationTypeClass:
                    kind = "enum"
                elif tc == TypeClass.NamedTypeReferenceClass:
                    kind = "typedef"
                elif tc == TypeClass.FunctionTypeClass:
                    kind = "function"
                elif tc == TypeClass.WideCharTypeClass:
                    kind = "wchar"
                elif tc == TypeClass.PointerTypeClass:
                    kind = "pointer"
                elif tc == TypeClass.ArrayTypeClass:
                    kind = "array"

                decl = None
                try:
                    decl = str(tobj)
                except Exception:
                    try:
                        decl = str(getattr(tobj, "type", None))
                    except Exception:
                        decl = None

                # If kind is unknown or a named typedef, try to infer underlying from declaration text
                try:
                    dlow = (decl or "").strip().lower()
                    if dlow:
                        if dlow.startswith("struct ") or " struct " in dlow:
                            kind = "struct"
                        elif dlow.startswith("union ") or " union " in dlow:
                            kind = "union"
                        elif dlow.startswith("enum ") or " enum " in dlow:
                            kind = "enum"
                except Exception:
                    pass

                key = (name_str, decl or "")
                if key in seen_keys:
                    return
                results.append(
                    {
                        "name": name_str,
                        "kind": kind,
                        "type_class": str(tc) if tc is not None else None,
                        "decl": decl,
                    }
                )
                seen_keys.add(key)

            # Source 1: user_type_container (explicit local/user types)
            try:
                utc = getattr(self._current_view, "user_type_container", None)
                if utc and getattr(utc, "types", None):
                    for type_id in list(utc.types.keys()):
                        try:
                            entry = utc.types[type_id]
                            name = (
                                entry[0]
                                if isinstance(entry, (tuple, list))
                                else getattr(entry, "name", None)
                            )
                            tobj = (
                                entry[1]
                                if isinstance(entry, (tuple, list))
                                else getattr(entry, "type", entry)
                            )
                            add_type_entry(name, tobj)
                        except Exception:
                            continue
            except Exception:
                pass

            # Source 2: every named type the view knows about. Use
            # `bv.type_names` + `bv.get_type_by_name` rather than
            # iterating `bv.types`, because the latter misses types
            # that BN's auto-loaders (DWARF, type libraries) registered
            # — those live in `auto_type_container` and are surfaced
            # to the view via name-lookup, not via the `types` dict.
            try:
                names = list(getattr(self._current_view, "type_names", []) or [])
            except Exception:
                names = []
            for qname in names:
                try:
                    tobj = self._current_view.get_type_by_name(qname)
                    add_type_entry(str(qname), tobj)
                except Exception:
                    continue

            # Source 3: platform type libraries (optional; can be heavy)
            if include_libraries:
                try:
                    plat = getattr(self._current_view, "platform", None)
                    libs = []
                    try:
                        libs = list(getattr(plat, "type_libraries", []) or [])
                    except Exception:
                        libs = []
                    for lib in libs:
                        # Try multiple ways to enumerate names in this library
                        names = []
                        try:
                            nt = getattr(lib, "named_types", None)
                            if isinstance(nt, dict):
                                names = list(nt.keys())
                        except Exception:
                            pass
                        if not names:
                            try:
                                tmap = getattr(lib, "types", None)
                                if isinstance(tmap, dict):
                                    names = list(tmap.keys())
                            except Exception:
                                pass
                        if not names:
                            try:
                                get_names = getattr(lib, "get_type_names", None)
                                if callable(get_names):
                                    names = list(get_names())
                            except Exception:
                                pass
                        # Fetch each type object if possible
                        for nm in names:
                            try:
                                tobj = None
                                try:
                                    g = getattr(lib, "get_type_by_name", None)
                                    if callable(g):
                                        tobj = g(nm)
                                except Exception:
                                    tobj = None
                                add_type_entry(nm, tobj)
                            except Exception:
                                continue
                except Exception:
                    pass
        except Exception as e:
            bn.log_error(f"Error listing local types: {e}")
        return results[offset : offset + limit]

    def search_local_types(
        self, query: str, offset: int = 0, limit: int = 100, include_libraries: bool = False
    ) -> list[dict[str, Any]]:
        """Search local/view types whose name or declaration contains the substring.

        Returns entries with {name, kind, type_class, decl}.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        if not query:
            return []
        ql = str(query).lower()
        # Only local types by default (fast). Optionally include libraries.
        all_types = self.list_local_types(0, 1_000_000, include_libraries=include_libraries)
        matches: list[dict[str, Any]] = []
        for t in all_types:
            try:
                name = t.get("name") or ""
                decl = t.get("decl") or ""
                if (ql in str(name).lower()) or (ql in str(decl).lower()):
                    matches.append(t)
            except Exception:
                continue
        if isinstance(limit, int) and limit < 0:
            return matches[offset:]
        return matches[offset : offset + limit]

    def get_type_info(self, name: str) -> dict[str, Any]:
        """Resolve a type by name and return detailed information.

        Returns a dictionary with:
        - name: type name
        - kind: struct/union/class/enum/typedef/... (best-effort)
        - decl: declaration string
        - members: for struct/union [{name, type, offset}]
        - enum_members: for enums [{name, value}]
        - underlying: for typedefs, best-effort underlying declaration
        - source: local | library | unknown
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        type_name = str(name)
        tobj = None
        source = "unknown"

        # 1) Try view-local resolution first (covers any type already
        #    imported into the current BinaryView, including Mach-O /
        #    ELF header types BN brings in automatically).
        try:
            if hasattr(self._current_view, "get_type_by_name"):
                t = self._current_view.get_type_by_name(type_name)
                if t is not None:
                    tobj = t
                    source = "local"
        except Exception:
            pass

        plat = getattr(self._current_view, "platform", None)

        # 2) Ask the platform directly — this covers the libc typedefs
        #    (`size_t`, `pid_t`, `FILE`, ...) that BN knows about but
        #    hasn't necessarily imported into the view yet.
        if tobj is None and plat is not None:
            get_t = getattr(plat, "get_type_by_name", None)
            if callable(get_t):
                try:
                    try:
                        t = get_t(bn.QualifiedName(type_name))
                    except Exception:
                        t = get_t(type_name)
                    if t is not None:
                        tobj = t
                        source = "platform"
                except Exception:
                    pass

        # 3) Last resort: walk the platform's type libraries one by one.
        #    Slower than the platform-level lookup above but catches
        #    types registered only in a specific library.
        if tobj is None and plat is not None:
            try:
                libs = list(getattr(plat, "type_libraries", []) or [])
                for lib in libs:
                    get_t = getattr(lib, "get_type_by_name", None) or getattr(
                        lib, "get_named_type", None
                    )
                    if not callable(get_t):
                        continue
                    try:
                        try:
                            t = get_t(bn.QualifiedName(type_name))
                        except Exception:
                            t = get_t(type_name)
                    except Exception:
                        t = None
                    if t is not None:
                        tobj = t
                        source = "library"
                        break
            except Exception:
                pass

        # Prepare defaults
        kind = "unknown"
        decl = None
        members: list[dict[str, Any]] = []
        enum_members: list[dict[str, Any]] = []
        underlying = None

        # Extract details from type object
        if tobj is not None:
            try:
                decl = str(tobj)
            except Exception:
                try:
                    decl = str(getattr(tobj, "type", None))
                except Exception:
                    decl = None

            tc = getattr(tobj, "type_class", None)
            if tc == TypeClass.StructureTypeClass:
                # structure variant
                try:
                    v = getattr(tobj, "type", None)
                    if v == StructureVariant.UnionStructureType:
                        kind = "union"
                    elif v == StructureVariant.ClassStructureType:
                        kind = "class"
                    else:
                        kind = "struct"
                except Exception:
                    kind = "struct"

                # collect members
                try:
                    for m in getattr(
                        tobj, "members", getattr(getattr(tobj, "structure", None), "members", [])
                    ):
                        try:
                            members.append(
                                {
                                    "name": getattr(m, "name", None),
                                    "type": str(getattr(m, "type", ""))
                                    if hasattr(m, "type")
                                    else None,
                                    "offset": int(getattr(m, "offset", 0))
                                    if hasattr(m, "offset")
                                    else None,
                                }
                            )
                        except Exception:
                            continue
                except Exception:
                    pass

            elif tc == TypeClass.EnumerationTypeClass:
                kind = "enum"
                try:
                    for em in getattr(tobj, "members", []):
                        try:
                            enum_members.append(
                                {
                                    "name": getattr(em, "name", None),
                                    "value": getattr(em, "value", None),
                                }
                            )
                        except Exception:
                            continue
                except Exception:
                    pass

            elif tc == TypeClass.NamedTypeReferenceClass:
                kind = "typedef"
                # best-effort underlying from decl text
                try:
                    dlow = (decl or "").lower()
                    if dlow:
                        if dlow.startswith("struct ") or " struct " in dlow:
                            underlying = "struct"
                        elif dlow.startswith("union ") or " union " in dlow:
                            underlying = "union"
                        elif dlow.startswith("enum ") or " enum " in dlow:
                            underlying = "enum"
                except Exception:
                    pass

            elif tc == TypeClass.IntegerTypeClass:
                kind = "int"
            elif tc == TypeClass.FloatTypeClass:
                kind = "float"
            elif tc == TypeClass.BoolTypeClass:
                kind = "bool"
            elif tc == TypeClass.VoidTypeClass:
                kind = "void"
            elif tc == TypeClass.PointerTypeClass:
                kind = "pointer"
            elif tc == TypeClass.ArrayTypeClass:
                kind = "array"
            elif tc == TypeClass.FunctionTypeClass:
                kind = "function"

            # Infer kind from decl if still unknown
            if kind == "unknown" and decl:
                try:
                    dl = decl.lower()
                    if dl.startswith("struct ") or " struct " in dl:
                        kind = "struct"
                    elif dl.startswith("union ") or " union " in dl:
                        kind = "union"
                    elif dl.startswith("enum ") or " enum " in dl:
                        kind = "enum"
                except Exception:
                    pass

        return {
            "name": type_name,
            "kind": kind,
            "decl": decl,
            "members": members if members else None,
            "enum_members": enum_members if enum_members else None,
            "underlying": underlying,
            "source": source,
        }

    def get_strings(self, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Get list of strings in the current binary view with pagination.

        Returns a list of dictionaries containing:
        - address: start address of the string (hex)
        - length: length in bytes (int if available)
        - type: Binary Ninja string type (str if available)
        - value: best-effort decoded and escaped string value
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        results: list[dict[str, Any]] = []

        try:
            # Prefer modern API if available
            strings_iter = None
            if hasattr(self._current_view, "get_strings"):
                try:
                    strings_iter = self._current_view.get_strings()
                except TypeError:
                    strings_iter = None

            if strings_iter is None and hasattr(self._current_view, "strings"):
                try:
                    strings_iter = list(self._current_view.strings)
                except Exception:
                    strings_iter = []

            if strings_iter is None:
                strings_iter = []

            for s in strings_iter:
                try:
                    addr = None
                    length = None
                    stype = None
                    value = None

                    # Common attributes on StringReference
                    addr = getattr(s, "start", getattr(s, "address", None))
                    length = getattr(s, "length", None)
                    stype = getattr(s, "type", None)
                    if stype is not None:
                        try:
                            stype = str(stype)
                        except Exception:
                            stype = str(stype)

                    value = getattr(s, "value", None)

                    # Best-effort read/decode if value is not present
                    if value is None and addr is not None and length is not None:
                        try:
                            raw = self._current_view.read(addr, length)
                            # Stop at first null byte if present
                            nul = raw.find(b"\x00")
                            if nul != -1:
                                raw = raw[:nul]
                            try:
                                value = raw.decode("utf-8", errors="ignore")
                            except Exception:
                                value = raw.decode("latin-1", errors="ignore")
                        except Exception:
                            value = None

                    # Ensure value is a string and escape non-ASCII
                    if value is None:
                        value = ""
                    value = escape_non_ascii(str(value))

                    results.append(
                        {
                            "address": hex(addr)
                            if isinstance(addr, int)
                            else (str(addr) if addr is not None else None),
                            "length": int(length)
                            if isinstance(length, (int,))
                            else (None if length is None else int(length)),
                            "type": stype,
                            "value": value,
                        }
                    )
                except Exception as e:
                    # Keep collecting even if one entry fails
                    bn.log_debug(f"Error processing string entry: {e}")
                    continue

            return results[offset : offset + limit]
        except Exception as e:
            bn.log_error(f"Error getting strings: {e}")
            return []

    def set_comment(self, address: int, comment: str) -> bool:
        """Set a comment at a specific address.

        Args:
            address: The address to set the comment at
            comment: The comment text to set

        Returns:
            True if the comment was set successfully, False otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if not self._current_view.is_valid_offset(address):
                bn.log_error(f"Invalid address for comment: {hex(address)}")
                return False

            self._current_view.set_comment_at(address, comment)
            bn.log_info(f"Set comment at {hex(address)}: {comment}")
            return True
        except Exception as e:
            bn.log_error(f"Failed to set comment: {e}")
            return False

    def set_function_comment(self, identifier: str | int, comment: str) -> bool:
        """Set a comment for a function.

        Args:
            identifier: Function name or address
            comment: The comment text to set

        Returns:
            True if the comment was set successfully, False otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                bn.log_error(f"Function not found: {identifier}")
                return False

            self._current_view.set_comment_at(func.start, comment)
            bn.log_info(f"Set comment for function {func.name} at {hex(func.start)}: {comment}")
            return True
        except Exception as e:
            bn.log_error(f"Failed to set function comment: {e}")
            return False

    def get_comment(self, address: int) -> str | None:
        """Get the comment at a specific address.

        Args:
            address: The address to get the comment from

        Returns:
            The comment text if found, None otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if not self._current_view.is_valid_offset(address):
                bn.log_error(f"Invalid address for comment: {hex(address)}")
                return None

            comment = self._current_view.get_comment_at(address)
            return comment if comment else None
        except Exception as e:
            bn.log_error(f"Failed to get comment: {e}")
            return None

    def get_function_comment(self, identifier: str | int) -> str | None:
        """Get the comment for a function.

        Args:
            identifier: Function name or address

        Returns:
            The comment text if found, None otherwise
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                bn.log_error(f"Function not found: {identifier}")
                return None

            comment = self._current_view.get_comment_at(func.start)
            return comment if comment else None
        except Exception as e:
            bn.log_error(f"Failed to get function comment: {e}")
            return None

    def delete_comment(self, address: int) -> bool:
        """Delete a comment at a specific address"""
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            if self._current_view.is_valid_offset(address):
                self._current_view.set_comment_at(address, None)
                return True
        except Exception as e:
            bn.log_error(f"Failed to delete comment: {e}")
        return False

    def delete_function_comment(self, identifier: str | int) -> bool:
        """Delete the comment at a function's start.

        Mirrors the storage used by `set_function_comment` /
        `get_function_comment` (a view-level comment at `func.start`),
        not BN's `Function.comment` attribute — those are independent
        slots and writing the wrong one silently no-ops.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                return False

            self._current_view.set_comment_at(func.start, None)
            return True
        except Exception as e:
            bn.log_error(f"Failed to delete function comment: {e}")
        return False

    # set_integer_display removed per request

    def get_assembly_function(self, identifier: str | int) -> str | None:
        """Get the assembly representation of a function with practical annotations.

        Args:
            identifier: Function name or address

        Returns:
            Assembly code as string, or None if the function cannot be found
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            func = self.get_function_by_name_or_address(identifier)
            if not func:
                bn.log_error(f"Function not found: {identifier}")
                return None

            bn.log_info(f"Found function: {func.name} at {hex(func.start)}")

            var_map = {}  # TODO: Implement this functionality (issues with var.storage not returning the correst sp offset)
            assembly_blocks = {}

            if not hasattr(func, "basic_blocks") or not func.basic_blocks:
                bn.log_error(f"Function {func.name} has no basic blocks")
                # Try alternate approach with linear disassembly
                start_addr = func.start
                try:
                    func_length = func.total_bytes
                    if func_length <= 0:
                        func_length = 1024  # Use a reasonable default if length not available
                except Exception:
                    func_length = 1024  # Use a reasonable default if error

                try:
                    # Create one big block for the entire function
                    block_lines = []
                    current_addr = start_addr
                    end_addr = start_addr + func_length

                    while current_addr < end_addr:
                        try:
                            # Get instruction length
                            instr_len = self._current_view.get_instruction_length(current_addr)
                            if instr_len <= 0:
                                instr_len = 4  # Default to a reasonable instruction length

                            # Get disassembly for this instruction
                            line = self._get_instruction_with_annotations(
                                current_addr, instr_len, var_map
                            )
                            if line:
                                block_lines.append(line)

                            current_addr += instr_len
                        except Exception as e:
                            bn.log_error(f"Error processing address {hex(current_addr)}: {e!s}")
                            block_lines.append(f"# Error at {hex(current_addr)}: {e!s}")
                            current_addr += 1  # Skip to next byte

                    assembly_blocks[start_addr] = [
                        f"# Block at {hex(start_addr)}",
                        *block_lines,
                        "",
                    ]

                except Exception as e:
                    bn.log_error(f"Linear disassembly failed: {e!s}")
                    return None
            else:
                for i, block in enumerate(func.basic_blocks):
                    try:
                        block_lines = []

                        # Process each address in the block
                        addr = block.start
                        while addr < block.end:
                            try:
                                instr_len = self._current_view.get_instruction_length(addr)
                                if instr_len <= 0:
                                    instr_len = 4  # Default to a reasonable instruction length

                                # Get disassembly for this instruction
                                line = self._get_instruction_with_annotations(
                                    addr, instr_len, var_map
                                )
                                if line:
                                    block_lines.append(line)

                                addr += instr_len
                            except Exception as e:
                                bn.log_error(f"Error processing address {hex(addr)}: {e!s}")
                                block_lines.append(f"# Error at {hex(addr)}: {e!s}")
                                addr += 1  # Skip to next byte

                        # Store block with its starting address as key
                        assembly_blocks[block.start] = [
                            f"# Block {i + 1} at {hex(block.start)}",
                            *block_lines,
                            "",
                        ]

                    except Exception as e:
                        bn.log_error(f"Error processing block {i + 1} at {hex(block.start)}: {e!s}")
                        assembly_blocks[block.start] = [
                            f"# Error processing block {i + 1} at {hex(block.start)}: {e!s}",
                            "",
                        ]

            # Sort blocks by address and concatenate them
            sorted_blocks = []
            for addr in sorted(assembly_blocks.keys()):
                sorted_blocks.extend(assembly_blocks[addr])

            return "\n".join(sorted_blocks)
        except Exception as e:
            bn.log_error(f"Error getting assembly for function {identifier}: {e!s}")
            import traceback

            bn.log_error(traceback.format_exc())
            return None

    def _get_instruction_with_annotations(
        self, addr: int, instr_len: int, var_map: dict[int, str]
    ) -> str | None:
        """Get a single instruction with practical annotations.

        Args:
            addr: Address of the instruction
            instr_len: Length of the instruction
            var_map: Dictionary mapping offsets to variable names

        Returns:
            Formatted instruction string with annotations
        """
        if not self._current_view:
            return None

        try:
            # Get raw bytes for fallback
            try:
                raw_bytes = self._current_view.read(addr, instr_len)
                hex_bytes = " ".join(f"{b:02x}" for b in raw_bytes)
            except Exception:
                hex_bytes = "??"

            # Get basic disassembly
            disasm_text = ""
            try:
                if hasattr(self._current_view, "get_disassembly"):
                    disasm = self._current_view.get_disassembly(addr)
                    if disasm:
                        disasm_text = disasm
            except Exception:
                disasm_text = hex_bytes + " ; [Raw bytes]"

            if not disasm_text:
                disasm_text = hex_bytes + " ; [Raw bytes]"

            # Check if this is a call instruction and try to get target function name
            if "call" in disasm_text.lower():
                try:
                    # Extract the address from the call instruction
                    import re

                    addr_pattern = r"0x[0-9a-fA-F]+"
                    match = re.search(addr_pattern, disasm_text)
                    if match:
                        call_addr_str = match.group(0)
                        call_addr = int(call_addr_str, 16)

                        # Look up the target function name
                        sym = self._current_view.get_symbol_at(call_addr)
                        if sym and hasattr(sym, "name"):
                            # Replace the address with the function name
                            disasm_text = disasm_text.replace(call_addr_str, sym.name)
                except Exception:
                    pass

            # Try to annotate memory references with variable names
            try:
                # Look for memory references like [reg+offset]
                import re

                mem_ref_pattern = r"\[([^\]]+)\]"
                mem_refs = re.findall(mem_ref_pattern, disasm_text)

                # For each memory reference, check if it's a known variable
                for mem_ref in mem_refs:
                    # Parse for ebp relative references
                    offset_pattern = r"(ebp|rbp)(([+-]0x[0-9a-fA-F]+)|([+-]\d+))"
                    offset_match = re.search(offset_pattern, mem_ref)
                    if offset_match:
                        # Extract base register and offset
                        offset_match.group(1)
                        offset_str = offset_match.group(2)

                        # Convert offset to integer
                        try:
                            offset = (
                                int(offset_str, 16)
                                if offset_str.startswith("0x") or offset_str.startswith("-0x")
                                else int(offset_str)
                            )

                            # Try to find variable name
                            var_name = var_map.get(offset)

                            # If found, add it to the memory reference
                            if var_name:
                                old_ref = f"[{mem_ref}]"
                                new_ref = f"[{mem_ref} {{{var_name}}}]"
                                disasm_text = disasm_text.replace(old_ref, new_ref)
                        except Exception:
                            pass
            except Exception:
                pass

            # Get comment if any
            comment = None
            try:
                comment = self._current_view.get_comment_at(addr)
            except Exception:
                pass

            # Format the final line
            addr_str = f"{addr:08x}"
            # Include hex bytes column padded for readability
            bytes_col = f"{hex_bytes}".ljust(16)
            line = f"{addr_str}  {bytes_col} {disasm_text}"

            # Add comment at the end if any
            if comment:
                line += f"  ; {comment}"

            return line
        except Exception as e:
            bn.log_error(f"Error annotating instruction at {hex(addr)}: {e!s}")
            return f"{addr:08x}  {hex_bytes} ; [Error: {e!s}]"

    def get_functions_containing_address(self, address: int) -> list:
        """Get functions containing a specific address.

        Args:
            address: The instruction address to find containing functions for

        Returns:
            List of function names containing the address
        """
        if not self.current_view:
            raise RuntimeError("No binary loaded")

        try:
            functions = list(self.current_view.get_functions_containing(address))
            return [func.name for func in functions]
        except Exception as e:
            bn.log_error(f"Error getting functions containing address {hex(address)}: {e}")
            return []

    def get_entry_points(self) -> list[dict[str, Any]]:
        """Return entry point(s) for the current binary view.

        Primarily uses `bv.entry_point`. Also includes common startup symbols like
        `_start` when resolvable.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        bv = self._current_view
        results: list[dict[str, Any]] = []

        def _append(addr: int):
            try:
                if addr is None:
                    return
                name = None
                try:
                    sym = bv.get_symbol_at(addr)
                    if sym and getattr(sym, "name", None):
                        name = sym.name
                except Exception:
                    pass
                if name is None:
                    try:
                        func = bv.get_function_at(addr)
                        if func and getattr(func, "name", None):
                            name = func.name
                    except Exception:
                        pass
                results.append(
                    {
                        "address": hex(int(addr)),
                        "name": name,
                    }
                )
            except Exception:
                pass

        # Primary entry point
        try:
            ep = getattr(bv, "entry_point", None)
            if isinstance(ep, int) and ep >= 0:
                _append(ep)
        except Exception:
            pass

        # Common startup symbol fallback
        for sname in ("_start", "entry", "start", "WinMain", "mainCRTStartup"):
            try:
                sym = bv.get_symbol_by_name(sname) if hasattr(bv, "get_symbol_by_name") else None
                if sym and hasattr(sym, "address"):
                    addr = int(sym.address)
                    if not any(r.get("address") == hex(addr) for r in results):
                        _append(addr)
            except Exception:
                continue

        return results

    # Removed: get_function_code_references() in favor of address-based get_xrefs_to_* helpers

    def get_user_defined_type(self, type_name: str) -> dict[str, Any] | None:
        """Get the definition of a user-defined type (struct, enum, etc.)

        Args:
            type_name: Name of the user-defined type to retrieve

        Returns:
            Dictionary with type information and definition, or None if not found
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            # Check if we have a user type container
            if (
                not hasattr(self._current_view, "user_type_container")
                or not self._current_view.user_type_container
            ):
                bn.log_info("No user type container available")
                return None

            # Search for the requested type by name
            found_type = None
            found_type_id = None

            for type_id in self._current_view.user_type_container.types.keys():
                current_type = self._current_view.user_type_container.types[type_id]
                type_name_from_container = current_type[0]

                if type_name_from_container == type_name:
                    found_type = current_type
                    found_type_id = type_id
                    break

            if not found_type or not found_type_id:
                bn.log_info(f"Type not found: {type_name}")
                return None

            # Determine the type category (struct, enum, etc.)
            type_category = "unknown"
            type_object = found_type[1]
            bn.log_info("Stage1")
            bn.log_info(f"Stage1.5 {type_object.type_class} {StructureVariant.StructStructureType}")
            if type_object.type_class == TypeClass.EnumerationTypeClass:
                type_category = "enum"
            elif type_object.type_class == TypeClass.StructureTypeClass:
                if type_object.type == StructureVariant.StructStructureType:
                    type_category = "struct"
                elif type_object.type == StructureVariant.UnionStructureType:
                    type_category = "union"
                elif type_object.type == StructureVariant.ClassStructureType:
                    type_category = "class"
            elif type_object.type_class == TypeClass.NamedTypeReferenceClass:
                type_category = "typedef"

            # Generate the C++ style definition
            definition_lines = []

            try:
                if (
                    type_category == "struct"
                    or type_category == "class"
                    or type_category == "union"
                ):
                    definition_lines.append(f"{type_category} {type_name} {{")
                    for member in type_object.members:
                        if hasattr(member, "name") and hasattr(member, "type"):
                            definition_lines.append(f"    {member.type} {member.name};")
                    definition_lines.append("};")
                elif type_category == "enum":
                    definition_lines.append(f"enum {type_name} {{")
                    for member in type_object.members:
                        if hasattr(member, "name") and hasattr(member, "value"):
                            definition_lines.append(f"    {member.name} = {member.value},")
                    definition_lines.append("};")
                elif type_category == "typedef":
                    str_type_object = str(type_object)
                    definition_lines.append(f"typedef {str_type_object};")
            except Exception as e:
                bn.log_error(f"Error getting type lines: {e}")

            # Construct the final definition string
            definition = "\n".join(definition_lines)

            return {"name": type_name, "type": type_category, "definition": definition}
        except Exception as e:
            bn.log_error(f"Error getting user-defined type {type_name}: {e}")
            return None

    def get_xrefs_to_address(self, address: int | str) -> dict[str, Any]:
        """Get all cross references (code and data) to a given address.

        Args:
            address: Address as int, hex string (e.g., "0x401000"), or decimal string

        Returns:
            Dictionary with address, code_references, and data_references lists
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        try:
            addr = parse_address(address)
        except ValueError as e:
            raise ValueError(str(e)) from None

        result: dict[str, Any] = {
            "address": hex(addr),
            "code_references": [],
            "data_references": [],
        }

        # Code references
        try:
            if hasattr(self._current_view, "get_code_refs"):
                for ref in list(self._current_view.get_code_refs(addr)):
                    try:
                        fn_name = ref.function.name if getattr(ref, "function", None) else None
                        entry = {"function": fn_name, "address": hex(ref.address)}

                        # Heuristic: only attach a following call if the referenced data
                        # is carried in a parameter register up to that call (likely passed as an arg)
                        try:
                            func = (
                                ref.function
                                if getattr(ref, "function", None)
                                else self._current_view.get_function_at(ref.address)
                            )
                            if func is not None:
                                import re as _re

                                # identify destination register at xref instruction
                                def _canon_reg(r: str) -> str:
                                    r = (r or "").strip().lower()
                                    mp = {
                                        "rcx": "rcx",
                                        "ecx": "rcx",
                                        "cx": "rcx",
                                        "cl": "rcx",
                                        "ch": "rcx",
                                        "rdx": "rdx",
                                        "edx": "rdx",
                                        "dx": "rdx",
                                        "dl": "rdx",
                                        "dh": "rdx",
                                        "r8": "r8",
                                        "r8d": "r8",
                                        "r8w": "r8",
                                        "r8b": "r8",
                                        "r9": "r9",
                                        "r9d": "r9",
                                        "r9w": "r9",
                                        "r9b": "r9",
                                        "rdi": "rdi",
                                        "edi": "rdi",
                                        "di": "rdi",
                                        "dil": "rdi",
                                        "rsi": "rsi",
                                        "esi": "rsi",
                                        "si": "rsi",
                                        "sil": "rsi",
                                    }
                                    return mp.get(r, r)

                                def _first_op_reg(d: str) -> str:
                                    try:
                                        parts = d.strip().split(None, 1)
                                        if len(parts) < 2:
                                            return ""
                                        ops = parts[1].split(";", 1)[0]
                                        first = ops.split(",", 1)[0].strip()
                                        if "[" in first:
                                            return ""
                                        for kw in ("byte", "word", "dword", "qword", "ptr"):
                                            if first.startswith(kw):
                                                first = first[len(kw) :].strip()
                                        return first.split()[0]
                                    except Exception:
                                        return ""

                                try:
                                    xdis = self._current_view.get_disassembly(ref.address) or ""
                                except Exception:
                                    xdis = ""
                                dest = _canon_reg(_first_op_reg(xdis))
                                arg_regs = {"rcx", "rdx", "r8", "r9", "rdi", "rsi"}
                                if dest in arg_regs:
                                    steps = 16
                                    curr = ref.address
                                    overwritten = False
                                    while steps > 0 and curr < getattr(
                                        func, "highest_address", curr + 1024
                                    ):
                                        ilen = self._current_view.get_instruction_length(curr) or 1
                                        try:
                                            dis = self._current_view.get_disassembly(curr) or ""
                                        except Exception:
                                            dis = ""
                                        # detect clobber of the arg register
                                        if (
                                            curr != ref.address
                                            and _canon_reg(_first_op_reg(dis)) == dest
                                        ):
                                            overwritten = True
                                        if ("call" in dis.lower()) and not overwritten:
                                            entry["following_call_address"] = hex(curr)
                                            m = _re.search(r"0x[0-9a-fA-F]+", dis)
                                            tgt = None
                                            if m:
                                                try:
                                                    tgt = int(m.group(0), 16)
                                                except Exception:
                                                    tgt = None
                                            if tgt is not None:
                                                sym = self._current_view.get_symbol_at(tgt)
                                                if sym and hasattr(sym, "name"):
                                                    entry["following_call_target"] = sym.name
                                                else:
                                                    tfn = self._current_view.get_function_at(tgt)
                                                    entry["following_call_target"] = (
                                                        tfn.name
                                                        if (tfn and hasattr(tfn, "name"))
                                                        else hex(tgt)
                                                    )
                                            break
                                        curr += max(1, ilen)
                                        steps -= 1
                        except Exception:
                            pass

                        result["code_references"].append(entry)
                    except Exception:
                        continue
        except Exception as e:
            bn.log_error(f"Error getting code references to {hex(addr)}: {e}")

        # Data references
        try:
            if hasattr(self._current_view, "get_data_refs"):
                for ref_addr in list(self._current_view.get_data_refs(addr)):
                    try:
                        fn = self._current_view.get_function_at(ref_addr)
                        fn_name = fn.name if fn else None
                        result["data_references"].append(
                            {"function": fn_name, "address": hex(ref_addr)}
                        )
                    except Exception:
                        continue
        except Exception as e:
            bn.log_error(f"Error getting data references to {hex(addr)}: {e}")

        return result

    def get_xrefs_to_field(self, struct_name: str, field_name: str) -> list[dict[str, Any]]:
        """Get all cross references to a named struct field (member).

        This uses a best-effort heuristic:
        - Scans HLIL for occurrences of the field name (e.g., ".field" or "->field")
        - If a global instance of the struct is found, computes the field's absolute
          address (base + offset) and includes code refs to that address
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        struct_name = str(struct_name).strip()
        field_name = str(field_name).strip()
        results: list[dict[str, Any]] = []

        # Try to resolve struct member offset
        member_offset = None
        try:
            if hasattr(self._current_view, "types") and self._current_view.types:
                for t in self._current_view.types.values():
                    try:
                        if (
                            getattr(t, "name", None) == struct_name
                            and hasattr(t, "structure")
                            and t.structure
                        ):
                            for m in getattr(t, "members", getattr(t.structure, "members", [])):
                                if getattr(m, "name", None) == field_name and hasattr(m, "offset"):
                                    member_offset = int(m.offset)
                                    break
                            if member_offset is not None:
                                break
                    except Exception:
                        continue
        except Exception:
            pass

        # HLIL scan for textual member access
        import re

        pattern = re.compile(rf"(\.|->)\s*{re.escape(field_name)}(\b|\W)")
        for func in list(self._current_view.functions):
            try:
                if not hasattr(func, "hlil") or not func.hlil:
                    continue
                for ins in func.hlil.instructions:
                    try:
                        text = str(ins)
                        if pattern.search(text):
                            results.append(
                                {
                                    "kind": "hlil-match",
                                    "function": func.name,
                                    "address": hex(getattr(ins, "address", func.start)),
                                    "text": text,
                                }
                            )
                    except Exception:
                        continue
            except Exception:
                continue

        # If we know the member offset, try to find global instances and code-refs
        if member_offset is not None:
            try:
                for var_addr in list(self._current_view.data_vars):
                    try:
                        t = None
                        if hasattr(self._current_view, "get_type_at"):
                            t = self._current_view.get_type_at(var_addr)
                        t_str = str(t) if t is not None else ""
                        # crude match for exact or pointer to struct
                        if (
                            t_str == struct_name
                            or t_str.endswith(f"* {struct_name}")
                            or struct_name in t_str
                        ):
                            field_addr = var_addr + member_offset
                            # code refs to this absolute address
                            try:
                                for ref in list(self._current_view.get_code_refs(field_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    results.append(
                                        {
                                            "kind": "global-field-ref",
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "field_address": hex(field_addr),
                                        }
                                    )
                            except Exception:
                                pass
                    except Exception:
                        continue
            except Exception:
                pass

        return results

    def get_xrefs_to_type(self, type_name: str) -> dict[str, Any]:
        """Get cross references/usages related to a struct/type name.

        Best-effort heuristics:
        - Finds global data variables whose type string mentions the type name; includes code refs to those globals
        - Scans HLIL text for instructions mentioning the type (casts/annotations)
        - Marks functions whose signature mentions the type
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        type_name = str(type_name).strip()
        tnl = type_name.lower()

        result: dict[str, Any] = {
            "type": type_name,
            "data_instances": [],  # [{address, type, name?}]
            "data_code_references": [],  # [{function, address, target}]
            "code_references": [],  # HLIL matches [{function, address, text}]
            "functions_with_type": [],  # function names
        }

        # 1) Global data variables whose type matches the type name
        try:
            for var_addr in list(self._current_view.data_vars):
                try:
                    t = None
                    if hasattr(self._current_view, "get_type_at"):
                        t = self._current_view.get_type_at(var_addr)
                    t_str = str(t) if t is not None else ""
                    if t_str and tnl in t_str.lower():
                        sym = self._current_view.get_symbol_at(var_addr)
                        result["data_instances"].append(
                            {
                                "address": hex(var_addr),
                                "type": t_str,
                                "name": sym.name if sym else None,
                            }
                        )
                        # Also add code refs to this global
                        try:
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(var_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    result["data_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "target": hex(var_addr),
                                        }
                                    )
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass

        # 2) HLIL textual matches for the type (casts/annotations)
        try:
            import re

            # Look for the type name as a word or part of a cast/annotation
            pat = re.compile(re.escape(type_name), re.IGNORECASE)
            for func in list(self._current_view.functions):
                try:
                    if hasattr(func, "hlil") and func.hlil:
                        for ins in func.hlil.instructions:
                            try:
                                text = str(ins)
                                if pat.search(text):
                                    result["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                            except Exception:
                                continue
                    # 3) Functions whose signature mentions the type
                    try:
                        sig_text = str(func.type)
                        if sig_text and tnl in sig_text.lower():
                            result["functions_with_type"].append(func.name)
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        # Deduplicate function list
        try:
            result["functions_with_type"] = sorted(list(set(result["functions_with_type"])))
        except Exception:
            pass

        return result

    def get_xrefs_to_enum(self, enum_name: str) -> dict[str, Any]:
        """Find usages of an enum by matching its member values in code and variables.

        Notes:
        - Enums are values, not addresses; there are no traditional "data references" to enums.
        - This scans for immediate constants equal to enum members and common bitmask checks.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        enum_name_str = str(enum_name).strip()
        en_lower = enum_name_str.lower()

        result: dict[str, Any] = {
            "enum": enum_name_str,
            "members": [],  # [{name, value}]
            "usages": [],  # [{function, address, text, member, value}]
        }

        # Locate the enum type. Direct name-lookup first (covers both
        # user-defined and DWARF-imported enums); fall back to a
        # substring scan over `bv.type_names` for partial matches.
        # `bv.types.values()` is intentionally NOT iterated — it only
        # carries types in the main container, missing anything BN's
        # auto-loaders (DWARF, libraries) registered.
        enum_type = None
        try:
            t = self._current_view.get_type_by_name(enum_name_str)
            if t is not None and getattr(t, "type_class", None) == TypeClass.EnumerationTypeClass:
                enum_type = t
        except Exception:
            pass

        if enum_type is None:
            try:
                for qname in getattr(self._current_view, "type_names", []) or []:
                    try:
                        name_str = str(qname)
                        if en_lower not in name_str.lower():
                            continue
                        t = self._current_view.get_type_by_name(qname)
                        if (
                            t is not None
                            and getattr(t, "type_class", None) == TypeClass.EnumerationTypeClass
                        ):
                            enum_type = t
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        members: list[dict[str, Any]] = []
        values: list[int] = []
        if enum_type is not None:
            try:
                for m in getattr(enum_type, "members", []):
                    try:
                        name = getattr(m, "name", None)
                        val = getattr(m, "value", None)
                        if name is not None and isinstance(val, int):
                            members.append({"name": name, "value": val})
                            values.append(val)
                    except Exception:
                        continue
            except Exception:
                pass

        result["members"] = members

        # Build simple patterns for HLIL text matching of constants (hex)
        import re

        # Build a single regex matching either the enumerator NAME
        # (which BN substitutes into HLIL for typed comparisons) or
        # the raw hex literal (HLIL leaves constants un-named when
        # the surrounding expression isn't typed). The capture group
        # tells us which alternative fired so we can fill `member`
        # and `value` correctly.
        alternatives: list[tuple[str, str, int]] = []  # (regex, member_name, value)
        for mem in members:
            alternatives.append((rf"\b{re.escape(mem['name'])}\b", mem["name"], mem["value"]))
            alternatives.append((rf"\b0x{mem['value']:x}\b", mem["name"], mem["value"]))
        combined = (
            re.compile("|".join(f"(?:{pat})" for pat, _, _ in alternatives), re.IGNORECASE)
            if alternatives
            else None
        )

        # Scan every function's HLIL for any of the patterns above.
        # Dedupe by (function, address, member) so a line that
        # contains both `PRIORITY_HIGH` and `0x7` doesn't double-count.
        seen: set[tuple[str, str, str | None]] = set()
        for func in list(self._current_view.functions):
            try:
                hlil = getattr(func, "hlil", None)
                if not hlil:
                    continue
                for ins in hlil.instructions:
                    try:
                        text = str(ins)
                        if combined is None or not combined.search(text):
                            continue
                        # Walk every member to see which one(s) actually
                        # appear in this instruction — multiple members
                        # can show up if the line compares against several.
                        for _, member_name, value in alternatives:
                            mem_pat = re.compile(
                                rf"\b({re.escape(member_name)}|0x{value:x})\b",
                                re.IGNORECASE,
                            )
                            if not mem_pat.search(text):
                                continue
                            addr_hex = hex(getattr(ins, "address", func.start))
                            key = (func.name, addr_hex, member_name)
                            if key in seen:
                                continue
                            seen.add(key)
                            result["usages"].append(
                                {
                                    "function": func.name,
                                    "address": addr_hex,
                                    "text": text,
                                    "member": member_name,
                                    "value": value,
                                }
                            )
                    except Exception:
                        continue
            except Exception:
                continue

        return result

    def get_xrefs_to_struct(self, struct_name: str) -> dict[str, Any]:
        """Get cross references/usages related specifically to a struct name.

        Includes:
        - members: list of struct members with offsets and types
        - data_instances: globals whose type mentions the struct
        - data_code_references: code refs to those globals
        - field_code_references: code refs to addresses of global_instance + member offset
        - code_references: HLIL lines with member access (".field"/"->field")
        - functions_with_type: functions whose signatures mention the struct
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        name = str(struct_name).strip()
        name_l = name.lower()
        # Build candidate names to handle common PE struct aliases
        candidate_names = {name}
        # Remove leading underscore variant
        if name.startswith("_"):
            candidate_names.add(name[1:])
        else:
            candidate_names.add("_" + name)
        # PE-specific heuristics
        nl = name_l
        if "coff" in nl and "header" in nl:
            candidate_names.update({"IMAGE_FILE_HEADER", "_IMAGE_FILE_HEADER"})
        if ("pe64" in nl or "optional_header64" in nl or "optional" in nl) and "header" in nl:
            candidate_names.update({"IMAGE_OPTIONAL_HEADER64", "_IMAGE_OPTIONAL_HEADER64"})
        if (
            "pe32" in nl or "optional_header32" in nl or ("optional" in nl and "64" not in nl)
        ) and "header" in nl:
            candidate_names.update({"IMAGE_OPTIONAL_HEADER32", "_IMAGE_OPTIONAL_HEADER32"})
        if "dos" in nl and "header" in nl:
            candidate_names.update({"IMAGE_DOS_HEADER", "_IMAGE_DOS_HEADER"})
        candidate_names_l = {c.lower() for c in candidate_names}

        out: dict[str, Any] = {
            "struct": name,
            "members": [],
            "data_instances": [],
            "data_code_references": [],
            "field_code_references": [],
            "code_references": [],
            "functions_with_type": [],
            "vars_with_type": [],
            "code_references_by_cast": [],
        }

        # Resolve the struct type and members
        members = []
        try:
            for t in self._current_view.types.values():
                try:
                    if getattr(t, "type_class", None) == TypeClass.StructureTypeClass:
                        tname = getattr(t, "name", None)
                        if not tname:
                            continue
                        tl = tname.lower()
                        if tl == name_l or name_l in tl or tl in candidate_names_l:
                            for m in getattr(
                                t, "members", getattr(getattr(t, "structure", None), "members", [])
                            ):
                                try:
                                    members.append(
                                        {
                                            "name": getattr(m, "name", None),
                                            "offset": int(getattr(m, "offset", 0))
                                            if hasattr(m, "offset")
                                            else None,
                                            "type": str(getattr(m, "type", ""))
                                            if hasattr(m, "type")
                                            else None,
                                        }
                                    )
                                except Exception:
                                    continue
                            break
                except Exception:
                    continue
        except Exception:
            pass
        out["members"] = members

        # Gather globals with this struct in their type string
        global_instances: list[int] = []
        try:
            for var_addr in list(self._current_view.data_vars):
                try:
                    t = None
                    if hasattr(self._current_view, "get_type_at"):
                        t = self._current_view.get_type_at(var_addr)
                    t_str = str(t) if t is not None else ""
                    if t_str:
                        tl = t_str.lower()
                        if name_l in tl or any(cn in tl for cn in candidate_names_l):
                            sym = self._current_view.get_symbol_at(var_addr)
                            out["data_instances"].append(
                                {
                                    "address": hex(var_addr),
                                    "type": t_str,
                                    "name": sym.name if sym else None,
                                }
                            )
                            global_instances.append(var_addr)
                            # Code refs to the variable itself
                        try:
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(var_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    out["data_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "target": hex(var_addr),
                                        }
                                    )
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass

        # Also gather symbol-based instances whose name mentions the struct alias
        symbol_instances: list[int] = []
        try:
            for sym in list(self._current_view.get_symbols()):
                try:
                    sname = getattr(sym, "name", "") or ""
                    sfull = getattr(sym, "full_name", "") or ""
                    sl = (sname + " " + sfull).lower()
                    if any(cn in sl for cn in candidate_names_l):
                        addr = getattr(sym, "address", None)
                        if isinstance(addr, int):
                            # capture as data instance if not already present
                            out["data_instances"].append(
                                {
                                    "address": hex(addr),
                                    "type": None,
                                    "name": sname,
                                }
                            )
                            symbol_instances.append(addr)
                            # code refs to this symbol
                            try:
                                if hasattr(self._current_view, "get_code_refs"):
                                    for ref in list(self._current_view.get_code_refs(addr)):
                                        fn_name = (
                                            ref.function.name
                                            if getattr(ref, "function", None)
                                            else None
                                        )
                                        out["data_code_references"].append(
                                            {
                                                "function": fn_name,
                                                "address": hex(ref.address),
                                                "target": hex(addr),
                                            }
                                        )
                            except Exception:
                                pass
                except Exception:
                    continue
        except Exception:
            pass

        # Code refs to computed field addresses for each global instance
        if members and (global_instances or symbol_instances):
            try:
                for base in list(set(global_instances + symbol_instances)):
                    for m in members:
                        try:
                            off = m.get("offset")
                            if off is None:
                                continue
                            field_addr = base + int(off)
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(field_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    out["field_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "field_address": hex(field_addr),
                                            "member": m.get("name"),
                                        }
                                    )
                        except Exception:
                            continue
            except Exception:
                pass

        # If the struct is contained as a field of another struct, try deriving field addresses from parent instances
        try:
            parent_offsets: list[dict[str, Any]] = []
            for t in self._current_view.types.values():
                try:
                    if getattr(t, "type_class", None) == TypeClass.StructureTypeClass:
                        tname = getattr(t, "name", None)
                        if not tname:
                            continue
                        tl = tname.lower()
                        # scan members for types that mention our struct aliases
                        for mem in getattr(
                            t, "members", getattr(getattr(t, "structure", None), "members", [])
                        ):
                            try:
                                mtype = getattr(mem, "type", None)
                                mtype_str = str(mtype) if mtype is not None else ""
                                ml = mtype_str.lower()
                                if ml and (
                                    name_l in ml or any(cn in ml for cn in candidate_names_l)
                                ):
                                    parent_offsets.append(
                                        {
                                            "parent": tname,
                                            "offset": int(getattr(mem, "offset", 0))
                                            if hasattr(mem, "offset")
                                            else None,
                                            "member": getattr(mem, "name", None),
                                        }
                                    )
                            except Exception:
                                continue
                except Exception:
                    continue

            # For each parent type, find instances and compute field address
            for po in parent_offsets:
                poff = po.get("offset")
                if poff is None:
                    continue
                parent_name = po.get("parent")
                try:
                    # scan data variables
                    for var_addr in list(self._current_view.data_vars):
                        try:
                            t = None
                            if hasattr(self._current_view, "get_type_at"):
                                t = self._current_view.get_type_at(var_addr)
                            t_str = str(t) if t is not None else ""
                            if t_str and parent_name and parent_name.lower() in t_str.lower():
                                field_addr = var_addr + poff
                                if hasattr(self._current_view, "get_code_refs"):
                                    for ref in list(self._current_view.get_code_refs(field_addr)):
                                        fn_name = (
                                            ref.function.name
                                            if getattr(ref, "function", None)
                                            else None
                                        )
                                        out["field_code_references"].append(
                                            {
                                                "function": fn_name,
                                                "address": hex(ref.address),
                                                "field_address": hex(field_addr),
                                                "member": po.get("member"),
                                            }
                                        )
                        except Exception:
                            continue
                    # scan symbols with parent type in name
                    for sym in list(self._current_view.get_symbols()):
                        try:
                            sname = getattr(sym, "name", "") or ""
                            sfull = getattr(sym, "full_name", "") or ""
                            sl = (sname + " " + sfull).lower()
                            if parent_name and parent_name.lower() in sl:
                                addr = getattr(sym, "address", None)
                                if isinstance(addr, int):
                                    field_addr = addr + poff
                                    if hasattr(self._current_view, "get_code_refs"):
                                        for ref in list(
                                            self._current_view.get_code_refs(field_addr)
                                        ):
                                            fn_name = (
                                                ref.function.name
                                                if getattr(ref, "function", None)
                                                else None
                                            )
                                            out["field_code_references"].append(
                                                {
                                                    "function": fn_name,
                                                    "address": hex(ref.address),
                                                    "field_address": hex(field_addr),
                                                    "member": po.get("member"),
                                                }
                                            )
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass

        # HLIL matches for member access text

        try:
            import re

            patterns = []
            for m in members:
                nm = m.get("name")
                if not nm:
                    continue
                patterns.append(
                    re.compile(rf"(\.|->)\s*{re.escape(str(nm))}(\b|\W)", re.IGNORECASE)
                )

            for func in list(self._current_view.functions):
                try:
                    # Capture variables whose type mentions the struct
                    try:
                        for v in getattr(func, "vars", []):
                            try:
                                vtype = getattr(v, "type", None)
                                vname = getattr(v, "name", None)
                                vtype_str = str(vtype) if vtype is not None else ""
                                if vtype_str and name_l in vtype_str.lower():
                                    out["vars_with_type"].append(
                                        {
                                            "function": func.name,
                                            "var": vname,
                                            "type": vtype_str,
                                        }
                                    )
                            except Exception:
                                continue
                    except Exception:
                        pass

                    if hasattr(func, "hlil") and func.hlil:
                        for ins in func.hlil.instructions:
                            try:
                                text = str(ins)
                                if any(p.search(text) for p in patterns):
                                    out["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                                # Also capture casts/annotations explicitly mentioning the struct name
                                tl = text.lower()
                                if name_l in tl or any(cn in tl for cn in candidate_names_l):
                                    # Heuristic: detect patterns like '(COFF_Header*)' or '(struct COFF_Header*)'
                                    cast_pat = (
                                        r"\(.*("
                                        + "|".join(re.escape(c) for c in candidate_names)
                                        + r").*\)"
                                    )
                                    if re.search(cast_pat, text, re.IGNORECASE):
                                        out["code_references_by_cast"].append(
                                            {
                                                "function": func.name,
                                                "address": hex(getattr(ins, "address", func.start)),
                                                "text": text,
                                            }
                                        )
                            except Exception:
                                continue
                    # Functions whose signature mentions the struct
                    try:
                        sig_text = str(func.type)
                        if sig_text:
                            sl = sig_text.lower()
                            if name_l in sl or any(cn in sl for cn in candidate_names_l):
                                out["functions_with_type"].append(func.name)
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        # Dedup functions list
        try:
            out["functions_with_type"] = sorted(list(set(out["functions_with_type"])))
        except Exception:
            pass

        return out

    def get_xrefs_to_union(self, union_name: str) -> dict[str, Any]:
        """Get cross references/usages related to a union type by name.

        Includes:
        - members: list of union members with offsets/types (offsets may be 0/overlapping)
        - data_instances: globals whose type mentions the union
        - data_code_references: code refs to those globals
        - code_references: HLIL lines with member access (".field"/"->field")
        - functions_with_type: functions whose signatures mention the union
        - vars_with_type: function-local variables typed as the union
        - code_references_by_cast: HLIL lines with explicit casts mentioning the union
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        name = str(union_name).strip()
        name_l = name.lower()

        out: dict[str, Any] = {
            "union": name,
            "members": [],
            "data_instances": [],
            "data_code_references": [],
            "code_references": [],
            "functions_with_type": [],
            "vars_with_type": [],
            "code_references_by_cast": [],
        }

        # Resolve union members
        members: list[dict[str, Any]] = []
        try:
            for t in self._current_view.types.values():
                try:
                    # Union types are presented via StructureTypeClass with UnionStructureType variant
                    if getattr(t, "type_class", None) == TypeClass.StructureTypeClass:
                        tname = getattr(t, "name", None)
                        if not tname:
                            continue
                        tl = tname.lower()
                        if tl == name_l or name_l in tl:
                            # If the BN type exposes a variant, prefer checking for union
                            try:
                                if getattr(t, "type", None) == StructureVariant.UnionStructureType:
                                    pass
                            except Exception:
                                pass
                            for m in getattr(
                                t, "members", getattr(getattr(t, "structure", None), "members", [])
                            ):
                                try:
                                    members.append(
                                        {
                                            "name": getattr(m, "name", None),
                                            "offset": int(getattr(m, "offset", 0))
                                            if hasattr(m, "offset")
                                            else None,
                                            "type": str(getattr(m, "type", ""))
                                            if hasattr(m, "type")
                                            else None,
                                        }
                                    )
                                except Exception:
                                    continue
                            break
                except Exception:
                    continue
        except Exception:
            pass
        out["members"] = members

        # Gather globals with this union in their type string
        try:
            for var_addr in list(self._current_view.data_vars):
                try:
                    t = None
                    if hasattr(self._current_view, "get_type_at"):
                        t = self._current_view.get_type_at(var_addr)
                    t_str = str(t) if t is not None else ""
                    if t_str and name_l in t_str.lower():
                        sym = self._current_view.get_symbol_at(var_addr)
                        out["data_instances"].append(
                            {
                                "address": hex(var_addr),
                                "type": t_str,
                                "name": sym.name if sym else None,
                            }
                        )
                        # Code refs to that variable
                        try:
                            if hasattr(self._current_view, "get_code_refs"):
                                for ref in list(self._current_view.get_code_refs(var_addr)):
                                    fn_name = (
                                        ref.function.name
                                        if getattr(ref, "function", None)
                                        else None
                                    )
                                    out["data_code_references"].append(
                                        {
                                            "function": fn_name,
                                            "address": hex(ref.address),
                                            "target": hex(var_addr),
                                        }
                                    )
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass

        # HLIL member access and casts; function variables/signatures
        try:
            import re

            patterns = []
            for m in members:
                nm = m.get("name")
                if not nm:
                    continue
                patterns.append(
                    re.compile(rf"(\.|->)\s*{re.escape(str(nm))}(\b|\W)", re.IGNORECASE)
                )

            for func in list(self._current_view.functions):
                try:
                    # variables typed as this union
                    try:
                        for v in getattr(func, "vars", []):
                            try:
                                vtype = getattr(v, "type", None)
                                vname = getattr(v, "name", None)
                                vtype_str = str(vtype) if vtype is not None else ""
                                if vtype_str and name_l in vtype_str.lower():
                                    out["vars_with_type"].append(
                                        {
                                            "function": func.name,
                                            "var": vname,
                                            "type": vtype_str,
                                        }
                                    )
                            except Exception:
                                continue
                    except Exception:
                        pass

                    if hasattr(func, "hlil") and func.hlil:
                        for ins in func.hlil.instructions:
                            try:
                                text = str(ins)
                                tl = text.lower()
                                matched_member = (
                                    any(p.search(text) for p in patterns) if patterns else False
                                )
                                if matched_member:
                                    out["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                                # Capture casts mentioning the union
                                cast_matched = False
                                if name_l in tl:
                                    if re.search(
                                        rf"\(.*{re.escape(name)}.*\)", text, re.IGNORECASE
                                    ):
                                        out["code_references_by_cast"].append(
                                            {
                                                "function": func.name,
                                                "address": hex(getattr(ins, "address", func.start)),
                                                "text": text,
                                            }
                                        )
                                        cast_matched = True
                                # Fallback: any HLIL mention of the union name counts as a code reference
                                if (not matched_member) and (not cast_matched) and (name_l in tl):
                                    out["code_references"].append(
                                        {
                                            "function": func.name,
                                            "address": hex(getattr(ins, "address", func.start)),
                                            "text": text,
                                        }
                                    )
                            except Exception:
                                continue
                    # function signature mentions
                    try:
                        sig_text = str(func.type)
                        if sig_text and name_l in sig_text.lower():
                            out["functions_with_type"].append(func.name)
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        # Dedup functions list
        try:
            out["functions_with_type"] = sorted(list(set(out["functions_with_type"])))
        except Exception:
            pass

        return out

    def define_user_symbol(self, address: int, name: str, kind: str = "data") -> dict[str, Any]:
        """Create a user symbol (label) at an address.

        Args:
            address: Target address as an integer.
            name: Symbol name. Whitespace is stripped; empty names are rejected.
            kind: "data" (default) or "function". Selects the underlying
                ``SymbolType`` (DataSymbol vs FunctionSymbol).

        Returns:
            Dict with status, address, the resolved name, and the kind used.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the name is empty, the kind is unknown, or BN
                refuses to apply the symbol.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Empty symbol name")

        kind_map: dict[str, Any] = {}
        symbol_type_enum = getattr(bn, "SymbolType", None)
        if symbol_type_enum is not None:
            data_type = getattr(symbol_type_enum, "DataSymbol", None)
            func_type = getattr(symbol_type_enum, "FunctionSymbol", None)
            if data_type is not None:
                kind_map["data"] = data_type
            if func_type is not None:
                kind_map["function"] = func_type
        if not kind_map:
            raise RuntimeError("SymbolType enum unavailable in this Binary Ninja version")

        norm_kind = (kind or "data").strip().lower()
        sym_type = kind_map.get(norm_kind)
        if sym_type is None:
            known = ", ".join(sorted(kind_map))
            raise ValueError(f"Unknown symbol kind {kind!r}. Use one of: {known}")

        try:
            symbol = bn.Symbol(sym_type, int(address), clean_name)
            self._current_view.define_user_symbol(symbol)
        except Exception as e:
            raise ValueError(f"Failed to define symbol: {e!s}")

        return {
            "status": "ok",
            "address": hex(int(address)),
            "name": clean_name,
            "kind": norm_kind,
        }

    def undefine_user_symbol(self, address: int) -> dict[str, Any]:
        """Remove the user symbol at an address.

        Returns:
            Dict with status, address, and the name of the symbol removed
            (when the BN API exposes it).

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If there is no symbol at the address, or BN refuses
                to undefine it (e.g. it is an auto-generated symbol).
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view

        addr = int(address)
        sym = None
        try:
            sym = bv.get_symbol_at(addr)
        except Exception:
            sym = None
        if sym is None:
            raise ValueError(f"No symbol found at {hex(addr)}")

        # BN happily accepts `undefine_user_symbol(auto_sym)` and either
        # silently no-ops or removes the auto symbol — either way the
        # caller's "ok / removed" response would mislead them. Refuse
        # the request and tell them what kind of symbol it is so they
        # know they have to leave it alone (or, for DWARF-imported
        # globals, rename instead via /renameData).
        if getattr(sym, "auto", False):
            raise ValueError(
                f"Symbol at {hex(addr)} ({getattr(sym, 'name', '?')!r}) is "
                "auto-generated, not user-defined; refusing to undefine. "
                "Use /renameData to relabel an auto symbol."
            )

        prior_name = getattr(sym, "name", None)
        try:
            bv.undefine_user_symbol(sym)
        except Exception as e:
            raise ValueError(f"Failed to undefine symbol: {e!s}")

        return {
            "status": "ok",
            "address": hex(addr),
            "removed": prior_name,
        }

    # ---------------- Function metadata ----------------
    def get_function_metadata(self, function_ident: str | int) -> dict[str, Any]:
        """Return a read-only bundle of diagnostic flags for a function.

        Useful when ``/decompile`` returns sparse or weird output and the
        agent wants to know *why* — e.g. the function is a thunk, BN
        skipped analysis, or it's variadic with no known prototype.

        Args:
            function_ident: Function name or address.

        Returns:
            Dict with the function name, start address, and BN's flags:
            ``is_thunk``, ``can_return``, ``has_variable_arguments``,
            ``is_pure``, ``analysis_skipped``, ``analysis_skip_reason``,
            ``analysis_skip_override``, ``auto``, and ``parameter_count``.
            Values that aren't exposed by the running BN version come back
            as ``None``.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function can't be found.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        func = self.get_function_by_name_or_address(function_ident)
        if func is None:
            raise ValueError(f"Function not found: {function_ident!r}")

        def _read_bool(name: str) -> bool | None:
            try:
                v = getattr(func, name, None)
                return bool(v) if v is not None else None
            except Exception:
                return None

        def _read_str(name: str) -> str | None:
            try:
                v = getattr(func, name, None)
                if v is None:
                    return None
                # Many BN enums stringify cleanly; ints just become "0", "1", ...
                return str(v)
            except Exception:
                return None

        parameter_count: int | None = None
        try:
            params = getattr(func, "parameter_vars", None)
            if params is not None:
                # parameter_vars may be a ParameterVariables wrapper that's
                # iterable rather than directly len()-able.
                try:
                    parameter_count = len(params)
                except TypeError:
                    parameter_count = sum(1 for _ in params)
        except Exception:
            parameter_count = None

        return {
            "function": getattr(func, "name", None),
            "address": hex(int(getattr(func, "start", 0))),
            "is_thunk": _read_bool("is_thunk"),
            "can_return": _read_bool("can_return"),
            "has_variable_arguments": _read_bool("has_variable_arguments"),
            "is_pure": _read_bool("is_pure"),
            "analysis_skipped": _read_bool("analysis_skipped"),
            "analysis_skip_reason": _read_str("analysis_skip_reason"),
            "analysis_skip_override": _read_str("analysis_skip_override"),
            "auto": _read_bool("auto"),
            "parameter_count": parameter_count,
        }

    def set_function_can_return(
        self, function_ident: str | int, can_return: bool
    ) -> dict[str, Any]:
        """Override BN's no-return inference for a function.

        Use this when BN thinks a function returns but it actually
        terminates the process (custom abort/panic wrappers), or vice
        versa. BN uses can_return to propagate flow into callers, so
        getting this wrong corrupts the CFG of every caller.

        Args:
            function_ident: Function name or address.
            can_return: True if the function returns; False to mark it
                as never-returning.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function can't be found or BN refuses.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        func = self.get_function_by_name_or_address(function_ident)
        if func is None:
            raise ValueError(f"Function not found: {function_ident!r}")

        value = bool(can_return)
        setter = getattr(func, "set_user_can_return", None)
        try:
            if callable(setter):
                setter(value)
            else:
                # Fallback: property setter
                try:
                    func.can_return = value
                except Exception as e:
                    raise ValueError(
                        f"Function.set_user_can_return / can_return setter unavailable: {e!s}"
                    )
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Failed to set can_return: {e!s}")

        # BN caches function metadata; without a synchronous
        # reanalyze, /getFunctionMetadata keeps returning the
        # pre-mutation value until something else triggers analysis.
        # `reanalyze` alone queues background work — pair it with
        # `update_analysis_and_wait` so the agent sees the new value
        # the moment this call returns.
        try:
            func.reanalyze(bn.FunctionUpdateType.UserFunctionUpdate)
            self._current_view.update_analysis_and_wait()
        except Exception:
            pass

        return {
            "status": "ok",
            "function": getattr(func, "name", None),
            "address": hex(int(getattr(func, "start", 0))),
            "can_return": value,
        }

    def set_function_return_type(self, function_ident: str | int, type_str: str) -> dict[str, Any]:
        """Set a function's return type without rewriting the full prototype.

        Args:
            function_ident: Function name or address.
            type_str: C-style type string (e.g. "int", "void *",
                "struct Foo *"). Parsed via
                ``BinaryView.parse_type_string``.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function can't be found, the type string
                fails to parse, or BN refuses to apply it.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_type = (type_str or "").strip()
        if not clean_type:
            raise ValueError("Empty type string")
        bv = self._current_view
        func = self.get_function_by_name_or_address(function_ident)
        if func is None:
            raise ValueError(f"Function not found: {function_ident!r}")

        parsed_type = None
        try:
            parsed_type, _ = bv.parse_type_string(clean_type)
        except Exception as e:
            raise ValueError(f"Failed to parse type {clean_type!r}: {e!s}")
        if parsed_type is None:
            raise ValueError(f"Type {clean_type!r} parsed to None")

        setter = getattr(func, "set_user_return_type", None)
        try:
            if callable(setter):
                setter(parsed_type)
            else:
                try:
                    func.return_type = parsed_type
                except Exception as e:
                    raise ValueError(
                        f"Function.set_user_return_type / return_type setter unavailable: {e!s}"
                    )
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Failed to apply return type: {e!s}")

        # See `set_function_can_return` — synchronous reanalysis so
        # the metadata cache reflects the new type immediately.
        try:
            func.reanalyze(bn.FunctionUpdateType.UserFunctionUpdate)
            self._current_view.update_analysis_and_wait()
        except Exception:
            pass

        return {
            "status": "ok",
            "function": getattr(func, "name", None),
            "address": hex(int(getattr(func, "start", 0))),
            "return_type": str(parsed_type),
        }

    def set_function_inline(self, function_ident: str | int, inline: bool) -> dict[str, Any]:
        """Force or un-force BN's inline-during-analysis behavior.

        Useful for small helper functions where inlining cleans up
        decompilation, or for un-inlining when BN's heuristic made the
        wrong call.

        Args:
            function_ident: Function name or address.
            inline: True to force inlining; False to disable.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function can't be found or BN refuses.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        func = self.get_function_by_name_or_address(function_ident)
        if func is None:
            raise ValueError(f"Function not found: {function_ident!r}")

        value = bool(inline)
        try:
            func.inline_during_analysis = value
        except Exception as e:
            raise ValueError(f"Failed to set inline_during_analysis: {e!s}")

        # See `set_function_can_return` — synchronous reanalysis so
        # the metadata cache reflects the change immediately.
        try:
            func.reanalyze(bn.FunctionUpdateType.UserFunctionUpdate)
            self._current_view.update_analysis_and_wait()
        except Exception:
            pass

        return {
            "status": "ok",
            "function": getattr(func, "name", None),
            "address": hex(int(getattr(func, "start", 0))),
            "inline_during_analysis": value,
        }

    # ---------------- Per-instruction data flow ----------------
    def _resolve_func_and_addr(self, function_ident: str | int, address: int) -> tuple[Any, int]:
        """Resolve (function, int address) for per-instruction queries."""
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        func = self.get_function_by_name_or_address(function_ident)
        if func is None:
            raise ValueError(f"Function not found: {function_ident!r}")
        return func, int(address)

    @staticmethod
    def _reg_name(reg: Any) -> str:
        """Extract a register name from whatever BN returns."""
        if isinstance(reg, str):
            return reg
        name = getattr(reg, "name", None)
        if isinstance(name, str):
            return name
        return str(reg)

    def get_constants_referenced_by(
        self, function_ident: str | int, address: int
    ) -> dict[str, Any]:
        """Return immediate constants referenced by the instruction at an address.

        Backed by ``Function.get_constants_referenced_by(addr)``. Each
        returned ``ConstantReference`` carries the integer value, byte
        size, and BN's ``pointer`` / ``intermediate`` flags.

        Args:
            function_ident: Function name or address.
            address: Instruction address inside that function.

        Returns:
            Dict with function, address, count, and a ``constants`` list
            of ``{value, size, pointer, intermediate}`` entries.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function can't be found or the BN call fails.
        """
        func, addr = self._resolve_func_and_addr(function_ident, address)
        getter = getattr(func, "get_constants_referenced_by", None)
        if not callable(getter):
            raise RuntimeError(
                "Function.get_constants_referenced_by is unavailable in this BN version"
            )
        try:
            raw = list(getter(addr) or [])
        except Exception as e:
            raise ValueError(f"Failed to get constants: {e!s}")

        constants: list[dict[str, Any]] = []
        for c in raw:
            try:
                value = getattr(c, "value", None)
                size = getattr(c, "size", None)
                pointer = getattr(c, "pointer", None)
                intermediate = getattr(c, "intermediate", None)
                constants.append(
                    {
                        "value": hex(int(value)) if value is not None else None,
                        "size": int(size) if size is not None else None,
                        "pointer": bool(pointer) if pointer is not None else None,
                        "intermediate": (bool(intermediate) if intermediate is not None else None),
                    }
                )
            except Exception:
                continue

        return {
            "function": getattr(func, "name", None),
            "function_address": hex(int(getattr(func, "start", 0))),
            "address": hex(addr),
            "count": len(constants),
            "constants": constants,
        }

    def get_regs_read_by(self, function_ident: str | int, address: int) -> dict[str, Any]:
        """Return register names read by the instruction at an address.

        Backed by ``Function.get_regs_read_by(addr)``.
        """
        func, addr = self._resolve_func_and_addr(function_ident, address)
        getter = getattr(func, "get_regs_read_by", None)
        if not callable(getter):
            raise RuntimeError("Function.get_regs_read_by is unavailable in this BN version")
        try:
            raw = list(getter(addr) or [])
        except Exception as e:
            raise ValueError(f"Failed to get regs read: {e!s}")
        names = [self._reg_name(r) for r in raw]
        return {
            "function": getattr(func, "name", None),
            "function_address": hex(int(getattr(func, "start", 0))),
            "address": hex(addr),
            "count": len(names),
            "registers": names,
        }

    def get_regs_written_by(self, function_ident: str | int, address: int) -> dict[str, Any]:
        """Return register names written by the instruction at an address.

        Backed by ``Function.get_regs_written_by(addr)``.
        """
        func, addr = self._resolve_func_and_addr(function_ident, address)
        getter = getattr(func, "get_regs_written_by", None)
        if not callable(getter):
            raise RuntimeError("Function.get_regs_written_by is unavailable in this BN version")
        try:
            raw = list(getter(addr) or [])
        except Exception as e:
            raise ValueError(f"Failed to get regs written: {e!s}")
        names = [self._reg_name(r) for r in raw]
        return {
            "function": getattr(func, "name", None),
            "function_address": hex(int(getattr(func, "start", 0))),
            "address": hex(addr),
            "count": len(names),
            "registers": names,
        }

    # ---------------- Typed symbol queries ----------------
    _SYMBOL_TYPE_ALIASES: ClassVar[dict[str, str]] = {
        "function": "FunctionSymbol",
        "data": "DataSymbol",
        "import": "ImportedFunctionSymbol",
        "import_function": "ImportedFunctionSymbol",
        "imported_function": "ImportedFunctionSymbol",
        "import_data": "ImportedDataSymbol",
        "imported_data": "ImportedDataSymbol",
        "import_address": "ImportAddressSymbol",
        "external": "ExternalSymbol",
        "library_function": "LibraryFunctionSymbol",
        "symbolic_function": "SymbolicFunctionSymbol",
        "label": "LocalLabelSymbol",
        "local_label": "LocalLabelSymbol",
    }

    def get_symbols_by_type(
        self,
        symbol_type: str,
        start: int | None = None,
        end: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List symbols of a given type, optionally bounded by address range.

        Args:
            symbol_type: One of the agent-friendly aliases (``function``,
                ``data``, ``import``, ``import_data``, ``import_address``,
                ``external``, ``library_function``, ``symbolic_function``,
                ``label``) or a raw BN ``SymbolType`` enum name
                (e.g. ``"FunctionSymbol"``).
            start: Optional inclusive starting address. Defaults to view start.
            end: Optional exclusive ending address. Defaults to view end.
            limit: Cap on results. 0 or negative means "no cap".

        Returns:
            Dict with the resolved BN type name, range, count, and a
            ``symbols`` list of ``{address, name, raw_name, full_name, type}``.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the symbol-type alias is unknown.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view

        clean_kind = (symbol_type or "").strip()
        if not clean_kind:
            raise ValueError("Empty symbol_type")

        resolved_name = self._SYMBOL_TYPE_ALIASES.get(clean_kind.lower(), clean_kind)
        sym_type_enum = getattr(bn, "SymbolType", None)
        if sym_type_enum is None:
            raise RuntimeError("SymbolType enum unavailable in this BN version")
        sym_type = getattr(sym_type_enum, resolved_name, None)
        if sym_type is None:
            known = ", ".join(sorted(set(self._SYMBOL_TYPE_ALIASES.values())))
            raise ValueError(
                f"Unknown symbol type {symbol_type!r}. Aliases: "
                f"{', '.join(sorted(self._SYMBOL_TYPE_ALIASES))}; "
                f"raw enum names also accepted ({known})."
            )

        getter = getattr(bv, "get_symbols_of_type", None)
        if not callable(getter):
            raise RuntimeError("BinaryView.get_symbols_of_type is unavailable in this BN version")
        try:
            raw_symbols = list(getter(sym_type) or [])
        except Exception as e:
            raise RuntimeError(f"get_symbols_of_type failed: {e!s}")

        if start is None:
            start = int(getattr(bv, "start", 0))
        view_end_attr = getattr(bv, "end", None)
        view_end = int(view_end_attr) if view_end_attr is not None else None
        if end is None:
            end = view_end

        symbols: list[dict[str, Any]] = []
        for sym in raw_symbols:
            try:
                addr_attr = getattr(sym, "address", None)
                if addr_attr is None:
                    continue
                addr = int(addr_attr)
                if start is not None and addr < int(start):
                    continue
                if end is not None and addr >= int(end):
                    continue
                symbols.append(
                    {
                        "address": hex(addr),
                        "name": getattr(sym, "name", None),
                        "raw_name": getattr(sym, "raw_name", None),
                        "full_name": getattr(sym, "full_name", None),
                        "type": resolved_name,
                    }
                )
                if 0 < limit <= len(symbols):
                    break
            except Exception:
                continue

        return {
            "type": resolved_name,
            "start": hex(int(start)) if start is not None else None,
            "end": hex(int(end)) if end is not None else None,
            "count": len(symbols),
            "limit": limit,
            "symbols": symbols,
        }

    # ---------------- SSA data flow ----------------
    def _serialize_il_instr(self, instr: Any, kind: str) -> dict[str, Any]:
        """Render a HLIL/MLIL instruction as a JSON-friendly dict."""
        try:
            addr_attr = getattr(instr, "address", None)
            return {
                "address": hex(int(addr_attr)) if addr_attr is not None else None,
                "il_type": str(getattr(instr, "il_type", None)) or None,
                "expression": str(instr),
                "kind": kind,
            }
        except Exception:
            return {"kind": kind, "raw": str(instr)}

    def _il_function(self, func: Any, il_level: str) -> Any | None:
        norm = (il_level or "hlil").strip().lower()
        if norm == "mlil":
            return getattr(func, "mlil", None)
        return getattr(func, "hlil", None)

    def get_ssa_var_uses(
        self,
        function_ident: str | int,
        var_name: str,
        version: int = 0,
        il_level: str = "hlil",
    ) -> dict[str, Any]:
        """Return SSA-precise use sites of a variable inside a function.

        Args:
            function_ident: Function name or address.
            var_name: Local variable name.
            version: SSA version of the variable. Default 0 (the first
                definition's outgoing value).
            il_level: ``"hlil"`` (default) or ``"mlil"``.

        Returns:
            Dict with function context, variable, SSA version, IL level,
            count, and a ``uses`` list of ``{address, il_type, expression, kind}``.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function/variable can't be found, the IL
                function isn't available, or BN refuses the call.
        """
        func, var = self._resolve_func_and_var(function_ident, var_name)
        il_func = self._il_function(func, il_level)
        if il_func is None:
            raise ValueError(
                f"IL function ({il_level}) unavailable for "
                f"{getattr(func, 'name', '?')} — analysis may not have completed"
            )
        ssa_ctor = getattr(bn, "SSAVariable", None)
        if ssa_ctor is None:
            raise RuntimeError("SSAVariable is unavailable in this BN version")
        try:
            ssa_var = ssa_ctor(var, int(version))
        except Exception as e:
            raise ValueError(f"Failed to construct SSAVariable for {var_name!r} v{version}: {e!s}")

        getter = getattr(il_func, "get_ssa_var_uses", None)
        if not callable(getter):
            raise RuntimeError(
                f"{il_level.upper()}.get_ssa_var_uses is unavailable in this BN version"
            )
        try:
            raw = list(getter(ssa_var) or [])
        except Exception as e:
            raise ValueError(f"Failed to get SSA uses: {e!s}")
        uses = [self._serialize_il_instr(i, kind="use") for i in raw]
        return {
            "function": getattr(func, "name", None),
            "function_address": hex(int(getattr(func, "start", 0))),
            "variable": (var_name or "").strip(),
            "version": int(version),
            "il_level": il_level,
            "count": len(uses),
            "uses": uses,
        }

    def get_ssa_var_definition(
        self,
        function_ident: str | int,
        var_name: str,
        version: int = 0,
        il_level: str = "hlil",
    ) -> dict[str, Any]:
        """Return the SSA definition site of a variable inside a function.

        SSA semantics guarantee at most one definition per (variable,
        version), so the response carries a single ``definition`` field
        rather than a list.
        """
        func, var = self._resolve_func_and_var(function_ident, var_name)
        il_func = self._il_function(func, il_level)
        if il_func is None:
            raise ValueError(
                f"IL function ({il_level}) unavailable for "
                f"{getattr(func, 'name', '?')} — analysis may not have completed"
            )
        ssa_ctor = getattr(bn, "SSAVariable", None)
        if ssa_ctor is None:
            raise RuntimeError("SSAVariable is unavailable in this BN version")
        try:
            ssa_var = ssa_ctor(var, int(version))
        except Exception as e:
            raise ValueError(f"Failed to construct SSAVariable for {var_name!r} v{version}: {e!s}")

        getter = getattr(il_func, "get_ssa_var_definition", None)
        if not callable(getter):
            raise RuntimeError(
                f"{il_level.upper()}.get_ssa_var_definition is unavailable in this BN version"
            )
        try:
            raw = getter(ssa_var)
        except Exception as e:
            raise ValueError(f"Failed to get SSA definition: {e!s}")
        definition = self._serialize_il_instr(raw, kind="definition") if raw is not None else None
        return {
            "function": getattr(func, "name", None),
            "function_address": hex(int(getattr(func, "start", 0))),
            "variable": (var_name or "").strip(),
            "version": int(version),
            "il_level": il_level,
            "definition": definition,
        }

    # ---------------- Variable data flow ----------------
    def _serialize_var_refs(
        self,
        func: Any,
        refs: list[Any],
        il_level: str = "all",
    ) -> list[dict[str, Any]]:
        """Convert a list of ILReferenceSource-like objects into JSON dicts.

        Filters by ``il_level`` when it is one of "hlil", "mlil", "llil"
        (case-insensitive substring match against BN's il_type string).
        Dedupes by ``(address, il_type)`` so a use that BN reports at
        multiple IL levels isn't double-counted at the same level.
        Attaches the HLIL line at each address as a best-effort snippet.
        """
        out: list[dict[str, Any]] = []
        seen: set[tuple[int, str | None]] = set()
        wanted = (il_level or "all").strip().lower()
        for ref in refs:
            try:
                ref_il_type = getattr(ref, "il_type", None)
                il_str = str(ref_il_type) if ref_il_type is not None else None
                if wanted != "all" and il_str and wanted not in il_str.lower():
                    continue
                addr = getattr(ref, "addr", None)
                if addr is None:
                    addr = getattr(ref, "address", None)
                if addr is None:
                    continue
                addr_int = int(addr)
                key = (addr_int, il_str)
                if key in seen:
                    continue
                seen.add(key)
                snippet: str | None = None
                try:
                    hlil = getattr(func, "hlil", None)
                    if hlil is not None:
                        # One machine address can map to several IL
                        # instructions; the first one at that address
                        # is the best textual representative.
                        start_getter = getattr(hlil, "get_instruction_start", None)
                        if callable(start_getter):
                            try:
                                idx = start_getter(addr_int)
                            except Exception:
                                idx = None
                            if idx is not None and idx >= 0:
                                try:
                                    snippet = str(hlil[int(idx)])
                                except (IndexError, KeyError, ValueError):
                                    snippet = None
                except Exception:
                    snippet = None
                out.append(
                    {
                        "address": hex(addr_int),
                        "il_type": il_str,
                        "hlil": snippet,
                    }
                )
            except Exception:
                continue
        return out

    def _resolve_func_and_var(self, function_ident: str | int, var_name: str) -> tuple[Any, Any]:
        """Resolve (function, variable) or raise ValueError with a clear msg."""
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_var = (var_name or "").strip()
        if not clean_var:
            raise ValueError("Empty variable name")
        func = self.get_function_by_name_or_address(function_ident)
        if func is None:
            raise ValueError(f"Function not found: {function_ident!r}")
        var = None
        try:
            getter = getattr(func, "get_variable_by_name", None)
            if callable(getter):
                var = getter(clean_var)
        except Exception:
            var = None
        if var is None:
            raise ValueError(f"Variable {clean_var!r} not found in {func.name}")
        return func, var

    @staticmethod
    def _collect_var_refs(
        func: Any,
        var: Any,
        il_level: str,
        method_name: str,
    ) -> list[Any]:
        """Call ``method_name`` (``get_var_uses`` or ``get_var_definitions``)
        on the HLIL and/or MLIL function objects per the ``il_level``
        filter, wrapping each returned IL instruction in a tiny adapter
        so :meth:`_serialize_var_refs` sees the same `(addr, il_type)`
        shape it gets from ``ILReferenceSource`` objects.

        BN exposes these methods on the *IL* function classes, not on
        ``Function`` itself, and LLIL has no concept of named variables
        — so ``il_level="llil"`` is rejected outright.
        """

        class _Ref:
            __slots__ = ("addr", "address", "il_type")

            def __init__(self, addr: int, il_type: str) -> None:
                self.addr = addr
                self.address = addr
                self.il_type = il_type

        wanted = (il_level or "all").strip().lower()
        if wanted == "llil":
            raise ValueError(
                "LLIL does not track named variables; use il_level='hlil', 'mlil', or 'all'"
            )
        if wanted not in ("hlil", "mlil", "all"):
            raise ValueError(f"Unsupported il_level {il_level!r}; use 'hlil', 'mlil', or 'all'")

        out: list[Any] = []
        for level in ("hlil", "mlil"):
            if wanted not in (level, "all"):
                continue
            il_func = getattr(func, level, None)
            if il_func is None:
                continue
            getter = getattr(il_func, method_name, None)
            if not callable(getter):
                continue
            try:
                for instr in getter(var) or []:
                    addr = getattr(instr, "address", None)
                    if addr is None:
                        continue
                    out.append(_Ref(int(addr), level))
            except Exception as e:
                raise ValueError(f"{level.upper()} {method_name} failed: {e!s}") from e
        return out

    def get_var_uses(
        self,
        function_ident: str | int,
        var_name: str,
        il_level: str = "all",
    ) -> dict[str, Any]:
        """Return all use sites of a local variable inside a function.

        Args:
            function_ident: Function name or address.
            var_name: Local variable name (as shown by
                ``get_stack_frame_vars`` or in the decompilation).
            il_level: Filter by IL level — "all" (default), "hlil",
                or "mlil". Case-insensitive. ``"llil"`` is rejected
                because Low Level IL does not model named variables.

        Returns:
            Dict with function, function_address, variable, il_level,
            count, and a ``uses`` list of ``{address, il_type, hlil}``
            entries.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function or variable can't be found, the
                ``il_level`` is unsupported, or the BN call fails.
        """
        func, var = self._resolve_func_and_var(function_ident, var_name)
        refs = self._collect_var_refs(func, var, il_level, "get_var_uses")
        uses = self._serialize_var_refs(func, refs, il_level)
        return {
            "function": getattr(func, "name", None),
            "function_address": hex(int(getattr(func, "start", 0))),
            "variable": (var_name or "").strip(),
            "il_level": il_level,
            "count": len(uses),
            "uses": uses,
        }

    def get_var_definitions(
        self,
        function_ident: str | int,
        var_name: str,
        il_level: str = "all",
    ) -> dict[str, Any]:
        """Return all definition sites of a local variable inside a function.

        Same shape as :meth:`get_var_uses` but the result list is keyed
        ``definitions`` instead of ``uses``.
        """
        func, var = self._resolve_func_and_var(function_ident, var_name)
        refs = self._collect_var_refs(func, var, il_level, "get_var_definitions")
        defs = self._serialize_var_refs(func, refs, il_level)
        return {
            "function": getattr(func, "name", None),
            "function_address": hex(int(getattr(func, "start", 0))),
            "variable": (var_name or "").strip(),
            "il_level": il_level,
            "count": len(defs),
            "definitions": defs,
        }

    def get_parameter_at(
        self,
        address: int,
        index: int,
        function_ident: str | int | None = None,
    ) -> dict[str, Any]:
        """Resolve the i-th argument at a callsite as an MLIL expression.

        Backed by the MLIL call instruction's ``params`` attribute — gives
        the lifted expression that's actually being passed (e.g. ``var_18``
        or ``"hello"`` rather than a raw register name).

        Args:
            address: Call instruction address.
            index: Zero-based parameter index.
            function_ident: Optional containing-function identifier.
                Auto-resolved when omitted.

        Returns:
            Dict with status, function, callsite address, index, the
            best-effort callee name, and the MLIL expression as a string.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the function can't be found, no call exists at
                the address, or the parameter index is out of range.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        addr = int(address)
        idx = int(index)
        if idx < 0:
            raise ValueError(f"Parameter index must be non-negative, got {idx}")

        # Resolve containing function (auto when not supplied).
        if function_ident in (None, ""):
            try:
                container_getter = getattr(bv, "get_functions_containing", None)
                fns = list(container_getter(addr) or []) if callable(container_getter) else []
            except Exception:
                fns = []
            if not fns:
                raise ValueError(f"No function contains {hex(addr)}; pass function explicitly")
            func = fns[0]
        else:
            func = self.get_function_by_name_or_address(function_ident)
            if func is None:
                raise ValueError(f"Function not found: {function_ident!r}")

        # Walk MLIL instructions at the address and pick the first call.
        mlil = getattr(func, "mlil", None)
        if mlil is None:
            raise ValueError(
                f"MLIL unavailable for {getattr(func, 'name', '?')} — "
                "analysis may not have completed"
            )
        # One machine instruction can lower to several MLIL instructions,
        # so we walk forward from the first IL instruction at this address
        # until the address changes. `get_instruction_start` gives us that
        # starting index; if it isn't available we fall back to scanning
        # every instruction in the function (slower but always correct).
        call_instr = None
        try:
            start_idx = None
            start_getter = getattr(mlil, "get_instruction_start", None)
            if callable(start_getter):
                try:
                    start_idx = start_getter(addr)
                except Exception:
                    start_idx = None

            if start_idx is not None and start_idx >= 0:
                i = int(start_idx)
                while True:
                    try:
                        instr = mlil[i]
                    except (IndexError, KeyError, ValueError):
                        break
                    if instr is None or getattr(instr, "address", None) != addr:
                        break
                    op_name = str(getattr(instr, "operation", "") or "")
                    if "CALL" in op_name.upper():
                        call_instr = instr
                        break
                    i += 1
            else:
                for instr in mlil.instructions:
                    if getattr(instr, "address", None) != addr:
                        continue
                    op_name = str(getattr(instr, "operation", "") or "")
                    if "CALL" in op_name.upper():
                        call_instr = instr
                        break
        except Exception as e:
            raise ValueError(f"Failed to enumerate MLIL at {hex(addr)}: {e!s}") from e

        if call_instr is None:
            raise ValueError(f"No call instruction at {hex(addr)} in {getattr(func, 'name', '?')}")

        params = getattr(call_instr, "params", None)
        if params is None:
            raise ValueError(f"Call at {hex(addr)} has no params attribute on its MLIL instruction")
        try:
            params_list = list(params)
        except Exception:
            params_list = []

        if not 0 <= idx < len(params_list):
            raise ValueError(
                f"Parameter index {idx} out of range; call at {hex(addr)} has {len(params_list)} args"
            )

        expr_str = str(params_list[idx])

        # Best-effort callee name resolution from the call's dest.
        callee_name: str | None = None
        try:
            dest = getattr(call_instr, "dest", None)
            if dest is not None:
                dest_val = getattr(dest, "constant", None)
                if dest_val is None:
                    dest_val = getattr(dest, "value", None)
                if dest_val is not None:
                    try:
                        callee_addr = int(dest_val)
                    except Exception:
                        callee_addr = None
                    if callee_addr is not None:
                        try:
                            callee_func = bv.get_function_at(callee_addr)
                            if callee_func is not None:
                                callee_name = getattr(callee_func, "name", None)
                        except Exception:
                            pass
                        if callee_name is None:
                            try:
                                sym = bv.get_symbol_at(callee_addr)
                                if sym is not None:
                                    callee_name = getattr(sym, "name", None)
                            except Exception:
                                pass
        except Exception:
            callee_name = None

        return {
            "status": "ok",
            "function": getattr(func, "name", None),
            "address": hex(addr),
            "index": idx,
            "callee": callee_name,
            "param_count": len(params_list),
            "expression": expr_str,
        }

    # ---------------- Tags ----------------
    def _serialize_tag(self, tag: Any, kind: str, addr: int | None = None) -> dict[str, Any]:
        """Render a Tag into a JSON-friendly dict.

        BN's `Tag` class only carries `type`, `data`, and `id`; the
        address is implicit in how the caller looked it up, so it
        must be passed in (or left None for function-scope tags
        which apply to the whole function).
        """
        try:
            type_obj = getattr(tag, "type", None)
            type_name = getattr(type_obj, "name", None) if type_obj else None
            icon = getattr(type_obj, "icon", None) if type_obj else None
            data = getattr(tag, "data", None)
            return {
                "type": type_name,
                "icon": icon,
                "data": data,
                "kind": kind,
                "address": hex(int(addr)) if addr is not None else None,
            }
        except Exception:
            return {"kind": kind, "raw": str(tag)}

    def list_tag_types(self) -> list[dict[str, Any]]:
        """Return all tag types known to the current view.

        Returns:
            List of ``{"name", "icon", "visible"}`` dicts. The list includes
            BN's built-in tag types (Important, Bug, Bookmark, etc.) alongside
            anything created by the user.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        out: list[dict[str, Any]] = []
        try:
            tag_types = getattr(bv, "tag_types", None) or {}
            # tag_types is typically a dict {name: TagType}; iterate values.
            iterable = tag_types.values() if hasattr(tag_types, "values") else tag_types
            for tt in iterable:
                try:
                    out.append(
                        {
                            "name": getattr(tt, "name", None),
                            "icon": getattr(tt, "icon", None),
                            "visible": getattr(tt, "visible", True),
                        }
                    )
                except Exception:
                    continue
        except Exception as e:
            bn.log_warn(f"list_tag_types fallback: {e}")
        return out

    def create_tag_type(self, name: str, icon: str = "🏷") -> dict[str, Any]:
        """Create a tag type, or return the existing one with that name.

        Args:
            name: Tag-type name (e.g. "Crypto", "Syscall", "TODO").
            icon: Short string used as the BN icon (typically an emoji).

        Returns:
            Dict with status, the resolved name, icon, and a ``created`` flag
            indicating whether a new type was created (False if it already
            existed).

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the name is empty or BN refuses to create the type.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Empty tag-type name")
        bv = self._current_view

        existing = None
        try:
            tag_types = getattr(bv, "tag_types", None) or {}
            if hasattr(tag_types, "get"):
                existing = tag_types.get(clean_name)
            elif hasattr(tag_types, "__getitem__"):
                try:
                    existing = tag_types[clean_name]
                except KeyError:
                    existing = None
        except Exception:
            existing = None

        if existing is not None:
            return {
                "status": "ok",
                "name": clean_name,
                "icon": getattr(existing, "icon", None),
                "created": False,
            }

        try:
            new_type = bv.create_tag_type(clean_name, icon or "🏷")
        except Exception as e:
            raise ValueError(f"Failed to create tag type {clean_name!r}: {e!s}")

        return {
            "status": "ok",
            "name": clean_name,
            "icon": getattr(new_type, "icon", icon),
            "created": True,
        }

    def _resolve_tag_type(self, name: str, auto_create: bool = True):
        """Look up a tag type by name; optionally create it on the fly."""
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        tag_types = getattr(bv, "tag_types", None) or {}
        tt = None
        try:
            if hasattr(tag_types, "get"):
                tt = tag_types.get(name)
            elif hasattr(tag_types, "__getitem__"):
                try:
                    tt = tag_types[name]
                except KeyError:
                    tt = None
        except Exception:
            tt = None
        if tt is None and auto_create:
            try:
                tt = bv.create_tag_type(name, "🏷")
            except Exception as e:
                raise ValueError(f"Failed to auto-create tag type {name!r}: {e!s}")
        if tt is None:
            raise ValueError(f"Tag type {name!r} not found")
        return tt

    def add_tag(
        self,
        address: int,
        tag_type: str,
        data: str = "",
        kind: str = "auto",
    ) -> dict[str, Any]:
        """Attach a tag to an address, function, or data location.

        Args:
            address: Target address.
            tag_type: Tag-type name. Auto-created (with the default icon) if it
                doesn't already exist.
            data: Optional payload string (description, context, etc.).
            kind: One of ``"auto"`` (default), ``"address"``, ``"function"``,
                or ``"data"``. When ``"auto"``, the server picks:

                - ``"function"`` if the address is the start of a function,
                - ``"address"`` if the address is inside a function body,
                - ``"data"`` otherwise.

        Returns:
            Dict with status, the resolved kind, address, tag-type name, and
            the data payload that was stored.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the kind is unknown or BN refuses to attach the tag.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        addr = int(address)
        payload = data or ""
        norm_kind = (kind or "auto").strip().lower()
        if norm_kind not in ("auto", "address", "function", "data"):
            raise ValueError(f"Unknown tag kind {kind!r}. Use auto, address, function, or data.")

        # Locate any function that contains this address.
        containing_funcs: list[Any] = []
        try:
            getter = getattr(bv, "get_functions_containing", None)
            containing_funcs = list(getter(addr) or []) if callable(getter) else []
        except Exception:
            containing_funcs = []

        if norm_kind == "auto":
            if containing_funcs:
                func = containing_funcs[0]
                if int(getattr(func, "start", addr + 1)) == addr:
                    norm_kind = "function"
                else:
                    norm_kind = "address"
            else:
                norm_kind = "data"

        # `_resolve_tag_type` auto-creates the tag type if missing; we
        # only need its name string for BN's `add_tag` APIs.
        tt = self._resolve_tag_type(tag_type, auto_create=True)
        tt_name = getattr(tt, "name", tag_type)

        try:
            if norm_kind == "data":
                # `BinaryView.add_tag(addr, tag_type_name, data, user=True)`
                bv.add_tag(addr, tt_name, payload, user=True)
            elif norm_kind == "function":
                if not containing_funcs:
                    raise ValueError(f"No function at {hex(addr)} for kind='function'")
                func = containing_funcs[0]
                # `Function.add_tag(tag_type, data)` — omit `addr` to
                # tag the whole function rather than a specific insn.
                func.add_tag(tt_name, payload)
            else:  # "address"
                if not containing_funcs:
                    raise ValueError(
                        f"Address {hex(addr)} is not inside any function; "
                        "use kind='data' for data-section tags."
                    )
                func = containing_funcs[0]
                # `Function.add_tag(tag_type, data, addr=addr)` — the
                # presence of `addr` is what makes it an address tag.
                func.add_tag(tt_name, payload, addr=addr)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Failed to add {norm_kind} tag: {e!s}")

        return {
            "status": "ok",
            "address": hex(addr),
            "kind": norm_kind,
            "tag_type": tt_name,
            "data": payload,
        }

    def get_tags_at(self, address: int) -> dict[str, Any]:
        """Return all tags at an address, across kinds.

        Returns:
            Dict with the address (echoed) and three lists keyed by kind:
            ``data_tags`` (always populated when present), ``address_tags``
            (in-function code addresses), and ``function_tags`` (tags on
            the containing function as a whole).

        Raises:
            RuntimeError: If no binary is loaded.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        addr = int(address)

        data_tags: list[dict[str, Any]] = []
        address_tags: list[dict[str, Any]] = []
        function_tags: list[dict[str, Any]] = []

        # Data tags at this address. `bv.tags_for_data` yields
        # `(addr, Tag)` tuples for every data tag in the view; filter
        # by address. (There's no per-address data-tag getter on this
        # BN version — `get_user_data_tags_at` doesn't exist.)
        try:
            for entry in getattr(bv, "tags_for_data", None) or []:
                try:
                    t_addr, tag = entry
                except (TypeError, ValueError):
                    continue
                if int(t_addr) == addr:
                    data_tags.append(self._serialize_tag(tag, "data", int(t_addr)))
        except Exception:
            pass

        # In-function tags. Use `get_tags_at(addr)` for address-scoped
        # tags and `get_function_tags()` for whole-function tags —
        # `func.tags` returns `(arch, addr, Tag)` tuples and is the
        # wrong shape to feed `_serialize_tag` directly.
        try:
            container_getter = getattr(bv, "get_functions_containing", None)
            containing = list(container_getter(addr) or []) if callable(container_getter) else []
            for func in containing:
                try:
                    for tag in func.get_tags_at(addr) or []:
                        address_tags.append(self._serialize_tag(tag, "address", addr))
                except Exception:
                    pass
                try:
                    for tag in func.get_function_tags() or []:
                        function_tags.append(self._serialize_tag(tag, "function", None))
                except Exception:
                    pass
        except Exception:
            pass

        return {
            "address": hex(addr),
            "data_tags": data_tags,
            "address_tags": address_tags,
            "function_tags": function_tags,
            "total": len(data_tags) + len(address_tags) + len(function_tags),
        }

    def _can_undo(self) -> bool | None:
        """Whether undo is currently possible. None if undetermined."""
        bv = self._current_view
        if bv is None:
            return None
        fmd = getattr(bv, "file", None)
        if fmd is None:
            return None
        try:
            entries = getattr(fmd, "undo_entries", None)
            if entries is None:
                return None
            return bool(list(entries))
        except Exception:
            return None

    def undo(self, count: int = 1) -> dict[str, Any]:
        """Undo the most recent `count` BN actions (default 1).

        Batches all steps in a single call so the post-revert
        reanalysis pass only runs once at the end — much faster than
        `count` separate HTTP calls.

        Returns:
            Dict with status, how many steps actually ran (`performed`),
            BN's last raw return value stringified, and post-call
            `can_undo`.

        Raises:
            RuntimeError: If no binary is loaded or BN refuses an undo
                step (the error message includes how many steps had
                succeeded before the failure).
            ValueError: If `count` is less than 1.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        n = int(count)
        if n < 1:
            raise ValueError(f"count must be >= 1, got {n}")

        bv = self._current_view
        op = getattr(bv, "undo", None)
        if not callable(op):
            raise RuntimeError("BinaryView.undo is unavailable in this Binary Ninja version")

        performed = 0
        last_raw = None
        for _ in range(n):
            # BN's bv.undo() returns None whether it actually undid an
            # entry or the stack was empty — there's no in-band signal.
            # Check can_undo before each step so an empty stack doesn't
            # inflate `performed`.
            if self._can_undo() is False:
                break
            try:
                last_raw = op()
            except Exception as e:
                raise RuntimeError(f"undo failed after {performed} step(s): {e!s}")
            performed += 1

        try:
            bv.update_analysis_and_wait()
        except Exception:
            pass

        return {
            "status": "ok",
            "action": "undo",
            "performed": performed,
            "result": str(last_raw) if last_raw is not None else None,
            "can_undo": self._can_undo(),
        }

    def reanalyze_function(self, function_ident: str | int) -> dict[str, Any]:
        """Trigger reanalysis of a single function.

        Faster than ``update_analysis_and_wait`` when only one function
        changed. The call returns as soon as BN accepts the request; the
        reanalysis itself runs asynchronously, so follow up with
        ``update_analysis`` if you need the result fully settled before
        the next query.

        Args:
            function_ident: Function name or address.

        Returns:
            Dict with status, function name, start address, and a note
            reminding the caller that reanalysis is async.

        Raises:
            RuntimeError: If no binary is loaded or BN refuses.
            ValueError: If the function can't be found.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        func = self.get_function_by_name_or_address(function_ident)
        if func is None:
            raise ValueError(f"Function not found: {function_ident!r}")

        reanalyze = getattr(func, "reanalyze", None)
        if not callable(reanalyze):
            raise RuntimeError("Function.reanalyze is unavailable in this Binary Ninja version")

        try:
            # UserFunctionUpdate when available — it's the right update type
            # for an agent-driven change. Older BN versions take no args.
            update_type_enum = getattr(bn, "FunctionUpdateType", None)
            user_update = (
                getattr(update_type_enum, "UserFunctionUpdate", None)
                if update_type_enum is not None
                else None
            )
            if user_update is not None:
                try:
                    reanalyze(user_update)
                except TypeError:
                    reanalyze()
            else:
                reanalyze()
        except Exception as e:
            raise RuntimeError(f"reanalyze failed: {e!s}")

        return {
            "status": "ok",
            "function": getattr(func, "name", None),
            "address": hex(int(getattr(func, "start", 0))),
            "note": "Reanalysis triggered. Call update_analysis to block until it settles.",
        }

    def update_analysis_and_wait(self) -> dict[str, Any]:
        """Force a full reanalysis of the current view and block until idle.

        Intended for use after a batch of mutations (rename, retype,
        define-data-var, declare-type, etc.) so the next query observes the
        propagated state. May be slow on large binaries.

        Returns:
            Dict with status, wall-clock duration in ms, and a best-effort
            snapshot of the BN analysis state afterwards.

        Raises:
            RuntimeError: If no binary is loaded or the BN call fails.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        update_call = getattr(bv, "update_analysis_and_wait", None)
        if not callable(update_call):
            raise RuntimeError(
                "BinaryView.update_analysis_and_wait is unavailable in this BN version"
            )

        start = time.monotonic()
        try:
            update_call()
        except Exception as e:
            raise RuntimeError(f"update_analysis_and_wait failed: {e!s}")
        duration_ms = int((time.monotonic() - start) * 1000)

        info: dict[str, Any] = {}
        try:
            ai = getattr(bv, "analysis_info", None)
            if ai is not None:
                for k in ("state", "analysis_time", "active_info"):
                    v = getattr(ai, k, None)
                    if v is None:
                        continue
                    info[k] = v if isinstance(v, (int, float, str, bool)) else str(v)
        except Exception:
            info = {}

        return {
            "status": "ok",
            "duration_ms": duration_ms,
            "analysis_info": info or None,
        }

    def define_user_data_var(self, address: int, type_str: str) -> dict[str, Any]:
        """Type a global at an address as a user data variable.

        Args:
            address: Target address as an integer.
            type_str: C-style type string (e.g. "int", "struct Foo *",
                "char[16]"). Parsed via ``BinaryView.parse_type_string``.

        Returns:
            Dict with status, address, and the resolved type as a string.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the type string is empty, fails to parse, or BN
                refuses to apply the data variable.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_type = (type_str or "").strip()
        if not clean_type:
            raise ValueError("Empty type string")
        bv = self._current_view
        addr = int(address)

        parsed_type = None
        try:
            parsed_type, _ = bv.parse_type_string(clean_type)
        except Exception as e:
            raise ValueError(f"Failed to parse type {clean_type!r}: {e!s}")
        if parsed_type is None:
            raise ValueError(f"Type {clean_type!r} parsed to None")

        try:
            bv.define_user_data_var(addr, parsed_type)
        except Exception as e:
            raise ValueError(f"Failed to define data variable at {hex(addr)}: {e!s}")

        return {
            "status": "ok",
            "address": hex(addr),
            "type": str(parsed_type),
        }

    def read_int(self, address: int, size: int, signed: bool = False) -> dict[str, Any]:
        """Read ``size`` bytes at ``address`` as an integer.

        Args:
            address: Target address.
            size: 1, 2, 4, or 8 bytes.
            signed: Two's-complement interpretation when True.

        Returns:
            Dict with address, size, signed flag, integer value, and hex form.

        Raises:
            RuntimeError: If no binary is loaded or BN doesn't expose ``read_int``.
            ValueError: If the read fails or returns None (uninitialized memory).
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        if int(size) not in (1, 2, 4, 8):
            raise ValueError(f"size must be 1, 2, 4, or 8 — got {size}")
        bv = self._current_view
        reader = getattr(bv, "read_int", None)
        if not callable(reader):
            raise RuntimeError("BinaryView.read_int is unavailable in this Binary Ninja version")
        addr = int(address)
        try:
            value = reader(addr, int(size), bool(signed))
        except Exception as e:
            raise ValueError(f"Failed to read int at {hex(addr)}: {e!s}")
        if value is None:
            raise ValueError(f"Read at {hex(addr)} returned None (uninitialized memory?)")
        return {
            "address": hex(addr),
            "size": int(size),
            "signed": bool(signed),
            "value": int(value),
            "hex": hex(int(value) & ((1 << (int(size) * 8)) - 1)),
        }

    def read_pointer(self, address: int) -> dict[str, Any]:
        """Read a pointer-sized integer at ``address``.

        The size is taken from the current view's address size, so this
        works correctly for 32-bit and 64-bit binaries without the caller
        specifying it. When the resulting value matches a known symbol,
        the symbol name is attached for navigation.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        reader = getattr(bv, "read_pointer", None)
        if not callable(reader):
            raise RuntimeError(
                "BinaryView.read_pointer is unavailable in this Binary Ninja version"
            )
        addr = int(address)
        try:
            value = reader(addr)
        except Exception as e:
            raise ValueError(f"Failed to read pointer at {hex(addr)}: {e!s}")
        if value is None:
            raise ValueError(f"Pointer read at {hex(addr)} returned None (uninitialized memory?)")
        value_int = int(value)
        points_to: str | None = None
        try:
            sym = bv.get_symbol_at(value_int)
            if sym is not None:
                points_to = getattr(sym, "name", None)
        except Exception:
            points_to = None
        return {
            "address": hex(addr),
            "value": value_int,
            "hex": hex(value_int),
            "points_to": points_to,
        }

    def add_type_library(self, path: str) -> dict[str, Any]:
        """Load a BNTL type library from disk and attach it to the current view.

        Loading a typelib like libc.bntl, msvcrt.bntl, or a WDK kernel
        library types every matching import in one call — one of the
        highest-leverage actions for malware / driver / firmware RE.

        Args:
            path: Absolute path to a ``.bntl`` file.

        Returns:
            Dict with status, the path, the library name (when exposed),
            and the architecture string.

        Raises:
            RuntimeError: If no binary is loaded or ``TypeLibrary`` is missing.
            ValueError: If the path is empty, the file doesn't exist, or
                BN refuses to load / attach the library.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        import os as _os

        clean_path = (path or "").strip()
        if not clean_path:
            raise ValueError("Empty type library path")
        if not _os.path.exists(clean_path):
            raise ValueError(f"Type library file not found: {clean_path}")

        tl_class = getattr(bn, "TypeLibrary", None)
        if tl_class is None:
            raise RuntimeError("TypeLibrary unavailable in this BN version")
        loader = getattr(tl_class, "load_from_file", None)
        if not callable(loader):
            raise RuntimeError("TypeLibrary.load_from_file unavailable in this BN version")

        try:
            library = loader(clean_path)
        except Exception as e:
            raise ValueError(f"Failed to load type library: {e!s}")
        if library is None:
            raise ValueError(f"Type library at {clean_path} loaded as None")

        attach = getattr(bv, "add_type_library", None)
        if not callable(attach):
            raise RuntimeError("BinaryView.add_type_library unavailable in this BN version")
        try:
            attach(library)
        except Exception as e:
            raise ValueError(f"Failed to attach type library: {e!s}")

        return {
            "status": "ok",
            "path": clean_path,
            "name": str(getattr(library, "name", None))
            if getattr(library, "name", None) is not None
            else None,
            "arch": str(getattr(library, "arch", None))
            if getattr(library, "arch", None) is not None
            else None,
        }

    def demangle(self, name: str, abi: str = "auto") -> dict[str, Any]:
        """Demangle a C++ symbol name to a human-readable form.

        Tries the Itanium (``gnu3``) and Microsoft (``ms``) demanglers,
        in that order when ``abi="auto"``. Returns the first one that
        produces a result.

        Args:
            name: Mangled symbol (e.g. ``"_ZN5MyLib7ProcessC1EPKc"`` or
                ``"?Process@MyLib@@QEAA@PEBD@Z"``).
            abi: ``"auto"`` (default), ``"gnu3"``/``"itanium"``, or
                ``"ms"``/``"msvc"``.

        Returns:
            Dict with status, the input mangled name, the ABI that
            succeeded, the demangled pretty name, and (best-effort) the
            recovered C type string.

        Raises:
            RuntimeError: If no binary is loaded or BN's demangle module
                is unavailable.
            ValueError: If the name is empty, the ABI is unknown, or no
                demangler accepted the input.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Empty mangled name")

        bv = self._current_view
        arch = getattr(bv, "arch", None) or getattr(getattr(bv, "platform", None), "arch", None)

        demangle_mod = getattr(bn, "demangle", None)
        if demangle_mod is None:
            raise RuntimeError("binaryninja.demangle module unavailable")
        gnu3 = getattr(demangle_mod, "demangle_gnu3", None)
        ms = getattr(demangle_mod, "demangle_ms", None)

        norm_abi = (abi or "auto").strip().lower()
        attempts: list[tuple[str, Any]] = []
        if norm_abi in ("auto", "gnu3", "itanium") and callable(gnu3):
            attempts.append(("gnu3", gnu3))
        if norm_abi in ("auto", "ms", "msvc") and callable(ms):
            attempts.append(("ms", ms))
        if not attempts:
            raise ValueError(f"Unknown ABI {abi!r} or no matching demanglers exposed by BN")

        last_error: Exception | None = None
        for attempt_abi, fn in attempts:
            try:
                result = fn(arch, clean_name)
            except Exception as e:
                last_error = e
                continue
            if not result:
                continue
            type_obj = None
            name_obj = None
            if isinstance(result, tuple):
                if len(result) >= 1:
                    type_obj = result[0]
                if len(result) >= 2:
                    name_obj = result[1]
            else:
                name_obj = result
            if name_obj is None:
                continue
            # name_obj may be a QualifiedName (iterable of parts), a list, or a string.
            if isinstance(name_obj, str):
                pretty = name_obj
            else:
                try:
                    parts = [str(p) for p in list(name_obj)]
                    pretty = "::".join(parts) if parts else str(name_obj)
                except Exception:
                    pretty = str(name_obj)
            return {
                "status": "ok",
                "mangled": clean_name,
                "abi": attempt_abi,
                "demangled": pretty,
                "type": str(type_obj) if type_obj is not None else None,
            }

        if last_error is not None:
            raise ValueError(f"Demangling failed: {last_error!s}")
        raise ValueError(f"Demangling failed for {clean_name!r} — no demangler accepted it")

    def get_data_var_at(self, address: int) -> dict[str, Any]:
        """Read the data variable at an address.

        Args:
            address: Target address.

        Returns:
            Dict with address, symbol name (if any), type as a C string,
            and a best-effort string representation of the stored value.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If no data variable exists at the address.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        addr = int(address)

        dv = None
        try:
            dv = bv.get_data_var_at(addr)
        except Exception:
            dv = None
        if dv is None:
            raise ValueError(f"No data variable at {hex(addr)}")

        type_str: str | None = None
        try:
            t = getattr(dv, "type", None)
            type_str = str(t) if t is not None else None
        except Exception:
            type_str = None

        sym_name: str | None = None
        try:
            sym = bv.get_symbol_at(addr)
            if sym is not None:
                sym_name = getattr(sym, "name", None)
        except Exception:
            sym_name = None

        value_str: str | None = None
        try:
            v = getattr(dv, "value", None)
            if v is not None:
                value_str = str(v)
        except Exception:
            value_str = None

        return {
            "address": hex(addr),
            "name": sym_name,
            "type": type_str,
            "value": value_str,
        }

    def undefine_user_data_var(self, address: int) -> dict[str, Any]:
        """Remove a user data variable at an address.

        Returns:
            Dict with status, address, and the prior type when one was
            observable at the address.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If no data variable exists at the address, or BN
                refuses to undefine it.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        addr = int(address)

        dv = None
        try:
            dv = bv.get_data_var_at(addr)
        except Exception:
            dv = None
        if dv is None:
            raise ValueError(f"No data variable at {hex(addr)}")

        prior_type = None
        try:
            prior_type = str(getattr(dv, "type", None))
        except Exception:
            prior_type = None

        try:
            bv.undefine_user_data_var(addr)
        except Exception as e:
            raise ValueError(f"Failed to undefine data variable at {hex(addr)}: {e!s}")

        return {
            "status": "ok",
            "address": hex(addr),
            "removed_type": prior_type,
        }

    def undefine_user_type(self, name: str) -> dict[str, Any]:
        """Remove a user-defined type by name.

        Args:
            name: The type name as it appears in /localTypes or
                /getUserDefinedType. Whitespace is stripped.

        Returns:
            Dict with status, name, and a string snapshot of the prior
            declaration when one was available.

        Raises:
            RuntimeError: If no binary is loaded.
            ValueError: If the name is empty, no user type by that name
                exists, or BN refuses to remove it.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Empty type name")
        bv = self._current_view

        # Verify the type is a user type (auto/library types must not be removable
        # via this endpoint) and capture its declaration for the response.
        prior_decl: str | None = None
        found = False
        try:
            container = getattr(bv, "user_type_container", None)
            types_attr = getattr(container, "types", None) if container is not None else None
            if types_attr:
                for type_id in list(types_attr.keys()):
                    entry = types_attr[type_id]
                    try:
                        entry_name = entry[0]
                        type_obj = entry[1]
                    except (TypeError, IndexError):
                        entry_name = getattr(entry, "name", None)
                        type_obj = getattr(entry, "type", None)
                    if entry_name == clean_name:
                        found = True
                        if type_obj is not None:
                            try:
                                prior_decl = str(getattr(type_obj, "type", type_obj))
                            except Exception:
                                prior_decl = None
                        break
        except Exception:
            # If introspection fails for any reason, refuse rather than guess.
            found = False

        if not found:
            raise ValueError(f"Type {clean_name!r} is not defined as a user type")

        try:
            bv.undefine_user_type(clean_name)
        except Exception as e:
            raise ValueError(f"Failed to undefine type {clean_name!r}: {e!s}")

        return {
            "status": "ok",
            "name": clean_name,
            "removed_declaration": prior_decl,
        }

    def _find_flag_insensitive(self) -> Any | None:
        """Return BN's FindCaseInsensitive enum value if available, else None."""
        try:
            find_flag_enum = getattr(bn, "FindFlag", None) or getattr(
                getattr(bn, "enums", None), "FindFlag", None
            )
            if find_flag_enum is not None:
                return getattr(find_flag_enum, "FindCaseInsensitive", None)
        except Exception:
            pass
        return None

    def _scan(
        self,
        find_next: Any,
        target: Any,
        start: int | None,
        end: int | None,
        limit: int,
        advance: int,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Generic loop around a BN ``find_next_*`` method.

        Shared between find_bytes, find_text, and find_constant. Caller
        provides the BN method, the target to look for, search bounds,
        result cap, the per-hit advance, and any version-specific kwargs.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view
        if start is None:
            start = int(getattr(bv, "start", 0))
        view_end_attr = getattr(bv, "end", None)
        view_end = int(view_end_attr) if view_end_attr is not None else None
        if end is None:
            end = view_end
        elif view_end is not None:
            end = min(int(end), view_end)

        kwargs = dict(extra_kwargs or {})
        matches: list[dict[str, Any]] = []
        cur = int(start)
        max_iter = limit if limit > 0 else 1_000_000
        for _ in range(max_iter):
            if end is not None and cur >= end:
                break
            try:
                hit = find_next(cur, target, **kwargs)
            except TypeError:
                # Older BN that doesn't accept these kwargs; retry minimal.
                try:
                    hit = find_next(cur, target)
                except Exception as e:
                    bn.log_warn(f"find_next_* raised: {e}")
                    break
            except Exception as e:
                bn.log_warn(f"find_next_* raised: {e}")
                break
            if hit is None:
                break
            addr = int(hit)
            if end is not None and addr >= end:
                break
            fn_name: str | None = None
            try:
                getter = getattr(bv, "get_functions_containing", None)
                fns = getter(addr) if callable(getter) else []
                if fns:
                    fn_name = getattr(fns[0], "name", None)
            except Exception:
                fn_name = None
            matches.append({"address": hex(addr), "function": fn_name})
            if limit > 0 and len(matches) >= limit:
                break
            cur = addr + max(advance, 1)
        return matches

    def find_text(
        self,
        text: str,
        start: int | None = None,
        end: int | None = None,
        limit: int = 100,
        case_sensitive: bool = True,
    ) -> list[dict[str, Any]]:
        """Find non-overlapping occurrences of a text string in the view.

        Distinct from `/strings`: this greps raw bytes anywhere in the
        binary (data *and* code), whereas `/strings` enumerates only
        BN-defined string objects.

        Args:
            text: Search text. Empty strings are rejected.
            start: Optional starting address (inclusive). Defaults to view start.
            end: Optional ending address (exclusive). Defaults to view end.
            limit: Cap on results. 0 or negative means "no cap".
            case_sensitive: When False and the BN API exposes
                `FindFlag.FindCaseInsensitive`, the search uses it.

        Returns:
            List of ``{"address": "0x...", "function": <name|None>}``.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        if not text:
            raise ValueError("Empty text")
        bv = self._current_view
        find_next = getattr(bv, "find_next_text", None)
        if not callable(find_next):
            raise RuntimeError(
                "BinaryView.find_next_text is unavailable in this Binary Ninja version"
            )

        extra_kwargs: dict[str, Any] = {}
        if not case_sensitive:
            flag = self._find_flag_insensitive()
            if flag is not None:
                extra_kwargs["flags"] = flag

        advance = max(len(text.encode("utf-8", errors="ignore")), 1)
        return self._scan(find_next, text, start, end, limit, advance, extra_kwargs)

    def find_constant(
        self,
        value: int,
        start: int | None = None,
        end: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Find every instruction whose MLIL contains the given constant.

        Walks the MLIL of every analyzed function and recursively scans
        each instruction's operand tree for ``MLIL_CONST`` /
        ``MLIL_CONST_PTR`` leaves matching ``value``. Returns one entry
        per (instruction address, function) pair where the constant
        appears.

        This is the right tool for "where is the magic value X used as
        a literal in the code?". It is intentionally not backed by
        BN's `find_next_constant`, which searches the linear-view text
        and silently misses most instruction immediates (the `7` in
        `i * 7`, the `1` in `mov w8, #0x1`, etc.).

        Args:
            value: The integer constant to search for.
            start: Optional starting address (inclusive). Defaults to view start.
            end: Optional ending address (exclusive). Defaults to view end.
            limit: Cap on results. 0 or negative means "no cap".

        Returns:
            List of ``{"address": "0x...", "function": <name>}``.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        bv = self._current_view

        # Normalise to 64-bit unsigned so caller-supplied 0xFFFFFFFF
        # matches BN's sign-extended -1 representation and vice versa.
        target = int(value) & 0xFFFFFFFFFFFFFFFF
        start_int = int(start) if start is not None else None
        end_int = int(end) if end is not None else None
        cap = int(limit) if limit and int(limit) > 0 else None

        matches: list[dict[str, Any]] = []
        for func in getattr(bv, "functions", []) or []:
            mlil = getattr(func, "mlil", None)
            if mlil is None:
                continue
            try:
                instructions = list(mlil.instructions)
            except Exception:
                continue
            seen_addrs_in_func: set[int] = set()
            for instr in instructions:
                try:
                    addr = int(instr.address)
                except Exception:
                    continue
                if start_int is not None and addr < start_int:
                    continue
                if end_int is not None and addr >= end_int:
                    continue
                if addr in seen_addrs_in_func:
                    # One machine address can map to several MLIL
                    # instructions; only report it once per function.
                    continue
                if self._expr_contains_const(instr, target):
                    seen_addrs_in_func.add(addr)
                    matches.append({"address": hex(addr), "function": getattr(func, "name", None)})
                    if cap is not None and len(matches) >= cap:
                        return matches
        return matches

    @staticmethod
    def _expr_contains_const(expr: Any, target: int) -> bool:
        """Recursively walk an MLIL expression tree and return True
        iff any leaf is a constant whose value (normalised to 64-bit
        unsigned) equals ``target``."""
        op = getattr(expr, "operation", None)
        op_name = str(op) if op is not None else ""
        if "CONST" in op_name.upper():
            c = getattr(expr, "constant", None)
            if c is not None:
                try:
                    if (int(c) & 0xFFFFFFFFFFFFFFFF) == target:
                        return True
                except Exception:
                    pass
        for child in getattr(expr, "operands", []) or []:
            # Only recurse into nodes that look like IL expressions
            # themselves; bare ints, variables, etc. can't contain
            # nested expressions.
            if hasattr(child, "operation"):
                if BinaryOperations._expr_contains_const(child, target):
                    return True
        return False

    def parse_expression(self, expr: str, here: int = 0) -> dict[str, Any]:
        """Evaluate a Binary Ninja expression string to an address.

        BN's expression language accepts symbol names, arithmetic
        (``+``, ``-``, ``*``, ``/``), hex (``0x...``) and decimal literals,
        and the ``$here`` placeholder. This lets the agent pass strings
        like ``main+0x40`` or ``sub_401000+8`` to any tool that takes an
        address, without computing the result first.

        Args:
            expr: Expression to evaluate.
            here: Address substituted for ``$here``. Default 0.

        Returns:
            Dict with status, the echoed expression and ``here`` value,
            and the resolved address as both hex and integer.

        Raises:
            RuntimeError: If no binary is loaded or BN doesn't expose
                ``parse_expression``.
            ValueError: If the expression doesn't parse.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        clean_expr = (expr or "").strip()
        if not clean_expr:
            raise ValueError("Empty expression")
        bv = self._current_view

        parse = getattr(bv, "parse_expression", None)
        if not callable(parse):
            raise RuntimeError(
                "BinaryView.parse_expression is unavailable in this Binary Ninja version"
            )

        here_int = int(here or 0)
        try:
            try:
                raw = parse(clean_expr, here_int)
            except TypeError:
                # Some BN versions accept only the expression argument.
                raw = parse(clean_expr)
        except Exception as e:
            raise ValueError(f"Failed to parse expression {clean_expr!r}: {e!s}")

        # BN occasionally returned ``Tuple[int, str]`` in older releases;
        # the str part carries an error message when parsing fails.
        addr_val: int | None = None
        if isinstance(raw, tuple):
            if len(raw) >= 2 and raw[1]:
                raise ValueError(f"Expression error: {raw[1]!s}")
            if len(raw) >= 1 and raw[0] is not None:
                addr_val = int(raw[0])
        elif raw is not None:
            try:
                addr_val = int(raw)
            except Exception as e:
                raise ValueError(f"Unexpected parse_expression result {raw!r}: {e!s}")

        if addr_val is None:
            raise ValueError(f"Expression {clean_expr!r} did not produce an address")

        return {
            "status": "ok",
            "expression": clean_expr,
            "here": hex(here_int),
            "address": hex(addr_val),
            "value": addr_val,
        }

    def find_bytes(
        self,
        pattern: bytes,
        start: int | None = None,
        end: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Find non-overlapping occurrences of a byte pattern in the current view.

        Iterates `BinaryView.find_next_data` so it works across BN versions that
        expose that method, even when the newer `find_all_data` is unavailable.

        Args:
            pattern: Bytes to search for. Empty patterns are rejected.
            start: Optional starting address (inclusive). Defaults to view start.
            end: Optional ending address (exclusive). Defaults to view end.
            limit: Cap on results. 0 or negative means "no cap".

        Returns:
            List of `{"address": "0x...", "function": <name|None>}` dicts.
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")
        if not pattern:
            raise ValueError("Empty pattern")
        bv = self._current_view
        find_next = getattr(bv, "find_next_data", None)
        if not callable(find_next):
            raise RuntimeError(
                "BinaryView.find_next_data is unavailable in this Binary Ninja version"
            )

        if start is None:
            start = int(getattr(bv, "start", 0))
        view_end_attr = getattr(bv, "end", None)
        view_end = int(view_end_attr) if view_end_attr is not None else None
        if end is None:
            end = view_end
        elif view_end is not None:
            end = min(int(end), view_end)

        matches: list[dict[str, Any]] = []
        cur = int(start)
        # Hard cap iterations to keep a pathological pattern from spinning forever.
        max_iter = limit if limit > 0 else 1_000_000
        for _ in range(max_iter):
            if end is not None and cur >= end:
                break
            try:
                hit = find_next(cur, pattern)
            except Exception as e:
                bn.log_warn(f"find_next_data raised at {hex(cur)}: {e}")
                break
            if hit is None:
                break
            addr = int(hit)
            if end is not None and addr >= end:
                break
            fn_name = None
            try:
                getter = getattr(bv, "get_functions_containing", None)
                fns = getter(addr) if callable(getter) else []
                if fns:
                    fn_name = getattr(fns[0], "name", None)
            except Exception:
                fn_name = None
            matches.append({"address": hex(addr), "function": fn_name})
            if limit > 0 and len(matches) >= limit:
                break
            # Advance past the match for non-overlapping search.
            cur = addr + len(pattern)
        return matches

    def patch_bytes(
        self, address: str | int, data: str | bytes | list[int], save_to_file: bool = False
    ) -> dict[str, Any]:
        """Patch bytes at a given address in the binary.

        Args:
            address: Address to patch (hex string like "0x401000" or integer)
            data: Bytes to write. Can be:
                - Hex string: "90 90" or "9090" or "0x90 0x90"
                - List of integers: [0x90, 0x90]
                - Bytes object: b"\x90\x90"
            save_to_file: If True, save the patched binary to disk.
                Defaults to False, which only modifies the BinaryView in memory.

        Returns:
            Dictionary with status, address, original bytes, and patched bytes

        Raises:
            RuntimeError: If no binary is loaded
            ValueError: If address or data format is invalid
        """
        if not self._current_view:
            raise RuntimeError("No binary loaded")

        addr = parse_address(address)

        # Parse data into bytes
        patch_bytes = None
        if isinstance(data, bytes):
            patch_bytes = data
        elif isinstance(data, str):
            # Accept "90 90", "9090", "0x9090", and "0x90 0x90".
            data_str = data.strip().replace(" ", "").replace("\n", "").replace("\t", "")
            data_str = data_str.replace("0x", "").replace("0X", "")
            try:
                patch_bytes = bytes.fromhex(data_str)
            except ValueError as e:
                raise ValueError(f"Invalid hex string: {e}")
        elif isinstance(data, list):
            # List of integers
            try:
                patch_bytes = bytes(data)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid byte list: {e}")
        else:
            raise ValueError(f"Unsupported data type: {type(data)}")

        if not patch_bytes:
            raise ValueError("Empty patch data")

        # Reject unmapped addresses up front. Without this guard, bv.write()
        # silently reports the requested length as "written" for addresses
        # outside any segment, masking the failure as a successful patch.
        first_seg = self._current_view.get_segment_at(addr)
        last_seg = self._current_view.get_segment_at(addr + len(patch_bytes) - 1)
        if first_seg is None or last_seg is None:
            raise ValueError(
                f"Address range {hex(addr)}-{hex(addr + len(patch_bytes) - 1)} "
                "is not mapped in the binary"
            )

        # Read original bytes for comparison
        try:
            original_bytes = self._current_view.read(addr, len(patch_bytes))
            if original_bytes is None:
                original_bytes = b""
        except Exception as e:
            bn.log_warn(f"Could not read original bytes at {hex(addr)}: {e}")
            original_bytes = b""

        # Write the patch
        try:
            written = self._current_view.write(addr, patch_bytes)

            # Determine status based on whether all bytes were written
            if written != len(patch_bytes):
                bn.log_warn(f"Only wrote {written} of {len(patch_bytes)} bytes at {hex(addr)}")
                status = "partial"
            else:
                status = "ok"

            result = {
                "status": status,
                "address": hex(addr),
                "original_bytes": original_bytes.hex() if original_bytes else "",
                "patched_bytes": patch_bytes.hex(),
                "bytes_written": written,
                "bytes_requested": len(patch_bytes),
                "saved_to_file": False,
            }

            # Add warning message if partial write
            if status == "partial":
                result["warning"] = f"Only wrote {written} of {len(patch_bytes)} bytes"

            # Save to file if requested
            if save_to_file:
                try:
                    # Get the original file path
                    original_file = self._current_view.file.filename
                    if original_file:
                        # Save the patched binary back to the original file
                        if self._current_view.save(original_file):
                            result["saved_to_file"] = True
                            result["saved_path"] = original_file
                            bn.log_info(f"Patched binary saved to: {original_file}")

                            # On macOS, re-sign the binary to avoid "killed" error
                            if platform.system() == "Darwin":
                                result["codesign"] = self._codesign_binary(original_file)
                        else:
                            bn.log_warn(f"Failed to save patched binary to: {original_file}")
                            result["save_error"] = "save() returned False"
                    else:
                        bn.log_warn("No original file path available for saving")
                        result["save_error"] = "No original file path"
                except Exception as save_e:
                    bn.log_warn(f"Failed to save patched binary: {save_e}")
                    result["save_error"] = str(save_e)

            return result
        except Exception as e:
            raise ValueError(f"Failed to patch bytes at {hex(addr)}: {e!s}")

    def _codesign_binary(self, file_path: str) -> dict[str, Any]:
        """Re-sign a binary on macOS after patching.

        On macOS, modifying a binary invalidates its code signature, causing the
        system to kill the process when executed. This method removes the old
        signature and applies an ad-hoc signature to make the binary executable.

        Args:
            file_path: Path to the binary file to sign

        Returns:
            Dictionary with codesign status and any error messages
        """
        result = {
            "attempted": True,
            "success": False,
            "platform": "macOS",
        }

        try:
            # Step 1: Remove existing signature (optional, codesign -f will overwrite anyway)
            remove_result = subprocess.run(
                ["codesign", "--remove-signature", file_path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if remove_result.returncode != 0:
                # It's okay if removal fails (binary might not have been signed)
                bn.log_info(
                    f"codesign --remove-signature returned {remove_result.returncode}: {remove_result.stderr}"
                )

            # Step 2: Apply ad-hoc signature with force flag
            sign_result = subprocess.run(
                ["codesign", "-f", "-s", "-", file_path], capture_output=True, text=True, timeout=30
            )

            if sign_result.returncode == 0:
                result["success"] = True
                result["message"] = "Binary re-signed with ad-hoc signature"
                bn.log_info(f"Successfully re-signed binary: {file_path}")
            else:
                result["error"] = (
                    sign_result.stderr or f"codesign failed with code {sign_result.returncode}"
                )
                bn.log_warn(f"Failed to re-sign binary: {result['error']}")

        except FileNotFoundError:
            result["error"] = "codesign command not found"
            bn.log_warn("codesign command not found - is Xcode Command Line Tools installed?")
        except subprocess.TimeoutExpired:
            result["error"] = "codesign command timed out"
            bn.log_warn("codesign command timed out")
        except Exception as e:
            result["error"] = str(e)
            bn.log_warn(f"Error during codesign: {e}")

        return result
