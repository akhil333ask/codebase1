# ==========================================
# agent.py — the ReAct loop: system instructions, the chat call, tool
# dispatch, and (Phase 2) evidence-checked <verify> enforcement.
#
# The planner-merged-into-worker design, the rate limiter, and the basic
# shape of the loop are UNCHANGED from the original script. What's new
# in this pass:
#   - Phase 1: `context.compact_if_needed` is applied to the working
#     messages list right before every model call.
#   - Phase 2: every executed tool call gets a short id (t-<n>), and the
#     final `<verify>` block is checked programmatically against those
#     ids instead of just checked for existence.
#   - Phase 5: empty/no-match tool results get explicit guidance appended
#     instead of being fed back silently, and a repeated identical
#     (tool, args) call across turns triggers a forced-change system
#     message instead of letting the model loop forever.
#   - Phase 6: system instructions now teach the model to fall back from
#     search_graph/search_code to the native grep_search/read_file_chunk
#     tools (see mcp_tools.py) when structured search comes up empty.
#   - Phase 13 (mission's "Phase 11"): tool_result_map/next_tool_id are now
#     session-scoped (initialized once, alongside full_transcript/
#     working_messages) instead of being reset every time a new
#     investigation starts, so a <verify> citation against an id from an
#     earlier question in the same session is still accepted instead of
#     wrongly rejected as "not a tool result from this conversation".
# ==========================================
import json
import re
import time
from collections import deque

import requests

import context
from config import MAX_RPM
from mcp_tools import (
    TOOL_SCHEMAS,
    ensure_graph_schema_known,
    execute_mcp_tool,
    known_context,
    note_known_projects,
    validate_tool_call,
)


# ==========================================
# RATE LIMITER (RPM MANAGER) — unchanged
# ==========================================
class RateLimiter:
    def __init__(self, max_rpm):
        self.max_rpm = max_rpm
        self.requests = deque()

    def wait_if_needed(self):
        now = time.time()
        while self.requests and now - self.requests[0] > 60:
            self.requests.popleft()

        if len(self.requests) >= self.max_rpm:
            wait_time = 60 - (now - self.requests[0])
            if wait_time > 0:
                print(f"\n[!] RPM limit ({self.max_rpm}) reached. Pausing for {wait_time:.1f} seconds to cool down...")
                time.sleep(wait_time)
            now = time.time()
            while self.requests and now - self.requests[0] > 60:
                self.requests.popleft()

        self.requests.append(time.time())
        print(f" [RPM: {len(self.requests)}/{self.max_rpm}] ", end="", flush=True)


rate_limiter = RateLimiter(MAX_RPM)


