#!/usr/bin/env python3
"""Single-file Turn Autopilot runtime for Antigravity + Orca.

The engine is deliberately small and mechanical:

* PreInvocation expands `,mode`, `,1`/`,2`/`,3`, custom `,` commands and the
  short entry commands, then records whether the new turn was auto-submitted.
* Stop reads the trailing structured suggestion tag, stores the
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
AUTO_UPDATE_INTERVAL_SECONDS = 6 * 3600
_RUNNING_SCRIPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

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

def _find_all_positions(text: str, marker: str) -> list[int]:
    positions: list[int] = []
    start = text.find(marker)
    while start >= 0:
        positions.append(start)
        start = text.find(marker, start + len(marker))
    return positions


def _command_parts(text: str, *, comma: bool = False) -> tuple[str, str]:
    if comma:
        if not text.startswith(","):
            return "", ""
        text = text[1:].lstrip()
    parts = text.split(maxsplit=1)
    return (parts[0], parts[1].strip() if len(parts) == 2 else "") if parts else ("", "")


def _named_command(text: str, names: set[str], targets: set[str], default: str = "") -> str:
    name, arg = _command_parts(text, comma=True)
    if name.lower() not in names:
        return ""
    target = arg.lower() or default
    return target if target in targets else ""


def _shortcut_parts(text: str, *, comma: bool = False) -> tuple[str, str]:
    # Custom aliases do not allow whitespace between the comma and their name.
    raw = text[1:] if comma and text.startswith(",") else text
    if comma and (not text.startswith(",") or not raw or raw[0].isspace()):
        return "", ""
    key, arg = _command_parts(raw)
    checked = key.lower() if comma else key
    if not checked or not all(char in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in checked):
        return "", ""
    if not comma and checked[0] not in "abcdefghijklmnopqrstuvwxyz":
        return "", ""
    return ("," + checked if comma else checked), arg


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


def _pilot_binary() -> str:
    override = os.environ.get("ORCA_AUTOPILOT_PILOT_BIN", "").strip()
    local = Path.home() / "the-pilot/bin/pilot"
    if override:
        candidate = override
    elif (local.parent.parent / ".git").exists() and local.is_file() and os.access(local, os.X_OK):
        candidate = str(local)
    else:
        candidate = shutil.which("pilot") or str(local)
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else ""


def maybe_sync_project_overlay(payload: dict[str, Any]) -> None:
    workspace_paths = payload.get("workspacePaths")
    if not isinstance(workspace_paths, list):
        workspace_paths = []
    if (os.environ.get("ORCA_AUTOPILOT_GEMINI_DIR", "").strip()
            and os.environ.get("ORCA_AUTOPILOT_ENABLE_AUTO_UPDATE", "").strip() != "1"
            and not os.environ.get("ORCA_AUTOPILOT_PILOT_BIN", "").strip()):
        return
    pilot_bin = _pilot_binary()
    if not pilot_bin:
        return
    workspace = str(workspace_paths[0]) if workspace_paths else ""
    command = [pilot_bin, "self-update", "--project-only"]
    if workspace:
        command.extend(["--workspace", workspace])
    command.extend(["--home-dir", str(Path.home()), "--gemini-dir", str(GEMINI_DIR), "--json"])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_event("project_auto_sync_failed", workspace=workspace, error=str(exc))
        return
    if result.returncode != 0:
        log_event(
            "project_auto_sync_failed",
            workspace=workspace,
            returncode=result.returncode,
            error=(result.stderr or result.stdout).strip()[-300:],
        )
        return
    try:
        receipt = json.loads(result.stdout)
    except ValueError as exc:
        log_event("project_auto_sync_failed", workspace=workspace, error=str(exc))
        return
    if isinstance(receipt, dict) and (receipt.get("project_status") == "updated" or receipt.get("local_source_status") == "updated"):
        log_event("project_auto_synced", workspace=workspace)


def maybe_trigger_auto_update() -> None:
    if os.environ.get("ORCA_AUTOPILOT_DISABLE_AUTO_UPDATE", "").strip() == "1":
        return
    if (os.environ.get("ORCA_AUTOPILOT_GEMINI_DIR", "").strip()
            and os.environ.get("ORCA_AUTOPILOT_ENABLE_AUTO_UPDATE", "").strip() != "1"):
        return
    metadata_path = STATE_DIR / "auto_update.json"
    try:
        metadata = load_json(metadata_path)
        if time.time() - float(metadata.get("last_checked_at") or 0) < AUTO_UPDATE_INTERVAL_SECONDS:
            return
    except (TypeError, ValueError):
        metadata = {}
    pilot_bin = _pilot_binary()
    if not pilot_bin:
        return
    with _file_lock(STATE_DIR / "auto_update.lock"):
        metadata = load_json(metadata_path)
        try:
            cooldown = time.time() - float(metadata.get("last_checked_at") or 0)
        except (TypeError, ValueError):
            cooldown = AUTO_UPDATE_INTERVAL_SECONDS
        if cooldown < AUTO_UPDATE_INTERVAL_SECONDS:
            return
        try:
            subprocess.Popen([pilot_bin, "self-update", "--gemini-dir", str(GEMINI_DIR), "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            metadata["status"] = "failed"
            metadata["error"] = str(exc)
            metadata["last_failed_at"] = time.time()
            save_json(metadata_path, metadata)
            log_event("auto_update_spawn_failed", path=pilot_bin, error=str(exc))
            return
        metadata["last_checked_at"] = time.time()
        if not save_json(metadata_path, metadata):
            return
        log_event("auto_update_spawned", path=pilot_bin)


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


def _replace_until(text: str, marker: str, stops: str, replacement: str) -> str:
    position = 0
    while (start := text.find(marker, position)) >= 0:
        start += len(marker)
        end = start
        while end < len(text) and text[end] not in stops:
            end += 1
        text = text[:start] + replacement + text[end:]
        position = start + len(replacement)
    return text


def replace_worker_guidance(text: str, workers: list[str]) -> str:
    """Replace executor slots without owning or reproducing orchestration rules."""
    priority = " > ".join(AGENT_LABELS[agent] for agent in workers)
    primary, secondary = workers
    for marker in ("Orca Orchestration：", "执行器优先级："):
        text = _replace_until(text, marker, "）\n", priority)
    for old_agent, label in (("codex", "Codex"), ("claude", "Claude Code")):
        old = f"{label}（`--agent {old_agent}`）"
        # Only the explicitly labelled default slot is executable guidance.
        for prefix in ("默认优先使用 ", "默认优先使用", "默认优先 ", "优先使用 ", "优先使用"):
            start = 0
            while (index := text.find(prefix + old, start)) >= 0:
                end = index + len(prefix + old)
                for other, other_label in (("codex", "Codex"), ("claude", "Claude Code")):
                    suffix = f"，其次使用 {other_label}（`--agent {other}`）"
                    if text.startswith(suffix, end):
                        end += len(suffix)
                        break
                replacement = f"{prefix}{AGENT_LABELS[primary]}（`--agent {primary}`），其次使用 {AGENT_LABELS[secondary]}（`--agent {secondary}`）"
                text = text[:index] + replacement + text[end:]
                start = index + len(replacement)
    lines = []
    for line in text.splitlines(keepends=True):
        position = 0
        while (start := line.find("--worktree ", position)) >= 0:
            end = start
            while end < len(line) and line[end] not in "`\n":
                end += 1
            segment = line[start:end]
            before, marker, after = segment.partition("--agent ")
            token = after.split(maxsplit=1)[0] if after else ""
            if marker and token in {"codex", "claude"}:
                segment = before + marker + primary + after[len(token):]
                line = line[:start] + segment + line[end:]
            position = start + len(segment)
        lines.append(line)
    text = "".join(lines)
    for old_agent in ("codex", "claude"):
        text = text.replace(f"次选 `--agent {old_agent}`", f"次选 `--agent {secondary}`")
    for marker in ("（或 `task-create`", "（`orca worktree create`"):
        text = text.replace(marker, f"（次选 `--agent {secondary}`；" + marker[1:])
    return text


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
                marker = "<!-- BEGIN ORCA_AUTOPILOT_PROFILE -->"
                start = text.find(marker)
                content_start = start + len(marker)
                boundaries = [text.find(end, content_start) for end in (
                    "## 个人研发状态机与 Turn Autopilot 规范", "<!-- END ORCA_AUTOPILOT_PROFILE -->"
                )]
                boundaries = [end for end in boundaries if end >= 0]
                if start < 0 or not boundaries:
                    return False, old_workers, updated
                end = min(boundaries)
                text = text[:content_start] + replace_worker_guidance(text[content_start:end], new_workers) + text[end:]
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
    start = raw.find("<USER_REQUEST>")
    end = raw.find("</USER_REQUEST>", start + len("<USER_REQUEST>")) if start >= 0 else -1
    return (raw[start + len("<USER_REQUEST>"):end] if end >= 0 else raw).strip()


def _has_worker_preamble(text: str) -> bool:
    for line in text.splitlines():
        key, colon, value = line.partition(":")
        token = value.strip()
        if colon and key.strip().lower() in {"task id", "dispatch id"} and len(token) >= 6:
            if all(char.isascii() and (char.isalnum() or char in "_-") for char in token):
                return True
    return False


def is_dispatched_worker(steps: list[dict[str, Any]], text: str = "") -> bool:
    for key in ("ORCA_DISPATCH_ID", "ORCA_TASK_ID", "ORCA_WORKER_DISPATCH_ID"):
        if os.environ.get(key, "").strip():
            return True
    if text and _has_worker_preamble(text):
        return True
    for step in steps[:4]:
        if step.get("type") == "USER_INPUT":
            return bool(_has_worker_preamble(str(step.get("content") or "")))
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
        else:
            return {}, "invalid_json"
    return values, ""


def _parse_tag_body(body: str) -> tuple[dict[str, str], str]:
    text = body.strip()
    if not text:
        return {}, "empty_tag"
    if text.startswith("{"):
        return _parse_json_tag(text)
    if text.lower() == "scene=none":
        return {"scene": "NONE"}, ""
    return {}, "invalid_json"


def _has_unquoted_comment_delimiter(payload: str) -> bool:
    if not payload.startswith("{"):
        return "<!--" in payload or "-->" in payload
    in_string = False
    escape = False
    for index, char in enumerate(payload):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif payload.startswith("<!--", index) or payload.startswith("-->", index):
            return True
    return False


def _trailing_tag(text: str) -> tuple[str, str] | None:
    trimmed = text.rstrip()
    if not trimmed.endswith("-->"):
        return None
    content = trimmed[:-3]
    for position in _find_all_positions(content, "<!--"):
        body = content[position + 4:].strip()
        if not body.lower().startswith("autopilot"):
            continue
        rest = body[len("autopilot"):]
        if rest and not (rest[0].isspace() or rest[0] == ":"):
            continue
        payload = rest.lstrip().removeprefix(":").strip()
        if _has_unquoted_comment_delimiter(payload):
            continue
        return content[:position], payload
    return None


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
    tag = _trailing_tag(text or "")
    if tag is None:
        return _invalid_suggestion("missing_tag")
    prefix, body = tag
    if _trailing_tag(prefix) is not None:
        return _invalid_suggestion("multiple_tags")
    values, tag_error = _parse_tag_body(body)
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

    primary, conflict = _tag_field(values, ("1", "p1", "prompt"))
    if conflict:
        return _invalid_suggestion("conflicting_prompt")
    if not primary:
        return _invalid_suggestion("missing_prompt")
    alternatives: list[dict[str, Any]] = []
    seen = {primary}
    for index, names in ((2, ("2", "p2", "alt")), (3, ("3", "p3"))):
        value, conflict = _tag_field(values, names)
        if conflict:
            return _invalid_suggestion("duplicate_alternative")
        if value and value not in seen:
            alternatives.append({"index": index, "prompt": value})
            seen.add(value)
    return {
        "scene": scene,
        "prompt": primary,
        "alternatives": alternatives,
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


def _is_response_header(line: str) -> bool:
    header, colon, _ = line.partition(":")
    return bool(colon) and " ".join(header.lower().split()) in {"created at", "completed at"}


def _answer_body_lines(content: str) -> list[str]:
    """Return the answer lines without the runtime's timestamp header."""
    return [line for line in content.splitlines() if not _is_response_header(line)]


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
        header, colon, answer = line.partition(":")
        digits = header[1:]
        if colon and header.startswith("A") and digits and digits[0] in "123456789" and digits.isdecimal():
            index = int(digits)
            if index in chunks:
                return None
            chunks[index] = [answer.lstrip(" \t")]
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

    maybe_sync_project_overlay(payload)
    if os.environ.get("ORCA_AUTOPILOT_REEXEC") != "1":
        updated_engine = AUTOPILOT_DIR / "autopilot.py"
        try:
            changed = updated_engine.is_file() and hashlib.sha256(updated_engine.read_bytes()).hexdigest() != _RUNNING_SCRIPT_SHA256
        except OSError:
            changed = False
        if changed:
            try:
                result = subprocess.run(
                    [sys.executable, str(updated_engine), *sys.argv[1:]],
                    input=json.dumps(payload, ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    timeout=10,
                    env={**os.environ, "ORCA_AUTOPILOT_REEXEC": "1"},
                )
                if result.returncode == 0:
                    try:
                        return json.loads(result.stdout) if result.stdout.strip() else {}
                    except ValueError as exc:
                        log_event("engine_reexec_failed", error=str(exc))
                else:
                    log_event("engine_reexec_failed", returncode=result.returncode, error=(result.stderr or result.stdout).strip()[-300:])
            except (OSError, subprocess.SubprocessError) as exc:
                log_event("engine_reexec_failed", error=str(exc))
    maybe_trigger_auto_update()
    worker_target = _named_command(text, {"worker", "agent"}, {"codex", "claude", "claude-code", "cc", "status"}, "status")
    mode_target = _named_command(text, {"mode"}, {"gate", "confirm", "off", "status"})
    config = load_config()
    profile = load_profile()
    user_turn = turn_user_identity(steps)
    recovery_requested = bool(_named_command(text, {"autopilot"}, {"recover"}))
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

        if worker_target:
            target = worker_target
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

        if mode_target:
            target = mode_target
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
        candidate, note = _command_parts(text, comma=True)
        if candidate in {"1", "2", "3"}:
            selected = int(candidate)
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
            alias, arg = _shortcut_parts(text, comma=True)
            if alias:
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
            key, arg = _shortcut_parts(text)
            if key:
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


def _has_unasked_question_lead_in(planner_step: dict[str, Any]) -> bool:
    """Return whether a final planner reply stops at a question lead-in.

    Long Antigravity answers can end right after `请确认以下 2 个问题：`
    without ever emitting the question tool call. Both `content` and
    `tool_calls` must be complete (`TRUNCATING_STEP_FIELDS` must not intersect
    the step) and `tool_calls`, when present, must be a real list; only then
    does a last non-code-fence line ending with a colon prove that shape. A
    truncated prefix ending in a colon is never evidence.
    """
    if planner_step.get("type") != "PLANNER_RESPONSE":
        return False
    if planner_step.get("tool_calls"):
        return False
    if TRUNCATING_STEP_FIELDS.intersection(_truncated_fields(planner_step)):
        return False
    if "tool_calls" in planner_step and not isinstance(planner_step.get("tool_calls"), list):
        return False
    content = planner_step.get("content")
    if not isinstance(content, str):
        return False
    trimmed = content.rstrip()
    if not trimmed:
        return False
    if sum(1 for line in trimmed.splitlines() if line.lstrip().startswith("```")) % 2 == 1:
        return False
    last_line = trimmed.splitlines()[-1].strip()
    if not last_line or last_line.startswith("```") or last_line.endswith("```"):
        return False
    return last_line.endswith(("：", ":"))


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
            last_planner = planners[-1]
            if (
                parsed["invalid"] == "missing_tag"
                and current_mode() != "off"
                and not bool(payload.get("continuationActive"))
                and state.get("continued_unasked_turn_id") != user_turn
                and turn_steps[-1] is last_planner
                and _has_unasked_question_lead_in(last_planner)
            ):
                clear_suggestion(state)
                state["continued_unasked_turn_id"] = user_turn
                if not save_state(conversation_id, state):
                    log_event(
                        "stop_continue_state_failed",
                        cid=conversation_id,
                        turn_id=turn_id,
                    )
                    return {"decision": "allow"}
                log_event(
                    "stop_continue_unasked_question",
                    cid=conversation_id,
                    turn_id=turn_id,
                )
                return {
                    "decision": "continue",
                    "reason": (
                        "上一条回复在冒号引导语处提前结束，既未发出 ask_question 工具调用，"
                        "也未输出回合结束标签。若需向用户确认问题，请立即直接调用 ask_question "
                        "工具（不要重复已输出的正文，同一步不写 autopilot 标签）；若无需提问，"
                        "请补齐收束结论与末尾 <!-- autopilot: ... --> 标签。"
                    ),
                }
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
        starts = _find_all_positions(text, SCENES_BEGIN)
        ends = _find_all_positions(text, SCENES_END)
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
