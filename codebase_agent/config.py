# ==========================================
# config.py — all constants in one place.
# Nothing in here changes behavior; values are copied verbatim from the
# original single-file script.
# ==========================================

# --- OAuth / endpoint config (unchanged, out of scope for this pass) ---
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{PORT}{CALLBACK_PATH}"
AUTH_URL_BASE = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
TOKEN_FILE = "auth_session.json"

# --- MCP & rate limiting config (unchanged) ---
MCP_EXE_PATH = r"C:\Users\Akhil S Kumar\Downloads\codebase-memory-mcp-ui-windows-amd64\codebase-memory-mcp.exe"
MAX_RPM = 30
# NOTE: model/backend selection itself is intentionally left unchanged here
# (see auth.fetch_available_model / agent.chat_with_codex) — flagging, not
# changing. A non-mini/larger model is likely to sustain multi-step
# investigative strategy shifts (e.g. switching from keyword search to
# outline_file after search comes up empty) better than a mini model does,
# which is a plausible contributing factor when the model gives up on a
# sub-goal instead of trying a different tool.

# --- Phase 1: context compaction config ---
# tiktoken has no encoding registered for every model name; cl100k_base is
# used as a close-enough stand-in for token *counting* purposes only (it
# does not need to match the real Codex tokenizer exactly to be useful as
# a budget signal).
TOKEN_COUNT_ENCODING = "cl100k_base"

# Leave headroom under the real model context window — this is deliberately
# conservative since the running `messages` list is only part of the total
# request (system instructions + tool schemas also count against the window).
CONTEXT_TOKEN_BUDGET = 60000

# How many of the most recent messages to always keep verbatim (uncompacted)
# when the budget is exceeded.
CONTEXT_KEEP_RECENT_TURNS = 12

# --- Phase 6: native fallback tools (grep_search / read_file_chunk) config ---
# Every other tool sends an opaque `project` name to MCP_EXE_PATH, which
# owns the mapping from that name to an actual directory on disk. These two
# tools bypass the MCP binary and read source files directly, so — unlike
# every other tool — THIS script needs to know where each project lives.
#
# Fill in an entry for every project name (spelled exactly as `list_projects`
# reports it) you want grep_search/read_file_chunk to work against, e.g.:
#   PROJECT_ROOTS = {
#       "C-JViewer": r"C:\Users\Akhil S Kumar\Projects\weasis-dicom-viewer",
#   }
PROJECT_ROOTS = {
    "C-JViewer": r"C:\JViewer"
}

# Fallback when a project has no PROJECT_ROOTS entry: tried as
# WORKSPACE_ROOT/<project name>. Leave as None to require explicit entries.
WORKSPACE_ROOT = None

# Directory names skipped while walking a project tree — vendored/build
# output that would otherwise drown out real matches (and slow grep_search
# down considerably on large repos).
GREP_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target", ".idea", ".vscode"}

# Hard caps so a broad regex or a huge file can't blow up the context budget
# or hang the session.
GREP_MAX_RESULTS = 200
GREP_MAX_FILE_BYTES = 2_000_000  # skip files bigger than ~2MB when scanning
READ_FILE_CHUNK_MAX_LINES = 400
