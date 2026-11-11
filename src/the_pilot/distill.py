"""Read-only distillation of recent multi-agent user chat histories.

Scans the user's recent conversations across Antigravity/Gemini CLI, Claude Code,
Codex, and Cursor over a bounded day window (default 7 days), filters out
machine-generated orchestration/review preambles, clusters recurring user input
patterns without regular expressions, and produces scene and shortcut automation
recommendations for agent-assisted setup or tuning.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

DISTILL_SCHEMA = "the-pilot-distillation/v1"
DEFAULT_DAYS = 7
MAX_DAYS = 90
DEFAULT_MAX_SESSIONS = 80
MAX_SESSIONS_LIMIT = 500
MAX_LINES_PER_SESSION = 2000
MAX_EXAMPLES_PER_PATTERN = 3
MAX_PATTERNS = 25
MAX_SAMPLED_PROMPTS = 30

MACHINE_PREFIXES = (
    "Please carry out this task from my Orca coordinator",
    "You are working inside Orca, a multi-agent IDE. You are a dispatched worker",
    "You are an independent Adversarial Architecture",
    "Read-only static review: no writes",
    "This session is being continued from a previous conversation",
    "Generate a short git branch name that summarizes the coding task",
    "Reply with ONLY:",
    "<environment_context>",
    "<permissions instructions>",
    "# AGENTS.md instructions",
)

BUILTIN_AUTOPILOT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("再整体 review", "architecture-re-review"),
    ("对照最初的审查结果文档和设计文档", "architecture-re-review"),
    ("对照最初的设计方案", "architecture-re-review"),
    ("对照最初的设计文档", "architecture-re-review"),
    ("先保存审查结果文档", "plan-and-orchestrate"),
    ("先输出详细分阶段方案", "plan-and-orchestrate"),
    ("合入 main 并发 dev", "deliver-and-verify"),
    ("发 dev 完成环境部署核验", "deliver-and-verify"),
    ("核查同步本会话整个任务涉及的文档", "closeout-and-sync"),
    ("同步本任务文档、待发台账与 WORKING.md", "closeout-and-sync"),
    ("把这条反馈标记完成", "closeout-and-sync"),
    ("只把这条反馈标记完成", "closeout-and-sync"),
    ("再整体核查一下本会话整个任务涉及的相关文档", "closeout-and-sync"),
)

CONTINUE_NUDGES = frozenset({
    "继续",
    "好了",
    "ok",
    "okay",
    "continue",
    "go on",
    "proceed",
    "1",
    "yes",
    "好",
    "可以",
})


def distill_chat_history(
    home_dir: Path,
    gemini_dir: Path,
    *,
    days: int = DEFAULT_DAYS,
    max_sessions_per_agent: int = DEFAULT_MAX_SESSIONS,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Collect and cluster recent user messages across local coding agents."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days 必须是 1..{MAX_DAYS} 的整数")
    if (
        isinstance(max_sessions_per_agent, bool)
        or not isinstance(max_sessions_per_agent, int)
        or not 1 <= max_sessions_per_agent <= MAX_SESSIONS_LIMIT
    ):
        raise ValueError(f"max_sessions 必须是 1..{MAX_SESSIONS_LIMIT} 的整数")

    current_ts = float(time.time() if now_ts is None else now_ts)
    cutoff_ts = current_ts - days * 86400.0
    installed_templates = _load_installed_templates(gemini_dir)

    sources: dict[str, dict[str, int]] = {}
    all_records: list[dict[str, Any]] = []
    for agent, collector in (
        ("antigravity", lambda: _collect_antigravity(home_dir, gemini_dir, cutoff_ts, max_sessions_per_agent)),
        ("claude", lambda: _collect_claude(home_dir, cutoff_ts, max_sessions_per_agent)),
        ("codex", lambda: _collect_codex(home_dir, cutoff_ts, max_sessions_per_agent)),
        ("cursor", lambda: _collect_cursor(home_dir, cutoff_ts, max_sessions_per_agent)),
    ):
        stats, records = collector()
        sources[agent] = stats
        all_records.extend(records)

    autopilot_counts: Counter[str] = Counter()
    pattern_groups: dict[str, dict[str, Any]] = {}
    manual_records: list[dict[str, Any]] = []
    entry_count = 0
    followup_count = 0

    for item in all_records:
        role = item["turn_role"]
        if role == "entry":
            entry_count += 1
        else:
            followup_count += 1

        matched_autopilot = _match_autopilot_activity(item["text"], installed_templates)
        if matched_autopilot is not None:
            autopilot_counts[matched_autopilot] += 1
            continue

        manual_records.append(item)
        signature = _normalize_signature(item["text"])
        if not signature:
            continue
        group = pattern_groups.get(signature)
        if group is None:
            group = {
                "signature": signature,
                "count": 0,
                "entry_count": 0,
                "followup_count": 0,
                "agents": set(),
                "examples": [],
            }
            pattern_groups[signature] = group
        group["count"] += 1
        if role == "entry":
            group["entry_count"] += 1
        else:
            group["followup_count"] += 1
        group["agents"].add(item["agent"])
        preview = _single_line_preview(item["text"], limit=180)
        if preview and preview not in group["examples"] and len(group["examples"]) < MAX_EXAMPLES_PER_PATTERN:
            group["examples"].append(preview)

    sorted_groups = sorted(
        (g for g in pattern_groups.values() if g["count"] >= 2),
        key=lambda g: (-g["count"], g["signature"]),
    )[:MAX_PATTERNS]

    recurring_patterns: list[dict[str, Any]] = []
    for group in sorted_groups:
        turn_role = _resolve_turn_role(group["entry_count"], group["followup_count"])
        category, suggestion = _classify_pattern(group["signature"], turn_role, group["count"])
        recurring_patterns.append({
            "signature": group["signature"],
            "count": group["count"],
            "turn_role": turn_role,
            "agents": sorted(group["agents"]),
            "category": category,
            "examples": group["examples"],
            "suggested_automation": suggestion,
        })

    recommendations = _build_recommendations(recurring_patterns, autopilot_counts, manual_records)
    sampled_prompts = _sample_diverse_prompts(manual_records, limit=MAX_SAMPLED_PROMPTS)

    return {
        "schema": DISTILL_SCHEMA,
        "window_days": days,
        "max_sessions_per_agent": max_sessions_per_agent,
        "sources": sources,
        "totals": {
            "human_messages": len(all_records),
            "entry_messages": entry_count,
            "followup_messages": followup_count,
            "autopilot_expanded_messages": sum(autopilot_counts.values()),
            "manual_messages": len(manual_records),
        },
        "autopilot_activity": dict(autopilot_counts.most_common(15)),
        "recurring_patterns": recurring_patterns,
        "recommendations": recommendations,
        "sampled_prompts": sampled_prompts,
        "next_step": (
            "Review recurring_patterns, recommendations, and sampled_prompts with the user; "
            "confirm desired scenes, guardrails, and shortcuts before running pilot apply."
        ),
    }


def _collect_antigravity(
    home_dir: Path,
    gemini_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    roots = [gemini_dir / "antigravity-cli/brain"]
    default_brain = home_dir / ".gemini/antigravity-cli/brain"
    if default_brain != roots[0]:
        roots.append(default_brain)

    seen_files: set[Path] = set()
    candidates: list[tuple[float, Path]] = []
    for brain_root in roots:
        if not brain_root.is_dir():
            continue
        for path in brain_root.glob("*/.system_generated/logs/transcript.jsonl"):
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            mtime = _safe_mtime(path)
            if mtime >= cutoff_ts:
                candidates.append((mtime, path))

    candidates.sort(key=lambda item: item[0], reverse=True)
    sampled = candidates[:max_sessions]
    records: list[dict[str, Any]] = []
    machine_filtered = 0

    for mtime, path in sampled:
        turn_idx = 0
        for line in _iter_jsonl_lines(path):
            if line.get("type") != "USER_INPUT":
                continue
            source = line.get("source")
            if source not in (None, "", "USER_EXPLICIT"):
                continue
            ts = _parse_timestamp(line.get("created_at"), fallback=mtime)
            if ts < cutoff_ts:
                continue
            for text in _extract_user_requests(str(line.get("content") or "")):
                if _is_machine_message(text):
                    machine_filtered += 1
                    continue
                records.append({
                    "agent": "antigravity",
                    "session_id": path.parents[2].name,
                    "turn_role": "entry" if turn_idx == 0 else "followup",
                    "timestamp": ts,
                    "text": text,
                })
                turn_idx += 1

    return {
        "discovered_files": len(candidates),
        "sampled_files": len(sampled),
        "human_messages": len(records),
        "filtered_machine_messages": machine_filtered,
    }, records


def _collect_claude(
    home_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    claude_dir = home_dir / ".claude"
    candidates: list[tuple[float, Path]] = []
    projects_dir = claude_dir / "projects"
    if projects_dir.is_dir():
        for path in projects_dir.glob("*/*.jsonl"):
            mtime = _safe_mtime(path)
            if mtime >= cutoff_ts:
                candidates.append((mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    sampled = candidates[:max_sessions]

    records: list[dict[str, Any]] = []
    seen_messages: set[tuple[str, str]] = set()
    session_turns: Counter[str] = Counter()
    machine_filtered = 0

    for mtime, path in sampled:
        session_id = path.stem
        for line in _iter_jsonl_lines(path):
            if line.get("type") != "user" or line.get("isMeta"):
                continue
            ts = _parse_timestamp(line.get("timestamp"), fallback=mtime)
            if ts < cutoff_ts:
                continue
            text = _extract_claude_content(line.get("message"))
            if not text:
                continue
            if _is_machine_message(text):
                machine_filtered += 1
                continue
            dedup_key = (session_id, text)
            if dedup_key in seen_messages:
                continue
            seen_messages.add(dedup_key)
            turn_idx = session_turns[session_id]
            session_turns[session_id] += 1
            records.append({
                "agent": "claude",
                "session_id": session_id,
                "turn_role": "entry" if turn_idx == 0 else "followup",
                "timestamp": ts,
                "text": text,
            })

    history_path = claude_dir / "history.jsonl"
    discovered_count = len(candidates)
    sampled_count = len(sampled)
    if history_path.is_file() and _safe_mtime(history_path) >= cutoff_ts:
        discovered_count += 1
        sampled_count += 1
        for line in _iter_jsonl_lines(history_path, max_lines=4000):
            ts = _parse_timestamp(line.get("timestamp"), fallback=0.0)
            if ts < cutoff_ts:
                continue
            text = str(line.get("display") or "").strip()
            if not text:
                continue
            if _is_machine_message(text):
                machine_filtered += 1
                continue
            session_id = str(line.get("sessionId") or "claude-history")
            dedup_key = (session_id, text)
            if dedup_key in seen_messages:
                continue
            seen_messages.add(dedup_key)
            turn_idx = session_turns[session_id]
            session_turns[session_id] += 1
            records.append({
                "agent": "claude",
                "session_id": session_id,
                "turn_role": "entry" if turn_idx == 0 else "followup",
                "timestamp": ts,
                "text": text,
            })

    return {
        "discovered_files": discovered_count,
        "sampled_files": sampled_count,
        "human_messages": len(records),
        "filtered_machine_messages": machine_filtered,
    }, records


def _collect_codex(
    home_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    codex_dir = home_dir / ".codex"
    candidates: list[tuple[float, Path]] = []
    sessions_dir = codex_dir / "sessions"
    if sessions_dir.is_dir():
        for path in sessions_dir.glob("*/*/*/*.jsonl"):
            mtime = _safe_mtime(path)
            if mtime >= cutoff_ts:
                candidates.append((mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    sampled = candidates[:max_sessions]

    records: list[dict[str, Any]] = []
    seen_messages: set[tuple[str, str]] = set()
    session_turns: Counter[str] = Counter()
    machine_filtered = 0

    for mtime, path in sampled:
        session_id = path.stem
        for line in _iter_jsonl_lines(path):
            text = _extract_codex_session_message(line)
            if not text:
                continue
            ts = _parse_timestamp(line.get("timestamp"), fallback=mtime)
            if ts < cutoff_ts:
                continue
            if _is_machine_message(text):
                machine_filtered += 1
                continue
            dedup_key = (session_id, text)
            if dedup_key in seen_messages:
                continue
            seen_messages.add(dedup_key)
            turn_idx = session_turns[session_id]
            session_turns[session_id] += 1
            records.append({
                "agent": "codex",
                "session_id": session_id,
                "turn_role": "entry" if turn_idx == 0 else "followup",
                "timestamp": ts,
                "text": text,
            })

    history_path = codex_dir / "history.jsonl"
    discovered_count = len(candidates)
    sampled_count = len(sampled)
    if history_path.is_file() and _safe_mtime(history_path) >= cutoff_ts:
        discovered_count += 1
        sampled_count += 1
        for line in _iter_jsonl_lines(history_path, max_lines=5000):
            ts = _parse_timestamp(line.get("ts"), fallback=0.0)
            if ts < cutoff_ts:
                continue
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            if _is_machine_message(text):
                machine_filtered += 1
                continue
            session_id = str(line.get("session_id") or "codex-history")
            dedup_key = (session_id, text)
            if dedup_key in seen_messages:
                continue
            seen_messages.add(dedup_key)
            turn_idx = session_turns[session_id]
            session_turns[session_id] += 1
            records.append({
                "agent": "codex",
                "session_id": session_id,
                "turn_role": "entry" if turn_idx == 0 else "followup",
                "timestamp": ts,
                "text": text,
            })

    return {
        "discovered_files": discovered_count,
        "sampled_files": sampled_count,
        "human_messages": len(records),
        "filtered_machine_messages": machine_filtered,
    }, records


def _collect_cursor(
    home_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    cursor_projects = home_dir / ".cursor/projects"
    candidates: list[tuple[float, Path]] = []
    if cursor_projects.is_dir():
        for path in cursor_projects.glob("*/agent-transcripts/*.jsonl"):
            mtime = _safe_mtime(path)
            if mtime >= cutoff_ts:
                candidates.append((mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    sampled = candidates[:max_sessions]

    records: list[dict[str, Any]] = []
    machine_filtered = 0
    for mtime, path in sampled:
        turn_idx = 0
        for line in _iter_jsonl_lines(path):
            role = line.get("role") or line.get("type")
            if role not in ("user", "USER_INPUT"):
                continue
            text = str(line.get("content") or line.get("text") or "").strip()
            if not text:
                continue
            if _is_machine_message(text):
                machine_filtered += 1
                continue
            records.append({
                "agent": "cursor",
                "session_id": path.stem,
                "turn_role": "entry" if turn_idx == 0 else "followup",
                "timestamp": mtime,
                "text": text,
            })
            turn_idx += 1

    return {
        "discovered_files": len(candidates),
        "sampled_files": len(sampled),
        "human_messages": len(records),
        "filtered_machine_messages": machine_filtered,
    }, records


def _iter_jsonl_lines(path: Path, *, max_lines: int = MAX_LINES_PER_SESSION) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for idx, raw_line in enumerate(handle):
                if idx >= max_lines:
                    break
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    items.append(value)
    except OSError:
        return []
    return items


def _extract_user_requests(raw: str) -> list[str]:
    results: list[str] = []
    pos = 0
    open_tag = "<USER_REQUEST>"
    close_tag = "</USER_REQUEST>"
    while True:
        start = raw.find(open_tag, pos)
        if start < 0:
            break
        end = raw.find(close_tag, start + len(open_tag))
        if end < 0:
            break
        chunk = raw[start + len(open_tag):end].strip()
        if chunk:
            results.append(chunk)
        pos = end + len(close_tag)
    if results:
        return results
    if "<SYSTEM_MESSAGE>" in raw and "<USER_REQUEST>" not in raw:
        return []
    cleaned = raw.strip()
    return [cleaned] if cleaned else []


def _extract_claude_content(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _extract_codex_session_message(line: dict[str, Any]) -> str:
    item_type = line.get("type")
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return ""
    if item_type == "event_msg" and payload.get("type") == "user_message":
        return str(payload.get("message") or "").strip()
    if item_type == "response_item" and payload.get("role") == "user":
        content = payload.get("content")
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "").strip()
                for block in content
                if isinstance(block, dict) and block.get("type") in ("input_text", "text")
            ]
            return "\n".join(p for p in parts if p).strip()
    return ""


def _is_machine_message(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    for prefix in MACHINE_PREFIXES:
        if stripped.startswith(prefix):
            return True
    if "Adversarial Architecture & Engineering Quality Reviewer" in stripped:
        return True
    if stripped.startswith("You have ") and "orchestration message" in stripped:
        return True
    if "Task ID:" in stripped and "Dispatch ID:" in stripped:
        return True
    return False


def _load_installed_templates(gemini_dir: Path) -> dict[str, str]:
    config_path = gemini_dir / "config/autopilot/config.json"
    if not config_path.is_file():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(config, dict):
        return {}
    templates: dict[str, str] = {}
    for section, label_prefix in (
        ("entry_shortcuts", "entry:"),
        ("comma_commands", "comma:,"),
    ):
        for key, spec in (config.get(section) or {}).items():
            if not isinstance(spec, dict):
                continue
            canonical = str(spec.get("canonical_key") or key).strip()
            tmpl = str(spec.get("template") or "").strip()
            if not tmpl:
                continue
            segments = [seg.strip() for seg in tmpl.split("{arg}") if len(seg.strip()) >= 10]
            if segments:
                longest = max(segments, key=len)[:48]
                templates[longest] = f"{label_prefix}{canonical}"
    return templates


def _match_autopilot_activity(text: str, installed_templates: dict[str, str]) -> str | None:
    stripped = text.strip()
    lower = stripped.lower()
    if lower in (",1", ",2", ",3") or lower.startswith((",1 ", ",2 ", ",3 ")):
        return f"candidate:{lower.split()[0]}"
    if lower.startswith(",mode"):
        return "command:,mode"
    if lower.startswith((",worker", ",agent")):
        return "command:,worker"
    for prefix, scene in BUILTIN_AUTOPILOT_PREFIXES:
        if stripped.startswith(prefix):
            return f"scene:{scene}"
    for fragment, label in installed_templates.items():
        if fragment in stripped:
            return label
    return None


def _normalize_signature(text: str) -> str:
    first_line = ""
    for raw_line in text.strip().splitlines():
        candidate = raw_line.strip()
        if candidate:
            first_line = candidate
            break
    if not first_line:
        return ""
    first_line = _strip_dynamic_parenthetical(first_line)
    tokens = first_line.split()
    normalized: list[str] = []
    for tok in tokens[:20]:
        clean = tok.strip(",.;:()[]{}\"'`")
        if _is_uuid_or_hex_sha(clean):
            normalized.append("<id>")
        elif clean.startswith(("http://", "https://")):
            normalized.append("<url>")
        elif "/" in clean and len(clean) > 3 and not clean.startswith("</"):
            normalized.append("<path>")
        elif clean.isdigit() and len(clean) >= 2:
            normalized.append("<n>")
        else:
            normalized.append(tok)
    signature = " ".join(normalized).strip()
    return signature[:140]


def _strip_dynamic_parenthetical(line: str) -> str:
    for marker in ("（反馈文字说明：", "(反馈文字说明:", "（告警详情：", "： 共 "):
        idx = line.find(marker)
        if idx > 8:
            return line[:idx].strip()
    return line


def _is_uuid_or_hex_sha(token: str) -> bool:
    if not token:
        return False
    if len(token) == 36 and token.count("-") == 4:
        parts = token.split("-")
        return all(part and all(ch in "0123456789abcdefABCDEF" for ch in part) for part in parts)
    if 7 <= len(token) <= 40:
        return all(ch in "0123456789abcdefABCDEF" for ch in token) and any(
            ch in "0123456789" for ch in token
        )
    return False


def _resolve_turn_role(entry_count: int, followup_count: int) -> str:
    if entry_count > 0 and followup_count == 0:
        return "entry"
    if followup_count > 0 and entry_count == 0:
        return "followup"
    return "mixed"


def _classify_pattern(signature: str, turn_role: str, count: int) -> tuple[str, dict[str, Any]]:
    lower = signature.lower()
    if lower in CONTINUE_NUDGES:
        return "continue_nudge", {
            "target": "autopilot.mode",
            "action": "Enable `gate` mode (or raise `max_consecutive_auto_turns`) so routine Class A turns advance without manual nudges.",
            "profile_patch": {"autopilot": {"mode": "gate"}},
        }
    if any(kw in lower for kw in ("认领", "排查", "报警", "告警", "反馈", "方案", "设计", "子工作区", "orca")):
        target = "shortcuts.custom_entry" if turn_role in ("entry", "mixed") else "scenes.plan-and-orchestrate"
        return "plan_and_dispatch", {
            "target": target,
            "action": (
                f"Repeated {count}x across sessions; register an entry shortcut in `shortcuts.custom_entry` (or `.pilot/project.json`) "
                "or bind its workflow rules to `plan-and-orchestrate`."
            ),
            "scene": "plan-and-orchestrate",
        }
    if any(kw in lower for kw in ("review", "审查", "复审", "最简架构")):
        return "architecture_review", {
            "target": "scenes.architecture-re-review",
            "action": "Keep `architecture-re-review` enabled and fold recurring review expectations into `scenes.guardrails`.",
            "scene": "architecture-re-review",
        }
    if any(kw in lower for kw in ("合入", "merge", "发 dev", "deploy", "实跑", "e2e", "验证", "verify", "prod", "testflight")):
        return "deliver_and_verify", {
            "target": "scenes.deliver-and-verify",
            "action": "Align `scenes.deliver_strategy` (`merge_dev_only`, `merge_dev_e2e`, or `local_verify_only`) or register a `,` shortcut for custom release checks.",
            "scene": "deliver-and-verify",
        }
    if any(kw in lower for kw in ("收尾", "文档", "working.md", "worker-release", "工作区", "closeout", "cleanup")):
        return "closeout_and_sync", {
            "target": "scenes.closeout-and-sync",
            "action": "Keep `closeout-and-sync` enabled and codify project closeout steps in `.pilot/project.json` (`scene_specs.closeout-and-sync.fallback_prompt`).",
            "scene": "closeout-and-sync",
        }
    if turn_role == "entry":
        return "entry_workflow", {
            "target": "shortcuts.custom_entry",
            "action": f"Repeated session opener ({count}x); extract a parameterized `{signature[:24]}...` template into `shortcuts.custom_entry`.",
        }
    return "custom_followup", {
        "target": "shortcuts.custom_comma",
        "action": f"Repeated mid-session instruction ({count}x); register a `,` alias in `shortcuts.custom_comma`.",
    }


def _build_recommendations(
    recurring_patterns: list[dict[str, Any]],
    autopilot_counts: Counter[str],
    manual_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    seen_targets: set[str] = set()

    for pattern in recurring_patterns:
        suggestion = pattern["suggested_automation"]
        target = suggestion["target"]
        key = f"{pattern['category']}:{target}"
        if key in seen_targets:
            continue
        seen_targets.add(key)
        recommendations.append({
            "category": pattern["category"],
            "target": target,
            "evidence_count": pattern["count"],
            "pattern_signature": pattern["signature"],
            "action": suggestion["action"],
        })

    if not autopilot_counts and manual_records:
        recommendations.insert(0, {
            "category": "onboarding",
            "target": "preset.recommended",
            "evidence_count": len(manual_records),
            "pattern_signature": "(no active autopilot tags detected)",
            "action": "Install the `recommended` preset (`gate` mode + all 4 scenes) to automate routine review, delivery, and closeout transitions.",
        })
    return recommendations[:12]


def _sample_diverse_prompts(records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    sorted_records = sorted(records, key=lambda r: r["timestamp"], reverse=True)
    sampled: list[dict[str, Any]] = []
    seen_previews: set[str] = set()
    for item in sorted_records:
        preview = _single_line_preview(item["text"], limit=200)
        if not preview or preview in seen_previews:
            continue
        seen_previews.add(preview)
        sampled.append({
            "agent": item["agent"],
            "turn_role": item["turn_role"],
            "preview": preview,
        })
        if len(sampled) >= limit:
            break
    return sampled


def _single_line_preview(text: str, *, limit: int = 180) -> str:
    collapsed = " ".join(line.strip() for line in text.strip().splitlines() if line.strip())
    return collapsed[:limit]


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _parse_timestamp(raw: Any, *, fallback: float) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
        return val / 1000.0 if val > 1e11 else val
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return fallback
    return fallback
