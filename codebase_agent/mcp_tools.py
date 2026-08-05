# ==========================================
# mcp_tools.py — MCP tool execution, session cache, and tool schemas.
# execute_mcp_tool and SessionCache are UNCHANGED behavior from the
# original script. TOOL_REQUIRED_ARGS / validate_tool_call have been
# replaced (Phase 3) with real JSON Schemas validated via `jsonschema`,
# but validate_tool_call's signature and error-string format are kept
# identical so agent.py needed no changes.
#
# Phase 6: two "god-tier" native fallback tools — grep_search and
# read_file_chunk — for when search_code/search_graph come up empty.
# Unlike every other tool here, they never touch MCP_EXE_PATH; they read
# the project's source files directly off disk via `_resolve_project_root`.
# ==========================================
import fnmatch
import json
import os
import re
import subprocess

from jsonschema import Draft7Validator

from config import (
    GREP_MAX_FILE_BYTES,
    GREP_MAX_RESULTS,
    GREP_SKIP_DIRS,
    MCP_EXE_PATH,
    PROJECT_ROOTS,
    READ_FILE_CHUNK_MAX_LINES,
    WORKSPACE_ROOT,
)


# ==========================================
# SESSION CACHE & MEMORY
# RPM NOTE: the single biggest source of wasted round-trips in a long
# chat session is the model re-discovering something it already
# learned — especially the project name, which nearly every tool call
# needs. `SessionCache` memoizes the raw MCP result; `known_context`
# goes further and gets injected straight into the prompt so the model
# doesn't even spend a *call* asking again.
# ==========================================
class SessionCache:
    def __init__(self):
        self.store = {}

    def _key(self, tool_name, args_dict):
        return f"{tool_name}:{json.dumps(args_dict, sort_keys=True)}"

    def get(self, tool_name, args_dict):
        return self.store.get(self._key(tool_name, args_dict))

    def set(self, tool_name, args_dict, result):
        self.store[self._key(tool_name, args_dict)] = result


session_cache = SessionCache()
# Phase 4: widened from {"list_projects", "get_architecture"} to cover every
# read-only, idempotent tool — search_code / get_code_snippet / search_graph
# give identical results for identical args within a session, same
# justification as the original two. query_graph is deliberately excluded:
# repeated identical cypher queries aren't common enough yet to be worth
# caching by default (revisit if that changes).
# Phase 6: grep_search / read_file_chunk are the same shape of read-only,
# idempotent call (same args -> same file contents within a session), so
# they're cached the same way even though they never touch MCP_EXE_PATH.
CACHEABLE_TOOLS = {
    "list_projects",
    "get_architecture",
    "search_code",
    "get_code_snippet",
    "search_graph",
    "grep_search",
    "read_file_chunk",
    "find_symbol_usages",
    "outline_file",
}

known_context = {"projects": None, "graph_schemas": {}}


def note_known_projects(list_projects_result_text):
    """Called once list_projects succeeds. Later turns get the project
    list injected directly into their system prompt instead of paying
    a full round-trip to ask for it again."""
    known_context["projects"] = list_projects_result_text


# ==========================================
# Phase 7: empirical graph-schema probing
# query_graph is the right primitive for "what calls/produces/implements
# X" questions, but the model has no way to know real relationship-type
# names up front, and guessing produces confidently-wrong Cypher. The
# first time a project is actually touched this session (any successful,
# project-scoped tool call — see the hook in agent.start_interactive_chat),
# fire one real introspection query against that project's live graph and
# cache whatever comes back verbatim; build_system_instructions injects it
# the same way it already injects known_context["projects"].
# ==========================================
_graph_schema_probe_attempted = set()

# Kept tiny on purpose — the number of distinct relationship TYPES in a
# graph is small regardless of how big the graph itself is, so this is a
# defensive cap, not a real limiting factor. It also guards the plain
# MATCH probe against being an unbounded scan on a huge graph.
_GRAPH_SCHEMA_PROBE_LIMIT = 50
_GRAPH_SCHEMA_MAX_CHARS = 1500