# ==========================================
# THE AGENT (plans + acts in one call)
# ==========================================
def build_system_instructions(is_new_investigation):
    known_block = ""
    if known_context["projects"]:
        known_block = (
            "\nALREADY KNOWN THIS SESSION — do NOT call list_projects again, reuse this:\n"
            f"{known_context['projects']}\n"
        )

    # Phase 7: real relationship types, empirically probed via query_graph
    # the first time each project is touched this session (see
    # mcp_tools.ensure_graph_schema_known and its call site below) — never
    # hardcoded here, since the real graph is the only source of truth.
    schema_block = ""
    if known_context["graph_schemas"]:
        schema_parts = [
            f"[{proj}]\n{schema_text}"
            for proj, schema_text in known_context["graph_schemas"].items()
        ]
        schema_block = (
            "\nGRAPH SCHEMA — relationship types empirically probed this session via query_graph "
            "(use these EXACT names in Cypher; don't invent new ones):\n" + "\n".join(schema_parts) + "\n"
        )

    planning_block = ""
    if is_new_investigation:
        planning_block = """
NEW QUESTION — PLAN BEFORE YOU ACT:
Open your <thought> block with a short plan, not just a single next step:
  - Restate the real underlying question, not just the literal words.
  - List the 2-4 sub-goals you need evidence for.
  - Say what you already know (see ALREADY KNOWN above) vs what you still need to look up.
  - Batch every lookup that doesn't depend on another lookup's result into ONE <tools> array.
Immediately after your <thought>, restate those same sub-goals as a <goals> block, one short line
each (no citations needed here — this is just so your eventual <verify> can be checked against all
of them later), e.g.:
<goals>
- The method that hands a BULKDATA_DESCRIPTOR to dcm4che3
- The class/method that does the delayed fetch of raw pixel bytes for rendering
- The class/method that applies Modality/VOI LUT (window/level)
</goals>
Then take your first action from that plan.
"""

    return f"""You are an elite Senior Software Engineer diagnosing a codebase via read-only tools.
{known_block}
{schema_block}
{TOOL_SCHEMAS}
{planning_block}
CRITICAL EXECUTION RULES:
1. THINK FIRST: You MUST output a `<thought>...</thought>` block explaining your logic before taking action or giving a final answer.
2. PARALLEL TOOLS: To run tools, output a `<tools>...</tools>` block containing a JSON ARRAY of tool calls. Combine tool calls when they don't depend on each other.
3. DEPENDENCY AWARENESS: If the project isn't already given above under ALREADY KNOWN, your ONLY tool call must be `list_projects` — do not bundle guessed `search_code`/`search_graph` calls alongside it. Once you know the project, always use it exactly as given; never pass `"project": "*"` or leave it blank.
4. QUALIFIED NAMES: `search_code`/`search_graph` results include a `qualified_name` field, e.g. `"qualified_name": "C-JViewer.weasis-dicom.weasis-dicom-viewer2d.src.main.java.org.weasis.dicom.viewer2d.View2d.computeCrosshair"`. To call `get_code_snippet`, copy that field's value EXACTLY, character for character. Never guess it, and never append anything to it — no ` = ...`, no method bodies, no extra code or expressions. If no `qualified_name` field is in front of you for what you need, search again instead of constructing one yourself.
5. NO RAW CODE IN CYPHER: Never put raw multi-line Java code into a `query_graph` query.
6. THE LOOP: When you run tools, the system will instantly reply with a `<tool_results>` block. Each individual result is tagged with a short id, e.g. `<result id="t-3" tool="search_code">`. Read it and continue your investigation automatically until you have the final answer. Do not ask the user for permission.
7. VALIDATE BEFORE FINISHING: Only on the turn where you are NOT calling more tools, output a `<verify>` block. For EACH factual claim you're about to make, write one line ending with the id(s) of the tool result(s) that support it — copy the id exactly as given in `<result id="...">`, e.g.:
<verify>
- The crosshair is computed in `View2d.computeCrosshair` (t-3)
- The DICOM parser lives in `DicomInputStream.readDataset` (t-1, t-5)
</verify>
   A claim with no id, or an id that wasn't actually given to you earlier in this conversation, will be rejected and you'll be asked to fix it. Drop or re-investigate anything you can't back with a real id. Don't include a `<verify>` block on turns where you're still calling tools.
8. LANGUAGE: Always respond strictly in English.
9. TOOL PREFERENCE: When looking for how things work, prefer `search_graph` to find underlying core engine/codec implementations.
10. FALL BACK TO TEXT SEARCH: If `search_graph` or `search_code` return empty results for a concept or keyword, immediately fall back to `grep_search` with a regex pattern instead of retrying more variations of the same structured query — graph/semantic search expects specific node names, not keyword salads, so a miss there often means brute-force text search over the real files will succeed where it didn't.
11. READ SURROUNDING CONTEXT: When `grep_search` (or any search) turns up a file of interest, use `read_file_chunk` to read roughly 100-200 surrounding lines before answering — a single matched line is rarely enough context to cite correctly in `<verify>`. Prefer it over asking for the whole file, which wastes context budget.
12. OUTLINE BEFORE MORE KEYWORDS: A `regex_pattern` must target a plausible identifier fragment or a real API/library/class name (e.g. something visible in an import) — NEVER a multi-word English phrase like `"raw pixel"` or `"bulk data"`; that is a "keyword salad", not a real search, since it will never appear verbatim in source code. If `grep_search`/`search_code`/`search_graph` all come up empty or weak for a concept INSIDE A FILE YOU ALREADY KNOW IS RELEVANT (e.g. because an earlier finding came from that same file), do not keep guessing keyword variations — call `outline_file` on that file instead. It lists every method/function/class/interface signature in the file in one call, so you can see the real name of what you're looking for even when it shares no vocabulary with the concept (e.g. a "delayed pixel fetch" concept may really be named `getUncacheImage`).
13. RELATIONSHIP QUESTIONS: For "what calls/produces/implements/consumes X" questions, `search_graph` is the wrong tool — it matches nodes by name/similarity, not edges between them. For a "does A call B" (or any other single-hop relationship) question specifically, try ONE `query_graph` traversal over the `CALLS` edge (or whichever edge name the GRAPH SCHEMA block above lists for that project) BEFORE falling back to manually reading and chaining method bodies by hand — one Cypher traversal is cheaper than several speculative `read_file_chunk` calls and far less likely to get lost. Start from a `qualified_name` you already have and walk the edge (if no GRAPH SCHEMA block is present yet, run `query_graph` with `MATCH ()-[r]->() RETURN DISTINCT type(r)` first to learn them — never guess a relationship type name); only fall back to manually reading/chaining method bodies, or to `find_symbol_usages` with the exact identifier for likely call sites by text pattern, once query_graph's traversal comes up empty or weak. WRAPPER-METHOD CONTINUATION: once you have a method's actual body in hand (from any of these), check what it DOES before treating it as an answer — if the body is just a call (or a `return` of a call) to another method, that is NOT a stopping point, the callee just became your new target, and you keep investigating into ITS body until you reach one that does real work. Worked example: asked whether `getUncacheImage` calls `readRaster`, the real chain is `getUncacheImage` -> `getImageFragment` -> `readRaster` — reading `getUncacheImage` and finding only `return getImageFragment(...)` is a wrapper, not an answer; `getImageFragment` is now the target, and only ITS body actually shows whether `readRaster` gets called. Always ask "what method's body am I actually looking at right now, and does it call something new" before citing anything as final.

Example Tool Invocation:
<thought>I need to search for the Main class.</thought>
<tools>
[
    {{"_mcp_tool": "search_graph", "_mcp_args": {{"project": "C-JViewer", "query": "main", "label": "Class", "limit": 5}}}}
]
</tools>

Example Fallback (structured search came up empty):
<thought>search_graph found nothing for "bulkdata descriptor pixel data window level" — that's a keyword salad, not a node name. Falling back to grep_search with a narrower regex, then I'll read the matched file's surrounding lines.</thought>
<tools>
[
    {{"_mcp_tool": "grep_search", "_mcp_args": {{"project": "C-JViewer", "regex_pattern": "BulkDataDescriptor|IncludeBulkData|DicomInputStream", "file_pattern": "**/*.java"}}}}
]
</tools>

Example Outline Fallback (grep_search on a file already known to be relevant still came up empty):
<thought>grep_search for pixel/fetch/lazy/deferred inside DicomImageElement.java came up empty, but I already know this file is relevant from an earlier read_file_chunk result. Instead of guessing more keyword variations, I'll call outline_file on it to see every method signature in the file at once.</thought>
<tools>
[
    {{"_mcp_tool": "outline_file", "_mcp_args": {{"project": "C-JViewer", "file_path": "weasis-dicom/weasis-dicom-codec/src/main/java/org/weasis/dicom/codec/DicomImageElement.java"}}}}
]
</tools>

Example Relationship Traversal (a "what calls X" question — prefer query_graph over search_graph):
<thought>The question is "what calls DicomInputStream.readDataset" — a relationship question, not a name-similarity one. I already have its exact qualified_name from an earlier result, and GRAPH SCHEMA above lists CALLS as a real relationship type in this project, so I'll traverse it directly instead of running another fuzzy search.</thought>
<tools>
[
    {{"_mcp_tool": "query_graph", "_mcp_args": {{"project": "C-JViewer", "query": "MATCH (caller)-[:CALLS]->(callee) WHERE callee.qualified_name = 'org.weasis.dicom.codec.DicomInputStream.readDataset' RETURN caller.qualified_name LIMIT 20"}}}}
]
</tools>
"""


