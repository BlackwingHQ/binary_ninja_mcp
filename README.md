# Binary Ninja MCP

This repository contains a Binary Ninja plugin, MCP server, and bridge that enables seamless integration of Binary Ninja's capabilities with your favorite LLM client.

![Binary Ninja MCP Logo](images/logo-small.png)

## Features

- Seamless, real-time integration between Binary Ninja and MCP clients
- Enhanced reverse engineering workflow with AI assistance
- Support for every MCP client (Cline, Claude desktop, Roo Code, etc.)
- Open multiple binaries and switch the active target automatically

## Examples

### Solving a CTF Challenge

Check out [this demo video on YouTube](https://www.youtube.com/watch?v=0ffMHH39L_M) that uses the extension to solve a CTF challenge.

## Components

This repository contains two separate components:

1. A Binary Ninja plugin that provides an MCP server that exposes Binary Ninja's capabilities through HTTP endpoints. This can be used with any client that implements the MCP protocol.
2. A separate MCP bridge component that connects your favorite MCP client to the Binary Ninja MCP server.

## Prerequisites

- [Binary Ninja](https://binary.ninja/)
- Python 3.12+
- MCP client (see the supported list below)

## Installation

### MCP Client

`scripts/install_mcp_client.py --client <name>` writes the bridge entry directly into the standard config file of these clients:

- Cline
- Roo Code
- Claude Desktop
- Cursor
- Windsurf
- Claude Code
- LM Studio

Any other MCP-protocol client can be wired up by hand with `--config-file <path>`.

### Extension Installation

Binary Ninja's Plugin Manager (`Plugins > Manage Plugins`) only lists the upstream plugin — it cannot install this fork. Install manually instead, then run the setup scripts described below to register the bridge with your MCP client.

Copy or symlink this repository into Binary Ninja's [plugins folder](https://docs.binary.ninja/guide/plugins.html):

- **macOS:** `~/Library/Application Support/Binary Ninja/plugins/`
- **Linux:** `~/.binaryninja/plugins/`
- **Windows:** `%APPDATA%\Binary Ninja\plugins\`

For development, a symlink is usually best so edits land without copying:

```bash
ln -s /path/to/this/repo ~/.binaryninja/plugins/binary_ninja_mcp
```

In Binary Ninja, **Plugins → Open Plugin Folder** takes you to the right directory. `setup_plugin.py` also prints the platform-appropriate path when it runs.

### Setup

Setup is a two-step process: prepare the plugin's local environment once per system, then register the bridge with each MCP client you want to use it from.

```bash
# One-time per system: create .venv, install bridge dependencies, mint an auth token.
python scripts/setup_plugin.py

# Per MCP client: register the bridge in that client's config file.
python scripts/install_mcp_client.py --list-clients          # show what we know about
python scripts/install_mcp_client.py --client "Claude Code"  # install into one client
python scripts/install_mcp_client.py --client Cursor --uninstall

# For unsupported clients, point at any JSON config file directly:
python scripts/install_mcp_client.py --config-file ~/some/mcp.json
```

`install_mcp_client.py` writes to exactly one file per invocation and refuses to run if `setup_plugin.py` hasn't been run yet. For an unsupported client, `setup_plugin.py` prints a ready-to-paste JSON snippet on completion that you can drop into the client's config by hand.

#### Auth token

The HTTP server only accepts requests carrying the right bearer token. `setup_plugin.py` mints one on first run and writes it to `<plugin_root>/.mcp_auth_token` (mode `0600`, gitignored). The bridge reads the same file directly, so MCP client configs never carry the secret — meaning you can share or commit those configs without leaking auth.

To rotate the token:

```bash
python scripts/setup_plugin.py --regen-token
```

Then restart your MCP client(s) so they respawn the bridge. The plugin re-reads the file on every request, so Binary Ninja does not need to be restarted.

## Usage

1. Open Binary Ninja and load a binary
2. Click the button shown at left bottom corner
3. Start using it through your MCP client

You may now start prompting LLMs about the currently open binary (or binaries). Example prompts:

### CTF Challenges

```txt
You're the best CTF player in the world. Please solve this reversing CTF challenge in the <folder_name> folder using Binary Ninja. Rename ALL the function and the variables during your analyzation process (except for main function) so I can better read the code. Write a python solve script if you need. Also, if you need to create struct or anything, please go ahead. Reverse the code like a human reverser so that I can read the decompiled code that analyzed by you.
```

### Malware Analysis

```txt
Your task is to analyze an unknown file which is currently open in Binary Ninja. You can use the existing MCP server called "binary_ninja_mcp" to interact with the Binary Ninja instance and retrieve information, using the tools made available by this server. In general use the following strategy:

- Start from the entry point of the code
- If this function call others, make sure to follow through the calls and analyze these functions as well to understand their context
- If more details are necessary, disassemble or decompile the function and add comments with your findings
- Inspect the decompilation and add comments with your findings to important areas of code
- Add a comment to each function with a brief summary of what it does
- Rename variables and function parameters to more sensible names
- Change the variable and argument types if necessary (especially pointer and array types)
- Change function names to be more descriptive, using mcp_ as prefix.
- NEVER convert number bases yourself. Use the convert_number MCP tool if needed!
- When you finish your analysis, report how long the analysis took
- At the end, create a report with your findings.
- Based only on these findings, make an assessment on whether the file is malicious or not.
```

## Supported Capabilities

The following table lists the available MCP functions for use:

| Function                                                             | Description                                                                                                  |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `decompile_function`                                                 | Decompile a specific function by name and return HLIL-like code with addresses.                              |
| `get_il(name_or_address, view, ssa)`                                 | Get IL for a function in `hlil`, `mlil`, or `llil` (SSA supported for MLIL/LLIL).                            |
| `define_types`                                                       | Add type definitions from a C string type definition.                                                        |
| `delete_comment`                                                     | Delete the comment at a specific address.                                                                    |
| `delete_function_comment`                                            | Delete the comment for a function.                                                                           |
| `declare_c_type(c_declaration)`                                      | Create/update a local type from a single C declaration.                                                      |
| `format_value(address, text, size)`                                  | Convert a value and annotate it at an address in BN (adds a comment).                                        |
| `function_at`                                                        | Retrieve the name of the function the address belongs to.                                                    |
| `fetch_disassembly`                                              | Get the assembly representation of a function by name or address.                                            |
| `get_entry_points()`                                                 | List entry point(s) of the loaded binary.                                                                    |
| `get_binary_status`                                                  | Get the current status of the loaded binary.                                                                 |
| `get_comment`                                                        | Get the comment at a specific address.                                                                       |
| `get_function_comment`                                               | Get the comment for a function.                                                                              |
| `get_user_defined_type`                                              | Retrieve definition of a user-defined type (struct, enumeration, typedef, union).                            |
| `get_xrefs_to(address)`                                              | Get all cross references (code and data) to an address.                                                      |
| `get_data_decl(name_or_address, length)`                             | Return a C-like declaration and a hexdump for a data symbol or address.                                      |
| `hexdump_address(address, length)`                                   | Text hexdump at address. `length < 0` reads exact defined size if available.                                 |
| `hexdump_data(name_or_address, length)`                              | Hexdump by data symbol name or address. `length < 0` reads exact defined size if available.                  |
| `get_xrefs_to_enum(enum_name)`                                       | Get usages related to an enum (matches member constants in code).                                            |
| `get_xrefs_to_field(struct_name, field_name)`                        | Get all cross references to a named struct field.                                                            |
| `get_xrefs_to_struct(struct_name)`                                   | Get xrefs/usages related to a struct (members, globals, code refs).                                          |
| `get_xrefs_to_type(type_name)`                                       | Get xrefs/usages related to a struct/type (globals, refs, HLIL matches).                                     |
| `get_xrefs_to_union(union_name)`                                     | Get xrefs/usages related to a union (members, globals, code refs).                                           |
| `get_stack_frame_vars(function_identifier)`                          | Get stack frame variable information for a function (names, offsets, sizes, types).                           |
| `get_type_info(type_name)`                                           | Resolve a type and return declaration, kind, and members.                                                    |
| `get_callers(identifiers)`                                           | List callers plus call sites for one or more function identifiers.                                           |
| `get_callees(identifiers)`                                           | List callees plus call sites for one or more function identifiers.                                           |
| `make_function_at(address, platform)`                                | Create a function at an address. `platform` optional; use `default` to pick the BinaryView/platform default. |
| `list_platforms()`                                                   | List all available platform names.                                                                           |
| `list_binaries()`                                                    | List managed/open binaries with ids and active flag.                                                         |
| `select_binary(view)`                                                | Select active binary by id or filename.                                                                      |
| `list_all_strings()`                                                 | List all strings (no pagination; aggregates all pages).                                                      |
| `list_classes`                                                       | List all namespace/class names in the program.                                                               |
| `list_data_items`                                                    | List defined data labels and their values.                                                                   |
| `list_exports`                                                       | List exported functions/symbols.                                                                             |
| `list_imports`                                                       | List imported symbols in the program.                                                                        |
| `list_local_types(offset, count)`                                    | List local Types in the current database (name/kind/decl).                                                   |
| `list_methods`                                                       | List all function names in the program.                                                                      |
| `list_namespaces`                                                    | List all non-global namespaces in the program.                                                               |
| `list_segments`                                                      | List all memory segments in the program.                                                                     |
| `list_strings(offset, count)`                                        | List all strings in the database (paginated).                                                                |
| `list_strings_filter(offset, count, filter)`                         | List matching strings (paginated, filtered by substring).                                                    |
| `rename_data`                                                        | Rename a data label at the specified address.                                                                |
| `rename_function`                                                    | Rename a function by its current name to a new user-defined name.                                            |
| `rename_single_variable`                                             | Rename a single local variable inside a function.                                                            |
| `rename_multi_variables`                                             | Batch rename multiple local variables in a function (mapping or pairs).                                      |
| `set_local_variable_type(function_address, variable_name, new_type)` | Set a local variable's type.                                                                                 |
| `retype_variable`                                                    | Retype variable inside a given function.                                                                     |
| `search_functions_by_name`                                           | Search for functions whose name contains the given substring.                                                |
| `find_bytes(pattern, start, end, limit)`                             | Find non-overlapping occurrences of a byte pattern. Pattern is hex (e.g. `"deadbeef"` or `"90 90 90"`). `start`/`end` are optional address bounds; `limit` caps results (0 = unlimited). |
| `find_text(text, start, end, limit, case_sensitive)`                 | Find non-overlapping occurrences of a text string anywhere in the binary's bytes (data *and* code). Surfaces text BN didn't recognize as a string. |
| `find_constant(value, start, end, limit)`                            | Find non-overlapping occurrences of a numeric constant in *instructions* (different from `find_bytes`, which scans raw bytes). |
| `parse_expression(expr, here)`                                       | Evaluate a BN expression (`main+0x40`, `sub_401000+8`, `&strtab`) to a hex address. Use whenever you'd otherwise hand-compute an address. |
| `define_user_symbol(address, name, kind)`                            | Create a user symbol (label) at an address. `kind` is `"data"` (default) or `"function"`.                     |
| `undefine_user_symbol(address)`                                      | Remove the user symbol at an address. Refuses to act on auto-generated symbols.                              |
| `undefine_user_type(name)`                                           | Remove a user-defined type by name. Auto/library types are rejected (returns 404).                           |
| `define_user_data_var(address, type)`                                | Type a global at an address. `type` is C-style (e.g. `"uint8_t"`, `"struct Foo *"`). Propagates the type through every xref. |
| `undefine_user_data_var(address)`                                    | Remove a user data variable at an address. Returns 404 if none exists.                                        |
| `get_data_var_at(address)`                                           | Read the data variable at an address — targeted read companion to `define_user_data_var`. Returns 404 if none exists. |
| `update_analysis()`                                                  | Force a full Binary Ninja reanalysis and block until idle. Use after batch mutations so later queries see propagated state. May be slow on large binaries. |
| `reanalyze_function(function)`                                       | Trigger reanalysis of a single function. Async — follow up with `update_analysis()` if you need the result settled before the next query. |
| `undo()`                                                             | Undo the most recent BN action. Response includes the post-call `can_undo` / `can_redo` flags.                |
| `redo()`                                                             | Redo the most recently undone BN action.                                                                      |
| `list_tag_types()`                                                   | List all tag types known to BN (built-in plus user-created).                                                  |
| `create_tag_type(name, icon)`                                        | Create a tag-type category. No-op if one with that name already exists.                                       |
| `add_tag(address, tag_type, data, kind)`                             | Tag an address. `kind` is `auto` (default) / `address` / `function` / `data`. `tag_type` is auto-created if missing. |
| `get_tags_at(address)`                                               | Return all tags at an address (data, in-function address, and containing function's tags).                    |
| `get_function_metadata(function)`                                    | Read-only bundle of `is_thunk`, `can_return`, `has_variable_arguments`, `is_pure`, `analysis_skipped`, `analysis_skip_reason`, `analysis_skip_override`, `auto`, `parameter_count`. Use to diagnose poor decompilation. |
| `set_function_can_return(function, can_return)`                      | Override BN's no-return inference. Wrong `can_return` corrupts the CFG of every caller; this is high-impact. |
| `set_function_return_type(function, type)`                           | Set just the return type without rewriting the full prototype. `type` is C-style.                            |
| `set_function_inline(function, inline)`                              | Force or un-force BN's inline-during-analysis behavior. Useful for small helpers.                             |
| `get_var_uses(function, variable, il_level)`                         | Find every use site of a local variable inside a function. `il_level` filters to `all`/`hlil`/`mlil`/`llil`. |
| `get_var_definitions(function, variable, il_level)`                  | Find every definition (write) site of a local variable inside a function.                                     |
| `get_constants_referenced_by(address, function)`                     | Immediate constants referenced by an instruction (value, size, pointer/intermediate flags). `function` is optional — auto-resolved when omitted. |
| `get_regs_read_by(address, function)`                                | Register names read by an instruction.                                                                        |
| `get_regs_written_by(address, function)`                             | Register names written by an instruction.                                                                     |
| `search_types(query, offset, count)`                                 | Search local Types by substring (name/decl).                                                                 |
| `set_comment`                                                        | Set a comment at a specific address.                                                                         |
| `set_function_comment`                                               | Set a comment for a function.                                                                                |
| `set_function_prototype(name_or_address, prototype)`                 | Set a function's prototype by name or address.                                                               |
| `patch_bytes(address, data, save_to_file)`                           | Patch raw bytes at an address (byte-level, not assembly). Can patch entire instructions by providing their bytecode. Address: hex (e.g., "0x401000") or decimal. Data: hex string (e.g., "90 90"). `save_to_file` (default True) saves to disk and re-signs on macOS. |

These are the list of HTTP endpoints that can be called:

- `/allStrings`: All strings in one response.
- `/formatValue?address=<addr>&text=<value>&size=<n>`: Convert and set a comment at an address.
- `/getXrefsTo?address=<addr>`: Xrefs to address (code+data).
- `/getDataDecl?name=<symbol>|address=<addr>&length=<n>`: JSON with declaration-style string and a hexdump for a data symbol or address. Keys: `address`, `name`, `size`, `type`, `decl`, `hexdump`. `length < 0` reads exact defined size if available.
- `/hexdump?address=<addr>&length=<n>`: Text hexdump aligned at address; `length < 0` reads exact defined size if available.
- `/hexdumpByName?name=<symbol>&length=<n>`: Text hexdump by symbol name. Recognizes BN auto-labels like `data_<hex>`, `byte_<hex>`, `word_<hex>`, `dword_<hex>`, `qword_<hex>`, `off_<hex>`, `unk_<hex>`, and plain hex addresses.
- `/makeFunctionAt?address=<addr>&platform=<name|default>`: Create a function at an address (idempotent if already exists). `platform=default` uses the BinaryView/platform default.
- `/platforms`: List all available platform names.
- `/binaries` or `/views`: List managed/open binaries with ids and active flag.
- `/selectBinary?view=<id|filename>`: Select active binary for subsequent operations.
- `/data?offset=<n>&limit=<m>&length=<n>`: Defined data items with previews. `length` controls bytes read per item (capped at defined size). Default behavior reads exact defined size when available; `length=-1` forces exact-size.
- `/getXrefsToEnum?name=<enum>`: Enum usages by matching member constants.
- `/getXrefsToField?struct=<name>&field=<name>`: Xrefs to struct field.
- `/getXrefsToType?name=<type>`: Xrefs/usages related to a struct/type name.
- `/getTypeInfo?name=<type>`: Resolve a type and return declaration and details.
- `/getXrefsToUnion?name=<union>`: Union xrefs/usages (members, globals, refs).
- `/getStackFrameVars?name=<function>|address=<addr>`: Get stack frame variable information for a function.
- `/getCallers?identifiers=<name|addr>[,...]`: Return caller summaries (functions, call sites, HLIL/IL snippets) for one or more identifiers. Accepts `identifiers`, `identifier`, `names`, or `addresses` query params.
- `/getCallees?identifiers=<name|addr>[,...]`: Return callee summaries with the same schema as `/getCallers`, detailing every outgoing call target per request identifier.
- `/localTypes?offset=<n>&limit=<m>`: List local types.
- `/strings?offset=<n>&limit=<m>`: Paginated strings.
- `/strings/filter?offset=<n>&limit=<m>&filter=<substr>`: Filtered strings.
- `/searchTypes?query=<substr>&offset=<n>&limit=<m>`: Search local types by substring.
- `/findBytes?pattern=<hex>&start=<addr>&end=<addr>&limit=<n>`: Find non-overlapping byte-pattern occurrences across the binary. Pattern tolerates spaces and `0x` prefixes (e.g. `deadbeef`, `de ad be ef`, `0xde 0xad`). `start`/`end` are optional address bounds; `limit` defaults to 100 (0 or negative = unlimited).
- `/findText?text=<string>&start=<addr>&end=<addr>&limit=<n>&caseSensitive=<1|0>`: Find non-overlapping text occurrences across the binary (raw bytes, not just BN-defined strings). `caseSensitive` defaults to 1.
- `/findConstant?value=<int>&start=<addr>&end=<addr>&limit=<n>`: Find non-overlapping instruction-level matches of a numeric constant. `value` accepts hex (`0xCAFEBABE`) or decimal.
- `/parseExpression?expr=<expression>&here=<addr>`: Evaluate a BN-syntax expression (`main+0x40`, `sub_401000+8`, `&strtab`) to an address. `here` is optional and substitutes for `$here` in the expression.
- `/defineUserSymbol?address=<addr>&name=<name>&kind=<data|function>`: Create a user symbol at an address. `kind` defaults to `data`.
- `/undefineUserSymbol?address=<addr>`: Remove the user symbol at an address. Returns 404 if no symbol exists there or if BN refuses (e.g. auto-generated symbol).
- `/undefineUserType?name=<typeName>`: Remove a user-defined type. Returns 404 if no user type by that name exists, or if BN refuses to remove it.
- `/defineUserDataVar?address=<addr>&type=<cType>`: Type a global at an address. `type` is C-style (`uint8_t`, `struct Foo *`, `char[16]`).
- `/undefineUserDataVar?address=<addr>`: Remove the user data variable at an address. Returns 404 if no data variable exists there.
- `/getDataVarAt?address=<addr>`: Read the data variable at an address. Returns `{address, name, type, value}` or 404.
- `/updateAnalysisAndWait`: Force a full reanalysis and block until BN reports analysis is idle. Returns `{status, duration_ms, analysis_info}`. No client-side timeout — may run for many seconds on large binaries.
- `/reanalyzeFunction?function=<name|addr>`: Trigger async reanalysis of a single function. Faster than `/updateAnalysisAndWait` when only one function changed.
- `/undo`: Undo the most recent BN action. Returns `{status, action, result, can_undo, can_redo}`.
- `/redo`: Redo the most recently undone BN action. Same response shape as `/undo`.
- `/tagTypes`: List all tag types defined on the current view.
- `/createTagType?name=<name>&icon=<glyph>`: Create a tag-type category (no-op if it already exists).
- `/addTag?address=<addr>&tagType=<name>&data=<text>&kind=<auto|address|function|data>`: Attach a tag. `kind` defaults to `auto` (function-tag at a function start, address-tag inside a function, data-tag otherwise). `tagType` is auto-created if missing.
- `/getTagsAt?address=<addr>`: Return data-, address-, and function-tags at the given address.
- `/getFunctionMetadata?function=<name|addr>`: Return a read-only bundle of BN function flags (`is_thunk`, `can_return`, `has_variable_arguments`, `is_pure`, `analysis_skipped`, `analysis_skip_reason`, `analysis_skip_override`, `auto`, `parameter_count`).
- `/setFunctionCanReturn?function=<name|addr>&canReturn=<true|false>`: Override BN's no-return inference. Accepts `true`/`false`/`1`/`0`/`yes`/`no`.
- `/setFunctionReturnType?function=<name|addr>&type=<cType>`: Set the return type only (parsed via `parse_type_string`).
- `/setFunctionInline?function=<name|addr>&inline=<true|false>`: Toggle `inline_during_analysis`.
- `/getVarUses?function=<name|addr>&variable=<name>&ilLevel=<all|hlil|mlil|llil>`: List use sites of a local variable inside a function. Each entry includes the address, BN's il_type, and the HLIL snippet when available.
- `/getVarDefinitions?function=<name|addr>&variable=<name>&ilLevel=<all|hlil|mlil|llil>`: Same shape as `/getVarUses` but for definition (write) sites.
- `/getConstantsReferencedBy?address=<addr>&function=<name|addr>`: Constants referenced by the instruction at `address`. `function` is optional — auto-resolved from the containing function when omitted.
- `/getRegsReadBy?address=<addr>&function=<name|addr>`: Register names read by the instruction.
- `/getRegsWrittenBy?address=<addr>&function=<name|addr>`: Register names written by the instruction.
- `/patch` or `/patchBytes?address=<addr>&data=<hex>&save_to_file=<bool>`: Patch raw bytes at an address (byte-level, not assembly). Can patch entire instructions by providing their bytecode. Address: hex (e.g., "0x401000") or decimal. Data: hex string (e.g., "90 90"). `save_to_file` (default True) saves to disk and re-signs on macOS.
- `/renameVariables`: Batch rename locals in a function. Parameters:
  - Function: one of `functionAddress`, `address`, `function`, `functionName`, or `name`.
  - Provide renames via one of:
    - `renames`: JSON array of `{old, new}` objects
    - `mapping`: JSON object of `old->new`
    - `pairs`: compact string `old1:new1,old2:new2`
          Returns per-item results plus totals. Order is respected; later pairs can refer to earlier new names.

## Development

### Code Quality

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Configuration is in `ruff.toml`.

#### Running Ruff Manually

Check for issues:
```bash
ruff check .
```

Auto-fix issues:
```bash
ruff check --fix .
```

Check formatting issues:
```bash
ruff format --check .
```

Format code:
```bash
ruff format .
```

#### GitHub Actions

A GitHub Action workflow (`.github/workflows/lint-format.yml`) automatically runs Ruff on:

- Every push to the `main` branch
- Every pull request targeting the `main` branch

The workflow will fail if there are linting errors or formatting issues, ensuring code quality in CI.

## Contributing

Contributions are welcome. Please feel free to submit a pull request.
