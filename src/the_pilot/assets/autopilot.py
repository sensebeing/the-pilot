#!/usr/bin/env python3
"""Single-file Turn Autopilot runtime for Antigravity + Orca.

The engine is deliberately small and mechanical:

* PreInvocation expands `,mode`, `,1`/`,2`/`,3`, custom `,` commands and the
  short entry commands, then records whether the new turn was auto-submitted.
* Stop reads the visible suggestion the model already showed, stores the
  candidates and asks Orca to pre-fill (or submit) exactly that sentence.
* Everything durable lives in `profile.json` (preferences) and a per-session
  JSON file (conversation turn state). No delivery ledger, no install marker.

All settings are read from the installed `config/autopilot` directory; no
personal paths are hard-coded.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


def _gemini_dir() -> Path:
    override = os.environ.get("ORCA_AUTOPILOT_GEMINI_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2]


GEMINI_DIR = _gemini_dir()
AUTOPILOT_DIR = GEMINI_DIR / "config" / "autopilot"
PROFILE_PATH = AUTOPILOT_DIR / "profile.json"
CONFIG_PATH = AUTOPILOT_DIR / "config.json"
SCENES_DIR = AUTOPILOT_DIR / "scenes"
GEMINI_MD_PATH = GEMINI_DIR / "config" / "GEMINI.md"
STATE_DIR = Path(
    os.environ.get("ORCA_AUTOPILOT_STATE_DIR", "").strip()
    or str(AUTOPILOT_DIR / "state")
).expanduser()
LOG_PATH = STATE_DIR / "autopilot.log"

AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex", "agy": "AGY"}
MODES = ("gate", "confirm", "off")
RECOVERY_FAILED_MESSAGE = (
    "【Autopilot 恢复失败】状态文件未能安全重建，自动投递保持暂停；"
    "请用一句话告知用户，无需执行其他操作。"
)
SCENES_BEGIN = "<!-- BEGIN AUTOPILOT SCENES -->"
SCENES_END = "<!-- END AUTOPILOT SCENES -->"
SYNC_ASK_QUESTION_TOOLS = frozenset(
    {"ask_question", "AskUserQuestion", "request_user_input"}
)
ASYNC_ASK_QUESTION_TOOLS = frozenset({"request_user_input_async"})
ASK_QUESTION_TOOLS = SYNC_ASK_QUESTION_TOOLS | ASYNC_ASK_QUESTION_TOOLS
TOOL_RESPONSE_STEP_TYPES = frozenset({"GENERIC", "TOOL_RESPONSE"})
# `transcript.jsonl` truncates these fields; `transcript_full.jsonl` keeps the
# same non-empty line indices with the complete values.
TRUNCATING_STEP_FIELDS = frozenset({"content", "tool_calls"})
STRUCTURED_ANSWER_TOOLS = frozenset({"AskUserQuestion", "request_user_input"})
STRUCTURED_COMPLETED_STATUSES = frozenset(
    {"ok", "done", "completed", "answered", "submitted"}
)
# The runtime prefixes a tool response with `Created At:` / `Completed At:`
# lines; they timestamp the call and are not part of the user's answer.
RESPONSE_HEADER_RE = re.compile(r"^(?:Created|Completed)\s+At\s*:", re.IGNORECASE)
# Owner receipt for `ask_question`: `A1: <answer>` followed by continuation lines.
QUESTION_ANSWER_HEADER_RE = re.compile(r"^A([1-9]\d*):(?:[ \t]*(.*))?$")
TAG_FIELDS = frozenset(
    {
        "scene",
        "stage",
        "gate",
        "action",
        "next",
        "1",
        "2",
        "3",
        "p1",
        "p2",
        "p3",
        "prompt",
        "alt",
    }
)

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
WORKER_PREAMBLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Task ID|Dispatch ID)\s*:\s*(?:task_|disp_|[a-z0-9_-]{6,})",
    re.IGNORECASE,
)
# Quote/box/list decoration in front of a suggestion line. Leading decoration is
# presentation, never content: the classifier looks at the label after `:`.
# A numeric list prefix (`1.`) is left in place; it is part of the label grammar.
LINE_PREFIX_RE = re.compile(
    r"^\s*(?:>\s*)?(?:│\s*)?(?:↳|->|→|-|\*|•)?\s*(?:💡\s*)?"
)
# A candidate label is a whole grammar, not a prefix: marker + optional index +
# optional parenthetical annotation, then end of label. Trailing prose
# （主推方案的风险…、候选1的风险…）never becomes an executable suggestion.
_LABEL_ANNOTATION = r"(?:[（(][^（）()]*[)）])?"
SUGGESTION_LABEL_RE = re.compile(
    r"^(?:"
    r"(?P<primary>下一步建议|建议下一步|(?i:nextsteps?)|主推)(?:[,，]1)?"
    r"|(?P<indexed>,[123])"
    r"|(?P<candidate>候选[123])"
    r"|(?P<alt>备选)(?:[,，](?P<alt_index>[23])|(?P<alt_attached>[23])"
    r"|(?P<alt_paren>[（(][,，]?[23][)）]))?"
    r")" + _LABEL_ANNOTATION + r"$"
)
# A bare `下一步建议` / `Next Steps` header line carries no prompt; it only opens
# a section scope. Inside that scope contiguous items may be labelled with a
# bare index.
SECTION_HEADER_RE = re.compile(
    r"^(?:下一步建议|建议下一步|(?i:nextsteps?))" + _LABEL_ANNOTATION + r"$"
)
BARE_SUGGESTION_LABEL_RE = re.compile(
    r"^(?P<bare>[123])(?:[.)、])?" + _LABEL_ANNOTATION + r"$"
)
FENCE_LINE_RE = re.compile(
    r"^(?P<prefix>(?:[ \t]*>[ \t]*)*[ \t]*"
    r"(?:(?:[-*+]|\d+[.)、])[ \t]+)?[ \t]*)"
    r"(?P<fence>`{3,}|~{3,})(?P<info>.*)$"
)
AUTOPILOT_TAG_RE = re.compile(
    r"<!--\s*autopilot\s*:?\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL
)
TAG_KV_RE = re.compile(
    r'([a-zA-Z0-9_][a-zA-Z0-9_-]*)\s*=\s*'
    r'(?:"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'|([^\s,|;>]+))'
)
TAG_KV_RESIDUE_TRANSLATION = str.maketrans("", "", " \t\r\n,|;")
TRAILING_ALT_RE = re.compile(
    r"\s*[（(]\s*备选\s*,2\s*[：:][^）)]*[）)]\s*$"
)
WORKER_RE = re.compile(r"^,\s*(?:worker|agent)(?:\s+(codex|claude|claude-code|cc|status))?\s*$", re.IGNORECASE)
MODE_RE = re.compile(r"^,\s*mode\s+(gate|confirm|off|status)\s*$", re.IGNORECASE)
RECOVERY_RE = re.compile(r"^,\s*autopilot\s+recover\s*$", re.IGNORECASE)
CANDIDATE_RE = re.compile(r"^,\s*([123])(?:\s+(.*))?$", re.DOTALL)
COMMA_RE = re.compile(r"^(,[a-z0-9_-]+)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
ENTRY_RE = re.compile(r"^([a-z][a-z0-9_-]*)(?:\s+(.*))?$", re.DOTALL)


def read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log_event("payload_invalid", error=str(exc))
        return {}
    return payload if isinstance(payload, dict) else {}


def log_event(event: str, **fields: Any) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        **fields,
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log_event("json_read_failed", path=str(path), error=str(exc))
        return {}
    if not isinstance(value, dict):
        log_event("json_not_object", path=str(path))
        return {}
    return value


def save_text(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError as exc:
        log_event("text_write_failed", path=str(path), error=str(exc))
        return False


def save_json(path: Path, value: dict[str, Any]) -> bool:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return save_text(path, text)


def load_profile() -> dict[str, Any]:
    return load_json(PROFILE_PATH)


def load_config() -> dict[str, Any]:
    return load_json(CONFIG_PATH)


def current_mode(profile: dict[str, Any] | None = None) -> str:
    current = profile if profile is not None else load_profile()
    autopilot = current.get("autopilot")
    if isinstance(autopilot, dict):
        mode = str(autopilot.get("mode") or "").lower()
        if mode in MODES:
            return mode
    return "off"


def _current_agents(profile: dict[str, Any] | None, key: str, defaults: list[str]) -> list[str]:
    current = profile if profile is not None else load_profile()
    orchestration = current.get("orchestration")
    agents = orchestration.get(key) if isinstance(orchestration, dict) else None
    if (isinstance(agents, list) and agents
            and all(isinstance(agent, str) and agent in defaults for agent in agents)
            and len(set(agents)) == len(agents)):
        return agents.copy()
    return defaults


def current_worker_agents(profile: dict[str, Any] | None = None) -> list[str]:
    return _current_agents(profile, "worker_agents", ["claude", "codex"])


def worker_priority(workers: list[str]) -> str:
    order = " > ".join(AGENT_LABELS[agent] for agent in workers)
    secondary = f", secondary: --agent {workers[1]}" if len(workers) > 1 else ""
    return f"Orca Worker Priority: {order} (primary: --agent {workers[0]}{secondary})"


def compile_worker_hot_reload(workers: list[str]) -> str:
    return (
        "<orca_worker_agent_hot_reload>\n"
        f"{worker_priority(workers)}。立即覆盖本会话启动时的旧设定，"
        f"本会话后续所有 worker-start 调用立即使用 --agent {workers[0]}。"
        "其他编排与审查边界保持不变。\n</orca_worker_agent_hot_reload>"
    )


def replace_worker_guidance(text: str, workers: list[str]) -> str:
    """Replace executor slots without owning or reproducing orchestration rules."""
    priority = " > ".join(AGENT_LABELS[agent] for agent in workers)
    primary, secondary = workers
    text = re.sub(r"(Orca Orchestration：|执行器优先级：)[^）\n]+", lambda m: m[1] + priority, text)
    text = re.sub(
        r"((?:默认优先(?:使用\s*|\s+)|优先使用\s*))(?:Claude Code|Codex)"
        r"（`--agent (?:claude|codex)`）(?:，其次使用 (?:Claude Code|Codex)（`--agent (?:claude|codex)`）)?",
        lambda m: f"{m[1]}{AGENT_LABELS[primary]}（`--agent {primary}`），其次使用 {AGENT_LABELS[secondary]}（`--agent {secondary}`）",
        text,
    )
    text = re.sub(r"(--worktree [^`\n]*?--agent )(?:claude|codex)\b", lambda m: m[1] + primary, text)
    text = re.sub(r"次选 `--agent (?:claude|codex)`", f"次选 `--agent {secondary}`", text)
    return re.sub(
        r"（(?=或 `task-create`|`orca worktree create`)",
        f"（次选 `--agent {secondary}`；",
        text,
    )


def set_worker_agents(target: str) -> tuple[bool, list[str], list[str]]:
    primary = "claude" if target.lower() in {"claude", "claude-code", "cc"} else target.lower()
    if primary not in {"claude", "codex"}:
        return False, current_worker_agents(), []
    updated: list[str] = []
    with _file_lock(AUTOPILOT_DIR / ".autopilot.lock"):
        profile = load_profile()
        if not profile:
            return False, current_worker_agents(profile), updated
        old_workers = current_worker_agents(profile)
        new_workers = ["codex", "claude"] if primary == "codex" else ["claude", "codex"]
        prepared: list[tuple[Path, str]] = []
        try:
            if GEMINI_MD_PATH.is_file():
                text = GEMINI_MD_PATH.read_text(encoding="utf-8")
                pattern = r"(<!-- BEGIN ORCA_AUTOPILOT_PROFILE -->)(.*?)(?=## 个人研发状态机与 Turn Autopilot 规范|<!-- END ORCA_AUTOPILOT_PROFILE -->)"
                text, count = re.subn(
                    pattern, lambda m: m[1] + replace_worker_guidance(m[2], new_workers),
                    text, count=1, flags=re.DOTALL,
                )
                if count != 1:
                    return False, old_workers, updated
                prepared.append((GEMINI_MD_PATH, text))
            if (not os.environ.get("ORCA_AUTOPILOT_GEMINI_DIR", "").strip()
                    and GEMINI_DIR.resolve() == (Path.home() / ".gemini").resolve()):
                for path in (Path.home() / ".claude/CLAUDE.md", Path.home() / ".codex/AGENTS.md"):
                    if path.is_file():
                        text = replace_worker_guidance(path.read_text(encoding="utf-8"), new_workers)
                        prepared.append((path, text))
        except (OSError, UnicodeError) as exc:
            log_event("worker_guidance_read_failed", error=str(exc))
            return False, old_workers, updated
        profile.setdefault("orchestration", {})["worker_agents"] = new_workers
        prepared.insert(0, (PROFILE_PATH, json.dumps(profile, ensure_ascii=False, indent=2) + "\n"))
        for path, text in prepared:
            if not save_text(path, text):
                return False, current_worker_agents(), updated
            updated.append(str(path))
    return True, new_workers, updated


def max_consecutive_turns(profile: dict[str, Any] | None = None) -> int:
    current = profile if profile is not None else load_profile()
    autopilot = current.get("autopilot")
    if isinstance(autopilot, dict):
        value = autopilot.get("max_consecutive_auto_turns")
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 50:
            return value
    return 8


def set_mode(mode: str) -> bool:
    with _file_lock(AUTOPILOT_DIR / ".autopilot.lock"):
        profile = load_profile()
        if not profile:
            return False
        profile.setdefault("autopilot", {})["mode"] = mode
        if not save_json(PROFILE_PATH, profile):
            return False
    log_event("mode_changed", mode=mode)
    return True


def _truncated_fields(step: dict[str, Any]) -> list[str]:
    fields = step.get("truncated_fields")
    if not isinstance(fields, list):
        return []
    return [field for field in fields if isinstance(field, str)]


def _current_turn_start(steps: list[dict[str, Any]]) -> int:
    for index in range(len(steps) - 1, -1, -1):
        if steps[index].get("type") == "USER_INPUT":
            return index
    return 0


def _hydrate_truncated_steps(steps: list[dict[str, Any]], path: Path) -> None:
    """Replace truncated current-turn steps with their full transcript lines.

    The compact transcript and its full sibling share 0-based non-empty line
    indices, so a truncated step is hydrated positionally. Every way hydration
    can fail is logged and leaves the compact step untouched, keeping its
    `truncated_fields` so callers still fail closed on a missing answer.
    """
    needed = {
        index
        for index in range(_current_turn_start(steps), len(steps))
        if TRUNCATING_STEP_FIELDS.intersection(_truncated_fields(steps[index]))
    }
    if not needed:
        return
    if path.name != "transcript.jsonl":
        log_event("transcript_full_unavailable", path=str(path), indices=sorted(needed))
        return
    full_path = path.parent / "transcript_full.jsonl"
    if not full_path.is_file():
        log_event("transcript_full_missing", path=str(full_path), indices=sorted(needed))
        return
    limit = max(needed)
    line_index = -1
    try:
        with full_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                line_index += 1
                if line_index > limit:
                    break
                if line_index in needed:
                    _hydrate_step(steps, line_index, line, full_path)
    except OSError as exc:
        log_event("transcript_full_read_failed", path=str(full_path), error=str(exc))
        return
    if line_index < limit:
        log_event(
            "transcript_full_short",
            path=str(full_path),
            expected=limit,
            actual=line_index,
        )


def _hydrate_step(
    steps: list[dict[str, Any]], index: int, line: str, full_path: Path
) -> None:
    try:
        full_item = json.loads(line)
    except ValueError:
        log_event("transcript_full_corrupt_line", path=str(full_path), index=index)
        return
    if not isinstance(full_item, dict):
        log_event("transcript_full_corrupt_line", path=str(full_path), index=index)
        return
    compact = steps[index]
    step_index = compact.get("step_index")
    if full_item.get("type") != compact.get("type") or (
        step_index is not None and full_item.get("step_index") != step_index
    ):
        log_event("transcript_full_step_mismatch", path=str(full_path), index=index)
        return
    steps[index] = full_item


def read_steps(transcript_path: str | None) -> tuple[list[dict[str, Any]], bool]:
    if not transcript_path:
        return [], False
    path = Path(transcript_path)
    if not path.is_file():
        log_event("transcript_missing", path=str(path))
        return [], False
    steps: list[dict[str, Any]] = []
    complete = True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    complete = False
                    continue
                if not isinstance(item, dict):
                    complete = False
                    continue
                steps.append(item)
    except OSError as exc:
        log_event("transcript_read_failed", path=str(path), error=str(exc))
        return [], False
    if not steps:
        complete = False
    if not complete:
        log_event("transcript_incomplete", path=str(path), steps=len(steps))
        return steps, complete
    _hydrate_truncated_steps(steps, path)
    return steps, complete


def current_turn_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index in range(len(steps) - 1, -1, -1):
        if steps[index].get("type") == "USER_INPUT":
            return steps[index + 1 :]
    return steps


def is_first_invocation_of_turn(steps: list[dict[str, Any]]) -> bool:
    for step in reversed(steps):
        step_type = str(step.get("type") or "")
        if step_type == "USER_INPUT":
            return True
        if step_type in {"PLANNER_RESPONSE", "EPHEMERAL_MESSAGE", "GENERIC", "TOOL_RESPONSE"}:
            return False
    return True


def extract_user_text(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    match = USER_REQUEST_RE.search(raw)
    return (match.group(1) if match else raw).strip()


def is_dispatched_worker(steps: list[dict[str, Any]], text: str = "") -> bool:
    for key in ("ORCA_DISPATCH_ID", "ORCA_TASK_ID", "ORCA_WORKER_DISPATCH_ID"):
        if os.environ.get(key, "").strip():
            return True
    if text and WORKER_PREAMBLE_RE.search(text):
        return True
    for step in steps[:4]:
        if step.get("type") == "USER_INPUT":
            return bool(WORKER_PREAMBLE_RE.search(str(step.get("content") or "")))
    return False


def render_template(template: str, arg: str = "", note: str = "") -> str:
    text = template.strip()
    if not text:
        return ""
    if "{arg}" in text:
        text = text.replace("{arg}", arg.strip())
    elif arg.strip():
        text = f"{text}\n\n{arg.strip()}"
    if note.strip():
        text = f"{text}（补充要求：{note.strip()}）"
    return text.strip()


def _strip_wrapping_markdown(text: str) -> str:
    """Remove balanced Markdown wrappers (``**`` / backticks) from both ends."""
    current = text.strip()
    changed = True
    while changed and current:
        changed = False
        for marker in ("**", "`"):
            if (
                len(current) >= 2 * len(marker)
                and current.startswith(marker)
                and current.endswith(marker)
            ):
                current = current[len(marker) : -len(marker)].strip()
                changed = True
    return current


def clean_prompt(raw: str) -> str:
    """Remove only provable outer Markdown wrappers; keep everything else literal."""
    text = _strip_wrapping_markdown(raw.strip())
    text = TRAILING_ALT_RE.sub("", text).strip()
    text = _strip_wrapping_markdown(text)
    joined = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not re.search(r"[\w一-鿿]", joined):
        return ""
    return joined


def _is_suggestion_header(label: str) -> bool:
    """Whether a label is a bare `下一步建议` / `建议下一步` / `Next Steps` header."""
    compact = re.sub(r"\s+", "", re.sub(r"^#+\s*", "", label))
    compact = re.sub(r"^\d+[.)、]💡?", "", compact)
    return SECTION_HEADER_RE.match(compact) is not None


def _suggestion_kind(label: str, *, in_section: bool = False) -> int:
    """Classify a compact label grammar: 1 primary, 2/3 alternative, 0 prose.

    The label must match the whole grammar (marker + optional index + optional
    annotation), so 主推方案的风险…, 候选1的风险…, 备选2的风险… and ,10 stay
    prose instead of overriding the real suggestion. A numbered list prefix
    (`1. 主推`) belongs to the label grammar, and only inside an open
    suggestion section may a bare `1 (Recommended)` / `1. (Recommended)` /
    `1` / `2` / `3` label count; the same list item in ordinary prose stays
    prose.
    """
    compact = re.sub(r"\s+", "", label)
    stripped = re.sub(r"^\d+[.)、]💡?", "", compact)
    match = SUGGESTION_LABEL_RE.match(stripped)
    if match is None:
        if not in_section:
            return 0
        bare_match = BARE_SUGGESTION_LABEL_RE.match(compact)
        return int(bare_match.group("bare")) if bare_match else 0
    if match.group("primary"):
        return 1
    if match.group("indexed"):
        return int(match.group("indexed")[1])
    if match.group("candidate"):
        return int(match.group("candidate")[2])
    for name in ("alt_index", "alt_attached", "alt_paren"):
        value = match.group(name)
        if value:
            return int(next(char for char in value if char.isdigit()))
    return 2


def _split_label_and_content(line: str) -> tuple[str, str] | None:
    """Split at the first fullwidth/ASCII colon without rewriting the content."""
    indexes = [index for index in (line.find("："), line.find(":")) if index >= 0]
    if not indexes:
        return None
    cut = min(indexes)
    return line[:cut], line[cut + 1 :]


def _fence_line(line: str) -> tuple[str, int, str, str] | None:
    """Return (char, length, container prefix, info) for a fence-shaped line."""
    match = FENCE_LINE_RE.match(line)
    if match is None:
        return None
    fence = match.group("fence")
    info = match.group("info").strip()
    if fence[0] in info:
        return None
    return fence[0], len(fence), match.group("prefix"), info


def _strip_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Consume every comment boundary in order; return (visible text, state).

    Closing one comment may reveal another opener on the same line, and a
    comment left open carries into the next line. Both directions share this
    single scan so no hidden region can leak into a candidate.
    """
    visible: list[str] = []
    position = 0
    while position < len(line):
        if in_comment:
            end = line.find("-->", position)
            if end == -1:
                return "".join(visible), True
            position = end + 3
            in_comment = False
            continue
        start = line.find("<!--", position)
        if start == -1:
            visible.append(line[position:])
            break
        visible.append(line[position:start])
        position = start + 4
        in_comment = True
    return "".join(visible), in_comment


