#!/usr/bin/env python3
"""Single-file Turn Pilot runtime for Antigravity + Orca.

The engine is deliberately small and mechanical:

* PreInvocation expands `,mode`, `,1`/`,2`/`,3`, custom `,` commands and the
  short entry commands, then records whether the new turn was auto-submitted.
* Stop reads the trailing structured suggestion tag and stores the candidates.
  It pre-fills a draft or submits it once and settles the receipt before returning.
* Everything durable lives in `profile.json` (preferences) and a per-session
  JSON file (conversation turn state). No delivery ledger, no install marker.

Global settings come from the installation. Repository shortcuts come from
the active workspace and live only for the current PreInvocation.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import select
import shlex
import signal
import stat
from collections.abc import Collection, Mapping, Sequence
from contextlib import contextmanager
from functools import cache
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Literal, TypeGuard


PILOT_SUBDIR = Path("config") / "the-pilot"
LEGACY_AUTOPILOT_SUBDIR = Path("config") / "autopilot"
PROFILE_BLOCK_HEADING = "## 个人研发状态机与 Turn Pilot 规范"
LEGACY_PROFILE_BLOCK_HEADING = "## 个人研发状态机与 Turn Autopilot 规范"


def _env_value(primary: str, legacy: str) -> str:
    """Read the canonical environment variable, then its legacy fallback."""
    return os.environ.get(primary, "").strip() or os.environ.get(legacy, "").strip()


def _gemini_dir() -> Path:
    override = _env_value("THE_PILOT_GEMINI_DIR", "ORCA_AUTOPILOT_GEMINI_DIR")
    if override:
        return Path(override).expanduser()
    script = Path(__file__)
    return (script.resolve() if __name__ == "__main__" else script.absolute()).parents[2]


GEMINI_DIR = _gemini_dir()
PILOT_DIR = GEMINI_DIR / PILOT_SUBDIR
PROFILE_PATH = PILOT_DIR / "profile.json"
CONFIG_PATH = PILOT_DIR / "config.json"
SCENES_DIR = PILOT_DIR / "scenes"
GEMINI_MD_PATH = GEMINI_DIR / "config" / "GEMINI.md"
STATE_DIR = Path(
    _env_value("THE_PILOT_STATE_DIR", "ORCA_AUTOPILOT_STATE_DIR")
    or str(PILOT_DIR / "state")
).expanduser()
LOG_PATH = STATE_DIR / "pilot.log"
LOG_ROTATE_BYTES = 512 * 1024
# The installer gives its own ``apply`` child 30 s; this outer deadline must be
# longer so a legitimate repair always finishes before the hook may kill it.
LOCAL_SYNC_TIMEOUT_SECONDS = 45
STOP_EXIT_TIMEOUT_SECONDS = 2.0
CONFIRM_TIMEOUT_SECONDS = 2.0
CONFIRM_DEADLINE_SECONDS = 5.0

WORKER_AGENT_ORDER = ("claude", "codex", "agy")
WORKER_AGENT_ALIASES = {
    "claude": "claude", "claude-code": "claude", "cc": "claude",
    "codex": "codex", "agy": "agy", "antigravity": "agy",
}
ORCA_AGENT_IDS = {"claude": "claude", "codex": "codex", "agy": "antigravity"}
AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex", "agy": "AGY"}
WORKER_EFFORT_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
WORKER_TARGETS = (*WORKER_AGENT_ALIASES, "status")
MODES = ("gate", "confirm", "off")
PROFILE_SCHEMA = "the-pilot-profile/v1"
LEGACY_PROFILE_SCHEMA = "orca-autopilot-profile/v1"
PROJECT_SCHEMA = "the-pilot-project/v1"
UNIVERSAL_ENTRY_SPECS = {
    "r": {
        "command_id": "CMD_MOD_REVIEW",
        "description": "对指定功能/模块进行完整代码与最简架构 Review",
        "requires_arg": True,
        "usage": "r <module>",
        "template": (
            "详细 review 一下目前 {arg} 的整个代码和设计，从整个架构去看是否有更简洁、"
            "更加根本的设计或收敛方案，判断是否有过度设计、工程质量是否达标、是否已经达到最简架构要求"
        ),
    },
    "rt": {
        "command_id": "CMD_TOOL_REVIEW",
        "description": "以 Anthropic 内部架构师视角对指定类别工具进行整体 Review",
        "requires_arg": True,
        "usage": "rt <工具类别>",
        "template": "再整体 review 下 {arg} 相关工具，以 anthropic 内部架构师的视角，工具的设计是否最佳，是否已经最简洁",
    },
    "d": {
        "command_id": "CMD_CLARIFY_DESIGN",
        "description": "先澄清需求并产出最符合工程架构的详细设计方案，再用 Orca 编排实施",
        "requires_arg": False,
        "usage": "d [需求描述]",
        "template": "你先和我澄清需求和产出最符合工程架构的详细设计方案。然后再用项目的 orca 编排模式来推进子会话/子工作区实施",
    },
    "pull": {
        "command_id": "CMD_GIT_PULL",
        "description": "先拉下代码到最新 origin/main",
        "requires_arg": False,
        "usage": "pull",
        "template": "先拉下代码到最新 origin/main",
    },
}
UNIVERSAL_ENTRY_KEYS = tuple(UNIVERSAL_ENTRY_SPECS)
RESERVED_COMMA_ALIASES = frozenset({"1", "2", "3", "mode", "worker", "agent", "pilot", "autopilot"})
LEGACY_PROJECT_FIELDS = frozenset({"presets", "scene_specs", "deliver_content", "feedback_closeout"})
RECOVERY_FAILED_MESSAGE = (
    "【Pilot 恢复失败】状态文件未能安全重建，自动投递保持暂停；"
    "请用一句话告知用户，无需执行其他操作。"
)
GEMINI_BEGIN = "<!-- BEGIN THE_PILOT_PROFILE -->"
GEMINI_END = "<!-- END THE_PILOT_PROFILE -->"
SCENES_BEGIN = "<!-- BEGIN THE_PILOT_SCENES -->"
SCENES_END = "<!-- END THE_PILOT_SCENES -->"
ORCHESTRATION_BEGIN = "<!-- BEGIN THE_PILOT_ORCHESTRATION -->"
ORCHESTRATION_END = "<!-- END THE_PILOT_ORCHESTRATION -->"
LEGACY_GEMINI_BEGIN = "<!-- BEGIN ORCA_AUTOPILOT_PROFILE -->"
LEGACY_GEMINI_END = "<!-- END ORCA_AUTOPILOT_PROFILE -->"
LEGACY_SCENES_BEGIN = "<!-- BEGIN AUTOPILOT SCENES -->"
LEGACY_SCENES_END = "<!-- END AUTOPILOT SCENES -->"
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
# Orca retains titles across PTY output gaps. Write activity first so a
# question blocks delivery before the neutral title clears the Ready title.
OSC_7501_WORKING = "\x1b]7501;state=working:app=antigravity\x07\x1b]2;\u280b Antigravity Working\x07"
OSC_7501_DONE = "\x1b]7501;state=done:app=antigravity\x07\x1b]2;Antigravity Ready\x07"
OSC_7501_WAITING_QUESTION = "\x1b]7501;state=blocked:kind=question:app=antigravity\x07\x1b]2;Antigravity\x07"
SHELL_WRAPPER_NAMES = frozenset({"sh", "dash", "bash", "zsh"})
AGY_PRINT_FLAGS = frozenset({"p", "print", "prompt"})
AGY_INTERACTIVE_PROMPT_FLAGS = frozenset({"i", "prompt-interactive"})
AGY_RESUME_BOOL_FLAGS = frozenset({"c", "continue"})
AGY_RESUME_VALUE_FLAGS = frozenset({"conversation", "r", "resume"})
AGY_BOOT_VALUE_FLAGS = (
    AGY_PRINT_FLAGS | AGY_INTERACTIVE_PROMPT_FLAGS | AGY_RESUME_VALUE_FLAGS | {
        "add-dir", "agent", "effort", "input-format", "json-schema",
        "log-file", "mode", "model", "output-format", "print-timeout", "project", "settings",
    }
)
AGY_NON_SESSION_FLAGS = AGY_PRINT_FLAGS | {"h", "help", "v", "version"}
AGY_MANAGEMENT_COMMANDS = frozenset({
    "acp", "agent", "agents", "changelog", "help", "install", "mcp", "mic-serve",
    "models", "plugin", "plugins", "remote-control", "update",
})
# A non-blocking PTY slave accepts status bytes only while its output ring
# buffer has room; drain transient backpressure for this long, then fail.
OSC_WRITE_TIMEOUT_SECONDS = 0.25
class HookExecutionError(RuntimeError):
    """A hook refused to act because its installed configuration is unusable."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

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


def _named_command(text: str, names: set[str], targets: Collection[str], default: str = "") -> str:
    name, arg = _command_parts(text, comma=True)
    if name.lower() not in names:
        return ""
    target = arg.lower() or default
    return target if target in targets else ""


def worker_command_target(text: str) -> str:
    return _named_command(text, {"worker", "agent"}, WORKER_TARGETS, "status")


def is_comma_alias(value: str) -> bool:
    return bool(value) and value[0] in "abcdefghijklmnopqrstuvwxyz0123456789" and all(
        char in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in value
    )


def is_entry_key(value: str) -> bool:
    return is_comma_alias(value) and value[0] in "abcdefghijklmnopqrstuvwxyz"


def shortcut_override_name(name: str) -> str:
    name = name.strip()
    return name.lower() if name.startswith(",") else name


def shortcut_override_owners(
    enabled_entries: Any, entries: Any, comma_commands: Any,
) -> tuple[dict[str, str], set[str]]:
    """Reserve durable and active shortcut names ahead of secondary command IDs."""
    entries = entries if isinstance(entries, dict) else {}
    commands = comma_commands if isinstance(comma_commands, dict) else {}
    enabled = enabled_entries if isinstance(enabled_entries, list) else []
    primary: dict[str, set[str]] = {}
    secondary: dict[str, set[str]] = {}

    def claim_id(spec: dict[str, Any], owner: str) -> None:
        command_id = spec.get("command_id")
        if not isinstance(command_id, str):
            return
        name = shortcut_override_name(command_id)
        if name.startswith(",") and (
            not is_comma_alias(name[1:]) or name[1:] in RESERVED_COMMA_ALIASES
        ):
            return
        if name:
            secondary.setdefault(name, set()).add(owner)

    for key in (*enabled, *entries):
        if isinstance(key, str) and key:
            primary.setdefault(key, set()).add(key)
            primary.setdefault(f",{key}", set()).add(key)
    for key, spec in entries.items():
        if isinstance(key, str) and isinstance(spec, dict):
            claim_id(spec, key)
    for spec in commands.values():
        if not isinstance(spec, dict):
            continue
        aliases = spec.get("aliases")
        if not isinstance(aliases, list):
            continue
        names = [shortcut_override_name(alias) for alias in aliases if isinstance(alias, str)]
        owner = spec.get("canonical_key") or (names[0].lstrip(",") if names else "")
        if not isinstance(owner, str) or not owner:
            continue
        for name in (owner, *names):
            primary.setdefault(name, set()).add(owner)
        claim_id(spec, owner)

    claims = {**secondary, **primary}
    return (
        {name: next(iter(owners)) for name, owners in claims.items() if len(owners) == 1},
        {name for name, owners in claims.items() if len(owners) > 1},
    )


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