def chat_with_codex(access_token, model_id, messages, is_new_investigation=False):
    rate_limiter.wait_if_needed()
    url = "https://chatgpt.com/backend-api/codex/responses"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": "custom_python_script"
    }

    system_instructions = build_system_instructions(is_new_investigation)

    payload = {
        "model": model_id,
        "store": False,
        "stream": True,
        "instructions": system_instructions,
        "input": messages,
        "text": {"verbosity": "low"}
    }

    print("\n\U0001f916 AI: ", end="", flush=True)

    response = requests.post(url, headers=headers, json=payload, stream=True)
    full_response = ""

    if response.status_code == 200:
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith("data: ") and decoded_line != "data: [DONE]":
                    try:
                        chunk = json.loads(decoded_line[6:])
                        if chunk.get("type") == "response.output_text.delta":
                            text_chunk = chunk.get("delta", "")
                            print(text_chunk, end="", flush=True)
                            full_response += text_chunk
                    except json.JSONDecodeError:
                        pass
        print()
        return full_response
    return None


# ==========================================
# Phase 2: evidence-checked <verify> enforcement
# ==========================================
_VERIFY_BLOCK_RE = re.compile(r'<verify>\s*(.*?)\s*</verify>', re.DOTALL)
_ID_REF_RE = re.compile(r't-\d+')

# How many times start_interactive_chat will nudge the model to fix a
# <verify> block that fails grounding before accepting its next answer as
# final, no matter what.
_MAX_VERIFY_RETRIES = 1


def _parse_verify_claims(verify_body):
    """A claim is any non-empty line inside the <verify> block."""
    return [line.strip() for line in verify_body.splitlines() if line.strip()]


def check_verify_block(assistant_reply, tool_result_map):
    """
    Returns (ok, detail):
      - ok=True, detail=None -> every claim line cites at least one tool-
        result id, every cited id exists in `tool_result_map` (i.e. was
        actually returned to the model earlier in this conversation), and
        (Phase 12) each cited result's text actually contains something
        the claim is about.
      - ok=False, detail=<str> -> names the first offending claim (or the
        reason no claims/no block exist at all) so the retry message can be
        specific instead of a generic "add a <verify> block" nudge.
    """
    match = _VERIFY_BLOCK_RE.search(assistant_reply)
    if not match:
        return False, "No <verify> block was found in the final answer."

    claims = _parse_verify_claims(match.group(1))
    if not claims:
        return False, "The <verify> block is present but contains no claims."

    for claim in claims:
        ids = _ID_REF_RE.findall(claim)
        if not ids:
            return False, f'Claim has no tool-result id attached: "{claim}"'

        # Phase 12: a real id (checked below) isn't enough on its own — a
        # claim can cite a genuine t-N and still say nothing that id's
        # result actually supports. Reuse `_significant_terms` (defined
        # further below, shared with the Phase 5c weak-result heuristic) to
        # confirm each cited result's text contains at least one term the
        # claim is actually about; the id tokens themselves never count
        # (stripped out before extracting terms).
        claim_terms = _significant_terms(_ID_REF_RE.sub("", claim))
        for tool_id in ids:
            if tool_id not in tool_result_map:
                return False, f'Claim cites "{tool_id}", which is not a tool result from this conversation: "{claim}"'
            result_text = tool_result_map[tool_id].get("result", "") or ""
            if claim_terms and not any(term in result_text.lower() for term in claim_terms):
                return False, (
                    f'Claim cites "{tool_id}", but none of its significant terms appear in that '
                    f"result's text — the citation doesn't actually support the claim: \"{claim}\""
                )

    return True, None


# Phase 12: lightweight signal for whether a failed claim is about a call
# relationship ("does A call B") specifically, so the retry message in
# start_interactive_chat can point at a concrete next action — open the
# enclosing method and keep tracing — instead of a generic "fix it" that
# leaves the model free to just hedge or reword the same unsupported claim
# again (the exact failure mode this exists for: five hedged retries in a
# row instead of reading one more chunk).
_CALL_RELATIONSHIP_TERMS = ("call", "calls", "calling", "invoke", "invokes", "invoking", "invoked")


def _mentions_call_relationship(text):
    lowered = (text or "").lower()
    return any(re.search(r'\b' + re.escape(term) + r'\b', lowered) for term in _CALL_RELATIONSHIP_TERMS)


# ==========================================
# Phase 5a: empty / "no matches" result detection
# A tool call that technically succeeds but finds nothing is easy for the
# model to skim past without changing strategy. Detecting it here and
# attaching concrete next-step suggestions (right where the model will
# read the result) is cheaper than letting it burn another blind turn.
# ==========================================
_NO_MATCH_PHRASES = (
    "no results", "no matches", "no match found", "0 results",
    "not found", "no such", "nothing found", "no data", "no items",
)


def _looks_like_empty_result(result_text):
    if result_text is None:
        return True
    stripped = result_text.strip()
    if not stripped:
        return True
    if stripped in ("[]", "{}", "null", "none"):
        return True
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, (list, dict)) and len(parsed) == 0:
            return True
    except (json.JSONDecodeError, TypeError):
        pass
    lowered = stripped.lower()
    return any(phrase in lowered for phrase in _NO_MATCH_PHRASES)