def _visible_lines(text: str) -> list[str | None]:
    """Lines outside fenced code blocks and HTML comment regions.

    A fence is closed only by the same character, an equal-or-longer run, and
    the same container prefix as its opener, so a ``> ``` `` literal inside an
    outer code block cannot close it. Hidden or undecidable regions never
    become executable. Every hidden line is returned as a ``None`` boundary
    instead of being dropped: a blank line is transparent prose, while a
    hidden line proves a break, so the two sides can never be glued into one
    contiguous suggestion section.
    """
    lines: list[str | None] = []
    fence_char = ""
    fence_length = 0
    fence_prefix = ""
    in_comment = False
    for raw_line in text.splitlines():
        if fence_char:
            closing = _fence_line(raw_line)
            if (
                closing is not None
                and closing[0] == fence_char
                and closing[1] >= fence_length
                and closing[2] == fence_prefix
                and not closing[3]
            ):
                fence_char = ""
                fence_length = 0
                fence_prefix = ""
            lines.append(None)
            continue
        was_hidden = in_comment
        visible_text, in_comment = _strip_comments(raw_line, in_comment)
        if was_hidden or "<!--" in raw_line or "-->" in raw_line:
            # A line touching a comment boundary was only partially visible;
            # never execute reconstructed text（行内代码里的 <!-- 也无法区分）.
            lines.append(None)
            continue
        if not visible_text.strip():
            continue
        opening = _fence_line(visible_text)
        if opening is not None:
            fence_char, fence_length, fence_prefix = opening[0], opening[1], opening[2]
            lines.append(None)
            continue
        lines.append(visible_text)
    return lines