def read_payload() -> tuple[dict[str, Any] | None, str]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}, ""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log_event("payload_invalid", error=str(exc))
        return None, f"invalid JSON payload: {exc}"
    if not isinstance(payload, dict):
        log_event("payload_invalid", error="not_object")
        return None, "hook payload must be a JSON object"
    return payload, ""


def log_event(event: str, **fields: Any) -> None:
    try:
        append_log_event(LOG_PATH, event, **fields)
    except OSError:
        return


def append_log_event(path: Path, event: str, /, **fields: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    archive = path.with_name(f"{path.name}.1")
    with _file_lock(path.with_name(f"{path.name}.lock")):
        try:
            if path.stat().st_size >= LOG_ROTATE_BYTES:
                os.replace(path, archive)
        except FileNotFoundError:
            pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_json(path: Path) -> tuple[dict[str, Any] | None, str, str]:
    """Read one JSON object file as ``(value, status, detail)``.

    ``status`` is ``"ok"``, ``"missing"`` or ``"corrupt"`` so callers can tell
    an absent optional file from content that must never be treated as empty
    configuration.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing", f"{path}: file does not exist"
    except (OSError, ValueError) as exc:
        log_event("json_read_failed", path=str(path), error=str(exc))
        return None, "corrupt", f"{path}: {exc}"
    if not isinstance(value, dict):
        log_event("json_not_object", path=str(path))
        return None, "corrupt", f"{path}: JSON root must be an object"
    return value, "ok", ""


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


def save_user_text(path: Path, text: str) -> bool:
    """Rewrite an existing user-owned file, keeping its symlink target and mode."""
    try:
        target = path.resolve(strict=True)
        mode = stat.S_IMODE(target.stat().st_mode)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        return True
    except OSError as exc:
        log_event("user_text_write_failed", path=str(path), error=str(exc))
        return False


def save_json(path: Path, value: dict[str, Any]) -> bool:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return save_text(path, text)


def _pilot_binary() -> str:
    override = _env_value("THE_PILOT_BIN", "ORCA_AUTOPILOT_PILOT_BIN")
    local = Path.home() / "the-pilot/bin/pilot"
    if override:
        candidate = override
    elif (local.parent.parent / ".git").exists() and local.is_file() and os.access(local, os.X_OK):
        candidate = str(local)
    else:
        candidate = shutil.which("pilot") or str(local)
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else ""


def _run_local_sync(command: list[str]) -> tuple[int, str, str] | None:
    """Run one local engine sync and kill the whole process group if it overruns.

    The updater may spawn an ``apply`` child that writes the installation. Its
    own 30 s budget fits inside this deadline, so a healthy repair finishes; if
    the updater still overruns, the group is killed so the caller can never
    leave an orphaned writer behind.
    """
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        log_event("local_auto_sync_failed", error=str(exc))
        return None
    try:
        stdout, stderr = process.communicate(timeout=LOCAL_SYNC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.kill()
        process.communicate()
        log_event("local_auto_sync_timeout")
        return None
    return process.returncode, stdout, stderr


def maybe_sync_local_engine() -> bool:
    """Sync the local engine and report whether no repair is outstanding.

    A ``False`` result means a repair was started but did not confirm
    completion, so callers must not inspect or re-execute the installed engine
    this turn.
    """
    if (_env_value("THE_PILOT_GEMINI_DIR", "ORCA_AUTOPILOT_GEMINI_DIR")
            and _env_value("THE_PILOT_ENABLE_AUTO_UPDATE", "ORCA_AUTOPILOT_ENABLE_AUTO_UPDATE") != "1"
            and not _env_value("THE_PILOT_BIN", "ORCA_AUTOPILOT_PILOT_BIN")):
        return True
    pilot_bin = _pilot_binary()
    if not pilot_bin:
        return True
    command = [pilot_bin, "self-update", "--project-only", "--gemini-dir", str(GEMINI_DIR), "--json"]
    completed = _run_local_sync(command)
    if completed is None:
        log_event("local_auto_sync_failed", error="local_sync_incomplete")
        return False
    returncode, stdout, stderr = completed
    if returncode != 0:
        log_event(
            "local_auto_sync_failed",
            returncode=returncode,
            error=(stderr or stdout).strip()[-300:],
        )
        return False
    try:
        receipt = json.loads(stdout) if stdout.strip() else {}
    except ValueError as exc:
        log_event("local_auto_sync_failed", error=str(exc))
        return False
    if not isinstance(receipt, dict):
        log_event("local_auto_sync_failed", error="receipt_not_object")
        return False
    if receipt.get("error"):
        log_event("local_auto_sync_failed", error=str(receipt["error"]))
        return False
    if receipt.get("local_source_status") == "updated":
        log_event("local_auto_synced")
    return True


def request_background_update() -> None:
    pilot_bin = _pilot_binary()
    if not pilot_bin:
        return
    try:
        result = subprocess.run(
            [pilot_bin, "self-update", "--background", "--gemini-dir", str(GEMINI_DIR), "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_event("auto_update_request_failed", error=str(exc))
        return
    if result.returncode != 0:
        log_event("auto_update_request_failed", error=(result.stderr or result.stdout).strip()[-300:])


def load_profile() -> tuple[dict[str, Any] | None, str, str]:
    value, status, detail = load_json(PROFILE_PATH)
    if status != "ok":
        return None, status, detail
    if "pilot" in value and "autopilot" in value:
        log_event("profile_section_ambiguous")
        return (
            None,
            "corrupt",
            f"{PROFILE_PATH}: profile contains both pilot and autopilot sections",
        )
    orchestration = value.get("orchestration")
    if isinstance(orchestration, dict) and orchestration.get("worker_models") is not None:
        try:
            orchestration["worker_models"] = normalize_worker_models(
                orchestration["worker_models"], clears="reject",
            )
        except ValueError as exc:
            log_event("profile_worker_models_invalid", error=str(exc))
            return None, "corrupt", f"{PROFILE_PATH}: {exc}"
    return value, "ok", ""


def load_config() -> tuple[dict[str, Any] | None, str, str]:
    return load_json(CONFIG_PATH)


def validate_project_entries(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate and expand the shortcut specs declared by a v1 overlay."""
    if not isinstance(raw, dict) or raw.get("schema") != PROJECT_SCHEMA:
        raise ValueError(f"project.schema must be {PROJECT_SCHEMA}")
    allowed = {"schema", "projectId", "description", "entry_order", "entry_specs", *LEGACY_PROJECT_FIELDS}
    if set(raw) - allowed:
        raise ValueError(f"Unknown project fields: {sorted(set(raw) - allowed)}")
    if not isinstance(raw.get("projectId"), str) or not raw["projectId"].strip():
        raise ValueError("projectId must be a nonempty string")
    if "description" in raw and not isinstance(raw["description"], str):
        raise ValueError("project.description must be a string")
    specs = raw.get("entry_specs", {})
    order = raw.get("entry_order", list(UNIVERSAL_ENTRY_KEYS))
    for field in LEGACY_PROJECT_FIELDS | {"entry_specs"}:
        if field in raw and not isinstance(raw[field], dict):
            raise ValueError(f"project.{field} must be an object")
    if (not isinstance(order, list) or not all(isinstance(key, str) for key in order)
            or len(order) != len(set(order)) or set(specs) - set(order)
            or set(order) - set(specs) - set(UNIVERSAL_ENTRY_KEYS)):
        raise ValueError("entry_order must list each entry spec once and may include built-in entries")
    identities = set()
    required = {"command_id", "description", "requires_arg", "usage", "template"}
    normalized = {}
    for key in order:
        if key not in specs:
            continue
        fields = specs[key]
        if not is_entry_key(key) or key in RESERVED_COMMA_ALIASES:
            raise ValueError(f"Invalid or reserved entry key: {key}")
        if not isinstance(fields, dict) or set(fields) - required - {"workflow_type"}:
            raise ValueError(f"Invalid entry spec: {key}")
        spec = {**UNIVERSAL_ENTRY_SPECS.get(key, {}), **fields}
        if not required <= set(spec):
            raise ValueError(f"Invalid entry spec: {key}")
        if not isinstance(spec["requires_arg"], bool):
            raise ValueError(f"{key}.requires_arg must be boolean")
        for field in set(spec) - {"requires_arg"}:
            if not isinstance(spec[field], str) or not spec[field].strip():
                raise ValueError(f"{key}.{field} must be nonempty text")
        if spec["command_id"] in identities:
            raise ValueError("Project entry command IDs must be unique")
        identities.add(spec["command_id"])
        normalized[key] = spec
    return normalized


def entry_comma_command(key: str, shortcut: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": shortcut["command_id"],
        "canonical_key": key,
        "aliases": [f",{key}"],
        "scene": "",
        "title": shortcut["description"],
        "template": shortcut["template"],
        **({"workflow_type": shortcut["workflow_type"]} if "workflow_type" in shortcut else {}),
    }


def _workspace_directory(payload: dict[str, Any]) -> Path:
    paths = payload.get("workspacePaths")
    if isinstance(paths, list):
        for path in paths:
            if isinstance(path, str) and path.strip():
                return Path(path)
    cwd = payload.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd.strip() else Path.cwd()


def _read_workspace_entries(
    payload: dict[str, Any],
) -> tuple[Path, dict[str, dict[str, Any]]] | None:
    workspace = _workspace_directory(payload).resolve()
    for directory in (workspace, *workspace.parents):
        path = directory / ".pilot/project.json"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        try:
            return path, validate_project_entries(json.loads(text))
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
    return None


def workspace_config(
    config: dict[str, Any], profile: dict[str, Any], payload: dict[str, Any],
) -> dict[str, Any]:
    """Layer shortcut specs in memory, preserving the base on invalid input."""
    preferences = profile.get("shortcuts")
    preferences = preferences if isinstance(preferences, dict) else {}
    enabled_entry = preferences.get("enabled_entry")
    if isinstance(enabled_entry, list) and not enabled_entry:
        return config
    try:
        overlay = _read_workspace_entries(payload)
        if overlay is None:
            return config
        path, specs = overlay
        if isinstance(enabled_entry, list):
            specs = {
                key: spec for key, spec in specs.items()
                if key not in UNIVERSAL_ENTRY_KEYS or key in enabled_entry
            }
        if not specs:
            return config
        entries = dict(config.get("entry_shortcuts") or {})
        commands = dict(config.get("comma_commands") or {})
        effective = {**config, "entry_shortcuts": entries, "comma_commands": commands}
        for key in specs.keys() & entries.keys():
            existing = entries[key]
            if isinstance(existing, dict) and isinstance(existing.get("command_id"), str):
                commands.pop(existing["command_id"], None)
        for key, spec in specs.items():
            entry = {**spec, "canonical_key": key}
            command_id = entry["command_id"]
            if command_id in commands or find_comma_command(effective, f",{key}"):
                raise ValueError(f"{path}: shortcut {key!r} conflicts with an installed command")
            entries[key] = entry
            commands[command_id] = entry_comma_command(key, entry)
        overrides = preferences.get("template_overrides")
        if isinstance(overrides, dict) and overrides:
            owners, _ = shortcut_override_owners(enabled_entry, entries, commands)
            overrides = {shortcut_override_name(alias): template for alias, template in overrides.items()}
            for key in specs:
                entry = entries[key]
                command_id = entry["command_id"]
                for alias in (key, f",{key}", shortcut_override_name(command_id)):
                    template = overrides.get(alias)
                    if owners.get(alias) == key and isinstance(template, str) and template.strip():
                        entry["template"] = commands[command_id]["template"] = template
                        break
        return effective
    except (OSError, ValueError, RuntimeError) as exc:
        log_event("project_overlay_invalid", cid=payload.get("conversationId"), error=str(exc))
        return config


def _installed_config_failure(
    profile_status: str,
    profile_err: str,
    *,
    config_status: str = "ok",
    config_err: str = "",
    scene_errors: list[str] | tuple[str, ...] = (),
) -> tuple[str, str] | None:
    """Return the hook failure code and detail for unusable installed inputs."""
    corrupt_details = []
    if profile_status == "corrupt":
        corrupt_details.append(profile_err)
    if config_status == "corrupt":
        corrupt_details.append(config_err)
    corrupt_details.extend(scene_errors)
    if corrupt_details:
        return "config_corrupt", "; ".join(item for item in corrupt_details if item)
    if profile_status == "missing" or config_status == "missing":
        return "config_missing", profile_err or config_err
    return None


def _pilot_section(profile: dict[str, Any]) -> Any:
    """Return the pilot-mode mapping, accepting one legacy ``autopilot`` key.

    Two keys in the same profile mean two owners for the same mode and turn
    cap, so the runtime fails closed instead of guessing which one wins.
    """
    if "pilot" in profile and "autopilot" in profile:
        log_event("profile_section_ambiguous")
        return None
    if "pilot" in profile:
        return profile.get("pilot")
    return profile.get("autopilot")


def _prepare_profile_for_write(profile: dict[str, Any]) -> bool:
    """Normalize a profile to canonical sections before writing it back.

    Two keys in the same profile leave two owners for the same mode and turn
    cap, so the write fails closed instead of guessing which one wins.
    """
    if "pilot" in profile and "autopilot" in profile:
        log_event("profile_section_ambiguous")
        return False
    if "autopilot" in profile:
        profile["pilot"] = profile.pop("autopilot")
    if profile.get("schema") == LEGACY_PROFILE_SCHEMA:
        profile["schema"] = PROFILE_SCHEMA
    return True


def current_mode(profile: dict[str, Any]) -> str:
    pilot = _pilot_section(profile)
    if isinstance(pilot, dict):
        mode = str(pilot.get("mode") or "").lower()
        if mode in MODES:
            return mode
    return "off"


def _current_agents(
    profile: dict[str, Any], *, key: str, allowed: Sequence[str], defaults: list[str],
) -> list[str]:
    orchestration = profile.get("orchestration")
    agents = orchestration.get(key) if isinstance(orchestration, dict) else None
    if (isinstance(agents, list) and agents
            and all(isinstance(agent, str) and agent in allowed for agent in agents)
            and len(set(agents)) == len(agents)):
        return agents.copy()
    return defaults


def current_worker_agents(profile: dict[str, Any]) -> list[str]:
    return _current_agents(
        profile, key="worker_agents", allowed=WORKER_AGENT_ORDER, defaults=["claude", "codex"],
    )


def normalize_worker_agent(raw: str) -> str | None:
    return WORKER_AGENT_ALIASES.get(raw.strip().lower())


def is_worker_model_id(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str) and bool(value) and value.isprintable()
        and not any(char.isspace() for char in value) and "`" not in value
    )


