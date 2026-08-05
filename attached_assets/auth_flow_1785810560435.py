import base64
import hashlib
import os
import urllib.parse
import webbrowser
import requests
import json
import subprocess
import time
import re
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# ARCHITECTURE NOTES (read me before editing)
# ------------------------------------------
# 1. PLANNER MERGED INTO THE WORKER: the old code made a dedicated
#    "planner" API call before every single worker turn. That's now
#    folded into the worker's own first <thought> for a new question
#    (see `is_new_investigation` / build_system_instructions). Same
#    depth of planning, one fewer network round-trip per question.
# 2. SESSION CACHE: facts like the project list rarely change mid
#    session. Once learned, they're injected straight into the prompt
#    (`known_context`) so the model never spends a call re-discovering
#    them, and the raw MCP call itself is memoized (`SessionCache`).
# 3. LOCAL VALIDATION: tool calls are checked against a real schema in
#    Python *before* they reach the MCP subprocess. A bad call is
#    rejected for free instead of costing a subprocess call plus a
#    confusing error the model has to spend a turn recovering from.
# 4. SELF-VERIFY: before accepting a final answer, the worker must
#    justify each claim against a tool result. If it forgets, exactly
#    one corrective turn is requested — so validation costs 0 extra
#    RPM in the normal case and at most 1 when something looks off.
# ==========================================

# ==========================================
# CONFIGURATION
# ==========================================
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{PORT}{CALLBACK_PATH}"
AUTH_URL_BASE = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
TOKEN_FILE = "auth_session.json"

# --- MCP & Rate Limiting Config ---
MCP_EXE_PATH = r"C:\Users\Akhil S Kumar\Downloads\codebase-memory-mcp-ui-windows-amd64\codebase-memory-mcp.exe"
MAX_RPM = 30  

# GLOBAL STATE
auth_code = None
expected_state = None

# ==========================================
# 0. RATE LIMITER (RPM MANAGER)
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
# 1. OAUTH CALLBACK SERVER
# ==========================================
class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        global auth_code, expected_state
        parsed_url = urllib.parse.urlparse(self.path)
        
        if parsed_url.path == CALLBACK_PATH:
            query_components = urllib.parse.parse_qs(parsed_url.query)
            if 'code' in query_components:
                returned_state = query_components.get('state', [''])[0]
                if returned_state != expected_state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authentication failed: State mismatch.")
                    return
                auth_code = query_components['code'][0]
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                success_html = "<html><body><h2>Authentication Successful!</h2><p>You can safely close this window.</p></body></html>"
                self.wfile.write(success_html.encode('utf-8'))
            else:
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

def perform_login():
    global auth_code, expected_state
    auth_code = None
    print("[*] Starting new browser login flow...")
    
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    sha256_hash = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(sha256_hash).decode('utf-8').rstrip('=')
    expected_state = os.urandom(16).hex()

    auth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile email offline_access",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": expected_state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "custom_python_script" 
    }
    
    auth_url = f"{AUTH_URL_BASE}?{urllib.parse.urlencode(auth_params)}"
    server = HTTPServer(('localhost', PORT), CallbackHandler)
    webbrowser.open(auth_url)
    
    while not auth_code:
        server.handle_request()
    
    print("[+] Exchanging code for tokens...")
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier
    }
    response = requests.post(TOKEN_URL, data=token_payload)
    if response.status_code == 200:
        return response.json()
    return None

def refresh_access_token(refresh_token):
    payload = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    response = requests.post(TOKEN_URL, data=payload)
    return response.json() if response.status_code == 200 else None

def fetch_available_model(access_token):
    url = "https://chatgpt.com/backend-api/codex/models?client_version=0.146.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers)
    if response.status_code == 401:
        return None, False 
    print("[*] Enforcing model: gpt-5.4-mini")
    return "gpt-5.4-mini", True 

# ==========================================
# 2. SESSION CACHE & MEMORY
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
CACHEABLE_TOOLS = {"list_projects", "get_architecture"}  # stable for the life of a session

known_context = {"projects": None}

def note_known_projects(list_projects_result_text):
    """Called once list_projects succeeds. Later turns get the project
    list injected directly into their system prompt instead of paying
    a full round-trip to ask for it again."""
    known_context["projects"] = list_projects_result_text