def _visible_candidates(
    text: str,
) -> tuple[str, list[dict[str, Any]], str]:
    """Read visible suggestion lines; return (primary, alternatives, error)."""
    primary = ""
    alternatives: list[dict[str, Any]] = []
    seen: set[int] = set()
    in_section = False
    for entry in _visible_lines(text):
        if entry is None:
            # A hidden line (code fence or comment boundary) is a break, not
            # invisible glue: the section ends here, so numbered prose on the
            # other side can never join it.
            in_section = False
            continue
        line = LINE_PREFIX_RE.sub("", entry).strip()
        if not line:
            continue
        parts = _split_label_and_content(line)
        if parts is None:
            in_section = _is_suggestion_header(
                line.replace("*", "").replace("`", "").strip()
            )
            continue
        raw_label, raw_content = parts
        prompt = clean_prompt(raw_content)
        clean_label = raw_label.replace("*", "").replace("`", "").strip()
        if not prompt:
            in_section = _is_suggestion_header(clean_label)
            continue
        kind = _suggestion_kind(clean_label, in_section=in_section)
        if kind == 1:
            # Only the last primary block counts: an earlier suggestion the
            # model already replaced must not keep contributing alternatives.
            # A headerless primary never opens a section, so a bare `1` list
            # item in the following prose cannot overwrite it.
            primary = prompt
            alternatives = []
            seen = set()
            continue
        if kind == 0 or not primary:
            # Any non-suggestion line immediately closes the section, so the
            # numbered lists in later prose can never become candidates.
            in_section = False
            continue
        if prompt == primary:
            continue
        if kind in seen:
            return "", [], "duplicate_alternative"
        if any(item["prompt"] == prompt for item in alternatives):
            continue
        seen.add(kind)
        alternatives.append({"index": kind, "prompt": prompt})
    return primary, alternatives, ""