def _empty_result_guidance(tool_name):
    """Tool-specific next steps, since "try something else" means something
    different for a text search vs. a graph query."""
    suggestions = {
        "search_code": (
            "No matches. Try a shorter or more generic `pattern`, drop `file_pattern` "
            "if one was set, or try `search_graph` instead — it may find the same "
            "code by structure rather than by text."
        ),
        "search_graph": (
            "No matches. Try a shorter or more generic `query`, drop or change the "
            "`label` filter, or try `search_code` instead — it may find the same "
            "code by text rather than by structure."
        ),
        "get_code_snippet": (
            "No snippet found for that `qualified_name`. It may be stale or slightly "
            "wrong — re-run `search_code`/`search_graph` to get a fresh, exact "
            "`qualified_name` instead of reusing or guessing one."
        ),
        "query_graph": (
            "No results. Simplify the cypher query, verify node/relationship labels "
            "exist in this project via `search_graph`, or double-check the project name."
        ),
        "get_architecture": (
            "No architecture data returned. Double-check the project name against the "
            "ALREADY KNOWN list or `list_projects`."
        ),
        "grep_search": (
            "No matches. Broaden the `regex_pattern` (fewer terms, looser alternation), "
            "drop `file_pattern` if one was set, or try `search_graph`/`search_code` "
            "instead — the concept may be named differently than expected."
        ),
        "read_file_chunk": (
            "That line range returned nothing usable (or errored). Re-run `grep_search` "
            "to confirm the exact `file_path` and a real matching line number before "
            "retrying, rather than guessing a new range."
        ),
    }
    return suggestions.get(
        tool_name,
        "No results. Double-check the arguments (especially the project name) and try "
        "a different approach rather than repeating the same call.",
    )


# ==========================================
# Shared lexical-overlap utility — used by both the weak-result check
# below (Phase 5c) and the goal-coverage check (Phase 8). Deliberately
# simple: strip common/generic words, keep everything else as a
# "significant term", and measure overlap by substring containment. This
# is a heuristic, not real NLP — it's calibrated to catch the reported
# regression cases, not to be linguistically rigorous.
# ==========================================
_STOPWORDS = {
    "the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "with",
    "that", "this", "is", "are", "how", "what", "does", "do", "which",
    "class", "method", "function", "code", "find", "locate", "identify",
    "hands", "applies", "gets", "used", "using", "via", "from", "into",
}


def _significant_terms(text):
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", (text or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) >= 3]


# ==========================================
# Phase 5c: weak / low-confidence result detection
# A result can be technically non-empty (so _looks_like_empty_result above
# lets it through) while still being too thin to cite with confidence —
# e.g. one weak candidate for a broad query. Left unflagged, the model
# tends to either force a citation out of a bad match or give up entirely
# instead of trying a different angle, per the reported regression.
# ==========================================
_WEAK_RESULT_TOOLS = ("search_graph", "search_code", "query_graph")
_WEAK_RESULT_MAX_ITEMS = 2             # this many or fewer candidates is "thin" regardless of overlap
_WEAK_RESULT_MIN_OVERLAP_RATIO = 0.25  # below this fraction of query terms matched is "thin"

# Phase 14 (mission's "Phase 13", fix 1): query_graph's `query` field is a
# literal Cypher query (e.g. "MATCH (caller)-[:CALLS]->(callee) WHERE
# callee.qualified_name = 'x' RETURN caller.qualified_name LIMIT 20"), not
# a natural-language query/pattern like search_graph/search_code use.
# These keywords never appear in result text made of real node/qualified
# names, so counting them as "significant terms" artificially deflates
# the overlap ratio below and can mislabel a genuinely correct traversal
# "weak/inconclusive." Scoped to query_graph only, applied as an extra
# filter at the call site below — _significant_terms/_STOPWORDS
# themselves are untouched so search_code/search_graph term extraction
# (and every other caller: check_verify_block, goal-coverage) is unaffected.
_CYPHER_KEYWORD_STOPWORDS = {"match", "where", "return", "limit", "distinct", "as", "order", "by"}