# Tried in order; the first non-error, non-empty response wins. The plain
# MATCH form is tried first since it's portable Cypher any backend should
# support; `db.relationshipTypes()` is a Neo4j/Memgraph system procedure
# that may not exist everywhere, so it's a fallback, not the primary probe.
_GRAPH_SCHEMA_PROBE_QUERIES = (
    f"MATCH ()-[r]->() RETURN DISTINCT type(r) AS relationshipType LIMIT {_GRAPH_SCHEMA_PROBE_LIMIT}",
    f"CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType LIMIT {_GRAPH_SCHEMA_PROBE_LIMIT}",
)


def _looks_like_probe_empty(text):
    stripped = (text or "").strip().lower()
    return stripped in ("", "[]", "{}", "null", "none") or "no results" in stripped or "no matches" in stripped


def _probe_graph_schema(project):
    """Best-effort only — a probe failure must never break the real tool
    call it rides in on. Caches the first usable response verbatim rather
    than re-parsing it into a "clean" list, since the MCP binary's exact
    output shape for query_graph isn't documented; the model can read raw
    rows or a table just fine once it's sitting in its own context."""
    for query in _GRAPH_SCHEMA_PROBE_QUERIES:
        result = execute_mcp_tool("query_graph", {"project": project, "query": query})
        if result and "Error" not in result and not _looks_like_probe_empty(result):
            trimmed = result.strip()
            if len(trimmed) > _GRAPH_SCHEMA_MAX_CHARS:
                trimmed = trimmed[:_GRAPH_SCHEMA_MAX_CHARS] + "\u2026 (truncated)"
            known_context["graph_schemas"][project] = trimmed
            return


def ensure_graph_schema_known(project):
    """Call after any successful project-scoped tool call. No-ops after
    the first attempt for a given project — success or failure — so this
    never turns into a per-call retry loop against a backend that simply
    doesn't support introspection."""
    if project in known_context["graph_schemas"] or project in _graph_schema_probe_attempted:
        return
    _graph_schema_probe_attempted.add(project)
    try:
        _probe_graph_schema(project)
    except Exception:
        pass


# ==========================================
# Phase 6: native fallback tools — grep_search / read_file_chunk
# These never call MCP_EXE_PATH. They resolve `project` to a local
# directory (see PROJECT_ROOTS / WORKSPACE_ROOT in config.py) and read
# files directly, so a project with no known root produces one clear
# error message instead of a confusing empty MCP response.
# ==========================================
_NATIVE_TOOLS = {"grep_search", "read_file_chunk", "find_symbol_usages", "outline_file"}


def _resolve_project_root(project):
    """Map a `project` name (spelled exactly as list_projects reports it)
    to a local filesystem directory. Returns (root, None) on success or
    (None, error_message) on failure — never guesses silently."""
    root = PROJECT_ROOTS.get(project)
    if root is None and WORKSPACE_ROOT:
        candidate = os.path.join(WORKSPACE_ROOT, project)
        if os.path.isdir(candidate):
            root = candidate
    if root is None or not os.path.isdir(root):
        return None, (
            f"Error: no local filesystem path is configured for project '{project}'. "
            "Add an entry to PROJECT_ROOTS (or set WORKSPACE_ROOT) in config.py mapping "
            "this exact project name to its directory on disk, then retry."
        )
    return os.path.abspath(root), None


