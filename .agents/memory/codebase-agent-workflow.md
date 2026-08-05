---
name: codebase_agent feature-request workflow
description: How the user requests upgrades to codebase_agent/ (the local Python CLI), and how to read the attached files they paste in.
---

The user iterates on `codebase_agent/` (a local-only Python ReAct-agent CLI — see replit.md's "Where things live") by running it against real codebases on their own machine, then pasting results back as `attached_assets/*.txt` files with a short instruction like "just add these features". Expect two shapes together in one request:

- A "mission" file: a narrative bug report or test result (e.g. "we ran a head-to-head test and here's why it failed") ending in a concrete, numbered ask.
- One or more "current state" files: a full dump of a source file as it exists now (or as a prior AI pass produced it) — this is the base to reconcile against, not just background reading.

**Why:** these look like generic pasted logs at a glance, but they are the literal task spec. Skimming them as "context" instead of reading them fully produces the wrong deliverable. Diff the dumped file(s) against the real files in `codebase_agent/` before writing code — they are usually identical or very close; the real repo file wins if they differ.

**How to apply:** whenever new `attached_assets/*.txt` files show up alongside a short instruction, read every attached file in full before planning any change.

## Verifying changes (can't run the CLI itself here)

`codebase_agent` can't be executed end-to-end in Replit — it shells out to a Windows `.exe` (`MCP_EXE_PATH`) and needs a real OAuth login. Verify logic changes by importing the module directly and exercising the pure-Python functions in isolation instead (e.g. `python3 -c` / a scratch script): monkeypatch `mcp_tools.PROJECT_ROOTS["TestProj"] = tmpdir` to point at a throwaway temp dir with hand-written sample files, then call internal functions (`_outline_file`, `_find_symbol_usages`, `find_uncovered_goals`, etc.) directly and assert on the output. `jsonschema`/`requests`/`tiktoken` (declared in root `pyproject.toml`) usually aren't installed in a fresh Replit env yet — install them first or the imports fail.

**Why:** this is the only way to catch real logic bugs pre-handoff. It already caught one: an early version of a "does this claim address that goal" heuristic used "shares ANY keyword" to pick which claim to inspect, which let one unrelated claim's hedge/give-up phrase leak into and falsely fail a totally different, actually-answered goal just because they shared one incidental word (e.g. "raw", "exact"). Prefer a "best (highest) keyword-overlap claim wins" match over "any claim sharing a word" whenever heuristically tying a free-text claim/result back to one of several similar goals/queries.

## Phase numbering can collide

The mission file's own "Phase N" labels are the user's private running count from their own notes, not synced with what's already landed in the repo — a mission asking for "Phase 9" and "Phase 10" showed up when `agent.py`/`mcp_tools.py` already had their own Phase 9 and 10 comments from a prior pass.

**Why:** silently reusing an already-used phase number makes the code comments describe two unrelated things under one label, and risks an agent thinking the requested work is already done (or redoing it).

**How to apply:** before implementing, `grep -n "Phase [0-9]" codebase_agent/*.py` to find the highest phase number already landed, renumber the new mission's phases to continue after it, and say so in the added code comments (e.g. "Phase 12 (mission's 'Phase 10')") so the mapping is traceable later.