class _DuplicateTagField(ValueError):
    """Raised from the JSON object hook so duplicate keys fail closed."""


def _collect_tag_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    collected: dict[str, Any] = {}
    for key, value in pairs:
        lowered = str(key).strip().lower()
        if lowered in collected:
            raise _DuplicateTagField(lowered)
        collected[lowered] = value
    return collected


def _parse_json_tag(text: str) -> tuple[dict[str, str], str]:
    try:
        parsed = json.loads(text, object_pairs_hook=_collect_tag_pairs)
    except _DuplicateTagField:
        return {}, "duplicate_field"
    except ValueError:
        return {}, "invalid_json"
    if not isinstance(parsed, dict):
        return {}, "invalid_json"
    values: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(value, str):
            values[key] = value.strip()
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            values[key] = str(value)
        else:
            return {}, "invalid_json"
    return values, ""


def _parse_tag_body(body: str) -> tuple[dict[str, str], str]:
    text = body.strip()
    if text.startswith("{"):
        return _parse_json_tag(text)
    values: dict[str, str] = {}
    for match in TAG_KV_RE.finditer(text):
        key = match.group(1).lower()
        if key in values:
            return {}, "duplicate_field"
        raw_value = match.group(2) or match.group(3) or match.group(4) or ""
        values[key] = raw_value.replace('\\"', '"').strip()
    remainder = TAG_KV_RE.sub("", text).translate(TAG_KV_RESIDUE_TRANSLATION)
    if remainder and values:
        return {}, "unknown_field"
    return values, ""


def _tag_field(values: dict[str, str], names: tuple[str, ...]) -> tuple[str, bool]:
    """Collapse one logical field across aliases; tag text is never display-cleaned."""
    selected = ""
    for name in names:
        if name not in values:
            continue
        candidate = values[name].strip()
        if not candidate:
            continue
        if selected and candidate != selected:
            return "", True
        selected = candidate
    return selected, False


def parse_suggestion(text: str) -> dict[str, Any]:
    source = text or ""
    visible_primary, visible_alternatives, visible_error = _visible_candidates(source)
    if visible_error:
        return _invalid_suggestion(visible_error)
    tags = list(AUTOPILOT_TAG_RE.finditer(source))
    if len(tags) != 1:
        return _invalid_suggestion("missing_tag" if not tags else "multiple_tags")
    values, tag_error = _parse_tag_body(tags[0].group(1))
    if tag_error:
        return _invalid_suggestion(tag_error)
    if not values:
        return _invalid_suggestion("empty_tag")
    if set(values) - TAG_FIELDS:
        return _invalid_suggestion("unknown_field")
    if (
        "scene" in values
        and "stage" in values
        and values["scene"] != values["stage"]
    ):
        return _invalid_suggestion("conflicting_scene")
    if (
        "gate" in values
        and "action" in values
        and values["gate"].lower() != values["action"].lower()
    ):
        return _invalid_suggestion("conflicting_gate")

    scene = values.get("scene") or values.get("stage") or ""
    gate = (values.get("gate") or values.get("action") or "").lower()
    next_value = (values.get("next") or "").upper()
    if scene.upper() in {"NONE", "SKIP", "WAIT", "WAITING_USER", "ASK_QUESTION"}:
        scene = "NONE"
    elif next_value in {"NONE", "SKIP", "WAIT", "ASK_QUESTION"}:
        scene = "NONE"
    elif not scene:
        return _invalid_suggestion("missing_scene")
    elif gate not in {"auto", "pause"}:
        return _invalid_suggestion("invalid_gate")
    if scene == "NONE":
        return {
            "scene": scene,
            "prompt": "",
            "alternatives": [],
            "gate": gate,
            "invalid": "",
        }

    # The visible suggestion is the only execution source（所见即所填）; tag
    # candidates may only cross-check it and never introduce unseen text.
    if not visible_primary:
        return _invalid_suggestion("missing_prompt")
    tag_primary, primary_conflict = _tag_field(values, ("1", "p1", "prompt"))
    if primary_conflict:
        return _invalid_suggestion("conflicting_prompt")
    if tag_primary and tag_primary != visible_primary:
        return _invalid_suggestion("conflicting_prompt")
    for index, names in ((2, ("2", "p2", "alt")), (3, ("3", "p3"))):
        value, conflict = _tag_field(values, names)
        if conflict:
            return _invalid_suggestion("duplicate_alternative")
        if not value:
            continue
        visible = next(
            (
                item["prompt"]
                for item in visible_alternatives
                if item["index"] == index
            ),
            "",
        )
        if not visible:
            return _invalid_suggestion("missing_alternative")
        if value != visible:
            return _invalid_suggestion("duplicate_alternative")
    return {
        "scene": scene,
        "prompt": visible_primary,
        "alternatives": visible_alternatives,
        "gate": gate,
        "invalid": "",
    }


def _invalid_suggestion(reason: str) -> dict[str, Any]:
    return {
        "scene": "",
        "prompt": "",
        "alternatives": [],
        "gate": "",
        "invalid": reason,
    }


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        value = raw.strip()
        if value.startswith('"'):
            try:
                meta[key.strip()] = json.loads(value)
            except ValueError:
                meta[key.strip()] = value.strip('"')
        elif value in ("true", "false"):
            meta[key.strip()] = value == "true"
        else:
            meta[key.strip()] = value
    return meta, text[end + 5 :]


def scan_scenes() -> dict[str, dict[str, Any]]:
    scenes: dict[str, dict[str, Any]] = {}
    if not SCENES_DIR.is_dir():
        return scenes
    for path in sorted(SCENES_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log_event("scene_read_failed", path=str(path), error=str(exc))
            continue
        meta, body = split_frontmatter(text)
        name = str(meta.get("name") or "")
        if name:
            scenes[name] = {"meta": meta, "body": body, "path": path}
    return scenes


def scene_gate_default(scene: str, scenes: dict[str, dict[str, Any]]) -> str:
    entry = scenes.get(scene)
    if not entry:
        return "pause"
    value = entry["meta"].get("auto_in_gate_mode", True)
    if isinstance(value, str):
        return "auto" if value.strip().lower() == "true" else "pause"
    return "auto" if value else "pause"


def resolve_gate(scene: str, explicit: str, scenes: dict[str, dict[str, Any]]) -> str:
    if explicit == "auto" and scene_gate_default(scene, scenes) == "auto":
        return "auto"
    return "pause"


def _positional_tool_responses(
    turn_steps: list[dict[str, Any]], index: int
) -> list[dict[str, Any]]:
    """Return the tool-response steps that belong to the planner step at ``index``.

    The transcript is positional: every step after a planner step until the
    next planner or user step, in call order, answers that planner's tool
    calls one for one.
    """
    responses: list[dict[str, Any]] = []
    for step in turn_steps[index + 1 :]:
        step_type = step.get("type")
        if step_type in {"PLANNER_RESPONSE", "USER_INPUT"}:
            break
        if step_type in TOOL_RESPONSE_STEP_TYPES:
            responses.append(step)
    return responses


def _answer_body_lines(content: str) -> list[str]:
    """Return the answer lines without the runtime's timestamp header."""
    return [line for line in content.splitlines() if not RESPONSE_HEADER_RE.match(line)]


def _expected_question_count(
    planner_step: dict[str, Any], call: dict[str, Any]
) -> tuple[int | None, str]:
    """Return the call's declared question count, or why it is untrustworthy.

    Every supported question tool declares `questions` as a non-empty list, so
    there is no legitimate unknown count: a truncated call or malformed
    arguments refuse the receipt instead of letting the answer define it.
    """
    if "tool_calls" in _truncated_fields(planner_step):
        return None, "tool_calls_truncated"
    raw_args: Any = call.get("args")
    if raw_args is None:
        raw_args = call.get("input")
    if raw_args is None:
        raw_args = call.get("arguments")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except ValueError:
            return None, "invalid_question_args"
    if not isinstance(raw_args, dict):
        return None, "invalid_question_args"
    raw_questions = raw_args.get("questions")
    if isinstance(raw_questions, str):
        try:
            raw_questions = json.loads(raw_questions)
        except ValueError:
            return None, "invalid_question_args"
    if (
        not isinstance(raw_questions, list)
        or not raw_questions
        or not all(isinstance(item, dict) for item in raw_questions)
    ):
        return None, "invalid_question_args"
    return len(raw_questions), ""


def _numbered_answers(body_lines: list[str]) -> dict[int, str] | None:
    """Parse the `ask_question` owner receipt, or None when it is not one."""
    chunks: dict[int, list[str]] = {}
    current: int | None = None
    for line in body_lines:
        match = QUESTION_ANSWER_HEADER_RE.match(line)
        if match:
            index = int(match.group(1))
            if index in chunks:
                return None
            chunks[index] = [match.group(2) or ""]
            current = index
            continue
        if current is None:
            if line.strip():
                return None
            continue
        chunks[current].append(line)
    return {index: "\n".join(lines).strip() for index, lines in chunks.items()}


def _numbered_answers_match(answers: dict[int, str], expected_count: int) -> bool:
    if set(answers) != set(range(1, expected_count + 1)):
        return False
    return all(answer.strip() for answer in answers.values())


def _structured_answer_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value) and all(
            isinstance(item, str) and item.strip() for item in value
        )
    if isinstance(value, dict):
        inner = value.get("answers")
        return (
            isinstance(inner, list)
            and bool(inner)
            and all(isinstance(item, str) and item.strip() for item in inner)
        )
    return False