def _grep_search(project, regex_pattern, file_pattern=None):
    root, err = _resolve_project_root(project)
    if err:
        return err

    try:
        compiled = re.compile(regex_pattern)
    except re.error as e:
        return f"Error: invalid regex_pattern '{regex_pattern}': {e}"

    matches = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in GREP_SKIP_DIRS]
        for filename in sorted(filenames):
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, root).replace(os.sep, "/")

            if file_pattern and not fnmatch.fnmatch(rel_path, file_pattern):
                continue
            try:
                if os.path.getsize(full_path) > GREP_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line_no, line in enumerate(fh, start=1):
                        if compiled.search(line):
                            matches.append(f"{rel_path}:{line_no}: {line.strip()}")
                            if len(matches) >= GREP_MAX_RESULTS:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return "No matches."

    header = f"{len(matches)} match(es) for /{regex_pattern}/"
    if file_pattern:
        header += f" in files matching '{file_pattern}'"
    if truncated:
        header += f" (truncated at {GREP_MAX_RESULTS} — narrow file_pattern or regex_pattern for more)"
    return header + ":\n" + "\n".join(matches)


def _read_file_chunk(project, file_path, start_line, end_line):
    root, err = _resolve_project_root(project)
    if err:
        return err

    if start_line < 1:
        return "Error: start_line must be >= 1."
    if end_line < start_line:
        return "Error: end_line must be >= start_line."

    # Confine reads to inside the resolved project root — reject absolute
    # paths and any ../ traversal outright rather than trying to sanitize.
    normalized = os.path.normpath(file_path)
    if os.path.isabs(normalized) or normalized.split(os.sep)[0] == "..":
        return f"Error: file_path must be relative to the project root, got '{file_path}'."

    full_path = os.path.normpath(os.path.join(root, normalized))
    if os.path.commonpath([root, full_path]) != root:
        return f"Error: file_path '{file_path}' resolves outside the project root."
    if not os.path.isfile(full_path):
        return f"Error: file '{file_path}' does not exist in project '{project}'."

    requested_span = end_line - start_line + 1
    capped = requested_span > READ_FILE_CHUNK_MAX_LINES
    if capped:
        end_line = start_line + READ_FILE_CHUNK_MAX_LINES - 1

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
    except OSError as e:
        return f"Error reading '{file_path}': {e}"

    if start_line > len(lines):
        return f"Error: file '{file_path}' only has {len(lines)} line(s); start_line={start_line} is past the end."

    selected = lines[start_line - 1:end_line]
    last_line = start_line + len(selected) - 1
    numbered = [f"{start_line + i:>6}: {line.rstrip(chr(10))}" for i, line in enumerate(selected)]
    note = f"\n[NOTE: capped at {READ_FILE_CHUNK_MAX_LINES} lines; requested range was larger.]" if capped else ""
    return f"{file_path} (lines {start_line}-{last_line} of {len(lines)}):\n" + "\n".join(numbered) + note


# ==========================================
# Phase 9: find_symbol_usages — a third native fallback tool, same pattern
# as grep_search/read_file_chunk above. A guaranteed backstop for
# relationship questions when the graph's call-graph support (Phase 7)
# turns out to be thin: given an exact identifier, return every occurrence
# with a heuristic definition-like/call-like/reference tag.
# ==========================================
def _definition_regexes(name_pattern):
    """Shape-based, heuristic signature patterns — not per-language grammar,
    so this generalizes loosely across languages. Shared by
    `_classify_symbol_occurrence` below (one already-known, exact symbol)
    and `_outline_file`'s Phase 10 (any identifier, to list every signature
    in a file) so the two never drift out of sync."""
    return (
        r'\b(class|interface|struct|enum|trait)\s+' + name_pattern + r'\b',
        r'\bdef\s+' + name_pattern + r'\s*\(',        # Python
        r'\bfunction\s+' + name_pattern + r'\s*\(',   # JS/TS/PHP
        # Java/C-like declaration: one or more modifier/type tokens, then the
        # symbol, an argument list, then either an opening brace or EOL.
        r'^(?:[\w<>\[\].]+\s+)+' + name_pattern + r'\s*\([^;]*\)\s*\{?\s*$',
    )


