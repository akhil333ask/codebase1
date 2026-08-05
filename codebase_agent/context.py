# ==========================================
# context.py — Phase 1: token-aware context compaction.
#
# Two data structures are involved wherever this module is used:
#
#   - `full_transcript`: every message ever appended, in order, kept
#     completely untouched. This is the permanent record — nothing here
#     is ever summarized or dropped.
#   - the "working" messages list that actually gets sent to
#     `chat_with_codex`: this is what `compact_if_needed` may shorten
#     once it grows past the token budget. Compaction only changes what
#     is *sent* to the model — the caller's `full_transcript` copy is
#     never mutated.
#
# Call `compact_if_needed(working_messages)` right before each
# `chat_with_codex` call and use its return value (a new list) as the
# `messages` argument for that call.
# ==========================================
import json
import re

from config import CONTEXT_KEEP_RECENT_TURNS, CONTEXT_TOKEN_BUDGET, TOKEN_COUNT_ENCODING

try:
    import tiktoken
    _ENCODING = tiktoken.get_encoding(TOKEN_COUNT_ENCODING)
except Exception:
    # If tiktoken (or its encoding data) is unavailable for any reason,
    # fall back to a deterministic character-based estimate rather than
    # crashing the whole agent over a token-counting nicety.
    _ENCODING = None


def _content_as_text(message):
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content)


def count_tokens(messages):
    """Estimate the token count of a `messages` list (role/content dicts)."""
    if _ENCODING is not None:
        total = 0
        for msg in messages:
            total += len(_ENCODING.encode(_content_as_text(msg))) + 4  # role/formatting overhead
        return total
    # ~4 characters per token is a standard rough estimate for English/code text.
    total_chars = sum(len(_content_as_text(msg)) for msg in messages)
    return total_chars // 4


_QUALIFIED_NAME_RE = re.compile(r'"qualified_name"\s*:\s*"([^"]+)"')
_FILE_HINT_RE = re.compile(r'\b[\w\-./]+\.(?:java|py|ts|tsx|js|jsx|go|rs|c|cpp|h|hpp|kt|rb)\b')


def _extract_facts(messages):
    """Deterministic summarization: pull out qualified_names, file-looking
    tokens, and a short excerpt of each message — no model call involved."""
    qualified_names = []
    files = []
    bullets = []

    for msg in messages:
        content = _content_as_text(msg)

        for qn in _QUALIFIED_NAME_RE.findall(content):
            if qn not in qualified_names:
                qualified_names.append(qn)
        for f in _FILE_HINT_RE.findall(content):
            if f not in files:
                files.append(f)

        role = msg.get("role", "unknown")
        excerpt = content.strip().replace("\n", " ")
        if len(excerpt) > 160:
            excerpt = excerpt[:160] + "\u2026"
        if excerpt:
            bullets.append(f"[{role}] {excerpt}")

    return qualified_names, files, bullets


# Absolute ceiling on the summary itself, independent of how much history it
# replaces. Without this, a deterministic "one bullet per message" summary
# of a long history can end up as big as (or bigger than) what it replaces
# — which would defeat the point of compacting at all.
_SUMMARY_TOKEN_CAP = 1500

# The summary must also shrink relative to what it's replacing, not just
# stay under the absolute ceiling — otherwise compacting a run of many
# *short* messages (little redundancy to exploit) could still net zero
# savings. Target at most half of the original token count, floored so
# tiny inputs still get a usable (if minimal) summary.
_SUMMARY_MIN_TARGET_TOKENS = 80
_SUMMARY_TARGET_FRACTION = 0.5


