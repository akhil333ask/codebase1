# [Project name]

_Replace the heading above with the project's name, and this line with one sentence describing what this app does for users._

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `codebase_agent/` — a separate, local-only Python CLI (not a Replit artifact/workflow). A ReAct-loop agent that chats with Codex over OAuth and inspects an arbitrary codebase via an external MCP binary (`config.py`'s `MCP_EXE_PATH`, a Windows executable run on the user's own machine). The user runs this outside Replit; it isn't wired to a workflow here.
  - `agent.py` — system instructions + the ReAct loop (tool dispatch, context compaction, `<verify>` evidence checking, stuck-loop detection).
  - `mcp_tools.py` — tool schemas/validation and `execute_mcp_tool`. Most tools shell out to `MCP_EXE_PATH`; `grep_search`/`read_file_chunk`/`find_symbol_usages`/`outline_file` are native Python fallbacks that read project files directly off disk instead (see Gotchas). `outline_file` lists every method/function/class/interface signature in one file (regex-heuristic, not a real parser) — the fallback of last resort when a concept shares no vocabulary with the real identifier name, since it needs no guessed keyword at all.
  - `context.py` — token-aware compaction of the working message list.
  - `auth.py` / `main.py` — OAuth login flow and entrypoint.
- _Populate the rest as you build the pnpm-workspace side — short repo map plus pointers to the source-of-truth file for DB schema, API contracts, theme files, etc._

## Architecture decisions

_Populate as you build — non-obvious choices a reader couldn't infer from the code (3-5 bullets)._

## Product

_Describe the high-level user-facing capabilities of this app once they exist._

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- `codebase_agent`'s native fallback tools (`grep_search`, `read_file_chunk`, `find_symbol_usages`, `outline_file`) need to know where a "project" actually lives on disk (the MCP binary never exposes that mapping back to this script). Configure `PROJECT_ROOTS` (or `WORKSPACE_ROOT`) in `codebase_agent/config.py` per project name exactly as `list_projects` reports it — otherwise those tools return a clear "no local path configured" error instead of silently searching nothing.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