def _extract_result_items(result_text):
    """Best-effort item count for a tool result of unknown shape (the MCP
    binary's exact output format isn't documented anywhere in this repo).
    Returns a list if the result parses as JSON containing one (top-level,
    or under a common wrapper key), else None if the shape can't be told."""
    try:
        parsed = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("results", "matches", "items", "nodes", "records", "rows", "data"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
    return None


def _looks_like_weak_result(tool_name, args_dict, result_text):
    """True for a result that isn't empty (Phase 5a already passed it
    through) but is still too thin to cite: very few candidates, and/or
    little lexical overlap between the query terms and what came back."""
    if tool_name not in _WEAK_RESULT_TOOLS or not result_text:
        return False

    query_field = args_dict.get("query") or args_dict.get("pattern") or ""
    terms = set(_significant_terms(query_field))
    if tool_name == "query_graph":
        terms -= _CYPHER_KEYWORD_STOPWORDS
    lowered = result_text.lower()

    items = _extract_result_items(result_text)
    if items is not None:
        if len(items) == 0:
            return False  # empty is Phase 5a's job, not this one
        if len(items) <= _WEAK_RESULT_MAX_ITEMS:
            return True
        if not terms:
            return False
        hits = sum(1 for t in terms if t in lowered)
        return (hits / len(terms)) < _WEAK_RESULT_MIN_OVERLAP_RATIO

    # Unknown/non-JSON shape (plain text or a formatted table) — fall back
    # to overlap only, since candidate count can't be told reliably here.
    if not terms:
        return False
    hits = sum(1 for t in terms if t in lowered)
    return (hits / len(terms)) < _WEAK_RESULT_MIN_OVERLAP_RATIO


def _weak_result_guidance(tool_name):
    suggestions = {
        "search_graph": (
            "This came back with very few or weakly-matching candidates — treat it as inconclusive, "
            "not a dead end. Try a different/shorter `query`, drop the `label` filter, try `search_code` "
            "or `grep_search` for the same concept by text, or switch to `query_graph` if this is really "
            "a relationship question (what calls/produces/implements something)."
        ),
        "search_code": (
            "This came back with very few or weakly-matching candidates — treat it as inconclusive, not "
            "a dead end. Try a shorter/more generic `pattern`, or try `search_graph`/`grep_search` for "
            "the same concept."
        ),
        "query_graph": (
            "This query returned very few rows — double-check the relationship type against GRAPH SCHEMA "
            "and the query logic before concluding this path is a dead end; a typo'd relationship type or "
            "label often looks like a real (but tiny) answer instead of an error."
        ),
    }
    return suggestions.get(tool_name, "This result looks thin — consider a different approach before relying on it.")


# ==========================================
# Phase 5b: repeated-call / stuck-loop detection
# The existing within-batch dedup only catches the same call appearing
# twice in one <tools> array. This catches the model re-issuing the exact
# same (tool, args) call across separate turns — a sign it's stuck rather
# than making progress — and forces a change of approach instead of
# silently letting it burn turns/rate-limit budget on repeats.
#
# Phase 12 extends this for read_file_chunk specifically: three retries
# with a slowly-drifting line-range window over the same region of the
# same file are just as stuck as three byte-identical calls, but never
# tripped this guard before since start_line/end_line always differed.
# ==========================================
_STUCK_LOOP_THRESHOLD = 3


def _ranges_overlap(a_start, a_end, b_start, b_end):
    return a_start <= b_end and b_start <= a_end


def _call_signature(tool_name, args_dict, read_chunk_groups=None):
    """Stuck-loop signature for one (tool, args) call, plus whether this
    call made progress. Returns (signature, made_progress).

    Identical args always produce the same signature with made_progress=
    False (the original Phase 5b behavior — repeating the exact same call
    is never progress, so the caller keeps incrementing its repeat count
    toward the stuck-loop threshold unchanged).

    Phase 12: for read_file_chunk specifically, a call on the same
    project+file_path whose line range OVERLAPS a previously-seen range
    for that file collapses onto that earlier signature instead of
    getting a fresh one every time — otherwise a model that drifts its
    window by a few lines on every retry (same region, never
    byte-identical start_line/end_line) never trips the repeat guard.

    Phase 14 (mission's "Phase 13", fix 2): merely overlapping isn't by
    itself evidence of being stuck — paginating forward through a large
    file with the ~100-200 line overlap the system instructions
    themselves recommend produces a run of pairwise-overlapping windows
    that are each genuine forward progress (e.g. 1-200, 150-350, 300-500),
    and previously collapsed onto one signature whose count climbed with
    every call regardless. Now a call only counts as "no progress" when
    its range is ALREADY fully covered by the accumulated union (both
    start and end within the existing group — no new lines gained); a
    call that extends the union's start or end reports made_progress=True
    instead, so the caller resets the repeat count rather than
    incrementing it. A brand-new group (first sighting of this region)
    also reports made_progress=True.

    `read_chunk_groups` is the per-question tracking dict this needs (see
    start_interactive_chat); omitting it falls back to the plain
    exact-args signature for every tool, unchanged from before."""
    if tool_name != "read_file_chunk" or read_chunk_groups is None:
        return f"{tool_name}:{json.dumps(args_dict, sort_keys=True)}", False

    project = args_dict.get("project")
    file_path = args_dict.get("file_path")
    start_line = args_dict.get("start_line")
    end_line = args_dict.get("end_line")
    if project is None or file_path is None or not isinstance(start_line, int) or not isinstance(end_line, int):
        # Malformed/incomplete args — fall back rather than guess at intent.
        return f"{tool_name}:{json.dumps(args_dict, sort_keys=True)}", False

    groups = read_chunk_groups.setdefault((project, file_path), [])
    for group in groups:
        if _ranges_overlap(group["start"], group["end"], start_line, end_line):
            # Phase 14: no new ground gained only if the call's range sits
            # entirely inside what's already covered.
            fully_covered = start_line >= group["start"] and end_line <= group["end"]
            # Extend to the union so a slowly-drifting window keeps landing
            # in this same group on every subsequent overlapping call.
            group["start"] = min(group["start"], start_line)
            group["end"] = max(group["end"], end_line)
            return group["sig"], not fully_covered

    sig = f"read_file_chunk:{project}:{file_path}:{start_line}-{end_line}"
    groups.append({"start": start_line, "end": end_line, "sig": sig})
    return sig, True


# ==========================================
# Phase 8: tie <verify> to the plan's stated sub-goals
# check_verify_block (Phase 2) only checks grounding — every claim cites a
# real tool-result id. It has no notion of the original question's
# sub-goals, so a <verify> block that silently drops one of several
# required findings (even while the model's own plan named it) still
# passes as a complete, finished answer. This closes that gap: the
# planning turn's <goals> block (see build_system_instructions) is parsed
# and kept for the question, then cross-checked against the final
# <verify> block's claim lines specifically — not the surrounding prose —
# once grounding has already passed.
# ==========================================
_GOALS_BLOCK_RE = re.compile(r'<goals>\s*(.*?)\s*</goals>', re.DOTALL)

# Fraction of a goal's significant terms that must show up in the verify
# claims text for that goal to count as addressed. Deliberately not 1.0 —
# the model will paraphrase its own stated goal when it writes the claim.
_GOAL_COVERAGE_MIN_RATIO = 0.4

# How many times start_interactive_chat will nudge the model to address a
# missing sub-goal before accepting its next answer as final. Raised from a
# one-shot nudge to 3 — one nudge often wasn't enough room for the model to
# actually change strategy (e.g. try outline_file) instead of repeating the
# same failed keyword search.
_MAX_GOAL_COVERAGE_RETRIES = 3

# A claim that reuses a goal's own wording can pass the keyword-overlap
# check below while actually being a give-up, not a resolution — e.g. "the
# delayed raw pixel-byte fetch method could not be resolved from the
# inspected snippets" scores high on overlap with a goal about that same
# fetch method. Any of these phrases in the claim(s) addressing a goal
# means that goal is NOT covered, regardless of overlap ratio.
_GIVE_UP_PHRASES = (
    "could not be resolved",
    "not found",
    "unresolved",
    "unable to locate",
    "not conclusively identified",
    "not identified",
    "no definitive",
)


def _parse_goal_lines(goals_body):
    """Each non-empty line inside a <goals> block is one stated sub-goal,
    with an optional leading bullet/number marker stripped."""
    goals = []
    for line in goals_body.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r'^[-*\u2022]\s*', '', line)
        line = re.sub(r'^\d+[.)]\s*', '', line)
        if line:
            goals.append(line)
    return goals