def _classify_symbol_occurrence(line, symbol):
    """Heuristic only — classifies a matched line by pattern shape rather
    than per-language grammar, so it generalizes loosely across languages.
    This is a hint for the model, not ground truth; the result text says
    so explicitly and points the model at read_file_chunk to confirm."""
    stripped = line.strip()
    escaped = re.escape(symbol)
    for pattern in _definition_regexes(escaped):
        if re.search(pattern, stripped):
            return "definition"
    if re.search(r'\b' + escaped + r'\s*\(', stripped):
        return "call"
    return "reference"


def _find_symbol_usages(project, symbol, file_pattern=None):
    root, err = _resolve_project_root(project)
    if err:
        return err

    if not symbol or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        return (
            f"Error: '{symbol}' doesn't look like one exact identifier. find_symbol_usages needs a bare "
            "symbol name (e.g. 'computeCrosshair'), not a qualified path or expression — use search_code "
            "or search_graph first if you don't have the exact name yet."
        )

    boundary_re = re.compile(r'\b' + re.escape(symbol) + r'\b')

    matches = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in GREP_SKIP_DIRS]
        for filename in sorted(filenames):
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, root).replace(os.sep, "/")

            if file_pattern and not fnmatch.fnmatch(rel_path, file_pattern):
                continue
            try:
                if os.path.getsize(full_path) > GREP_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line_no, line in enumerate(fh, start=1):
                        if boundary_re.search(line):
                            kind = _classify_symbol_occurrence(line, symbol)
                            matches.append(f"{rel_path}:{line_no}: [{kind}] {line.strip()}")
                            if len(matches) >= GREP_MAX_RESULTS:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return f"No occurrences of '{symbol}' found."

    definition_count = sum(1 for m in matches if "[definition]" in m)
    call_count = sum(1 for m in matches if "[call]" in m)
    header = f"{len(matches)} occurrence(s) of '{symbol}' ({definition_count} definition-like, {call_count} call-like)"
    if file_pattern:
        header += f" in files matching '{file_pattern}'"
    if truncated:
        header += f" (truncated at {GREP_MAX_RESULTS} — narrow file_pattern for more)"
    note = "\n[NOTE: definition/call/reference tags are heuristic pattern guesses, not a real parser — confirm with read_file_chunk before citing.]"
    return header + ":\n" + "\n".join(matches) + note


# ==========================================
# Phase 10: outline_file — a fourth native fallback tool, same pattern as
# grep_search/read_file_chunk/find_symbol_usages above. Answers a different
# question than any of them: not "where does X appear" but "what are ALL
# the method/function/class/interface signatures in this one file" — for
# when the thing being searched for shares no vocabulary with the concept
# a keyword search was built around (e.g. a "delayed pixel fetch" concept
# turning out to be a method actually named `getUncacheImage`), so no
# variation of grep_search's regex would ever have matched it. Meant to be
# called on a file already known to be relevant (see agent.py's system
# instructions), not as a first move.
# ==========================================
_IDENTIFIER_PATTERN = r'[A-Za-z_]\w*'
_OUTLINE_SIGNATURE_REGEXES = tuple(re.compile(p) for p in _definition_regexes(_IDENTIFIER_PATTERN))


def _outline_file(project, file_path):
    root, err = _resolve_project_root(project)
    if err:
        return err

    # Same path-safety rules as _read_file_chunk: relative to the project
    # root only, no absolute paths, no ../ traversal.
    normalized = os.path.normpath(file_path)
    if os.path.isabs(normalized) or normalized.split(os.sep)[0] == "..":
        return f"Error: file_path must be relative to the project root, got '{file_path}'."

    full_path = os.path.normpath(os.path.join(root, normalized))
    if os.path.commonpath([root, full_path]) != root:
        return f"Error: file_path '{file_path}' resolves outside the project root."
    if not os.path.isfile(full_path):
        return f"Error: file '{file_path}' does not exist in project '{project}'."

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
    except OSError as e:
        return f"Error reading '{file_path}': {e}"

    signatures = [
        f"{line_no}: {stripped}"
        for line_no, line in enumerate(lines, start=1)
        if (stripped := line.strip()) and any(regex.search(stripped) for regex in _OUTLINE_SIGNATURE_REGEXES)
    ]

    if not signatures:
        return f"No method/function/class/interface signatures found in '{file_path}' ({len(lines)} line(s))."

    header = f"{len(signatures)} signature(s) found in '{file_path}' ({len(lines)} line(s)):"
    note = "\n[NOTE: heuristic pattern match, not a real parser — a signature can be missed or a false positive can slip in; confirm with read_file_chunk before citing.]"
    return header + "\n" + "\n".join(signatures) + note