def summarize_deterministic(messages):
    """Turn a list of older messages into one compact system-style string.
    Pure extraction (facts / files / qualified_names / last open question) —
    no extra model call. This is the Phase 1 default; an LLM-based
    summarization fallback is intentionally NOT implemented yet, per the
    instruction to add it later only if this loses too much.

    Tries progressively more aggressive detail levels — fewer/shorter
    bullets, then no bullets at all, then counts-only — until the summary
    is both under the absolute cap AND meaningfully smaller than the
    messages it's replacing."""
    if not messages:
        return None

    original_tokens = count_tokens(messages)
    target_cap = min(_SUMMARY_TOKEN_CAP, max(_SUMMARY_MIN_TARGET_TOKENS, int(original_tokens * _SUMMARY_TARGET_FRACTION)))

    qualified_names, files, bullets = _extract_facts(messages)

    open_questions = [
        _content_as_text(m) for m in messages
        if m.get("role") == "user"
        and not _content_as_text(m).lstrip().startswith("<tool_results>")
        and not _content_as_text(m).lstrip().startswith("<system_")
    ]
    last_open_question = open_questions[-1] if open_questions else None
    if last_open_question and len(last_open_question) > 200:
        last_open_question = last_open_question[:200] + "\u2026"

    def build(bullet_limit, bullet_len, name_limit, file_limit, counts_only=False):
        lines = [
            "<compacted_context>",
            f"Summary of {len(messages)} earlier conversation message(s) (compacted to save space):",
        ]
        if counts_only:
            if files:
                lines.append(f"Files referenced earlier ({len(files)} total): " + ", ".join(files[-file_limit:]))
            if qualified_names:
                lines.append(f"Qualified names referenced earlier ({len(qualified_names)} total): " + ", ".join(qualified_names[-name_limit:]))
        else:
            if bullet_limit and bullets:
                lines.append("Key facts / exchanges:")
                for b in bullets[-bullet_limit:]:
                    if len(b) > bullet_len:
                        b = b[:bullet_len] + "\u2026"
                    lines.append(f"  - {b}")
            if files:
                lines.append("Files referenced earlier: " + ", ".join(files[-file_limit:]))
            if qualified_names:
                lines.append("Qualified names referenced earlier: " + ", ".join(qualified_names[-name_limit:]))
        if last_open_question:
            lines.append(f"Most recent open question from the compacted portion: {last_open_question}")
        lines.append("</compacted_context>")
        return "\n".join(lines)

    tiers = (
        (12, 140, 20, 20, False),
        (6, 100, 12, 12, False),
        (3, 80, 8, 8, False),
        (0, 0, 6, 6, True),   # counts-only: drop bullets, keep name/file totals
        (0, 0, 2, 2, True),   # most aggressive: counts-only with a tiny sample
    )
    candidate = None
    for bullet_limit, bullet_len, name_limit, file_limit, counts_only in tiers:
        candidate = build(bullet_limit, bullet_len, name_limit, file_limit, counts_only)
        if count_tokens([{"role": "system", "content": candidate}]) <= target_cap:
            return candidate

    return candidate  # most aggressive attempt, even if still over target


def compact_if_needed(messages):
    """
    If `messages` exceeds CONTEXT_TOKEN_BUDGET, return a NEW, shorter list:
      - everything except the most recent CONTEXT_KEEP_RECENT_TURNS messages
        is folded into one compact system-style summary message, and
      - the most recent CONTEXT_KEEP_RECENT_TURNS messages (which always
        includes the current, unanswered user question, since that is
        necessarily the tail of the list) are kept verbatim.
    `messages` itself is never mutated — the caller's full, untouched
    transcript is unaffected; only what gets sent to the model shrinks.
    """
    if count_tokens(messages) <= CONTEXT_TOKEN_BUDGET:
        return messages

    if len(messages) <= CONTEXT_KEEP_RECENT_TURNS + 1:
        # Not enough history to meaningfully compact away.
        return messages

    recent = messages[-CONTEXT_KEEP_RECENT_TURNS:]
    older = messages[:-CONTEXT_KEEP_RECENT_TURNS]

    summary_text = summarize_deterministic(older)
    if summary_text is None:
        return messages

    compacted = [{"role": "system", "content": summary_text}] + recent

    # Safety net: if the kept "recent" tail alone (e.g. one giant tool
    # result) still doesn't fit, trim from just after the summary —
    # never touch the summary itself or the last message (the current
    # question), since that's the one thing we must never lose.
    while count_tokens(compacted) > CONTEXT_TOKEN_BUDGET and len(compacted) > 2:
        del compacted[1]

    return compacted