def _structured_answers_settled(body: str, expected_count: int) -> bool:
    """Validate the structured `answers` owner receipt."""
    try:
        obj = json.loads(body)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    if any(obj.get(key) for key in ("error", "is_error", "cancelled", "canceled")):
        return False
    status = obj.get("status")
    if status is not None and (
        not isinstance(status, str)
        or status.strip().lower() not in STRUCTURED_COMPLETED_STATUSES
    ):
        return False
    answers = obj.get("answers")
    if not isinstance(answers, dict) or len(answers) != expected_count:
        return False
    return all(_structured_answer_value(value) for value in answers.values())


def _answer_receipt_settled(
    name: str, body_lines: list[str], expected_count: int
) -> bool:
    """Return whether the receipt answers exactly the declared questions."""
    body = "\n".join(body_lines).strip()
    if name in STRUCTURED_ANSWER_TOOLS and body.startswith("{"):
        return _structured_answers_settled(body, expected_count)
    answers = _numbered_answers(body_lines)
    if answers is None:
        return False
    return _numbered_answers_match(answers, expected_count)


def _response_unsettled_reason(response: Any) -> str | None:
    """Return why a positional response is not an answer, or None when it is."""
    if not isinstance(response, dict):
        return "missing_response"
    if str(response.get("status") or "").upper() != "DONE":
        return "status_not_done"
    if response.get("error") or response.get("is_error"):
        return "response_error"
    if "content" in _truncated_fields(response):
        return "content_truncated"
    content = response.get("content")
    if not isinstance(content, str):
        return "non_string_content"
    if not "\n".join(_answer_body_lines(content)).strip():
        return "empty_answer"
    return None


def ask_question_unsettled_reason(turn_steps: list[dict[str, Any]]) -> str | None:
    """Return why the turn still waits on the user, or None when it does not.

    A synchronous question tool blocks inside the current user turn: the
    runtime appends its positional tool response and resumes the planner in
    the same turn once the user answers. Only a `DONE` owner receipt answering
    exactly the declared question count settles the call — numbered `A1:`
    lines for `ask_question`, or a structured `answers` object for
    `AskUserQuestion` / `request_user_input`. An async question is never
    settled by its enqueue receipt — it waits for the next user turn.
    """
    for index, step in enumerate(turn_steps):
        if step.get("type") != "PLANNER_RESPONSE":
            continue
        calls = step.get("tool_calls")
        if not isinstance(calls, list):
            continue
        questions = [
            (offset, call)
            for offset, call in enumerate(calls)
            if isinstance(call, dict) and call.get("name") in ASK_QUESTION_TOOLS
        ]
        if not questions:
            continue
        responses = _positional_tool_responses(turn_steps, index)
        for offset, call in questions:
            if call.get("name") not in SYNC_ASK_QUESTION_TOOLS:
                # `request_user_input_async`: its enqueue receipt only proves
                # delivery; it stays pending until a new user turn.
                return "async_question"
            expected_count, args_reason = _expected_question_count(step, call)
            if expected_count is None:
                return args_reason or "invalid_question_args"
            response = responses[offset] if offset < len(responses) else None
            reason = _response_unsettled_reason(response)
            if reason:
                return reason
            body_lines = _answer_body_lines(response["content"])
            if not _answer_receipt_settled(
                str(call.get("name") or ""), body_lines, expected_count
            ):
                return "invalid_answer_receipt"
    return None


def _session_digest(conversation_id: str) -> str:
    return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:24]


def session_path(conversation_id: str) -> Path:
    return STATE_DIR / f"session-{_session_digest(conversation_id)}.json"


def state_lock_path(conversation_id: str) -> Path:
    return STATE_DIR / f"session-{_session_digest(conversation_id)}.lock"


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# Lock order: hooks may hold a session lock before taking the profile lock.
# Profile writers never acquire session locks or scan session state.
@contextmanager
def state_lock(conversation_id: str):
    with _file_lock(state_lock_path(conversation_id)):
        yield