# ==========================================
# MCP TOOL EXECUTOR
# ==========================================
def execute_mcp_tool(tool_name, args_dict):
    if tool_name in CACHEABLE_TOOLS:
        cached = session_cache.get(tool_name, args_dict)
        if cached is not None:
            print(f"\n   [\u26a1 {tool_name} \u2014 reused from session cache]", end="", flush=True)
            return cached

    if tool_name in _NATIVE_TOOLS:
        # Phase 6: bypass MCP_EXE_PATH entirely — read straight off disk.
        try:
            print(f"\n   [\u26a1 Executing: {tool_name}]...", end="", flush=True)
            if tool_name == "grep_search":
                result = _grep_search(args_dict["project"], args_dict["regex_pattern"], args_dict.get("file_pattern"))
            elif tool_name == "find_symbol_usages":
                result = _find_symbol_usages(args_dict["project"], args_dict["symbol"], args_dict.get("file_pattern"))
            elif tool_name == "outline_file":
                result = _outline_file(args_dict["project"], args_dict["file_path"])
            else:
                result = _read_file_chunk(args_dict["project"], args_dict["file_path"], args_dict["start_line"], args_dict["end_line"])
            print(" Done!")
        except Exception as e:
            print(" Error!")
            result = f"System Error executing '{tool_name}': {str(e)}"
        if tool_name in CACHEABLE_TOOLS:
            session_cache.set(tool_name, args_dict, result)
        return result

    try:
        print(f"\n   [\u26a1 Executing: {tool_name}]...", end="", flush=True)
        args_json_str = json.dumps(args_dict)
        cmd = [MCP_EXE_PATH, "cli", tool_name, args_json_str]
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        print(" Done!")
        if tool_name in CACHEABLE_TOOLS:
            session_cache.set(tool_name, args_dict, result)
        return result
    except subprocess.CalledProcessError as e:
        print(" Failed!")
        return f"Error executing tool '{tool_name}': {e.output}"
    except Exception as e:
        print(" Error!")
        return f"System Error executing '{tool_name}': {str(e)}"


# ==========================================
# AGENT TOOL SCHEMAS
# ==========================================
TOOL_SCHEMAS = """
STRICT TOOL SCHEMAS:
- list_projects: {}
- get_architecture: {"project": "project_name"}
- search_code: {"project": "project_name", "pattern": "search_term", "file_pattern": "**/*", "regex": false, "mode": "compact", "limit": 20}
- get_code_snippet: {"project": "project_name", "qualified_name": "Full.Package.ClassName.MethodName", "include_neighbors": true}
- search_graph: {"project": "project_name", "query": "term", "file_pattern": "**/*.java", "label": "Method|Class|File", "limit": 20}
- query_graph: {"project": "project_name", "query": "cypher query"}
- grep_search: {"project": "project_name", "regex_pattern": "BulkDataDescriptor|IncludeBulkData", "file_pattern": "**/*.java"}
- read_file_chunk: {"project": "project_name", "file_path": "src/main/java/.../DicomInputStream.java", "start_line": 1080, "end_line": 1185}
- find_symbol_usages: {"project": "project_name", "symbol": "computeCrosshair", "file_pattern": "**/*.java"}
- outline_file: {"project": "project_name", "file_path": "src/main/java/.../DicomImageElement.java"}
"""