# ==========================================
# MCP TOOL EXECUTOR
# ==========================================
def execute_mcp_tool(tool_name, args_dict):
    if tool_name in CACHEABLE_TOOLS:
        cached = session_cache.get(tool_name, args_dict)
        if cached is not None:
            print(f"\n   [⚡ {tool_name} — reused from session cache]", end="", flush=True)
            return cached
    try:
        print(f"\n   [⚡ Executing: {tool_name}]...", end="", flush=True)
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
"""

# ==========================================
# 3. TOOL CALL VALIDATION
# Catches a malformed or hallucinated tool call in Python, before it
# ever reaches a subprocess call. Rejecting it here costs nothing; an
# uncaught bad call costs an MCP subprocess invocation *and* pollutes
# context with a confusing error that usually eats an extra round-trip
# to recover from anyway.
# ==========================================
TOOL_REQUIRED_ARGS = {
    "list_projects": [],
    "get_architecture": ["project"],
    "search_code": ["project", "pattern"],
    "get_code_snippet": ["project", "qualified_name"],
    "search_graph": ["project", "query"],
    "query_graph": ["project", "query"],
}

def validate_tool_call(tool_name, args_dict):
    """Local, zero-cost validation against the schema above.
    Returns (is_valid, error_message)."""
    if not isinstance(args_dict, dict):
        return False, "_mcp_args must be a JSON object."
    if tool_name not in TOOL_REQUIRED_ARGS:
        return False, f"Unknown tool '{tool_name}'. Valid tools: {sorted(TOOL_REQUIRED_ARGS)}"
    missing = [a for a in TOOL_REQUIRED_ARGS[tool_name] if not args_dict.get(a)]
    if missing:
        return False, f"Tool '{tool_name}' is missing required argument(s): {missing}"
    if "project" in TOOL_REQUIRED_ARGS[tool_name] and args_dict.get("project") in ("*", ""):
        return False, f"Tool '{tool_name}' needs a real project name, not a wildcard or blank value."
    return True, None

# ==========================================
# 4. THE AGENT (plans + acts in one call)
# Previously this was two agents / two API calls: a "planner" that
# only produced a plan, and a "worker" that received the plan as a
# string and acted on it. They're merged here — see ARCHITECTURE
# NOTES at the top of the file for why.
# ==========================================
def build_system_instructions(is_new_investigation):
    known_block = ""
    if known_context["projects"]:
        known_block = (
            "\nALREADY KNOWN THIS SESSION — do NOT call list_projects again, reuse this:\n"
            f"{known_context['projects']}\n"
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
Then take your first action from that plan.
"""

    return f"""You are an elite Senior Software Engineer diagnosing a codebase via read-only tools.
{known_block}
{TOOL_SCHEMAS}
{planning_block}
CRITICAL EXECUTION RULES:
1. THINK FIRST: You MUST output a `<thought>...</thought>` block explaining your logic before taking action or giving a final answer.
2. PARALLEL TOOLS: To run tools, output a `<tools>...</tools>` block containing a JSON ARRAY of tool calls. Combine tool calls when they don't depend on each other.
3. DEPENDENCY AWARENESS: If the project isn't already given above under ALREADY KNOWN, your ONLY tool call must be `list_projects` — do not bundle guessed `search_code`/`search_graph` calls alongside it. Once you know the project, always use it exactly as given; never pass `"project": "*"` or leave it blank.
4. QUALIFIED NAMES: `search_code`/`search_graph` results include a `qualified_name` field, e.g. `"qualified_name": "C-JViewer.weasis-dicom.weasis-dicom-viewer2d.src.main.java.org.weasis.dicom.viewer2d.View2d.computeCrosshair"`. To call `get_code_snippet`, copy that field's value EXACTLY, character for character. Never guess it, and never append anything to it — no ` = ...`, no method bodies, no extra code or expressions. If no `qualified_name` field is in front of you for what you need, search again instead of constructing one yourself.
5. NO RAW CODE IN CYPHER: Never put raw multi-line Java code into a `query_graph` query.
6. THE LOOP: When you run tools, the system will instantly reply with a `<tool_results>` block. Read it and continue your investigation automatically until you have the final answer. Do not ask the user for permission.
7. VALIDATE BEFORE FINISHING: Only on the turn where you are NOT calling more tools, output a `<verify>` block listing each factual claim you're about to make next to the exact tool result (qualified_name / file / project) that supports it. Drop or re-investigate anything you can't point to evidence for. Don't include a `<verify>` block on turns where you're still calling tools.
8. LANGUAGE: Always respond strictly in English.
9. TOOL PREFERENCE: When looking for how things work, prefer `search_graph` to find underlying core engine/codec implementations.

Example Tool Invocation:
<thought>I need to search for the Main class.</thought>
<tools>
[
    {{"_mcp_tool": "search_graph", "_mcp_args": {{"project": "C-JViewer", "query": "main", "label": "Class", "limit": 5}}}}
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
    
    print("\n🤖 AI: ", end="", flush=True)

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
# MAIN INTERACTIVE LOOP
# ==========================================
def start_interactive_chat(access_token, chosen_model):
    print(f"\n=== Chat Session Started (Model: {chosen_model}) ===")
    print("Type 'quit' or 'exit' to stop.\n")
    
    messages = []
    
    while True:
        try:
            user_input = input("\n🧑 You: ")
            if user_input.strip().lower() in ['quit', 'exit']:
                print("Goodbye!")
                break
            if not user_input.strip():
                continue
                
            messages.append({"role": "user", "content": user_input})

            is_new_investigation = True   # only this question's first call plans deeply
            verify_retry_used = False     # allow exactly one nudge if <verify> is missing

            # THE ReAct LOOP (Synthetic User Message Architecture)
            while True:
                assistant_reply = chat_with_codex(access_token, chosen_model, messages, is_new_investigation)
                if not assistant_reply:
                    break

                messages.append({"role": "assistant", "content": assistant_reply})
                is_new_investigation = False
                
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
                        
                        # Execute all requested tools in parallel/sequentially
                        for tool in tools_array:
                            t_name = tool.get("_mcp_tool")
                            t_args = tool.get("_mcp_args", {})

                            is_valid, err = validate_tool_call(t_name, t_args)
                            if not is_valid:
                                t_result = f"VALIDATION ERROR (tool NOT executed): {err}"
                            else:
                                t_result = execute_mcp_tool(t_name, t_args)
                                if t_name == "list_projects" and "Error" not in t_result:
                                    note_known_projects(t_result)

                            # Wrap individual results to prevent context confusion
                            aggregated_results.append(f'  <result tool="{t_name}">\n{t_result}\n  </result>')
                        
                        aggregated_results.append("</tool_results>")
                        
                        # Feed the synthetic user message back into the loop silently
                        synthetic_user_message = "\n".join(aggregated_results)
                        messages.append({
                            "role": "user", 
                            "content": synthetic_user_message
                        })
                        verify_retry_used = False
                    except json.JSONDecodeError:
                        print("\n[!] AI outputted malformed JSON in <tools>. Retrying...")
                        messages.append({
                            "role": "user",
                            "content": "<system_error>The JSON array inside <tools> was malformed. Please fix the JSON syntax and try again.</system_error>"
                        })
                else:
                    # No <tools> tag — the model believes it's done investigating.
                    if "<verify>" not in assistant_reply and not verify_retry_used:
                        verify_retry_used = True
                        print("\n[i] No self-check found — requesting one before accepting the final answer...")
                        messages.append({
                            "role": "user",
                            "content": "<system_check>Before finalizing, add a <verify> block listing each claim next to the tool result that supports it. Drop or re-investigate anything unsupported, then give your final answer.</system_check>"
                        })
                        continue
                    break
            
        except KeyboardInterrupt:
            print("\nGoodbye!")
            break

def main():
    tokens = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            tokens = json.load(f)
            
    if tokens and 'access_token' in tokens:
        chosen_model, is_valid = fetch_available_model(tokens['access_token'])
        if not is_valid and 'refresh_token' in tokens:
            new_tokens = refresh_access_token(tokens['refresh_token'])
            if new_tokens:
                if 'refresh_token' not in new_tokens:
                    new_tokens['refresh_token'] = tokens['refresh_token']
                with open(TOKEN_FILE, 'w') as f:
                    json.dump(new_tokens, f)
                chosen_model, _ = fetch_available_model(new_tokens['access_token'])
                if chosen_model:
                    start_interactive_chat(new_tokens['access_token'], chosen_model)
                return
            else:
                tokens = None 
        elif is_valid and chosen_model:
            start_interactive_chat(tokens['access_token'], chosen_model)
            return
    
    if not tokens:
        tokens = perform_login()
        if tokens:
            with open(TOKEN_FILE, 'w') as f:
                json.dump(tokens, f)
            chosen_model, _ = fetch_available_model(tokens['access_token'])
            if chosen_model:
                start_interactive_chat(tokens['access_token'], chosen_model)

if __name__ == "__main__":
    main()