def load_state(conversation_id: str) -> dict[str, Any] | None:
    """Return session state; None when an existing file cannot be trusted.

    A missing file is a first run (`{}`). A corrupt or unreadable file is not
    evidence of a fresh session: hooks must neither overwrite it nor auto-send
    from a guessed state.
    """
    path = session_path(conversation_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log_event("state_read_failed", cid=conversation_id, error=str(exc))
        return None
    try:
        value = json.loads(text)
    except ValueError as exc:
        log_event("state_corrupt", cid=conversation_id, error=str(exc))
        return None
    if not isinstance(value, dict):
        log_event("state_not_object", cid=conversation_id)
        return None
    return value


def backup_state(conversation_id: str) -> bool:
    """Copy an unusable session file aside as evidence, leaving the original in place.

    The corrupt main file keeps blocking every hook until a rebuilt state is
    atomically persisted over it, so a failed rebuild can never look like a
    first run.
    """
    path = session_path(conversation_id)
    backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        log_event("state_backup_failed", cid=conversation_id, error=str(exc))
        return False
    log_event("state_backup_created", cid=conversation_id, backup=str(backup))
    return True


def save_state(conversation_id: str, state: dict[str, Any]) -> bool:
    state["conversation_id"] = conversation_id
    state["updated_at"] = time.time()
    return save_json(session_path(conversation_id), state)


def turn_identity(steps: list[dict[str, Any]]) -> str:
    last_user_index = -1
    last_planner = ""
    for index, step in enumerate(steps):
        if step.get("type") == "USER_INPUT":
            last_user_index = index
        elif step.get("type") == "PLANNER_RESPONSE":
            last_planner = str(step.get("content") or "")
    digest = hashlib.sha256(last_planner.encode("utf-8")).hexdigest()[:16]
    return f"{last_user_index}:{len(steps)}:{digest}"


def turn_user_identity(steps: list[dict[str, Any]]) -> str:
    for index in range(len(steps) - 1, -1, -1):
        if steps[index].get("type") != "USER_INPUT":
            continue
        content = str(steps[index].get("content") or "")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return f"{index}:{digest}"
    return ""


def clear_suggestion(state: dict[str, Any]) -> None:
    state.update(
        {
            "scene": "NONE",
            "gate_action": "pause",
            "primary_text": "",
            "candidates": [],
            "candidate_turn_id": "",
            "auto_submitted_text": "",
        }
    )


def orca_binary() -> str:
    return shutil.which("orca") or "orca"


def run_orca(args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [orca_binary(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_event("orca_call_failed", args=args, error=str(exc))
        return None


def same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return left.rstrip("/") == right.rstrip("/")


def resolve_terminal(workspace_path: str, explicit_handle: str = "") -> str:
    if not workspace_path:
        log_event("terminal_workspace_missing")
        return ""
    requested = explicit_handle.strip() or os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    result = run_orca(["terminal", "list", "--json"], timeout=3)
    if result is None or result.returncode != 0:
        log_event("terminal_list_failed", returncode=None if result is None else result.returncode)
        return ""
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        log_event("terminal_list_invalid", error=str(exc))
        return ""
    terminals = ((payload.get("result") or {}).get("terminals") or []) if isinstance(payload, dict) else []
    candidates = [
        item
        for item in terminals
        if isinstance(item, dict)
        and item.get("agentIdentity") == "antigravity"
        and item.get("connected") is True
        and item.get("writable", True) is not False
    ]
    if requested:
        matched = [
            item for item in candidates if str(item.get("handle") or "") == requested
        ]
        if len(matched) != 1:
            log_event("terminal_binding_missing", handle=requested, candidates=len(candidates))
            return ""
        if not same_path(str(matched[0].get("worktreePath") or ""), workspace_path):
            log_event("terminal_binding_workspace_mismatch", handle=requested)
            return ""
        return requested
    matched = [
        item
        for item in candidates
        if same_path(str(item.get("worktreePath") or ""), workspace_path)
    ]
    if len(matched) != 1:
        log_event("terminal_ambiguous", candidates=len(matched), workspace=workspace_path)
        return ""
    return str(matched[0].get("handle") or "")


def send_to_terminal(handle: str, text: str, enter: bool) -> str:
    args = ["terminal", "send", "--terminal", handle, "--text", text, "--json"]
    if enter:
        args.append("--enter")
    result = run_orca(args, timeout=5)
    if result is None:
        log_event("send_unknown", handle=handle, enter=enter, reason="no_result")
        return "unknown"
    if result.returncode != 0:
        log_event(
            "send_rejected",
            handle=handle,
            enter=enter,
            returncode=result.returncode,
            stderr=result.stderr[-500:],
        )
        return "rejected"
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        log_event("send_receipt_invalid", handle=handle, error=str(exc))
        return "unknown"
    receipt = ((payload.get("result") or {}).get("send") or {}) if isinstance(payload, dict) else {}
    accepted = receipt.get("accepted")
    if accepted is True:
        log_event("send_receipt", handle=handle, enter=enter, accepted=True)
        return "accepted"
    if accepted is False:
        log_event("send_receipt", handle=handle, enter=enter, accepted=False)
        return "rejected"
    log_event("send_receipt_invalid", handle=handle, reason="missing_accepted")
    return "unknown"


def injection(message: str) -> dict[str, Any]:
    return {"injectSteps": [{"ephemeralMessage": message}]}


def handle_pre_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(payload.get("conversationId") or "")
    if not conversation_id:
        return {}
    text = extract_user_text(str(payload.get("lastUserInput") or ""))
    if not text:
        return {}
    steps, complete = read_steps(payload.get("transcriptPath"))
    if not complete:
        log_event("pre_invocation_transcript_incomplete", cid=conversation_id)
    if is_dispatched_worker(steps, text):
        return {}
    if not is_first_invocation_of_turn(steps):
        return {}

    worker_match = WORKER_RE.match(text)
    mode_match = MODE_RE.match(text)
    config = load_config()
    profile = load_profile()
    user_turn = turn_user_identity(steps)
    recovery_requested = bool(RECOVERY_RE.match(text))
    with state_lock(conversation_id):
        state = load_state(conversation_id)
        if state is None:
            # A corrupt file is never rebuilt from an ordinary or auto-continued
            # turn: without the lost state those are indistinguishable. Only an
            # explicit recovery command on a complete transcript proves the user
            # asked for it; every other input merely pauses autopilot.
            if not (recovery_requested and complete and user_turn):
                log_event(
                    "state_recovery_blocked",
                    cid=conversation_id,
                    transcript_complete=complete,
                    user_turn=bool(user_turn),
                )
                return {}
            if not backup_state(conversation_id):
                return injection(RECOVERY_FAILED_MESSAGE)
            if not save_state(conversation_id, {"current_user_turn_id": user_turn}):
                log_event("state_recovery_failed", cid=conversation_id)
                return injection(RECOVERY_FAILED_MESSAGE)
            log_event("state_recovered", cid=conversation_id)
            return injection(
                "【Autopilot 会话状态已恢复】原状态文件已备份留证并重建；"
                "自动投递恢复正常。请用一句话告知用户，无需执行其他操作。"
            )
        worker_reload_msg = ""
        prev_workers = state.get("loaded_worker_agents")
        cur_workers = current_worker_agents(profile)
        unversioned_existing = bool(state) and not isinstance(prev_workers, list)
        if not state:
            state["initial_worker_agents"] = cur_workers
        else:
            state.setdefault("initial_worker_agents", [])
        last_checkpoint = max(
            (index for index, step in enumerate(steps) if step.get("type") == "CHECKPOINT"),
            default=-1,
        )
        if (unversioned_existing
                or isinstance(prev_workers, list) and prev_workers and prev_workers != cur_workers
                or state["initial_worker_agents"] != cur_workers
                and last_checkpoint > state.get("worker_reload_checkpoint", -1)):
            worker_reload_msg = compile_worker_hot_reload(cur_workers)
            state["worker_reload_checkpoint"] = last_checkpoint
        state["loaded_worker_agents"] = cur_workers
        state["current_user_turn_id"] = user_turn or "missing"
        auto_text = str(state.get("auto_submitted_text") or "")
        manual_input = not auto_text or text != auto_text
        if manual_input:
            state["consecutive_auto_turns"] = 0
        if state.get("delivery_status") in {"pending", "unknown"} and manual_input:
            previous_status = str(state.get("delivery_status") or "")
            state["delivery_status"] = "cleared"
            state["last_settled_turn_id"] = ""
            log_event(
                "delivery_state_cleared",
                cid=conversation_id,
                status=previous_status,
            )
        state["auto_submitted_text"] = ""
        candidates = state.get("candidates") or []
        candidate_turn = str(state.get("candidate_turn_id") or "")
        state["candidates"] = []
        state["candidate_turn_id"] = ""

        if worker_match:
            target = (worker_match.group(1) or "status").lower()
            workers = current_worker_agents(profile)
            if target == "status":
                message = worker_priority(workers)
            else:
                success, workers, _ = set_worker_agents(target)
                if success:
                    message = compile_worker_hot_reload(workers)
                    worker_reload_msg = ""
                    state["worker_reload_checkpoint"] = last_checkpoint
                else:
                    message = "Worker 切换失败，请检查 profile.json 与派生规则文件。"
            state["loaded_worker_agents"] = workers
            if not save_state(conversation_id, state):
                log_event("pre_state_save_failed", cid=conversation_id)
            message = "\n\n".join(p for p in (worker_reload_msg, message) if p)
            return injection(message + " 请仅用一句话回复，并以 <!-- autopilot: scene=NONE --> 结尾。")

        if mode_match:
            target = mode_match.group(1).lower()
            if target == "status":
                scene_count = len(scan_scenes())
                message = (
                    f"【Autopilot 状态】当前模式为 `{current_mode(profile)}`，"
                    f"已加载 {scene_count} 个场景文件。{worker_priority(cur_workers)}。请用一句话告知用户，无需调用工具。"
                )
            elif not set_mode(target):
                message = "【Autopilot】模式切换失败：profile.json 不可读或不可写。"
            else:
                message = (
                    f"【Autopilot 模式已切换】当前模式为 `{target}`。"
                    "请用一句话简短告知用户，无需执行其他操作。"
                )
            if not save_state(conversation_id, state):
                log_event("pre_state_save_failed", cid=conversation_id)
            return injection("\n\n".join(p for p in (worker_reload_msg, message) if p))

        message = ""
        candidate_match = CANDIDATE_RE.match(text)
        if candidate_match:
            selected = int(candidate_match.group(1))
            note = (candidate_match.group(2) or "").strip()
            chosen = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict) and item.get("index") == selected
                ),
                None,
            )
            if chosen:
                expanded = render_template(str(chosen.get("prompt") or ""), note=note)
                if expanded:
                    state["last_scene"] = str(chosen.get("scene") or "")
                    message = (
                        f"【Autopilot 候选指令 #{selected} 展开】用户选择了第 {selected} 条"
                        f"候选回应，请严格按以下要求执行：\n\n{expanded}"
                    )
                    log_event(
                        "candidate_expanded",
                        cid=conversation_id,
                        index=selected,
                        candidate_turn=candidate_turn,
                    )

        if not message:
            comma_match = COMMA_RE.match(text)
            if comma_match:
                alias = comma_match.group(1).lower()
                arg = (comma_match.group(2) or "").strip()
                command = find_comma_command(config, alias)
                if command:
                    expanded = render_template(str(command.get("template") or ""), arg=arg)
                    if expanded:
                        state["last_scene"] = str(command.get("scene") or "")
                        state["workflow_type"] = str(command.get("workflow_type") or "")
                        message = (
                            f"【Autopilot 快捷命令 `{alias}` 展开】"
                            f"请严格按以下完整要求执行：\n\n{expanded}"
                        )

        if not message:
            entry_match = ENTRY_RE.match(text)
            if entry_match:
                key = entry_match.group(1)
                arg = (entry_match.group(2) or "").strip()
                spec = (config.get("entry_shortcuts") or {}).get(key)
                if isinstance(spec, dict):
                    requires_arg = bool(spec.get("requires_arg", False))
                    if not requires_arg or arg:
                        expanded = render_template(str(spec.get("template") or ""), arg=arg)
                        if expanded:
                            state["workflow_type"] = str(spec.get("workflow_type") or "")
                            message = (
                                f"【Autopilot 启动口令 `{key}` 展开】"
                                f"请严格按以下完整要求执行：\n\n{expanded}"
                            )

        if not save_state(conversation_id, state):
            log_event("pre_state_save_failed", cid=conversation_id)
        message = "\n\n".join(p for p in (worker_reload_msg, message) if p)
        return injection(message) if message else {}


def find_comma_command(config: dict[str, Any], alias: str) -> dict[str, Any] | None:
    commands = config.get("comma_commands")
    if not isinstance(commands, dict):
        return None
    for command in commands.values():
        if not isinstance(command, dict):
            continue
        aliases = [str(item).lower() for item in command.get("aliases") or []]
        if alias in aliases:
            return command
    return None


def handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    termination = str(payload.get("terminationReason") or "")
    fully_idle = payload.get("fullyIdle")
    error = str(payload.get("error") or "").strip()
    conversation_id = str(payload.get("conversationId") or "")
    if termination != "NO_TOOL_CALL" or fully_idle is not True or error or not conversation_id:
        return {"decision": "allow"}
    steps, complete = read_steps(payload.get("transcriptPath"))
    if not complete:
        log_event("stop_transcript_incomplete", cid=conversation_id)
        return {"decision": "allow"}
    if not steps or is_dispatched_worker(steps):
        return {"decision": "allow"}

    turn_id = turn_identity(steps)
    user_turn = turn_user_identity(steps)
    if not user_turn:
        log_event("stop_turn_identity_missing", cid=conversation_id)
        return {"decision": "allow"}
    with state_lock(conversation_id):
        state = load_state(conversation_id)
        if state is None:
            log_event("stop_state_corrupt", cid=conversation_id)
            return {"decision": "allow"}
        current_user_turn = str(state.get("current_user_turn_id") or "")
        if current_user_turn and current_user_turn != user_turn:
            log_event(
                "stop_stale_turn",
                cid=conversation_id,
                current=current_user_turn,
                incoming=user_turn,
            )
            return {"decision": "allow"}
        state["current_user_turn_id"] = user_turn
        turn_steps = current_turn_steps(steps)
        unsettled_reason = ask_question_unsettled_reason(turn_steps)
        if unsettled_reason:
            clear_suggestion(state)
            save_state(conversation_id, state)
            log_event(
                "stop_waiting_ask_question",
                cid=conversation_id,
                reason=unsettled_reason,
            )
            return {"decision": "allow"}
        if state.get("last_settled_turn_id") == turn_id or state.get(
            "delivery_status"
        ) in {"pending", "unknown"}:
            log_event(
                "stop_delivery_frozen",
                cid=conversation_id,
                turn_id=turn_id,
                status=str(state.get("delivery_status") or ""),
            )
            return {"decision": "allow"}

        planners = [step for step in turn_steps if step.get("type") == "PLANNER_RESPONSE"]
        if not planners:
            clear_suggestion(state)
            save_state(conversation_id, state)
            return {"decision": "allow"}

        parsed = parse_suggestion(str(planners[-1].get("content") or ""))
        if parsed["invalid"]:
            clear_suggestion(state)
            save_state(conversation_id, state)
            log_event(
                "stop_invalid_tag",
                cid=conversation_id,
                reason=str(parsed["invalid"]),
            )
            return {"decision": "allow"}

        scenes = scan_scenes()
        scene = str(parsed["scene"])
        if scene == "NONE" or not parsed["prompt"] or scene not in scenes:
            clear_suggestion(state)
            save_state(conversation_id, state)
            log_event("stop_without_suggestion", cid=conversation_id, scene=scene or "")
            return {"decision": "allow"}

        mode = current_mode()
        if mode == "off":
            clear_suggestion(state)
            save_state(conversation_id, state)
            log_event("stop_mode_off", cid=conversation_id)
            return {"decision": "allow"}

        gate = resolve_gate(scene, str(parsed["gate"]), scenes)
        primary = str(parsed["prompt"])
        candidates = [{"index": 1, "scene": scene, "prompt": primary}]
        candidates.extend(
            {"index": int(item["index"]), "scene": scene, "prompt": item["prompt"]}
            for item in parsed["alternatives"]
        )
        state.update(
            {
                "scene": scene,
                "gate_action": gate,
                "primary_text": primary,
                "candidates": candidates,
                "candidate_turn_id": turn_id,
            }
        )
        workspaces = payload.get("workspacePaths") or []
        workspace_path = str(workspaces[0]) if isinstance(workspaces, list) and workspaces else ""
        explicit_handle = str(payload.get("terminalHandle") or "")

        auto_eligible = mode == "gate" and gate == "auto"
        auto_submit = auto_eligible and int(
            state.get("consecutive_auto_turns") or 0
        ) < max_consecutive_turns()
        if auto_submit:
            handle = resolve_terminal(workspace_path, explicit_handle)
            if not handle:
                state["last_settled_turn_id"] = turn_id
                state["auto_submitted_text"] = ""
                save_state(conversation_id, state)
                log_event("auto_submit_no_terminal", cid=conversation_id, scene=scene)
                return {"decision": "allow"}
            previous_count = int(state.get("consecutive_auto_turns") or 0)
            previous_text = str(state.get("auto_submitted_text") or "")
            state["consecutive_auto_turns"] = previous_count + 1
            state["auto_submitted_text"] = primary
            state["last_settled_turn_id"] = turn_id
            state["delivery_status"] = "pending"
            if not save_state(conversation_id, state):
                log_event("auto_submit_state_failed", cid=conversation_id, scene=scene)
                return {"decision": "allow"}
            outcome = send_to_terminal(handle, primary, enter=True)
            if outcome == "accepted":
                state["delivery_status"] = "accepted"
                log_event("auto_submitted", cid=conversation_id, scene=scene)
            elif outcome == "rejected":
                state["delivery_status"] = "rejected"
                state["consecutive_auto_turns"] = previous_count
                state["auto_submitted_text"] = previous_text
                state["last_settled_turn_id"] = ""
                log_event("auto_submit_rejected", cid=conversation_id, scene=scene)
            else:
                state["delivery_status"] = "unknown"
                log_event("auto_submit_unknown", cid=conversation_id, scene=scene)
            save_state(conversation_id, state)
        else:
            # The cap (or a pause) is the decision for this turn: keep the auto
            # count and mark the turn settled so a repeated Stop cannot upgrade
            # the pause into an auto-submit. Only a confirmed new user turn
            # restarts the budget in PreInvocation.
            state["last_settled_turn_id"] = turn_id
            state["auto_submitted_text"] = ""
            if mode in {"gate", "confirm"}:
                handle = resolve_terminal(workspace_path, explicit_handle)
                if handle and send_to_terminal(handle, primary, enter=False) != "accepted":
                    log_event("prefill_not_accepted", cid=conversation_id, scene=scene)
            log_event("prefilled_only", cid=conversation_id, scene=scene, gate=gate, mode=mode)
            save_state(conversation_id, state)
        return {"decision": "allow"}


def render_scenes_block(scenes: dict[str, dict[str, Any]]) -> str:
    entries: list[tuple[int, str, str]] = []
    for name, entry in scenes.items():
        meta = entry["meta"]
        order = int(meta.get("order") or 0)
        gate = scene_gate_default(name, scenes)
        heading = (
            f"### 场景 `{name}` — {meta.get('title') or name}\n"
            f"- 触发：{meta.get('description') or ''}\n"
            f"- 门禁：{gate}\n\n"
            f"{str(entry['body']).strip()}"
        )
        entries.append((order, name, heading))
    if not entries:
        return "（未启用场景：本机只使用 Orca 编排规则，不做回合结束预填。）"
    entries.sort(key=lambda item: (item[0], item[1]))
    return "\n\n".join(item[2] for item in entries)


def command_sync() -> int:
    # Serialize with apply/uninstall and re-read every input under the lock so a
    # sync that started before an uninstall cannot write the managed block back.
    with _file_lock(AUTOPILOT_DIR / ".autopilot.lock"):
        if not PROFILE_PATH.is_file():
            print(
                "未安装（缺少 profile.json）；请先运行 setup_autopilot.py apply。",
                file=sys.stderr,
            )
            return 2
        try:
            text = GEMINI_MD_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"无法读取 GEMINI.md：{exc}", file=sys.stderr)
            return 2
        starts = [match.start() for match in re.finditer(re.escape(SCENES_BEGIN), text)]
        ends = [match.start() for match in re.finditer(re.escape(SCENES_END), text)]
        if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
            print("GEMINI.md 的场景托管标记缺失、重复或顺序错误。", file=sys.stderr)
            return 2
        inner = f"{SCENES_BEGIN}\n\n{render_scenes_block(scan_scenes())}\n\n{SCENES_END}"
        merged = text[: starts[0]] + inner + text[ends[0] + len(SCENES_END) :]
        if not save_text(GEMINI_MD_PATH, merged):
            print("无法写入 GEMINI.md。", file=sys.stderr)
            return 2
    log_event("scenes_synced")
    print("已按 scenes/*.md 刷新 GEMINI.md 场景段。")
    return 0