# ==========================================
# TOOL CALL VALIDATION (Phase 3 — jsonschema-based)
# Catches a malformed or hallucinated tool call in Python, before it
# ever reaches a subprocess call. Rejecting it here costs nothing; an
# uncaught bad call costs an MCP subprocess invocation *and* pollutes
# context with a confusing error the model has to spend a turn
# recovering from.
#
# Replaces the old hand-rolled TOOL_REQUIRED_ARGS / presence-only check
# with real per-tool JSON Schemas (types, required fields, and enums
# where the tool has a real constraint, e.g. search_graph's `label`).
# ==========================================

# A real project name: non-empty and not the wildcard "*" the model
# sometimes hallucinates when it wants "all projects" (no tool here
# supports that; it must call list_projects and pick one).
_PROJECT_NAME_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "not": {"const": "*"},
}

TOOL_JSON_SCHEMAS = {
    "list_projects": {
        "type": "object",
        "additionalProperties": False,
    },
    "get_architecture": {
        "type": "object",
        "properties": {"project": _PROJECT_NAME_SCHEMA},
        "required": ["project"],
    },
    "search_code": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "pattern": {"type": "string", "minLength": 1},
            "file_pattern": {"type": "string"},
            "regex": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["compact", "full"]},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["project", "pattern"],
    },
    "get_code_snippet": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "qualified_name": {"type": "string", "minLength": 1},
            "include_neighbors": {"type": "boolean"},
        },
        "required": ["project", "qualified_name"],
    },
    "search_graph": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "query": {"type": "string", "minLength": 1},
            "file_pattern": {"type": "string"},
            "label": {"type": "string", "enum": ["Method", "Class", "File"]},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["project", "query"],
    },
    "query_graph": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "query": {"type": "string", "minLength": 1},
        },
        "required": ["project", "query"],
    },
    # Phase 6: native fallback tools — see _resolve_project_root/_grep_search/
    # _read_file_chunk above. end_line >= start_line is enforced at runtime,
    # not here — Draft7 has no clean way to compare two sibling properties.
    "grep_search": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "regex_pattern": {"type": "string", "minLength": 1},
            "file_pattern": {"type": "string"},
        },
        "required": ["project", "regex_pattern"],
    },
    "read_file_chunk": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "file_path": {"type": "string", "minLength": 1},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["project", "file_path", "start_line", "end_line"],
    },
    "find_symbol_usages": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "symbol": {"type": "string", "minLength": 1},
            "file_pattern": {"type": "string"},
        },
        "required": ["project", "symbol"],
    },
    "outline_file": {
        "type": "object",
        "properties": {
            "project": _PROJECT_NAME_SCHEMA,
            "file_path": {"type": "string", "minLength": 1},
        },
        "required": ["project", "file_path"],
    },
}

_VALIDATORS = {name: Draft7Validator(schema) for name, schema in TOOL_JSON_SCHEMAS.items()}


def validate_tool_call(tool_name, args_dict):
    """jsonschema-backed validation. Same signature and error-string
    shape as the version it replaces — (is_valid, error_message) — so
    callers (agent.py's "VALIDATION ERROR (tool NOT executed): ..."
    formatting) need no changes."""
    if not isinstance(args_dict, dict):
        return False, "_mcp_args must be a JSON object."
    if tool_name not in TOOL_JSON_SCHEMAS:
        return False, f"Unknown tool '{tool_name}'. Valid tools: {sorted(TOOL_JSON_SCHEMAS)}"

    validator = _VALIDATORS[tool_name]
    errors = sorted(validator.iter_errors(args_dict), key=lambda e: list(e.path))
    if not errors:
        return True, None

    first = errors[0]
    if first.validator == "not" and first.validator_value == {"const": "*"}:
        return False, f"Tool '{tool_name}' needs a real project name, not a wildcard or blank value."
    if first.validator == "required":
        return False, f"Tool '{tool_name}' is missing required argument(s): {first.message}"
    field = ".".join(str(p) for p in first.path) or "(top level)"
    return False, f"Tool '{tool_name}' argument '{field}' is invalid: {first.message}"