def _best_matching_claims(terms, claims):
    """The claim line(s) with the HIGHEST term-overlap count for this one
    goal's terms — i.e. the claim(s) that most plausibly address it — not
    just any claim sharing even a single incidental word. Using "any
    shared word" instead of "best match" lets one unrelated claim
    (e.g. a give-up on a DIFFERENT goal that happens to share a common
    word) contaminate this goal's check. Returns [] if no claim shares
    any term at all."""
    scored = [(sum(1 for t in terms if t in c.lower()), c) for c in claims]
    best_hits = max((h for h, _ in scored), default=0)
    if best_hits == 0:
        return []
    return [c for h, c in scored if h == best_hits]


def find_uncovered_goals(assistant_reply, goals):
    """Returns the subset of `goals` with no real lexical trace in the
    final <verify> block's claim lines, OR whose only supporting claim(s)
    are a hedge/give-up rather than an actual resolution. A goal only
    counts as addressed if there's a real, non-give-up claim backing it —
    dropping a goal silently, or "resolving" it with an admission of
    failure that happens to reuse the goal's own wording, must not pass
    just because the rest of the verify block is well-grounded elsewhere."""
    match = _VERIFY_BLOCK_RE.search(assistant_reply)
    claims = _parse_verify_claims(match.group(1)) if match else []
    claims_text_lower = "\n".join(claims).lower()

    missing = []
    for goal in goals:
        terms = set(_significant_terms(goal))
        if not terms:
            continue
        hits = sum(1 for t in terms if t in claims_text_lower)
        if (hits / len(terms)) < _GOAL_COVERAGE_MIN_RATIO:
            missing.append(goal)
            continue

        # Covered by keyword overlap — but check whether the claim(s) that
        # BEST address this specific goal (not just any claim sharing an
        # incidental word) are a give-up rather than a resolution. Fall
        # back to the whole claims block only if none can be tied to it.
        addressing = _best_matching_claims(terms, claims)
        search_space = " ".join(addressing).lower() if addressing else claims_text_lower
        if any(phrase in search_space for phrase in _GIVE_UP_PHRASES):
            missing.append(goal)
    return missing