def normalize_worker_effort(value: Any, where: str) -> str:
    effort = value.strip().lower() if isinstance(value, str) else None
    if effort is None or effort not in WORKER_EFFORT_LEVELS:
        raise ValueError(f"{where} 必须是 {WORKER_EFFORT_LEVELS} 之一，实际为 {value!r}")
    return effort


def canonical_worker_model_keys(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    where = "orchestration.worker_models"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是 JSON 对象")
    models: dict[str, Any] = {}
    for key, spec in raw.items():
        agent = normalize_worker_agent(key) if isinstance(key, str) else None
        if agent is None or agent not in WORKER_AGENT_ORDER:
            raise ValueError(f"{where} 含未知执行器 {key!r}")
        if agent in models:
            raise ValueError(f"{where} 重复指定执行器 {agent!r}")
        models[agent] = spec
    return models


def normalize_worker_models(
    raw: Any, *, clears: Literal["keep", "drop", "reject"] = "drop",
) -> dict[str, dict[str, str | None] | None]:
    """Validate launch preferences, retaining null directives only while merging."""
    models: dict[str, dict[str, str | None] | None] = {}
    for agent, spec in canonical_worker_model_keys(raw).items():
        if spec is None and clears != "reject":
            if clears == "keep":
                models[agent] = None
            continue
        where = f"orchestration.worker_models.{agent}"
        if not isinstance(spec, dict):
            raise ValueError(f"{where} 必须是 JSON 对象")
        unknown = spec.keys() - {"model", "effort"}
        if unknown:
            raise ValueError(f"{where} 含未知字段 {sorted(unknown, key=str)}")
        model = spec.get("model")
        if not is_worker_model_id(model):
            raise ValueError(f"{where}.model 必须是无空白、控制字符或反引号的非空字符串")
        config: dict[str, str | None] = {"model": model}
        if spec.get("effort") is not None:
            config["effort"] = normalize_worker_effort(spec["effort"], f"{where}.effort")
        elif clears == "keep" and "effort" in spec:
            config["effort"] = None
        models[agent] = config
    return models


def current_worker_models(profile: dict[str, Any]) -> dict[str, dict[str, str]]:
    orchestration = profile.get("orchestration")
    return (orchestration.get("worker_models") or {}) if isinstance(orchestration, dict) else {}


def format_agent_order(agents: Sequence[str]) -> str:
    return " > ".join(AGENT_LABELS[agent] for agent in agents)


def format_worker_launch_flags(
    agent: str, worker_models: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    flags = f"--agent {ORCA_AGENT_IDS[agent]}"
    spec = (worker_models or {}).get(agent, {})
    if model := spec.get("model"):
        flags += f" --model {shlex.quote(model)}"
        if effort := spec.get("effort"):
            flags += f" --effort {shlex.quote(effort)}"
    return flags


def format_worker_executor_clause(
    workers: Sequence[str], worker_models: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    entries = [f"{AGENT_LABELS[agent]}（`{format_worker_launch_flags(agent, worker_models)}`）"
               for agent in workers]
    clause = f"优先使用 {entries[0]}"
    if len(entries) > 1:
        clause += f"，其次使用 {entries[1]}"
    if len(entries) > 2:
        clause += "，备选 " + "、".join(entries[2:])
    return clause


def format_worker_secondary_hint(
    workers: Sequence[str], worker_models: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    if len(workers) <= 1:
        return ""
    alternatives = " / ".join(f"`{format_worker_launch_flags(agent, worker_models)}`" for agent in workers[1:])
    return f"次选 {alternatives}；"


def worker_priority(
    workers: Sequence[str], worker_models: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    primary, *alternatives = [format_worker_launch_flags(agent, worker_models) for agent in workers]
    secondary = f", secondary: {' / '.join(alternatives)}" if alternatives else ""
    return f"Orca Worker Priority: {format_agent_order(workers)} (primary: {primary}{secondary})"


def compile_worker_hot_reload(
    workers: Sequence[str], worker_models: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    return (
        "<orca_worker_agent_hot_reload>\n"
        f"{worker_priority(workers, worker_models)}。立即覆盖本会话启动时的旧设定，"
        f"本会话后续所有 worker-start 调用立即使用 {format_worker_launch_flags(workers[0], worker_models)}。"
        "其他编排与审查边界保持不变。\n</orca_worker_agent_hot_reload>"
    )


def _replace_priority(text: str, marker: str, priority: str) -> str:
    before, separator, tail = text.partition(marker)
    if not separator:
        return text
    end = next((index for index, char in enumerate(tail) if char in "）\r\n"), len(tail))
    return before + separator + priority + tail[end:]


def _after_worker_clause_entry(text: str) -> str | None:
    label, start, flags = text.partition("（`--agent ")
    _, end, tail = flags.partition("`）")
    if start and end and label.strip() in AGENT_LABELS.values():
        return tail
    return None


def _replace_worker_clause(line: str, executor: str) -> str:
    for prefix in ("优先使用", "默认优先"):
        before, marker, clause = line.partition(prefix)
        tail = _after_worker_clause_entry(clause) if marker else None
        if tail is None:
            continue
        for delimiter in ("，其次使用 ", "，备选 ", "、"):
            while tail.startswith(delimiter):
                rest = _after_worker_clause_entry(tail.removeprefix(delimiter))
                if rest is None:
                    return line
                tail = rest
        return before + prefix + executor.removeprefix("优先使用") + tail
    return line


def _replace_worker_example(line: str, primary: str, secondary_hint: str) -> str:
    before, worktree, tail = line.partition("--worktree ")
    command, fence, context = tail.partition("`")
    selector, agent, flags = command.partition(" --agent ")
    _, end, settings = flags.partition(" --setup ")
    if not end:
        _, end, settings = flags.partition(" --json")
    if not (worktree and fence and agent and end):
        return line

    if context.startswith("（"):
        choices = context[1:]
        if choices.startswith("次选 "):
            _, _, choices = choices.partition("`；")
        if choices.startswith(("或 `task-create`", "`orca worktree create`")):
            context = "（" + secondary_hint + choices
    return before + worktree + selector + " " + primary + end + settings + fence + context


def replace_worker_guidance(
    text: str, workers: Sequence[str], worker_models: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Replace executor slots without owning or reproducing orchestration rules."""
    priority = format_agent_order(workers)
    executor = format_worker_executor_clause(workers, worker_models)
    primary = format_worker_launch_flags(workers[0], worker_models)
    secondary_hint = format_worker_secondary_hint(workers, worker_models)
    for marker in ("Orca Orchestration：", "执行器优先级："):
        text = _replace_priority(text, marker, priority)
    return "".join(
        _replace_worker_example(_replace_worker_clause(line, executor), primary, secondary_hint)
        for line in text.splitlines(keepends=True)
    )


def update_orchestration_guidance(
    text: str, workers: Sequence[str], worker_models: Mapping[str, Mapping[str, str]] | None = None,
) -> str | None:
    """Refresh worker slots inside the single managed orchestration block.

    A single valid marker pair scopes the rewrite so unrelated user prose is
    never touched. Without markers the file is not ours to rewrite, so it is
    returned unchanged; duplicate or malformed markers return ``None`` so the
    caller fails closed instead of guessing which section the tool owns.
    """
    begins = _find_all_positions(text, ORCHESTRATION_BEGIN)
    ends = _find_all_positions(text, ORCHESTRATION_END)
    if not begins and not ends:
        return text
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        log_event("orchestration_block_ambiguous")
        return None
    start = begins[0] + len(ORCHESTRATION_BEGIN)
    end = ends[0]
    return (
        text[:start]
        + replace_worker_guidance(text[start:end], workers, worker_models)
        + text[end:]
    )


def _profile_block_bounds(text: str) -> tuple[int, int] | None:
    """Return the profile block content bounds, accepting one legacy block."""
    blocks: list[tuple[int, int]] = []
    for begin_marker, end_marker, heading in (
        (GEMINI_BEGIN, GEMINI_END, PROFILE_BLOCK_HEADING),
        (LEGACY_GEMINI_BEGIN, LEGACY_GEMINI_END, LEGACY_PROFILE_BLOCK_HEADING),
    ):
        starts = _find_all_positions(text, begin_marker)
        if not starts:
            continue
        if len(starts) != 1:
            return None
        content_start = starts[0] + len(begin_marker)
        boundaries = [text.find(candidate, content_start) for candidate in (heading, end_marker)]
        boundaries = [boundary for boundary in boundaries if boundary >= 0]
        if not boundaries:
            return None
        blocks.append((content_start, min(boundaries)))
    if len(blocks) > 1:
        log_event("profile_block_ambiguous")
        return None
    return blocks[0] if blocks else None


def set_worker_agents(target: str) -> tuple[bool, list[str], list[str], dict[str, dict[str, str]]]:
    primary = normalize_worker_agent(target)
    if primary is None:
        return False, [], [], {}
    updated: list[str] = []
    with _file_lock(PILOT_DIR / ".pilot.lock"):
        profile, profile_status, profile_err = load_profile()
        if profile_status != "ok" or profile is None:
            log_event(f"worker_write_profile_{profile_status}", error=profile_err)
            return False, [], updated, {}
        old_workers = current_worker_agents(profile)
        models = current_worker_models(profile)
        failure = False, old_workers, updated, models
        if not profile or not _prepare_profile_for_write(profile):
            return failure
        new_workers = [primary, *(agent for agent in old_workers if agent != primary)]
        prepared: list[tuple[Path, str]] = []
        user_prepared: list[tuple[Path, str]] = []
        try:
            if GEMINI_MD_PATH.is_file():
                text = GEMINI_MD_PATH.read_text(encoding="utf-8")
                bounds = _profile_block_bounds(text)
                if bounds is None:
                    return failure
                start, end = bounds
                guidance = replace_worker_guidance(text[start:end], new_workers, models)
                text = text[:start] + guidance + text[end:]
                prepared.append((GEMINI_MD_PATH, text))
            if GEMINI_DIR.resolve() == (Path.home() / ".gemini").resolve():
                for path in (Path.home() / ".claude/CLAUDE.md", Path.home() / ".codex/AGENTS.md"):
                    if path.is_file():
                        text = path.read_text(encoding="utf-8")
                        guidance = update_orchestration_guidance(text, new_workers, models)
                        if guidance is None:
                            return failure
                        if guidance != text:
                            user_prepared.append((path, guidance))
        except (OSError, UnicodeError) as exc:
            log_event("worker_guidance_read_failed", error=str(exc))
            return failure
        profile.setdefault("orchestration", {})["worker_agents"] = new_workers
        prepared.insert(0, (PROFILE_PATH, json.dumps(profile, ensure_ascii=False, indent=2) + "\n"))
        for path, text in prepared:
            if not save_text(path, text):
                return failure
            updated.append(str(path))
        for path, text in user_prepared:
            if not save_user_text(path, text):
                return failure
            updated.append(str(path))
    return True, new_workers, updated, models


def max_consecutive_turns(profile: dict[str, Any]) -> int:
    pilot = _pilot_section(profile)
    if isinstance(pilot, dict):
        value = pilot.get("max_consecutive_auto_turns")
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 50:
            return value
    return 8


def set_mode(mode: str) -> bool:
    with _file_lock(PILOT_DIR / ".pilot.lock"):
        profile, profile_status, profile_err = load_profile()
        if profile_status != "ok" or profile is None:
            log_event(f"mode_write_profile_{profile_status}", error=profile_err)
            return False
        if not profile or not _prepare_profile_for_write(profile):
            return False
        pilot = profile.setdefault("pilot", {})
        if not isinstance(pilot, dict):
            return False
        pilot["mode"] = mode
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


def read_steps(
    transcript_path: str | None, *, hydrate: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    if not transcript_path:
        return [], False
    path = Path(transcript_path)
    if not path.is_file():
        log_event("transcript_missing", path=str(path))
        return [], False
    steps: list[dict[str, Any]] = []
    complete = True
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
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
    if hydrate:
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
        if colon and key.strip().lower() in {
            "task id", "dispatch id", "your task id is", "your dispatch id is",
        } and len(token) >= 6:
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
    """Return the trailing suggestion tag's prefix and payload.

    Sessions opened before the rename still end with ``<!-- autopilot: ... -->``
    in their loaded system prompt, so both names stay readable during the
    transition.
    """
    trimmed = text.rstrip()
    if not trimmed.endswith("-->"):
        return None
    content = trimmed[:-3]
    for position in _find_all_positions(content, "<!--"):
        body = content[position + 4:].strip()
        payload = ""
        for name in ("pilot", "autopilot"):
            lowered = body.lower()
            if not lowered.startswith(name):
                continue
            rest = body[len(name):]
            if rest and not (rest[0].isspace() or rest[0] == ":"):
                continue
            payload = rest.lstrip().removeprefix(":").strip()
            break
        else:
            continue
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


def scan_scenes() -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Read every scene file, returning unreadable ones instead of hiding them."""
    scenes: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if not SCENES_DIR.is_dir():
        return scenes, errors
    for path in sorted(SCENES_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            log_event("scene_read_failed", path=str(path), error=str(exc))
            errors.append(f"{path}: {exc}")
            continue
        meta, body = split_frontmatter(text)
        name = str(meta.get("name") or "")
        if name:
            scenes[name] = {"meta": meta, "body": body, "path": path}
    return scenes, errors


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
    except (OSError, UnicodeError) as exc:
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


def _clear_delivery_request(state: dict[str, Any]) -> None:
    state.update(delivery_request_id="", delivery_deadline_at=0.0)


def _clear_delivery_confirmation(state: dict[str, Any]) -> None:
    _clear_delivery_request(state)
    state.update(last_confirmed_request_id="", unconfirmed_request_id="", rejected_reason="")


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
    _clear_delivery_confirmation(state)
    if state.get("delivery_status") not in {"pending", "unknown"}:
        state["delivery_status"] = "none"


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


def _connected_antigravity_terminals() -> list[dict[str, Any]]:
    result = run_orca(["terminal", "list", "--json"], timeout=3)
    if result is None or result.returncode != 0:
        log_event("terminal_list_failed", returncode=None if result is None else result.returncode)
        return []
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        log_event("terminal_list_invalid", error=str(exc))
        return []
    result_data = payload.get("result") if isinstance(payload, dict) else None
    terminals = result_data.get("terminals") if isinstance(result_data, dict) else None
    if not isinstance(terminals, list):
        log_event("terminal_list_invalid", reason="missing_terminals")
        return []
    return [
        item
        for item in terminals
        if isinstance(item, dict)
        and item.get("agentIdentity") == "antigravity"
        and item.get("connected") is True
    ]


def resolve_terminal(workspace_path: str, explicit_handle: str = "") -> str:
    if not workspace_path:
        log_event("terminal_workspace_missing")
        return ""
    requested = explicit_handle.strip() or os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    candidates = [
        item for item in _connected_antigravity_terminals()
        if item.get("writable", True) is not False
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


def _terminal_incarnation_matches(handle: str, expected_incarnation: str) -> bool:
    if not expected_incarnation:
        return False
    for terminal in _connected_antigravity_terminals():
        if terminal.get("handle") == handle:
            return terminal.get("incarnationId") == expected_incarnation
    return False


def _start_terminal_send(handle: str, text: str, enter: bool) -> subprocess.Popen[str]:
    args = [orca_binary(), "terminal", "send", "--terminal", handle, "--text", text, "--json"]
    if enter:
        args.append("--enter")
    return subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_for_terminal_receipt(
    process: subprocess.Popen[str], handle: str, enter: bool,
) -> tuple[str, dict[str, Any]]:
    with process:
        try:
            stdout, stderr = process.communicate(timeout=5.0)
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            process.kill()
            log_event("send_unknown", handle=handle, enter=enter, error=str(exc))
            return "unknown", {}
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        if process.returncode == 0:
            log_event("send_receipt_invalid", handle=handle, error=str(exc))
            return "unknown", {}
        payload = None
    result_data = payload.get("result") if isinstance(payload, dict) else None
    receipt = result_data.get("send") if isinstance(result_data, dict) else None
    if process.returncode != 0:
        error = payload.get("error") if isinstance(payload, dict) else None
        details = {}
        if isinstance(error, dict) and error.get("code"):
            details["error_code"] = error["code"]
        if isinstance(receipt, dict) and receipt.get("refusedReason"):
            details["refused_reason"] = receipt["refusedReason"]
        log_event(
            "send_rejected",
            handle=handle,
            enter=enter,
            returncode=process.returncode,
            stderr=stderr[-500:],
            stdout=stdout[-500:],
            **details,
        )
        return "rejected", {}
    if isinstance(receipt, dict) and isinstance(receipt.get("accepted"), bool):
        log_event("send_receipt", handle=handle, enter=enter, accepted=receipt["accepted"])
        return ("accepted" if receipt["accepted"] else "rejected"), receipt
    log_event("send_receipt_invalid", handle=handle, reason="missing_accepted")
    return "unknown", {}


def injection(message: str) -> dict[str, Any]:
    return {"injectSteps": [{"ephemeralMessage": message}]}


def _ps_process(pid: int) -> tuple[int, str, str] | None:
    """Return one process's `(ppid, tty, comm)` or None when it is unknown."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=,tty=,comm="],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(None, 2)
    if len(parts) != 3:
        return None
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    return ppid, parts[1], parts[2]


def _osc_target_from_tty(tty: str) -> tuple[Path, bool] | None:
    """Validate one `ps` tty column and turn it into a `/dev` path."""
    value = tty.strip()
    if not value or value in {"?", "??", "-"} or ".." in value.split("/"):
        return None
    candidate = Path("/dev") / value
    try:
        resolved = candidate.resolve()
        device_root = Path("/dev").resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(device_root):
        return None
    return candidate, False


def _uses_orca_pane() -> bool:
    return not _env_value("THE_PILOT_GEMINI_DIR", "ORCA_AUTOPILOT_GEMINI_DIR") and bool(
        os.environ.get("ORCA_PANE_KEY", "").strip()
        or os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    )


def _resolve_osc_target(*, allow_predecessor_shell: bool = False) -> tuple[Path, bool] | None:
    """Resolve the Orca pane TTY that owns the agent process.

    Antigravity allocates child PTYs for subprocesses, so `os.ctermid()` inside
    this hook names the wrong terminal. Only the ancestor `agy` process's `ps`
    tty column points at Orca's pane; nested shell shims (`sh -> bash -> agy`)
    are walked for up to three hops after skipping the re-exec Python parent.
    At boot, a shell's TTY can serve as the target before `agy` exists.
    An explicit override or captured pane binding wins. Otherwise Gemini-root
    isolation and non-Orca hosts return before probing processes or devices.
    """
    raw_override = os.environ.get("THE_PILOT_OSC_TARGET")
    if raw_override is not None:
        stripped = raw_override.strip()
        if not stripped or stripped.lower() == "none":
            return None
        is_override = os.environ.get("THE_PILOT_OSC_TARGET_IS_PANE") != "1"
        return Path(stripped).expanduser(), is_override
    if not _uses_orca_pane():
        return None
    skip_python_parent = (
        allow_predecessor_shell or _env_value("THE_PILOT_REEXEC", "ORCA_AUTOPILOT_REEXEC") == "1"
    )
    pid = os.getppid()
    shell_ttys: list[str] = []
    while pid > 1:
        process = _ps_process(pid)
        if process is None:
            break
        ppid, tty, comm = process
        executable = Path(comm.strip().lstrip("-")).name
        if executable == "agy":
            return _osc_target_from_tty(tty)
        if not (skip_python_parent and executable.lower().startswith("python")):
            if executable not in SHELL_WRAPPER_NAMES or len(shell_ttys) == 3:
                break
            shell_ttys.append(tty)
        skip_python_parent = False
        pid = ppid
    if allow_predecessor_shell:
        for tty in shell_ttys:
            target = _osc_target_from_tty(tty)
            if target is not None:
                return target
    return None


def _resolve_boot_osc_target() -> tuple[Path, bool] | None:
    return _resolve_osc_target(allow_predecessor_shell=True)


def _is_owned_character_device(status: os.stat_result) -> bool:
    return stat.S_ISCHR(status.st_mode) and status.st_uid == os.getuid()


def _bound_pane_matches(opened: os.stat_result) -> bool:
    if os.environ.get("THE_PILOT_OSC_TARGET_IS_PANE") != "1":
        return True
    try:
        expected = (int(os.environ["THE_PILOT_OSC_DEV"]), int(os.environ["THE_PILOT_OSC_INO"]))
    except (KeyError, ValueError):
        return False
    return (opened.st_dev, opened.st_ino) == expected


def _open_override_target(target: Path) -> int:
    """Open an explicit `THE_PILOT_OSC_TARGET` for one frame append."""
    if target.exists():
        mode = target.stat().st_mode
        if stat.S_ISCHR(mode) or stat.S_ISFIFO(mode):
            return os.open(target, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    target.parent.mkdir(parents=True, exist_ok=True)
    return os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)


def _write_osc_bytes(fd: int, data: bytes) -> None:
    """Write one frame, draining transient PTY backpressure until the deadline.

    A non-blocking tty slave raises `EAGAIN` while its output ring buffer is
    full (1 KiB on macOS), so a status frame can arrive exactly when agy is
    flushing a TUI redraw. Wait for writability instead of dropping the frame,
    but never spin or block the hook past `OSC_WRITE_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + OSC_WRITE_TIMEOUT_SECONDS
    written = 0
    while written < len(data):
        try:
            count = os.write(fd, data[written:])
        except InterruptedError:
            continue
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            _, writable, _ = select.select([], [fd], [], remaining)
            if not writable:
                raise
            continue
        if count <= 0:
            raise OSError("short_osc_write")
        written += count


def emit_osc_7501(frame: str, *, boot: bool = False) -> bool:
    """Write one paired activity/title frame to Orca's pane."""
    if not frame:
        return False
    resolved = _resolve_boot_osc_target() if boot else _resolve_osc_target()
    if resolved is None:
        return False
    target, is_override = resolved
    data = frame.encode("utf-8")
    try:
        if is_override:
            fd = _open_override_target(target)
        else:
            status = os.stat(target)
            if not _is_owned_character_device(status):
                log_event("osc_status_target_rejected", target=str(target))
                return False
            fd = os.open(target, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            if not is_override:
                # The path may be replaced between stat and open: re-check the
                # descriptor actually opened before writing escape bytes.
                opened = os.fstat(fd)
                if not _is_owned_character_device(opened) or not _bound_pane_matches(opened):
                    log_event("osc_status_target_rejected", target=str(target))
                    return False
            _write_osc_bytes(fd, data)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        log_event("osc_status_write_failed", target=str(target), error=str(exc))
        return False
    return True


def classify_agy_boot_args(args: Sequence[str]) -> str | None:
    """Resolve the initial frame from agy's Go flag and positional arguments."""
    tokens = iter(args)
    has_interactive_prompt = False
    has_positional_prompt = False
    has_resume = False
    for token in tokens:
        if token == "--":
            has_positional_prompt = any(value.strip() for value in tokens)
            break
        if token == "-" or not token.startswith("-"):
            if token in AGY_MANAGEMENT_COMMANDS:
                return None
            has_positional_prompt = bool(token.strip()) or any(value.strip() for value in tokens)
            break
        flag = token[2:] if token.startswith("--") else token[1:]
        name, separator, value = flag.partition("=")
        if name in AGY_NON_SESSION_FLAGS:
            return None
        if name in AGY_RESUME_BOOL_FLAGS and value.lower() not in {"0", "f", "false"}:
            has_resume = True
        if name in AGY_BOOT_VALUE_FLAGS:
            if not separator:
                value = next(tokens, "")
            if name in AGY_INTERACTIVE_PROMPT_FLAGS and value.strip():
                has_interactive_prompt = True
            if name in AGY_RESUME_VALUE_FLAGS and value:
                has_resume = True
    if has_interactive_prompt or has_positional_prompt:
        return OSC_7501_WORKING
    return OSC_7501_DONE if has_resume else None


def command_announce_boot(agy_args: Sequence[str]) -> int:
    """Announce a prospective interactive launch without holding up agy."""
    if os.environ.get("AGY_CLI_OSC7501", "").strip().lower() == "on":
        return 0
    try:
        frame = classify_agy_boot_args(agy_args)
        if frame is not None:
            emit_osc_7501(frame, boot=True)
    except Exception as exc:
        log_event("osc_boot_failed", error=repr(exc))
    return 0


@cache
def _running_script_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _sync_and_maybe_reexec(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    # The supervised child uses the updated engine without starting another
    # repair that its parent could interrupt when the re-exec times out.
    if _env_value("THE_PILOT_REEXEC", "ORCA_AUTOPILOT_REEXEC") == "1":
        return None
    updated_engine = PILOT_DIR / "runtime.py"
    try:
        # Capture the running script before the updater can replace it.
        running_sha256 = _running_script_sha256()
        if not maybe_sync_local_engine():
            return None
        if not updated_engine.is_file():
            return None
        if hashlib.sha256(updated_engine.read_bytes()).hexdigest() == running_sha256:
            return None
        result = subprocess.run(
            [sys.executable, str(updated_engine), "hook", "--event", event],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "THE_PILOT_REEXEC": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_event("engine_reexec_failed", error=str(exc))
        raise HookExecutionError("engine_reexec_failed", str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-300:]
        log_event("engine_reexec_failed", returncode=result.returncode, error=detail)
        raise HookExecutionError("engine_reexec_failed", detail)
    try:
        output = json.loads(result.stdout)
    except ValueError as exc:
        log_event("engine_reexec_failed", error=str(exc))
        raise HookExecutionError("engine_reexec_invalid_output", str(exc)) from exc
    if not isinstance(output, dict):
        log_event("engine_reexec_failed", error="output_not_object")
        raise HookExecutionError("engine_reexec_invalid_output", "hook output must be a JSON object")
    return output


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

    reexec_output = _sync_and_maybe_reexec("PreInvocation", payload)
    if reexec_output is not None:
        return reexec_output
    worker_target = worker_command_target(text)
    mode_target = _named_command(text, {"mode"}, {"gate", "confirm", "off", "status"})
    config, config_status, config_err = load_config()
    profile, profile_status, profile_err = load_profile()
    if config is not None and profile is not None:
        config = workspace_config(config, profile, payload)
    scenes, scene_errors = scan_scenes()
    user_turn = turn_user_identity(steps)
    recovery_requested = bool(_named_command(text, {"pilot", "autopilot"}, {"recover"}))
    with state_lock(conversation_id):
        state = load_state(conversation_id)
        failure = _installed_config_failure(
            profile_status,
            profile_err,
            config_status=config_status,
            config_err=config_err,
            scene_errors=scene_errors,
        )
        if failure is not None:
            # Installed inputs must be usable before any recovery or delivery
            # decision. Only touch an existing readable session; a corrupt or
            # absent session file stays exactly as it is.
            code, detail = failure
            if isinstance(state, dict) and session_path(conversation_id).is_file():
                state["auto_submitted_text"] = ""
                state["candidates"] = []
                state["candidate_turn_id"] = ""
                _clear_delivery_confirmation(state)
                if not save_state(conversation_id, state):
                    log_event("pre_state_save_failed", cid=conversation_id)
            log_event(f"pre_invocation_{code}", cid=conversation_id, error=detail)
            raise HookExecutionError(code, detail)
        if state is None:
            # A corrupt file is never rebuilt from an ordinary or auto-continued
            # turn: without the lost state those are indistinguishable. Only an
            # explicit recovery command on a complete transcript proves the user
            # asked for it; every other input merely pauses the pilot.
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
                "【Pilot 会话状态已恢复】原状态文件已备份留证并重建；"
                "自动投递恢复正常。请用一句话告知用户，无需执行其他操作。"
            )
        worker_reload_msg = ""
        prev_workers = state.get("loaded_worker_agents")
        prev_models = state.get("loaded_worker_models", {})
        cur_workers = current_worker_agents(profile)
        cur_models = current_worker_models(profile)
        unversioned_existing = bool(state) and not isinstance(prev_workers, list)
        if not state:
            state.update(initial_worker_agents=cur_workers, initial_worker_models=cur_models)
        else:
            state.setdefault("initial_worker_agents", [])
            state.setdefault("initial_worker_models", {})
        last_checkpoint = max(
            (index for index, step in enumerate(steps) if step.get("type") == "CHECKPOINT"),
            default=-1,
        )
        initial_worker_config = state["initial_worker_agents"], state["initial_worker_models"]
        worker_config = cur_workers, cur_models
        if (unversioned_existing
                or isinstance(prev_workers, list) and (prev_workers, prev_models) != worker_config
                or initial_worker_config != worker_config
                and last_checkpoint > state.get("worker_reload_checkpoint", -1)):
            worker_reload_msg = compile_worker_hot_reload(cur_workers, cur_models)
            state["worker_reload_checkpoint"] = last_checkpoint
        state.update(loaded_worker_agents=cur_workers, loaded_worker_models=cur_models)
        state["current_user_turn_id"] = user_turn or "missing"
        auto_text = str(state.get("auto_submitted_text") or "")
        manual_input = not auto_text or text != auto_text
        pending_req = str(state.get("delivery_request_id") or "")
        unconfirmed_req = str(state.get("unconfirmed_request_id") or "")
        _clear_delivery_confirmation(state)
        if manual_input:
            state["consecutive_auto_turns"] = 0
        elif pending_req:
            state.update(last_confirmed_request_id=pending_req, delivery_status="none")
        elif unconfirmed_req and state.get("delivery_status") == "unknown":
            state["delivery_status"] = "none"
            log_event(
                "auto_submitted", cid=conversation_id, scene=str(state.get("scene") or ""),
                request_id=unconfirmed_req, verified=True, late=True,
            )
        if state.get("delivery_status") in {"pending", "unknown"} and manual_input:
            previous_status = str(state.get("delivery_status") or "")
            state["delivery_status"] = "cleared"
            state["last_settled_turn_id"] = ""
            log_event(
                "delivery_state_cleared",
                cid=conversation_id,
                status=previous_status,
            )
        elif state.get("delivery_status") not in {"pending", "unknown"}:
            state["delivery_status"] = "none"
        state["auto_submitted_text"] = ""
        candidates = state.get("candidates") or []
        candidate_turn = str(state.get("candidate_turn_id") or "")
        state["candidates"] = []
        state["candidate_turn_id"] = ""

        if worker_target:
            if worker_target == "status":
                message = worker_priority(cur_workers, cur_models)
            else:
                success, workers, _, models = set_worker_agents(worker_target)
                if success:
                    message = compile_worker_hot_reload(workers, models)
                    worker_reload_msg = ""
                    state.update(
                        loaded_worker_agents=workers, loaded_worker_models=models,
                        worker_reload_checkpoint=last_checkpoint,
                    )
                else:
                    message = "Worker 切换失败，请检查 profile.json 与派生规则文件。"
            if not save_state(conversation_id, state):
                log_event("pre_state_save_failed", cid=conversation_id)
            message = "\n\n".join(p for p in (worker_reload_msg, message) if p)
            return injection(message + " 请仅用一句话回复，并以 <!-- pilot: scene=NONE --> 结尾。")

        if mode_target:
            target = mode_target
            if target == "status":
                scene_count = len(scenes)
                message = (
                    f"【Pilot 状态】当前模式为 `{current_mode(profile)}`，"
                    f"已加载 {scene_count} 个场景文件。{worker_priority(cur_workers, cur_models)}。请用一句话告知用户，无需调用工具。"
                )
            elif not set_mode(target):
                message = "【Pilot】模式切换失败：profile.json 不可读或不可写。"
            else:
                message = (
                    f"【Pilot 模式已切换】当前模式为 `{target}`。"
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
                        f"【Pilot 候选指令 #{selected} 展开】用户选择了第 {selected} 条"
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
                            f"【Pilot 快捷命令 `{alias}` 展开】"
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
                                f"【Pilot 启动口令 `{key}` 展开】"
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


def _osc_frame_for_delivery_status(state: dict[str, Any]) -> str:
    """Keep mail out of the composer until delivery is settled."""
    status = str(state.get("delivery_status") or "")
    if status == "accepted":
        return ""
    if status in {"pending", "unknown", "prefilled", "prefill_unknown"}:
        return OSC_7501_WAITING_QUESTION
    if status == "rejected" and state.get("rejected_reason") == "permission":
        return OSC_7501_WAITING_QUESTION
    return OSC_7501_DONE


def _load_stop_settings(
    conversation_id: str, state: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read installed Stop settings while the caller holds the profile lock."""
    profile, profile_status, profile_err = load_profile()
    scenes, scene_errors = scan_scenes()
    failure = _installed_config_failure(profile_status, profile_err, scene_errors=scene_errors)
    if failure is not None:
        code, detail = failure
        if state is not None and session_path(conversation_id).is_file():
            clear_suggestion(state)
            save_state(conversation_id, state)
        log_event(f"stop_{code}", cid=conversation_id, error=detail)
        raise HookExecutionError(code, detail)
    return profile or {}, scenes


def _admit_auto_submit_policy(
    profile: dict[str, Any],
    scenes: dict[str, dict[str, Any]],
    *,
    scene: str,
    gate_action: str,
    previous_count: int,
) -> tuple[str, str, bool]:
    mode = current_mode(profile)
    gate = resolve_gate(scene, gate_action, scenes)
    auto_submit = (
        scene in scenes and mode == "gate" and gate == "auto"
        and previous_count < max_consecutive_turns(profile)
    )
    return mode, gate, auto_submit


def _rollback_auto_submit(state: dict[str, Any], previous_count: int, previous_text: str) -> None:
    state["consecutive_auto_turns"] = previous_count
    state["auto_submitted_text"] = previous_text
    state["last_settled_turn_id"] = ""
    _clear_delivery_confirmation(state)


def _record_unconfirmed_delivery(
    conversation_id: str, state: dict[str, Any], request_id: str, reason: str,
) -> bool:
    _clear_delivery_request(state)
    state.update(delivery_status="unknown", unconfirmed_request_id=request_id)
    if not save_state(conversation_id, state):
        log_event("auto_submit_state_failed", cid=conversation_id, request_id=request_id, reason=reason)
        return False
    log_event(
        "auto_submit_unconfirmed", cid=conversation_id, scene=str(state.get("scene") or ""),
        request_id=request_id, reason=reason,
    )
    return True


def _delivery_confirmation_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("THE_PILOT_REEXEC", None)
    env.pop("ORCA_AUTOPILOT_REEXEC", None)
    if "THE_PILOT_OSC_TARGET" in env:
        return env
    env["THE_PILOT_OSC_TARGET"] = "none"
    resolved = _resolve_osc_target()
    if resolved is not None:
        pane, _ = resolved
        try:
            status = os.stat(pane)
        except OSError:
            return env
        if _is_owned_character_device(status):
            env.update(
                THE_PILOT_OSC_TARGET=str(pane),
                THE_PILOT_OSC_TARGET_IS_PANE="1",
                THE_PILOT_OSC_DEV=str(status.st_dev),
                THE_PILOT_OSC_INO=str(status.st_ino),
            )
    return env


def _settle_accepted_auto_submit(
    conversation_id: str, state: dict[str, Any], prompt: dict[str, Any],
    *, transcript_path: str, handle: str,
) -> str:
    scene = str(state.get("scene") or "")
    request_id = str(prompt.get("requestId") or "").strip()
    observation = prompt.get("observation")
    stages = prompt.get("stages")
    verified = bool(
        request_id and observation != "unsupported"
        and isinstance(stages, list) and "turn_started" in stages
    )
    confirm = observation == "supported" and bool(request_id) and not verified
    state["delivery_status"] = "accepted"
    if confirm:
        state.update(delivery_request_id=request_id, delivery_deadline_at=time.time() + CONFIRM_DEADLINE_SECONDS)
    if not save_state(conversation_id, state):
        log_event("auto_submit_state_failed", cid=conversation_id, scene=scene, request_id=request_id)
        return OSC_7501_WAITING_QUESTION
    if not confirm:
        details = {"request_id": request_id} if verified else {}
        log_event("auto_submitted", cid=conversation_id, scene=scene, verified=verified, **details)
        return ""

    reexecuted = _env_value("THE_PILOT_REEXEC", "ORCA_AUTOPILOT_REEXEC") == "1"
    stop_pid = os.getppid() if reexecuted else os.getpid()
    try:
        subprocess.Popen(
            [
                sys.executable, str(Path(__file__).resolve()), "confirm-delivery",
                "--cid", conversation_id,
                "--user-turn", state["current_user_turn_id"],
                "--turn-id", state["last_settled_turn_id"],
                "--request-id", request_id,
                "--terminal", handle,
                "--incarnation", str(prompt.get("processIncarnation") or ""),
                "--transcript", transcript_path,
                "--parent-pid", str(stop_pid),
            ],
            env=_delivery_confirmation_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        _record_unconfirmed_delivery(conversation_id, state, request_id, "confirm_spawn_failed")
        return OSC_7501_WAITING_QUESTION
    log_event("auto_submit_accepted", cid=conversation_id, scene=scene, request_id=request_id)
    return ""


def _evaluate_stop(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Decide the Stop hook action and the OSC 7501 frame it implies.

    The frame is emitted only after the hook prints its decision: ``""`` keeps
    Orca treating the agent as working (the turn continues or a new turn was
    already submitted), `done` releases orchestration mail, and
    `blocked:kind=question` makes Orca hold mail while the agent waits behind a
    picker or an unconfirmed prefilled draft. That hold decides mail delivery
    only while this hook is the pane's sole OSC 7501 emitter. Agy's built-in
    emitter can supersede the hook's frame after Stop returns.
    """
    termination = str(payload.get("terminationReason") or "")
    fully_idle = payload.get("fullyIdle")
    if fully_idle is None:
        fully_idle = payload.get("fully_idle")
    error = str(payload.get("error") or "").strip()
    conversation_id = str(payload.get("conversationId") or "")
    if termination != "NO_TOOL_CALL" or fully_idle is not True:
        # A tool call is running, background work is still active, or the stop
        # is synthetic: the turn has not reached rest, so no status is claimed.
        return {"decision": "allow"}, ""
    if error or not conversation_id:
        return {"decision": "allow"}, OSC_7501_DONE
    transcript_path = payload.get("transcriptPath")
    steps, complete = read_steps(transcript_path)
    if steps and ask_question_unsettled_reason(current_turn_steps(steps)):
        if not complete or is_dispatched_worker(steps):
            # A question UI is open without a settled answer. Pressing Enter
            # from an orchestration delivery would answer it by accident.
            return {"decision": "allow"}, OSC_7501_WAITING_QUESTION
    if not complete:
        log_event("stop_transcript_incomplete", cid=conversation_id)
        return {"decision": "allow"}, OSC_7501_DONE
    if not steps or is_dispatched_worker(steps):
        return {"decision": "allow"}, OSC_7501_DONE

    turn_id = turn_identity(steps)
    user_turn = turn_user_identity(steps)
    if not user_turn:
        log_event("stop_turn_identity_missing", cid=conversation_id)
        return {"decision": "allow"}, OSC_7501_DONE

    reexec_output = _sync_and_maybe_reexec("Stop", payload)
    if reexec_output is not None:
        result = reexec_output.get("result")
        osc_frame = reexec_output.get("osc_frame")
        if not isinstance(result, dict) or not isinstance(osc_frame, str):
            raise HookExecutionError(
                "engine_reexec_invalid_output", "Stop re-exec must return a hook result and OSC frame"
            )
        return result, osc_frame
    with state_lock(conversation_id):
        state = load_state(conversation_id)
        with _file_lock(PILOT_DIR / ".pilot.lock"):
            profile, scenes = _load_stop_settings(conversation_id, state)
        if state is None:
            log_event("stop_state_corrupt", cid=conversation_id)
            return {"decision": "allow"}, OSC_7501_DONE
        current_user_turn = str(state.get("current_user_turn_id") or "")
        if current_user_turn and current_user_turn != user_turn:
            log_event(
                "stop_stale_turn",
                cid=conversation_id,
                current=current_user_turn,
                incoming=user_turn,
            )
            # A newer PreInvocation already claimed this pane as working; an
            # obsolete Stop must never overwrite that active turn's status.
            return {"decision": "allow"}, ""
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
            return {"decision": "allow"}, OSC_7501_WAITING_QUESTION
        status = str(state.get("delivery_status") or "")
        request_id = str(state.get("delivery_request_id") or "")
        if (
            state.get("last_settled_turn_id") == turn_id
            and status == "accepted"
            and request_id
            and time.time() >= float(state.get("delivery_deadline_at") or 0.0)
        ):
            _record_unconfirmed_delivery(conversation_id, state, request_id, "confirm_abandoned")
            return {"decision": "allow"}, OSC_7501_WAITING_QUESTION
        if (
            state.get("last_settled_turn_id") == turn_id
            or status in {"pending", "unknown"}
            or status == "rejected" and state.get("candidate_turn_id") == turn_id
        ):
            log_event(
                "stop_delivery_frozen",
                cid=conversation_id,
                turn_id=turn_id,
                status=status,
            )
            return (
                {"decision": "allow"},
                _osc_frame_for_delivery_status(state),
            )
        _clear_delivery_confirmation(state)

        planners = [step for step in turn_steps if step.get("type") == "PLANNER_RESPONSE"]
        if not planners:
            clear_suggestion(state)
            save_state(conversation_id, state)
            return {"decision": "allow"}, OSC_7501_DONE

        parsed = parse_suggestion(str(planners[-1].get("content") or ""))
        if parsed["invalid"]:
            last_planner = planners[-1]
            if (
                parsed["invalid"] == "missing_tag"
                and current_mode(profile) != "off"
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
                    return {"decision": "allow"}, OSC_7501_DONE
                log_event(
                    "stop_continue_unasked_question",
                    cid=conversation_id,
                    turn_id=turn_id,
                )
                return (
                    {
                        "decision": "continue",
                        "reason": (
                            "上一条回复在冒号引导语处提前结束，既未发出 ask_question 工具调用，"
                            "也未输出回合结束标签。若需向用户确认问题，请立即直接调用 ask_question "
                            "工具（不要重复已输出的正文，同一步不写 pilot 标签）；若无需提问，"
                            "请补齐收束结论与末尾 <!-- pilot: ... --> 标签。"
                        ),
                    },
                    "",
                )
            clear_suggestion(state)
            save_state(conversation_id, state)
            log_event(
                "stop_invalid_tag",
                cid=conversation_id,
                reason=str(parsed["invalid"]),
            )
            return {"decision": "allow"}, OSC_7501_DONE

        scene = str(parsed["scene"])
        primary = str(parsed["prompt"])
        if not primary or scene == "NONE":
            clear_suggestion(state)
            save_state(conversation_id, state)
            log_event("stop_without_suggestion", cid=conversation_id, scene=scene or "")
            return {"decision": "allow"}, OSC_7501_DONE

        workspaces = payload.get("workspacePaths") or []
        workspace_path = str(workspaces[0]) if isinstance(workspaces, list) and workspaces else ""
        explicit_handle = str(payload.get("terminalHandle") or "")
        previous_count = int(state.get("consecutive_auto_turns") or 0)
        gate_action = str(parsed["gate"])
        mode, gate, auto_submit = _admit_auto_submit_policy(
            profile, scenes, scene=scene, gate_action=gate_action, previous_count=previous_count,
        )
        handle = resolve_terminal(workspace_path, explicit_handle) if scene in scenes and mode != "off" else ""

        candidates = [{"index": 1, "scene": scene, "prompt": primary}]
        candidates.extend(
            {"index": int(item["index"]), "scene": scene, "prompt": item["prompt"]}
            for item in parsed["alternatives"]
        )
        state.update(
            {
                "scene": scene,
                "primary_text": primary,
                "candidates": candidates,
                "candidate_turn_id": turn_id,
            }
        )
        process = None
        launch_error = None
        previous_text = str(state.get("auto_submitted_text") or "")
        if handle:
            # Lookup and receipt waiting stay outside the profile lock. Both
            # kinds of send launch against the installed policy under the lock.
            with _file_lock(PILOT_DIR / ".pilot.lock"):
                profile, scenes = _load_stop_settings(conversation_id, state)
                mode, gate, still_admitted = _admit_auto_submit_policy(
                    profile, scenes, scene=scene, gate_action=gate_action, previous_count=previous_count,
                )
                auto_submit = auto_submit and still_admitted
                if scene in scenes and mode != "off":
                    # Agy writes the transcript independently of both locks.
                    latest_steps, complete = read_steps(transcript_path)
                    if not complete:
                        log_event("stop_transcript_incomplete", cid=conversation_id)
                        return {"decision": "allow"}, ""
                    if turn_user_identity(latest_steps) != user_turn or turn_identity(latest_steps) != turn_id:
                        log_event("stop_transcript_changed", cid=conversation_id, turn_id=turn_id)
                        return {"decision": "allow"}, ""
                    if auto_submit:
                        state.update(
                            gate_action=gate,
                            consecutive_auto_turns=previous_count + 1,
                            auto_submitted_text=primary,
                            last_settled_turn_id=turn_id,
                            delivery_status="pending",
                        )
                        if not save_state(conversation_id, state):
                            log_event("auto_submit_state_failed", cid=conversation_id, scene=scene)
                            return {"decision": "allow"}, OSC_7501_DONE
                    try:
                        process = _start_terminal_send(handle, primary, enter=auto_submit)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        launch_error = str(exc)

        if scene not in scenes or mode == "off":
            clear_suggestion(state)
            save_state(conversation_id, state)
            event = "stop_without_suggestion" if scene not in scenes else "stop_mode_off"
            log_event(event, cid=conversation_id, scene=scene)
            return {"decision": "allow"}, OSC_7501_DONE

        outcome = "none"
        receipt = {}
        if process is not None:
            outcome, receipt = _wait_for_terminal_receipt(process, handle, enter=auto_submit)
        elif launch_error is not None:
            outcome = "rejected" if auto_submit else "unknown"
            log_event("send_start_failed", handle=handle, enter=auto_submit, error=launch_error)

        # Settle turns that had no terminal or were only eligible for prefill.
        # Auto-submit already wrote these fields before launching.
        if outcome == "none" or not auto_submit:
            state.update(gate_action=gate, last_settled_turn_id=turn_id, auto_submitted_text="")
        if auto_submit:
            details = {}
            if outcome == "accepted":
                prompt = receipt.get("prompt")
                prompt = prompt if isinstance(prompt, dict) else {}
                observation = prompt.get("observation")
                if observation in ("permission", "incarnation_replaced"):
                    outcome = "rejected"
                    details["reason"] = observation
                else:
                    return {"decision": "allow"}, _settle_accepted_auto_submit(
                        conversation_id, state, prompt, transcript_path=transcript_path, handle=handle,
                    )
            if outcome == "rejected":
                _rollback_auto_submit(state, previous_count, previous_text)
                if details.get("reason") == "permission":
                    state["rejected_reason"] = "permission"
            state["delivery_status"] = outcome
            event = {
                "none": "auto_submit_no_terminal",
                "rejected": "auto_submit_rejected",
                "unknown": "auto_submit_unknown",
            }[outcome]
        else:
            if handle and outcome != "accepted":
                log_event("prefill_not_accepted", cid=conversation_id, scene=scene)
            state["delivery_status"] = {
                "accepted": "prefilled",
                "unknown": "prefill_unknown",
                "rejected": "rejected",
                "none": "none",
            }[outcome]
            event = "prefilled_only"
            details = {"gate": gate, "mode": mode}
        save_state(conversation_id, state)
        log_event(event, cid=conversation_id, scene=scene, **details)
        return (
            {"decision": "allow"},
            _osc_frame_for_delivery_status(state),
        )


def handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only the Stop hook decision for direct callers."""
    return _evaluate_stop(payload)[0]


def _timeout_seconds(variable: str, default: float) -> float:
    try:
        milliseconds = int(os.environ.get(variable, ""))
    except ValueError:
        return default
    return min(max(milliseconds, 0), int(default * 1000)) / 1000


def _wait_for_stop_exit(parent_pid: int) -> bool:
    timeout = _timeout_seconds("THE_PILOT_STOP_EXIT_TIMEOUT_MS", STOP_EXIT_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    while parent_pid > 1:
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.01, remaining))
    return False


def _owns_delivery_confirmation(state: dict[str, Any], args: argparse.Namespace) -> bool:
    return (
        state.get("current_user_turn_id") == args.user_turn
        and state.get("last_settled_turn_id") == args.turn_id
        and state.get("delivery_status") == "accepted"
        and state.get("delivery_request_id") == args.request_id
    )


def _new_transcript_input(
    steps: list[dict[str, Any]], expected_user_turn: str,
) -> dict[str, Any] | None:
    user_turn = turn_user_identity(steps)
    if not user_turn or user_turn == expected_user_turn:
        return None
    latest_user = next(step for step in reversed(steps) if step.get("type") == "USER_INPUT")
    if (
        "content" in _truncated_fields(latest_user)
        and user_turn.partition(":")[0] == expected_user_turn.partition(":")[0]
    ):
        # The hook may have hydrated this input. The compact text at the same
        # position cannot prove that a new turn started.
        return None
    return latest_user


def _poll_turn_start(args: argparse.Namespace) -> None:
    # Polling is advisory. Repeat all decision reads after taking the session lock.
    timeout = _timeout_seconds("THE_PILOT_CONFIRM_TIMEOUT_MS", CONFIRM_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    while True:
        state = load_state(args.cid)
        if state is None or not _owns_delivery_confirmation(state, args):
            return
        steps, _ = read_steps(args.transcript, hydrate=False)
        if _new_transcript_input(steps, args.user_turn) is not None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def command_confirm_delivery(args: argparse.Namespace) -> int:
    stop_exited = _wait_for_stop_exit(args.parent_pid)
    if stop_exited:
        _poll_turn_start(args)
    cid = args.cid
    request_id = args.request_id
    with state_lock(cid):
        state = load_state(cid)
        if state is not None and state.get("last_confirmed_request_id") == request_id:
            state["last_confirmed_request_id"] = ""
            if save_state(cid, state):
                log_event(
                    "auto_submitted", cid=cid, scene=str(state.get("scene") or ""),
                    request_id=request_id, verified=True,
                )
            return 0
        if state is None or not _owns_delivery_confirmation(state, args):
            log_event("auto_submit_superseded", cid=cid, request_id=request_id)
            return 0

        steps, complete = read_steps(args.transcript, hydrate=False)
        latest_user = _new_transcript_input(steps, args.user_turn)
        if latest_user is not None:
            _clear_delivery_request(state)
            if save_state(cid, state):
                if extract_user_text(latest_user.get("content")) == state.get("auto_submitted_text"):
                    log_event(
                        "auto_submitted", cid=cid, scene=str(state.get("scene") or ""),
                        request_id=request_id, verified=True,
                    )
                else:
                    log_event("auto_submit_superseded", cid=cid, request_id=request_id)
            return 0

        if not stop_exited:
            reason = "stop_still_running"
        elif not complete:
            reason = "transcript_incomplete"
        elif turn_identity(steps) != args.turn_id:
            reason = "transcript_changed"
        else:
            reason = "turn_start_timeout"
        if _record_unconfirmed_delivery(cid, state, request_id, reason) and _terminal_incarnation_matches(
            args.terminal, args.incarnation,
        ):
            emit_osc_7501(OSC_7501_WAITING_QUESTION)
    return 0


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


def _scenes_marker_bounds(text: str) -> tuple[str, str, int, int] | None:
    """Return one managed scenes marker pair, canonical or legacy."""
    pairs: list[tuple[str, str, int, int]] = []
    for begin_marker, end_marker in (
        (SCENES_BEGIN, SCENES_END),
        (LEGACY_SCENES_BEGIN, LEGACY_SCENES_END),
    ):
        starts = _find_all_positions(text, begin_marker)
        ends = _find_all_positions(text, end_marker)
        if not starts and not ends:
            continue
        if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
            return None
        pairs.append((begin_marker, end_marker, starts[0], ends[0]))
    if len(pairs) != 1:
        return None
    return pairs[0]


def command_sync() -> int:
    # Serialize with apply/uninstall and re-read every input under the lock so a
    # sync that started before an uninstall cannot write the managed block back.
    with _file_lock(PILOT_DIR / ".pilot.lock"):
        profile, profile_status, profile_err = load_profile()
        scenes, scene_errors = scan_scenes()
        if profile_status == "missing":
            print(
                "未安装（缺少 profile.json）；请先运行 pilot apply。",
                file=sys.stderr,
            )
            return 2
        if profile_status == "corrupt" or not profile:
            print(
                f"profile.json 损坏：{profile_err or 'empty profile object'}",
                file=sys.stderr,
            )
            return 2
        if scene_errors:
            print("场景文件不可读：" + "; ".join(scene_errors), file=sys.stderr)
            return 2
        try:
            text = GEMINI_MD_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"无法读取 GEMINI.md：{exc}", file=sys.stderr)
            return 2
        bounds = _scenes_marker_bounds(text)
        if bounds is None:
            print("GEMINI.md 的场景托管标记缺失、重复或顺序错误。", file=sys.stderr)
            return 2
        begin_marker, end_marker, start, end = bounds
        inner = f"{begin_marker}\n\n{render_scenes_block(scenes)}\n\n{end_marker}"
        merged = text[:start] + inner + text[end + len(end_marker):]
        if not save_text(GEMINI_MD_PATH, merged):
            print("无法写入 GEMINI.md。", file=sys.stderr)
            return 2
    log_event("scenes_synced")
    print("已按 scenes/*.md 刷新 GEMINI.md 场景段。")
    request_background_update()
    return 0


def command_status(as_json: bool) -> int:
    profile, profile_status, profile_err = load_profile()
    scenes, scene_errors = scan_scenes()
    config, config_status, config_err = load_config()
    hooks, hooks_status, hooks_err = load_json(GEMINI_DIR / "config" / "hooks.json")
    errors = []
    for status, error in (
        (profile_status, profile_err),
        (config_status, config_err),
        (hooks_status, hooks_err),
    ):
        if status != "ok":
            errors.append(error)
    errors.extend(scene_errors)
    profile_data = profile or {}
    config_data = config or {}
    report = {
        "ok": not errors,
        "errors": errors,
        "installed": (
            profile_status == "ok"
            and config_status == "ok"
            and hooks_status == "ok"
            and "the-pilot" in (hooks or {})
        ),
        "profile_status": profile_status,
        "config_status": config_status,
        "hooks_status": hooks_status,
        "gemini_dir": str(GEMINI_DIR),
        "mode": current_mode(profile_data) if profile is not None else None,
        "worker_agents": current_worker_agents(profile_data) if profile is not None else [],
        "max_consecutive_auto_turns": (
            max_consecutive_turns(profile_data) if profile is not None else None
        ),
        "scenes": sorted(scenes),
        "entry_shortcuts": sorted((config_data.get("entry_shortcuts") or {})),
        "comma_commands": sorted(
            {
                alias
                for command in (config_data.get("comma_commands") or {}).values()
                if isinstance(command, dict)
                for alias in (command.get("aliases") or [])
            }
        ),
        "hooks_group": "the-pilot" in (hooks or {}),
        "engine": str(Path(__file__).resolve()),
        "profile": str(PROFILE_PATH),
    }
    if models := current_worker_models(profile_data):
        report["worker_models"] = models
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        print(f"mode: {report['mode']}  max_turns: {report['max_consecutive_auto_turns']}")
        print("scenes: " + (", ".join(report["scenes"]) or "（无）"))
        print("entry: " + (", ".join(report["entry_shortcuts"]) or "（无）"))
        print("hooks_group: " + ("yes" if report["hooks_group"] else "no"))
    return 2 if errors else 0


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


def _update_config_template(
    config: dict[str, Any], canonical: str, command_id: str, template: str,
) -> bool:
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
    return updated


def command_set(target: str, template: str) -> int:
    template = template.strip()
    if not template:
        print("模板不能为空。", file=sys.stderr)
        return 2
    with _file_lock(PILOT_DIR / ".pilot.lock"):
        profile, profile_status, profile_err = load_profile()
        config, config_status, config_err = load_config()
        if profile_status == "missing" or config_status == "missing":
            print(
                "未安装（缺少 profile.json 或 config.json）；请先运行 pilot apply。",
                file=sys.stderr,
            )
            return 2
        if profile_status == "corrupt" or profile is None:
            print(f"profile.json 损坏：{profile_err}", file=sys.stderr)
            return 2
        if config_status == "corrupt" or config is None:
            print(f"config.json 损坏：{config_err}", file=sys.stderr)
            return 2
        if not profile or not config:
            print(
                "profile.json 或 config.json 为空；请先运行 pilot apply。",
                file=sys.stderr,
            )
            return 2
        if not _prepare_profile_for_write(profile):
            print(
                "profile.json 同时包含 pilot 与旧版 autopilot 段；请只保留 pilot。",
                file=sys.stderr,
            )
            return 2
        workspace_catalog = None
        identity = _command_identity(config, target)
        if identity is None:
            workspace_catalog = workspace_config(config, profile, {})
            identity = _command_identity(workspace_catalog, target)
        if identity is None:
            print(f"未知口令 {target!r}；请用 status 查看可用口令。", file=sys.stderr)
            return 2
        canonical, command_id = identity
        if workspace_catalog is None and not _update_config_template(config, canonical, command_id, template):
            print(f"未知口令 {target!r}；请用 status 查看可用口令。", file=sys.stderr)
            return 2
        shortcuts = profile.setdefault("shortcuts", {})
        overrides = shortcuts.setdefault("template_overrides", {})
        if workspace_catalog is not None:
            installed_entries = config.get("entry_shortcuts") or {}
            enabled_entry = shortcuts.setdefault(
                "enabled_entry", [key for key in UNIVERSAL_ENTRY_KEYS if key in installed_entries],
            )
            if not isinstance(enabled_entry, list):
                print("profile.json 的 shortcuts.enabled_entry 必须是列表。", file=sys.stderr)
                return 2
            if canonical not in enabled_entry:
                enabled_entry.append(canonical)
            owners, _ = shortcut_override_owners(
                enabled_entry, workspace_catalog["entry_shortcuts"], workspace_catalog["comma_commands"],
            )
            for alias in list(overrides):
                if owners.get(shortcut_override_name(alias)) == canonical:
                    del overrides[alias]
        overrides[canonical] = template
        if not save_json(PROFILE_PATH, profile):
            print("profile.json 写入失败。", file=sys.stderr)
            return 2
        if workspace_catalog is None and not save_json(CONFIG_PATH, config):
            print("config.json 写入失败；下次 apply --upgrade 会重新编译。", file=sys.stderr)
            return 2
        log_event("template_set", target=target, key=canonical, command_id=command_id)
        print(f"已更新 `{canonical}` 的模板；下一次 apply/upgrade 会保留该覆盖。")
    request_background_update()
    return 0


def command_log(limit: int) -> int:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        print("（暂无 pilot 日志）")
        return 0
    except OSError as exc:
        print(f"无法读取日志：{exc}", file=sys.stderr)
        return 2
    for line in lines[-limit:]:
        print(line)
    return 0


def _hook_failure(event: str, error: str, detail: str) -> int:
    """Report a refused hook as structured JSON on stderr with exit code 2."""
    print(
        json.dumps(
            {"ok": False, "event": event, "error": error, "detail": detail},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 2


def run_hook(event: str) -> int:
    payload, payload_err = read_payload()
    if payload is None:
        return _hook_failure(event, "invalid_payload", payload_err)
    try:
        if event == "SessionStart":
            initial_input = extract_user_text(str(payload.get("lastUserInput") or ""))
            emit_osc_7501(OSC_7501_WORKING if initial_input else OSC_7501_DONE)
            print("{}", flush=True)
            return 0
        if event == "PreInvocation":
            result = handle_pre_invocation(payload)
            osc_frame = OSC_7501_WORKING
        else:
            result, osc_frame = _evaluate_stop(payload)
    except HookExecutionError as exc:
        return _hook_failure(event, exc.code, exc.detail)
    except Exception as exc:
        log_event("hook_failed", hook_event=event, error=repr(exc))
        return _hook_failure(event, "internal_error", repr(exc))
    reexecuted = _env_value("THE_PILOT_REEXEC", "ORCA_AUTOPILOT_REEXEC") == "1"
    # The parent publishes the decision and writes the child's exact frame
    # using its connection to the pane. Only the private Stop pipe gets this receipt.
    output = {"result": result, "osc_frame": osc_frame} if reexecuted and event == "Stop" else result
    print(json.dumps(output, ensure_ascii=False), flush=True)
    if not reexecuted and osc_frame:
        emit_osc_7501(osc_frame)
    return 0


def command_worker(target: str, as_json: bool) -> int:
    profile, profile_status, profile_err = load_profile()
    if profile_status != "ok" or profile is None:
        report = {
            "ok": False,
            "status": profile_status,
            "error": profile_err,
            "worker_agents": [],
            "updated_files": [],
        }
        if as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif profile_status == "missing":
            print(f"未安装（缺少 profile.json）：{profile_err}", file=sys.stderr)
        else:
            print(f"profile.json 损坏：{profile_err}", file=sys.stderr)
        return 2
    if target == "status":
        success, workers, updated = True, current_worker_agents(profile), []
        models = current_worker_models(profile)
    else:
        success, workers, updated, models = set_worker_agents(target)
        if success:
            request_background_update()
    report = {"ok": success, "worker_agents": workers, "updated_files": updated}
    if models:
        report["worker_models"] = models
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if workers:
            print(worker_priority(workers, models))
        if not success:
            print("Worker switch failed: check profile.json and derived guidance files.", file=sys.stderr)
        for path in updated:
            print(f"updated: {path}")
    return 0 if success else 2


def command_mode(target: str) -> int:
    profile, profile_status, profile_err = load_profile()
    if profile_status == "missing":
        print(f"未安装（缺少 profile.json）：{profile_err}", file=sys.stderr)
        return 2
    if profile_status == "corrupt" or profile is None:
        print(f"profile.json 损坏：{profile_err}", file=sys.stderr)
        return 2
    if target != "status":
        if not set_mode(target):
            print("Mode switch failed: profile.json is missing or unwritable.", file=sys.stderr)
            return 2
        profile, profile_status, profile_err = load_profile()
        if profile_status != "ok" or profile is None:
            print(f"profile.json 损坏：{profile_err}", file=sys.stderr)
            return 2
        request_background_update()
    print(f"Pilot mode: {current_mode(profile)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runtime.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker", help="切换或查看 Worker 执行器优先级")
    worker.add_argument("target", nargs="?", default="status", choices=WORKER_TARGETS)
    worker.add_argument("--json", action="store_true")
    mode = subparsers.add_parser("mode", help="切换或查看 Pilot 模式")
    mode.add_argument("target", nargs="?", default="status", choices=(*MODES, "status"))
    hook = subparsers.add_parser("hook", help="运行 Antigravity 生命周期 Hook")
    hook.add_argument("--event", required=True, choices=("SessionStart", "PreInvocation", "Stop"))
    status = subparsers.add_parser("status", help="查看模式、场景与 Hook 状态")
    status.add_argument("--json", action="store_true")
    subparsers.add_parser("sync", help="按 scenes/*.md 刷新 GEMINI.md 场景段")
    setter = subparsers.add_parser("set", help="修改开局/逗号口令模板")
    setter.add_argument("target")
    setter.add_argument("template")
    logger = subparsers.add_parser("log", help="查看最近事件")
    logger.add_argument("--limit", type=int, default=30)
    # argparse includes suppressed subparsers in usage and the help table.
    subparsers.metavar = "{" + ",".join(subparsers.choices) + "}"
    confirm = subparsers.add_parser("confirm-delivery", help=argparse.SUPPRESS)
    subparsers._choices_actions.pop()
    for option in ("cid", "user-turn", "turn-id", "request-id", "terminal", "incarnation", "transcript"):
        confirm.add_argument(f"--{option}", required=True)
    confirm.add_argument("--parent-pid", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else argv
    if raw_args and raw_args[0] == "announce-boot":
        agy_args = raw_args[1:]
        return command_announce_boot(agy_args[1:] if agy_args[:1] == ["--"] else agy_args)
    args = build_parser().parse_args(raw_args)
    if args.command == "worker":
        return command_worker(args.target, args.json)
    if args.command == "mode":
        return command_mode(args.target)
    if args.command == "hook":
        return run_hook(args.event)
    if args.command == "confirm-delivery":
        return command_confirm_delivery(args)
    if args.command == "status":
        return command_status(args.json)
    if args.command == "sync":
        return command_sync()
    if args.command == "set":
        return command_set(args.target, args.template)
    return command_log(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