def command_status(as_json: bool) -> int:
    profile = load_profile()
    scenes = scan_scenes()
    config = load_config()
    hooks = load_json(GEMINI_DIR / "config" / "hooks.json")
    report = {
        "gemini_dir": str(GEMINI_DIR),
        "mode": current_mode(profile),
        "worker_agents": current_worker_agents(profile),
        "max_consecutive_auto_turns": max_consecutive_turns(profile),
        "scenes": sorted(scenes),
        "entry_shortcuts": sorted((config.get("entry_shortcuts") or {})),
        "comma_commands": sorted(
            {
                alias
                for command in (config.get("comma_commands") or {}).values()
                if isinstance(command, dict)
                for alias in (command.get("aliases") or [])
            }
        ),
        "hooks_group": "orca-agy-autopilot" in hooks,
        "engine": str(Path(__file__).resolve()),
        "profile": str(PROFILE_PATH),
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"mode: {report['mode']}  max_turns: {report['max_consecutive_auto_turns']}")
        print("scenes: " + (", ".join(report["scenes"]) or "（无）"))
        print("entry: " + (", ".join(report["entry_shortcuts"]) or "（无）"))
        print("hooks_group: " + ("yes" if report["hooks_group"] else "no"))
    return 0


def _command_identity(
    config: dict[str, Any], target: str
) -> tuple[str, str] | None:
    entry_shortcuts = config.get("entry_shortcuts")
    entry_shortcuts = entry_shortcuts if isinstance(entry_shortcuts, dict) else {}
    commands = config.get("comma_commands")
    commands = commands if isinstance(commands, dict) else {}
    bare = target.strip()
    if bare in entry_shortcuts and isinstance(entry_shortcuts[bare], dict):
        entry = entry_shortcuts[bare]
        return str(entry.get("canonical_key") or bare), str(entry.get("command_id") or "")
    alias = (bare if bare.startswith(",") else f",{bare}").lower()
    for key, command in commands.items():
        if not isinstance(command, dict):
            continue
        aliases = [str(item).lower() for item in command.get("aliases") or []]
        if alias not in aliases:
            continue
        canonical = str(
            command.get("canonical_key")
            or (aliases[0].lstrip(",") if aliases else alias.lstrip(","))
        )
        return canonical, str(command.get("command_id") or key)
    return None