# ==========================================
# MAIN INTERACTIVE LOOP
# ==========================================
def start_interactive_chat(access_token, chosen_model):
    print(f"\n=== Chat Session Started (Model: {chosen_model}) ===")
    print("Type 'quit' or 'exit' to stop.\n")

    # Phase 1: the permanent, untouched record of every message. Compaction
    # (see context.compact_if_needed) only ever shortens what gets SENT to
    # the model each turn — it never modifies or drops anything from here.
    full_transcript = []
    # What actually gets sent — may be replaced with a compacted copy.
    working_messages = []

    # Phase 13 (mission's "Phase 11"): id -> {"tool", "args", "result"} for
    # EVERY tool call executed so far this SESSION (not just this
    # question), so <verify> claims can be checked against real evidence.
    # This must live at the same scope as full_transcript/working_messages
    # above — and NOT be reset per-investigation like repeated_call_counts/
    # current_goals further down — because the model can still see an
    # earlier investigation's <result id="t-N"> text in working_messages/
    # full_transcript (neither of those resets on a new question either).
    # Resetting this map on every new question previously made
    # check_verify_block reject a citation against a real, still-visible
    # id from an earlier question in the same session with a misleading
    # "not a tool result from this conversation" error. next_tool_id must
    # likewise keep incrementing across questions rather than resetting to
    # 1, so ids stay unique for the whole session — otherwise two different
    # investigations could mint the same id for two different results, and
    # a citation could silently validate against the wrong one.
    #
    # Phase 14 (mission's "Phase 13", fix 3 — closes out the Phase 13/"Phase
    # 11" flag above): intentionally decoupled from context.compact_if_needed
    # and staying that way. check_verify_block checks a citation against the
    # result text stored HERE, never against working_messages, so a message
    # being folded into a compacted summary (which only ever shortens what's
    # SENT to the model) doesn't change whether the evidence backing an id is
    # still real — evicting compacted-away ids would reject valid citations
    # just because the *display* got compacted, trading a false negative for
    # a fake one.
    tool_result_map = {}
    next_tool_id = 1

    while True:
        try:
            user_input = input("\n\U0001f9d1 You: ")
            if user_input.strip().lower() in ['quit', 'exit']:
                print("Goodbye!")
                break
            if not user_input.strip():
                continue

            user_msg = {"role": "user", "content": user_input}
            full_transcript.append(user_msg)
            working_messages.append(user_msg)

            is_new_investigation = True   # only this question's first call plans deeply
            verify_retry_count = 0        # small counter — allow up to _MAX_VERIFY_RETRIES nudges

            # Phase 8: sub-goals stated in this question's planning turn (parsed out
            # of its <goals> block below), and how many goal-coverage nudges have
            # been used so far — separate counter from verify_retry_count above,
            # since grounding and goal-coverage are different failure modes. Raised
            # from a one-shot boolean to a small counter (_MAX_GOAL_COVERAGE_RETRIES)
            # since a single nudge wasn't enough room for the model to actually
            # change strategy (e.g. try outline_file) instead of repeating itself.
            current_goals = []
            goal_coverage_retry_count = 0

            # NOTE: tool_result_map / next_tool_id are intentionally NOT
            # reset here — see the Phase 13 comment where they're
            # initialized above the outer `while True` loop. They persist
            # for the whole session, unlike the per-question state below.

            # Phase 5b: (tool, args) signature -> how many times it's been
            # called so far this question, across turns (not just within
            # one batch, which is already deduped above).
            repeated_call_counts = {}

            # Phase 12: (project, file_path) -> list of {"start", "end", "sig"}
            # groups seen so far this question, so _call_signature can fold
            # overlapping read_file_chunk ranges into one repeat-count bucket
            # instead of treating each slightly-different range as new. Reset
            # per question, same as repeated_call_counts above.
            read_chunk_groups = {}

            # THE ReAct LOOP (Synthetic User Message Architecture)
            while True:
                # Phase 1: compact the working list if it's grown past budget.
                # `full_transcript` is untouched no matter what happens here.
                working_messages = context.compact_if_needed(working_messages)

                assistant_reply = chat_with_codex(access_token, chosen_model, working_messages, is_new_investigation)
                if not assistant_reply:
                    break

                assistant_msg = {"role": "assistant", "content": assistant_reply}
                full_transcript.append(assistant_msg)
                working_messages.append(assistant_msg)
                is_new_investigation = False

                # Phase 8: capture this question's stated sub-goals the moment they're
                # planned, whichever turn that happens on. No-op when absent (most
                # turns, since only the planning turn emits <goals>).
                goals_match = _GOALS_BLOCK_RE.search(assistant_reply)
                if goals_match:
                    current_goals = _parse_goal_lines(goals_match.group(1))

                # Extract every <tools> block in the reply (usually one, but merge
                # instead of silently dropping extras if the model ever emits more
                # than one in a single turn).
                tools_blocks = re.findall(r'<tools>\s*(.*?)\s*</tools>', assistant_reply, re.DOTALL)

                if tools_blocks:
                    try:
                        tools_array = []
                        for block in tools_blocks:
                            tools_array.extend(json.loads(block))
                        if len(tools_blocks) > 1:
                            print(f"\n[i] Reply had {len(tools_blocks)} <tools> blocks — merged into one batch.")

                        # De-duplicate identical (tool, args) pairs within this batch —
                        # no reason to pay for the same call twice in one turn.
                        seen_calls = set()
                        deduped = []
                        for tool in tools_array:
                            sig = (tool.get("_mcp_tool"), json.dumps(tool.get("_mcp_args", {}), sort_keys=True))
                            if sig in seen_calls:
                                continue
                            seen_calls.add(sig)
                            deduped.append(tool)
                        tools_array = deduped

                        # Use XML tags to define boundaries exactly like VSCode
                        aggregated_results = ["<tool_results>"]
                        # Phase 5b: signatures that just crossed a repeat threshold
                        # in this batch, to warn about after results are assembled.
                        stuck_signatures = []

                        # Execute all requested tools in parallel/sequentially
                        for tool in tools_array:
                            t_name = tool.get("_mcp_tool")
                            t_args = tool.get("_mcp_args", {})

                            # Phase 5b (extended by Phase 12 for read_file_chunk
                            # range overlap-folding): count this call across
                            # the whole question, not just this batch. Phase 14
                            # (mission's "Phase 13", fix 2): a call that made
                            # progress (see _call_signature) resets the count
                            # instead of incrementing it, since forward
                            # progress is the opposite of being stuck.
                            sig, made_progress = _call_signature(t_name, t_args, read_chunk_groups)
                            if made_progress:
                                repeated_call_counts[sig] = 1
                            else:
                                repeated_call_counts[sig] = repeated_call_counts.get(sig, 0) + 1
                            call_count = repeated_call_counts[sig]
                            if call_count >= _STUCK_LOOP_THRESHOLD and call_count % _STUCK_LOOP_THRESHOLD == 0:
                                stuck_signatures.append((t_name, t_args, call_count))

                            is_valid, err = validate_tool_call(t_name, t_args)
                            if not is_valid:
                                t_result = f"VALIDATION ERROR (tool NOT executed): {err}"
                            else:
                                t_result = execute_mcp_tool(t_name, t_args)
                                if t_name == "list_projects" and "Error" not in t_result:
                                    note_known_projects(t_result)
                                elif "Error" not in t_result and t_args.get("project"):
                                    # Phase 7: first touch of this project this session —
                                    # probe its real graph relationship types once, best-
                                    # effort, so later relationship questions don't have to
                                    # guess Cypher edge names. No-op on repeat calls (see
                                    # ensure_graph_schema_known's own attempted-set guard).
                                    ensure_graph_schema_known(t_args["project"])

                                # Phase 5a: an empty/no-match result is easy to skim
                                # past — attach concrete next-step guidance right
                                # where the model will read it.
                                if "Error" not in t_result and _looks_like_empty_result(t_result):
                                    t_result = f"{t_result}\n\n[GUIDANCE: {_empty_result_guidance(t_name)}]"
                                # Phase 5c: not empty, but too thin to cite with
                                # confidence — flag it the same way so the model
                                # doesn't force a citation out of a weak match.
                                elif "Error" not in t_result and _looks_like_weak_result(t_name, t_args, t_result):
                                    t_result = f"{t_result}\n\n[GUIDANCE: {_weak_result_guidance(t_name)}]"

                            # Phase 2: assign this call a short id and remember it so
                            # <verify> claims can be checked against real evidence.
                            tool_id = f"t-{next_tool_id}"
                            next_tool_id += 1
                            tool_result_map[tool_id] = {
                                "tool": t_name,
                                "args": t_args,
                                "result": t_result,
                            }

                            # Wrap individual results to prevent context confusion
                            aggregated_results.append(f'  <result id="{tool_id}" tool="{t_name}">\n{t_result}\n  </result>')

                        aggregated_results.append("</tool_results>")

                        # Feed the synthetic user message back into the loop silently
                        synthetic_user_message = "\n".join(aggregated_results)
                        synth_msg = {"role": "user", "content": synthetic_user_message}
                        full_transcript.append(synth_msg)
                        working_messages.append(synth_msg)
                        verify_retry_count = 0

                        # Phase 5b: the same exact call repeating across turns means
                        # the model is stuck, not making progress — force a change
                        # of approach instead of letting it keep spending turns/RPM.
                        if stuck_signatures:
                            warning_lines = []
                            for sig_name, sig_args, count in stuck_signatures:
                                if sig_name == "read_file_chunk":
                                    # Phase 12: this count may include overlapping-but-not-identical
                                    # ranges folded together by _call_signature, not just byte-identical
                                    # repeats, so say so rather than implying the args never changed.
                                    action_desc = "re-read overlapping/drifting line ranges over the same region"
                                else:
                                    action_desc = "been called with these same args"
                                warning_lines.append(
                                    f'- `{sig_name}` has {action_desc} {count} times this question '
                                    f'(most recently: {json.dumps(sig_args)}) with no new information '
                                    'gained. Stop repeating this — change your approach.'
                                )
                            print(f"\n[i] Stuck-loop guard triggered for {len(stuck_signatures)} repeated call(s).")
                            stuck_msg = {
                                "role": "user",
                                "content": (
                                    "<system_warning>\n" + "\n".join(warning_lines) + "\n"
                                    "Change your approach: try different search terms, a different tool "
                                    "(search_code vs search_graph), re-check the project name, or "
                                    "conclude from what you already have instead of repeating this call.\n"
                                    "</system_warning>"
                                )
                            }
                            full_transcript.append(stuck_msg)
                            working_messages.append(stuck_msg)
                    except json.JSONDecodeError:
                        print("\n[!] AI outputted malformed JSON in <tools>. Retrying...")
                        malformed_msg = {
                            "role": "user",
                            "content": "<system_error>The JSON array inside <tools> was malformed. Please fix the JSON syntax and try again.</system_error>"
                        }
                        full_transcript.append(malformed_msg)
                        working_messages.append(malformed_msg)
                else:
                    # No <tools> tag — the model believes it's done investigating.
                    # Phase 2: check the <verify> block for real evidence, not just presence.
                    ok, detail = check_verify_block(assistant_reply, tool_result_map)
                    if not ok and verify_retry_count < _MAX_VERIFY_RETRIES:
                        verify_retry_count += 1
                        print(f"\n[i] Verify check failed ({detail}) — requesting a corrected one...")
                        # Phase 12: point at a concrete next action instead of a bare "fix it" —
                        # a call-relationship claim that fails grounding is exactly the failure
                        # mode this exists for (see build_system_instructions rule 13's
                        # wrapper-method continuation note). Don't leave the model free to just
                        # reword or hedge on the same unsupported claim; send it back to keep
                        # reading instead of repeating the question to itself.
                        if _mentions_call_relationship(detail):
                            action_hint = (
                                "This looks like a call-relationship claim (\"does A call B\") that "
                                "isn't actually backed by evidence yet. Don't just reword or hedge on "
                                "it — open the enclosing method around your most recent relevant "
                                "read_file_chunk (or get_code_snippet) result and read what its body "
                                "actually does; if that body is itself just a call to another method, "
                                "that method is now your new target. Keep tracing one method further "
                                "before answering again."
                            )
                        else:
                            action_hint = (
                                "Find a real tool result that actually supports the claim, or drop "
                                "it, then give your final answer again."
                            )
                        retry_msg = {
                            "role": "user",
                            "content": (
                                "<system_check>Your <verify> block did not pass evidence checking: "
                                f"{detail} Every claim must end with the id(s) (e.g. t-3) of a tool "
                                "result that both exists and actually supports it, copied exactly "
                                "from the <result id=\"...\"> tags you were given earlier. "
                                f"{action_hint}</system_check>"
                            )
                        }
                        full_transcript.append(retry_msg)
                        working_messages.append(retry_msg)
                        continue

                    # Phase 8: grounding passed — now check the answer actually
                    # covers every sub-goal the model itself planned for. Up to
                    # _MAX_GOAL_COVERAGE_RETRIES nudges (separate counter from
                    # verify_retry_count above); whatever comes back after that
                    # is accepted as final.
                    if current_goals and goal_coverage_retry_count < _MAX_GOAL_COVERAGE_RETRIES:
                        missing_goals = find_uncovered_goals(assistant_reply, current_goals)
                        if missing_goals:
                            goal_coverage_retry_count += 1
                            print(
                                f"\n[i] {len(missing_goals)} planned sub-goal(s) missing from <verify> — "
                                f"requesting completion (attempt {goal_coverage_retry_count}/{_MAX_GOAL_COVERAGE_RETRIES})..."
                            )
                            missing_lines = "\n".join(f"- {g}" for g in missing_goals)
                            # The 1st nudge just restates the ask. From the 2nd nudge
                            # onward, a repeat means the model's first strategy already
                            # failed once — point it explicitly at outline_file instead
                            # of letting it silently repeat the same keyword search.
                            if goal_coverage_retry_count == 1:
                                strategy_hint = (
                                    "Either investigate and add grounded claims for each (with real "
                                    "tool-result ids), or explicitly state in your answer that they "
                                    "couldn't be resolved and why."
                                )
                            else:
                                strategy_hint = (
                                    "Don't just repeat the same keyword search again — that already failed "
                                    "once. If you already know a file is relevant (e.g. from an earlier "
                                    "finding), call `outline_file` on it to see every method/function/"
                                    "class/interface signature in the file in one call — the thing you're "
                                    "looking for may not share any vocabulary with how you've been "
                                    "searching for it. Only fall back to stating a goal is unresolved after "
                                    "trying that."
                                )
                            goal_msg = {
                                "role": "user",
                                "content": (
                                    "<system_check>Your own plan named these sub-goals, but your <verify> "
                                    f"block doesn't address them:\n{missing_lines}\n"
                                    f"{strategy_hint} Then give your final answer again.</system_check>"
                                )
                            }
                            full_transcript.append(goal_msg)
                            working_messages.append(goal_msg)
                            continue
                    break

        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