def command_set(target: str, template: str) -> int:
    template = template.strip()
    if not template:
        print("模板不能为空。", file=sys.stderr)
        return 2
    with _file_lock(AUTOPILOT_DIR / ".autopilot.lock"):
        profile = load_profile()
        config = load_config()
        if not profile or not config:
            print(
                "profile.json 或 config.json 不存在；请先运行 setup_autopilot.py apply。",
                file=sys.stderr,
            )
            return 2
        identity = _command_identity(config, target)
        if identity is None:
            print(f"未知口令 {target!r}；请用 status 查看可用口令。", file=sys.stderr)
            return 2
        canonical, command_id = identity
        entry_shortcuts = config.get("entry_shortcuts")
        entry_shortcuts = entry_shortcuts if isinstance(entry_shortcuts, dict) else {}
        commands = config.get("comma_commands")
        commands = commands if isinstance(commands, dict) else {}
        updated = False
        for key, entry in entry_shortcuts.items():
            if not isinstance(entry, dict):
                continue
            if key == canonical or (command_id and entry.get("command_id") == command_id):
                entry["template"] = template
                updated = True
        for key, command in commands.items():
            if not isinstance(command, dict):
                continue
            aliases = [str(item).lower() for item in command.get("aliases") or []]
            if (
                key == command_id
                or command.get("command_id") == command_id
                or f",{canonical}".lower() in aliases
            ):
                command["template"] = template
                updated = True
        if not updated:
            print(f"未知口令 {target!r}；请用 status 查看可用口令。", file=sys.stderr)
            return 2
        shortcuts = profile.setdefault("shortcuts", {})
        overrides = shortcuts.setdefault("template_overrides", {})
        overrides[canonical] = template
        if not save_json(PROFILE_PATH, profile):
            print("profile.json 写入失败。", file=sys.stderr)
            return 2
        if not save_json(CONFIG_PATH, config):
            print("config.json 写入失败；下次 apply --upgrade 会重新编译。", file=sys.stderr)
            return 2
        log_event("template_set", target=target, key=canonical, command_id=command_id)
        print(f"已更新 `{canonical}` 的模板；下一次 apply/upgrade 会保留该覆盖。")
        return 0


def command_log(limit: int) -> int:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        print("（暂无 autopilot 日志）")
        return 0
    except OSError as exc:
        print(f"无法读取日志：{exc}", file=sys.stderr)
        return 2
    for line in lines[-limit:]:
        print(line)
    return 0


def run_hook(event: str) -> int:
    payload = read_payload()
    try:
        if event == "PreInvocation":
            result = handle_pre_invocation(payload)
        else:
            result = handle_stop(payload)
    except Exception as exc:
        log_event("hook_failed", event=event, error=repr(exc))
        result = {} if event == "PreInvocation" else {"decision": "allow"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


def command_worker(target: str, as_json: bool) -> int:
    success, workers, updated = (True, current_worker_agents(), []) if target == "status" else set_worker_agents(target)
    report = {"ok": success, "worker_agents": workers, "updated_files": updated}
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(worker_priority(workers))
        if not success:
            print("Worker switch failed: check profile.json and derived guidance files.", file=sys.stderr)
        for path in updated:
            print(f"updated: {path}")
    return 0 if success else 2


def command_mode(target: str) -> int:
    if target != "status" and not set_mode(target):
        print("Mode switch failed: profile.json is missing or unwritable.", file=sys.stderr)
        return 2
    print(f"Autopilot mode: {current_mode()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autopilot.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker", help="切换或查看 Worker 执行器优先级")
    worker.add_argument("target", nargs="?", default="status", choices=("codex", "claude", "claude-code", "cc", "status"))
    worker.add_argument("--json", action="store_true")
    mode = subparsers.add_parser("mode", help="切换或查看 Autopilot 模式")
    mode.add_argument("target", nargs="?", default="status", choices=(*MODES, "status"))
    hook = subparsers.add_parser("hook", help="运行 Antigravity 生命周期 Hook")
    hook.add_argument("--event", required=True, choices=("PreInvocation", "Stop"))
    status = subparsers.add_parser("status", help="查看模式、场景与 Hook 状态")
    status.add_argument("--json", action="store_true")
    subparsers.add_parser("sync", help="按 scenes/*.md 刷新 GEMINI.md 场景段")
    setter = subparsers.add_parser("set", help="修改开局/逗号口令模板")
    setter.add_argument("target")
    setter.add_argument("template")
    logger = subparsers.add_parser("log", help="查看最近事件")
    logger.add_argument("--limit", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "worker":
        return command_worker(args.target, args.json)
    if args.command == "mode":
        return command_mode(args.target)
    if args.command == "hook":
        return run_hook(args.event)
    if args.command == "status":
        return command_status(args.json)
    if args.command == "sync":
        return command_sync()
    if args.command == "set":
        return command_set(args.target, args.template)
    if args.command == "log":
        return command_log(args.limit)



if __name__ == "__main__":
    raise SystemExit(main())
