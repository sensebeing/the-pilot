"""Read-only distillation of recent multi-agent user chat histories.

Scans the user's recent conversations across Antigravity/Gemini CLI, Claude Code,
Codex, and Cursor over a bounded day window (default 7 days), filters out
machine-generated orchestration/review preambles, clusters recurring user input
patterns without regular expressions, and produces scene and shortcut automation
recommendations for agent-assisted setup or tuning.
"""
from __future__ import annotations

from collections import Counter, deque
import copy
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

from .compiler import split_frontmatter
from .defaults import DEFAULT_SPECS, PIPELINE_STAGE_CONTRACTS, SCENE_ORDER
from .profile import validate_profile
from .project import ProfileError, project_specs

DISTILL_SCHEMA = "the-pilot-distillation/v1"
DEFAULT_DAYS = 7
MAX_DAYS = 90
DEFAULT_MAX_SESSIONS = 80
MAX_SESSIONS_LIMIT = 500
MAX_LINES_PER_SESSION = 2000
MAX_HISTORY_LINES = 5000
MAX_BYTES_PER_FILE = 4 * 1024 * 1024
MAX_EXAMPLES_PER_PATTERN = 3
MAX_PATTERNS = 25
MAX_SAMPLED_PROMPTS = 30
MAX_ERRORS_PER_SOURCE = 5

AUTOPILOT_COMMENT_OPEN = "<!--"
AUTOPILOT_COMMENT_CLOSE = "-->"

MACHINE_PREFIXES = (
    "Please carry out this task from my Orca coordinator",
    "You are working inside Orca, a multi-agent IDE. You are a dispatched worker",
    "You are an independent Adversarial Architecture",
    "Read-only static review: no writes",
    "This session is being continued from a previous conversation",
    "Generate a short git branch name that summarizes the coding task",
    "Generate a short git branch slug",
    "Reply with ONLY:",
    "<environment_context>",
    "<permissions instructions>",
    "# AGENTS.md instructions",
    "<task-notification>",
    "<skill>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
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
    max_lines_per_session: int = MAX_LINES_PER_SESSION,
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
    if (
        isinstance(max_lines_per_session, bool)
        or not isinstance(max_lines_per_session, int)
        or max_lines_per_session < 2
    ):
        raise ValueError("max_lines_per_session 必须是 >= 2 的整数")

    current_ts = float(time.time() if now_ts is None else now_ts)
    cutoff_ts = current_ts - days * 86400.0
    config_status, installed_shortcuts = _load_installed_shortcuts(gemini_dir)
    installed_pipeline = _inspect_installed_pipeline(gemini_dir)

    sources: dict[str, dict[str, Any]] = {}
    all_records: list[dict[str, Any]] = []
    for agent, collector in (
        (
            "antigravity",
            lambda: _collect_antigravity(
                home_dir, gemini_dir, cutoff_ts, max_sessions_per_agent, max_lines_per_session
            ),
        ),
        (
            "claude",
            lambda: _collect_claude(
                home_dir, cutoff_ts, max_sessions_per_agent, max_lines_per_session
            ),
        ),
        (
            "codex",
            lambda: _collect_codex(
                home_dir, cutoff_ts, max_sessions_per_agent, max_lines_per_session
            ),
        ),
        (
            "cursor",
            lambda: _collect_cursor(
                home_dir, cutoff_ts, max_sessions_per_agent, max_lines_per_session
            ),
        ),
    ):
        stats, records = collector()
        sources[agent] = stats
        all_records.extend(records)

    autopilot_counts: Counter[str] = Counter()
    uncertain_template_counts: Counter[str] = Counter()
    pattern_groups: dict[str, dict[str, Any]] = {}
    manual_records: list[dict[str, Any]] = []
    entry_count = 0
    followup_count = 0
    unknown_role_count = 0

    for item in all_records:
        role = item["turn_role"]
        if role == "entry":
            entry_count += 1
        elif role == "followup":
            followup_count += 1
        else:
            unknown_role_count += 1

        matched_autopilot = item.get("tag_provenance") or _match_installed_autopilot(
            item["text"], installed_shortcuts
        )
        if matched_autopilot is not None:
            autopilot_counts[matched_autopilot] += 1
            continue

        uncertain_match = _match_template_similarity(item["text"], installed_shortcuts)
        if uncertain_match is not None:
            uncertain_template_counts[uncertain_match] += 1

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
                "unknown_count": 0,
                "agents": set(),
                "examples": [],
                "uncertain_template_match": uncertain_match,
            }
            pattern_groups[signature] = group
        group["count"] += 1
        if uncertain_match is not None and group.get("uncertain_template_match") is None:
            group["uncertain_template_match"] = uncertain_match
        if role == "entry":
            group["entry_count"] += 1
        elif role == "followup":
            group["followup_count"] += 1
        else:
            group["unknown_count"] += 1
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
        turn_role = _resolve_turn_role(
            group["entry_count"], group["followup_count"], group["unknown_count"]
        )
        category, suggestion = _classify_pattern(group["signature"], turn_role, group["count"])
        pattern_entry: dict[str, Any] = {
            "signature": group["signature"],
            "count": group["count"],
            "turn_role": turn_role,
            "agents": sorted(group["agents"]),
            "category": category,
            "examples": group["examples"],
            "suggested_automation": suggestion,
        }
        if group.get("uncertain_template_match"):
            pattern_entry["uncertain_template_match"] = group["uncertain_template_match"]
        recurring_patterns.append(pattern_entry)

    evidence_complete = (
        config_status.get("status") != "error"
        and installed_pipeline["profile_status"] != "error"
        and installed_pipeline["scenes_status"] != "error"
        and all(
            src["status"] != "degraded"
            and src["truncated_files"] == 0
            and src["undated_messages"] == 0
            for src in sources.values()
        )
    )
    pipeline_assessment = _analyze_pipeline_flow(all_records, manual_records, installed_pipeline)
    recommendations = _build_recommendations(
        recurring_patterns, autopilot_counts, manual_records, config_status, evidence_complete,
        pipeline_assessment,
    )
    sampled_prompts = _sample_diverse_prompts(manual_records, limit=MAX_SAMPLED_PROMPTS)

    return {
        "schema": DISTILL_SCHEMA,
        "window_days": days,
        "max_sessions_per_agent": max_sessions_per_agent,
        "evidence_complete": evidence_complete,
        "config_status": config_status,
        "sources": sources,
        "totals": {
            "human_messages": len(all_records),
            "entry_messages": entry_count,
            "followup_messages": followup_count,
            "unknown_role_messages": unknown_role_count,
            "autopilot_expanded_messages": sum(autopilot_counts.values()),
            "uncertain_template_messages": sum(uncertain_template_counts.values()),
            "manual_messages": len(manual_records),
        },
        "autopilot_activity": dict(autopilot_counts.most_common(15)),
        "uncertain_template_matches": dict(uncertain_template_counts.most_common(15)),
        "recurring_patterns": recurring_patterns,
        "pipeline_assessment": pipeline_assessment,
        "recommendations": recommendations,
        "sampled_prompts": sampled_prompts,
        "next_step": (
            "Review recurring_patterns, recommendations, and sampled_prompts with the user; "
            "confirm desired scenes, guardrails, and shortcuts before running pilot apply."
        ),
    }


def _new_source_stats() -> dict[str, Any]:
    return {
        "status": "absent",
        "discovered_files": 0,
        "sampled_files": 0,
        "truncated_files": 0,
        "read_errors": 0,
        "malformed_lines": 0,
        "undated_messages": 0,
        "human_messages": 0,
        "filtered_machine_messages": 0,
        "errors": [],
    }


def _record_error(stats: dict[str, Any], message: str) -> None:
    stats["read_errors"] += 1
    if len(stats["errors"]) < MAX_ERRORS_PER_SOURCE:
        stats["errors"].append(message)


def _finalize_source_stats(stats: dict[str, Any], root_exists: bool) -> dict[str, Any]:
    if stats["read_errors"] > 0 or stats["malformed_lines"] > 0:
        stats["status"] = "degraded"
    elif root_exists or stats["discovered_files"] > 0:
        stats["status"] = "ok"
    else:
        stats["status"] = "absent"
    return stats


def _collect_antigravity(
    home_dir: Path,
    gemini_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
    max_lines: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stats = _new_source_stats()
    roots = [gemini_dir / "antigravity-cli/brain"]
    default_brain = home_dir / ".gemini/antigravity-cli/brain"
    if default_brain != roots[0]:
        roots.append(default_brain)

    root_exists = any(r.is_dir() for r in roots)
    seen_files: set[Path] = set()
    candidates: list[tuple[float, Path]] = []
    for brain_root in roots:
        if not brain_root.is_dir():
            continue
        for path in brain_root.glob("*/.system_generated/logs/transcript.jsonl"):
            try:
                resolved = path.resolve()
            except OSError as exc:
                _record_error(stats, f"{path}: {exc}")
                continue
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            mtime = _stat_mtime(path, stats)
            if mtime is not None and mtime >= cutoff_ts:
                candidates.append((mtime, path))

    candidates.sort(key=lambda item: item[0], reverse=True)
    sampled = candidates[:max_sessions]
    stats["discovered_files"] = len(candidates)
    stats["sampled_files"] = len(sampled)
    records: list[dict[str, Any]] = []

    for _, path in sampled:
        lines, truncated = _read_tail_jsonl(path, stats, max_lines=max_lines)
        if truncated:
            stats["truncated_files"] += 1
        session_turn_idx = 0
        pending_tag: dict[str, Any] | None = None
        session_id = path.parents[2].name
        for line in lines:
            step_type = line.get("type")
            if step_type == "PLANNER_RESPONSE":
                content_text = str(line.get("content") or "").strip()
                if content_text:
                    pending_tag = _extract_autopilot_tag(content_text)
                continue
            if step_type != "USER_INPUT":
                continue
            source = line.get("source")
            if source not in (None, "", "USER_EXPLICIT"):
                continue
            requests = _extract_user_requests(str(line.get("content") or ""))
            if not requests:
                continue
            ts = _parse_optional_timestamp(line.get("created_at"))
            for text in requests:
                if _is_machine_message(text):
                    stats["filtered_machine_messages"] += 1
                    pending_tag = None
                    continue
                role = "unknown" if truncated else ("entry" if session_turn_idx == 0 else "followup")
                session_turn_idx += 1
                matched_scene = _match_pending_tag(text, pending_tag)
                pending_tag = None
                if ts is None:
                    stats["undated_messages"] += 1
                    continue
                if ts < cutoff_ts:
                    continue
                records.append({
                    "agent": "antigravity",
                    "session_id": session_id,
                    "turn_role": role,
                    "timestamp": ts,
                    "text": text,
                    "tag_provenance": matched_scene,
                })

    stats["human_messages"] = len(records)
    return _finalize_source_stats(stats, root_exists), records


def _collect_claude(
    home_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
    max_lines: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stats = _new_source_stats()
    claude_dir = home_dir / ".claude"
    projects_dir = claude_dir / "projects"
    history_path = claude_dir / "history.jsonl"
    root_exists = claude_dir.is_dir()

    file_by_session: dict[str, tuple[float, Path]] = {}
    if projects_dir.is_dir():
        for path in projects_dir.glob("*/*.jsonl"):
            mtime = _stat_mtime(path, stats)
            if mtime is not None and mtime >= cutoff_ts:
                sid = path.stem
                prev = file_by_session.get(sid)
                if prev is None or mtime > prev[0]:
                    file_by_session[sid] = (mtime, path)

    history_by_session: dict[str, list[tuple[float, str]]] = {}
    history_latest_ts: dict[str, float] = {}
    human_history_sids: set[str] = set()
    if history_path.is_file():
        hist_mtime = _stat_mtime(history_path, stats)
        if hist_mtime is not None and hist_mtime >= cutoff_ts:
            hist_lines, history_truncated = _read_tail_jsonl(
                history_path, stats, max_lines=MAX_HISTORY_LINES
            )
            if history_truncated:
                stats["truncated_files"] += 1
            for line in hist_lines:
                text = str(line.get("display") or "").strip()
                if not text:
                    continue
                sid = str(line.get("sessionId") or "").strip() or "claude-history"
                ts = _parse_optional_timestamp(line.get("timestamp"))
                if ts is None:
                    if sid not in file_by_session and not _is_machine_message(text):
                        stats["undated_messages"] += 1
                    continue
                if ts < cutoff_ts:
                    continue
                history_by_session.setdefault(sid, []).append((ts, text))
                if ts > history_latest_ts.get(sid, 0.0):
                    history_latest_ts[sid] = ts
                if not _is_machine_message(text):
                    human_history_sids.add(sid)

    all_session_ids: dict[str, float] = {
        sid: info[0] for sid, info in file_by_session.items()
    }
    for sid, latest in history_latest_ts.items():
        if latest > all_session_ids.get(sid, 0.0):
            all_session_ids[sid] = latest

    ordered_sessions = sorted(
        all_session_ids.items(),
        key=lambda item: (
            1 if (item[0] in file_by_session or item[0] in human_history_sids) else 0,
            item[1],
        ),
        reverse=True,
    )
    selected_sessions = [sid for sid, _ in ordered_sessions[:max_sessions]]
    stats["discovered_files"] = len(all_session_ids)
    stats["sampled_files"] = len(selected_sessions)

    records: list[dict[str, Any]] = []
    for sid in selected_sessions:
        session_records: list[dict[str, Any]] = []
        file_truncated = False
        if sid in file_by_session:
            _, path = file_by_session[sid]
            session_records, file_truncated = _parse_claude_session_file(
                path, sid, cutoff_ts, stats, max_lines=max_lines
            )
            records.extend(session_records)
        if sid in history_by_session and not file_truncated:
            records.extend(
                _reconcile_history_records(
                    "claude",
                    sid,
                    history_by_session[sid],
                    session_records,
                    sid in file_by_session,
                    cutoff_ts,
                    stats,
                )
            )

    stats["human_messages"] = len(records)
    return _finalize_source_stats(stats, root_exists), records


def _parse_claude_session_file(
    path: Path,
    session_id: str,
    cutoff_ts: float,
    stats: dict[str, Any],
    *,
    max_lines: int,
) -> tuple[list[dict[str, Any]], bool]:
    lines, truncated = _read_tail_jsonl(path, stats, max_lines=max_lines)
    if truncated:
        stats["truncated_files"] += 1
    session_turn_idx = 0
    pending_tag: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    for line in lines:
        msg_type = line.get("type")
        if msg_type == "assistant":
            text = _extract_claude_content(line.get("message"))
            if text:
                pending_tag = _extract_autopilot_tag(text)
            continue
        if msg_type != "user" or line.get("isMeta"):
            continue
        text = _extract_claude_content(line.get("message"))
        if not text:
            continue
        if _is_machine_message(text):
            stats["filtered_machine_messages"] += 1
            pending_tag = None
            continue
        role = "unknown" if truncated else ("entry" if session_turn_idx == 0 else "followup")
        session_turn_idx += 1
        matched_scene = _match_pending_tag(text, pending_tag)
        pending_tag = None
        ts = _parse_optional_timestamp(line.get("timestamp"))
        if ts is None:
            stats["undated_messages"] += 1
            continue
        if ts < cutoff_ts:
            continue
        records.append({
            "agent": "claude",
            "session_id": session_id,
            "turn_role": role,
            "timestamp": ts,
            "text": text,
            "tag_provenance": matched_scene,
        })
    return records, truncated


def _collect_codex(
    home_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
    max_lines: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stats = _new_source_stats()
    codex_dir = home_dir / ".codex"
    sessions_dir = codex_dir / "sessions"
    history_path = codex_dir / "history.jsonl"
    root_exists = codex_dir.is_dir()

    file_by_session: dict[str, tuple[float, Path]] = {}
    if sessions_dir.is_dir():
        for path in sessions_dir.glob("*/*/*/*.jsonl"):
            mtime = _stat_mtime(path, stats)
            if mtime is not None and mtime >= cutoff_ts:
                sid = _canonical_codex_session_id(path.stem)
                prev = file_by_session.get(sid)
                if prev is None or mtime > prev[0]:
                    file_by_session[sid] = (mtime, path)

    history_by_session: dict[str, list[tuple[float, str]]] = {}
    history_latest_ts: dict[str, float] = {}
    human_history_sids: set[str] = set()
    if history_path.is_file():
        hist_mtime = _stat_mtime(history_path, stats)
        if hist_mtime is not None and hist_mtime >= cutoff_ts:
            hist_lines, hist_truncated = _read_tail_jsonl(
                history_path, stats, max_lines=MAX_HISTORY_LINES
            )
            if hist_truncated:
                stats["truncated_files"] += 1
            for line in hist_lines:
                text = str(line.get("text") or "").strip()
                if not text:
                    continue
                sid = _canonical_codex_session_id(str(line.get("session_id") or "").strip() or "codex-history")
                ts = _parse_optional_timestamp(line.get("ts"))
                if ts is None:
                    if sid not in file_by_session and not _is_machine_message(text):
                        stats["undated_messages"] += 1
                    continue
                if ts < cutoff_ts:
                    continue
                history_by_session.setdefault(sid, []).append((ts, text))
                if ts > history_latest_ts.get(sid, 0.0):
                    history_latest_ts[sid] = ts
                if not _is_machine_message(text):
                    human_history_sids.add(sid)

    all_session_ids: dict[str, float] = {
        sid: info[0] for sid, info in file_by_session.items()
    }
    for sid, latest in history_latest_ts.items():
        if latest > all_session_ids.get(sid, 0.0):
            all_session_ids[sid] = latest

    ordered_sessions = sorted(
        all_session_ids.items(),
        key=lambda item: (
            1 if (item[0] in file_by_session or item[0] in human_history_sids) else 0,
            item[1],
        ),
        reverse=True,
    )
    selected_sessions = [sid for sid, _ in ordered_sessions[:max_sessions]]
    stats["discovered_files"] = len(all_session_ids)
    stats["sampled_files"] = len(selected_sessions)

    records: list[dict[str, Any]] = []
    for sid in selected_sessions:
        session_records: list[dict[str, Any]] = []
        file_truncated = False
        if sid in file_by_session:
            _, path = file_by_session[sid]
            session_records, file_truncated = _parse_codex_session_file(
                path, sid, cutoff_ts, stats, max_lines=max_lines
            )
            records.extend(session_records)
        if sid in history_by_session and not file_truncated:
            records.extend(
                _reconcile_history_records(
                    "codex",
                    sid,
                    history_by_session[sid],
                    session_records,
                    sid in file_by_session,
                    cutoff_ts,
                    stats,
                )
            )

    stats["human_messages"] = len(records)
    return _finalize_source_stats(stats, root_exists), records


def _parse_codex_session_file(
    path: Path,
    session_id: str,
    cutoff_ts: float,
    stats: dict[str, Any],
    *,
    max_lines: int,
) -> tuple[list[dict[str, Any]], bool]:
    lines, truncated = _read_tail_jsonl(path, stats, max_lines=max_lines)
    if truncated:
        stats["truncated_files"] += 1
    session_turn_idx = 0
    pending_tag: dict[str, Any] | None = None
    last_user_mirror: tuple[str, str] | None = None
    records: list[dict[str, Any]] = []

    for line in lines:
        item_type = line.get("type")
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        if item_type == "response_item" and payload.get("role") == "assistant":
            last_user_mirror = None
            assistant_text = _extract_codex_text_blocks(payload.get("content"))
            if assistant_text:
                pending_tag = _extract_autopilot_tag(assistant_text)
            continue
        if item_type == "event_msg" and payload.get("type") == "agent_message":
            last_user_mirror = None
            msg_text = str(payload.get("message") or "").strip()
            if msg_text:
                pending_tag = _extract_autopilot_tag(msg_text)
            continue

        is_event_user = item_type == "event_msg" and payload.get("type") == "user_message"
        is_resp_user = item_type == "response_item" and payload.get("role") == "user"
        if not (is_event_user or is_resp_user):
            continue

        kind = "event" if is_event_user else "response"
        text = (
            str(payload.get("message") or "").strip()
            if is_event_user
            else _extract_codex_text_blocks(payload.get("content"))
        )
        if not text:
            continue
        if last_user_mirror is not None and last_user_mirror[0] != kind and last_user_mirror[1] == text:
            last_user_mirror = None
            continue
        last_user_mirror = (kind, text)

        if _is_machine_message(text):
            stats["filtered_machine_messages"] += 1
            pending_tag = None
            continue
        role = "unknown" if truncated else ("entry" if session_turn_idx == 0 else "followup")
        session_turn_idx += 1
        matched_scene = _match_pending_tag(text, pending_tag)
        pending_tag = None
        ts = _parse_optional_timestamp(line.get("timestamp"))
        if ts is None:
            stats["undated_messages"] += 1
            continue
        if ts < cutoff_ts:
            continue
        records.append({
            "agent": "codex",
            "session_id": session_id,
            "turn_role": role,
            "timestamp": ts,
            "text": text,
            "tag_provenance": matched_scene,
        })
    return records, truncated


def _collect_cursor(
    home_dir: Path,
    cutoff_ts: float,
    max_sessions: int,
    max_lines: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stats = _new_source_stats()
    cursor_projects = home_dir / ".cursor/projects"
    root_exists = cursor_projects.is_dir()
    candidates: list[tuple[float, Path]] = []
    if root_exists:
        for path in cursor_projects.glob("*/agent-transcripts/*.jsonl"):
            mtime = _stat_mtime(path, stats)
            if mtime is not None and mtime >= cutoff_ts:
                candidates.append((mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    sampled = candidates[:max_sessions]
    stats["discovered_files"] = len(candidates)
    stats["sampled_files"] = len(sampled)

    records: list[dict[str, Any]] = []
    for _, path in sampled:
        lines, truncated = _read_tail_jsonl(path, stats, max_lines=max_lines)
        if truncated:
            stats["truncated_files"] += 1
        session_turn_idx = 0
        pending_tag: dict[str, Any] | None = None
        for line in lines:
            role = line.get("role") or line.get("type")
            text = str(line.get("content") or line.get("text") or "").strip()
            if role in ("assistant", "MODEL", "PLANNER_RESPONSE"):
                if text:
                    pending_tag = _extract_autopilot_tag(text)
                continue
            if role not in ("user", "USER_INPUT") or not text:
                continue
            if _is_machine_message(text):
                stats["filtered_machine_messages"] += 1
                pending_tag = None
                continue
            turn_role = "unknown" if truncated else ("entry" if session_turn_idx == 0 else "followup")
            session_turn_idx += 1
            matched_scene = _match_pending_tag(text, pending_tag)
            pending_tag = None
            ts = _parse_optional_timestamp(
                line.get("timestamp") or line.get("created_at") or line.get("ts")
            )
            if ts is None:
                stats["undated_messages"] += 1
                continue
            if ts < cutoff_ts:
                continue
            records.append({
                "agent": "cursor",
                "session_id": path.stem,
                "turn_role": turn_role,
                "timestamp": ts,
                "text": text,
                "tag_provenance": matched_scene,
            })

    stats["human_messages"] = len(records)
    return _finalize_source_stats(stats, root_exists), records


def _timestamps_equivalent(file_ts: float, history_ts: float) -> bool:
    """Confirm mirror equivalence across millisecond/ISO session logs and integer-second history logs."""
    return int(file_ts) == int(history_ts) or abs(file_ts - history_ts) < 1.0


def _reconcile_history_records(
    agent: str,
    session_id: str,
    history_entries: list[tuple[float, str]],
    session_records: list[dict[str, Any]],
    has_session_file: bool,
    cutoff_ts: float,
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    unmatched_session_turns: list[tuple[float, str]] = [
        (float(item["timestamp"]), str(item["text"])) for item in session_records
    ]
    extra_records: list[dict[str, Any]] = []
    for ts, text in history_entries:
        if _is_machine_message(text):
            if not has_session_file:
                stats["filtered_machine_messages"] += 1
            continue
        if ts < cutoff_ts:
            continue
        matched_idx = -1
        best_delta: float | None = None
        for idx, (file_ts, file_text) in enumerate(unmatched_session_turns):
            if file_text != text or not _timestamps_equivalent(file_ts, ts):
                continue
            delta = abs(file_ts - ts)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                matched_idx = idx
        if matched_idx >= 0:
            unmatched_session_turns.pop(matched_idx)
            continue
        extra_records.append({
            "agent": agent,
            "session_id": session_id,
            "turn_role": "unknown",
            "timestamp": ts,
            "text": text,
            "tag_provenance": None,
        })
    return extra_records


def _read_tail_jsonl(
    path: Path,
    stats: dict[str, Any],
    *,
    max_lines: int,
    max_bytes: int = MAX_BYTES_PER_FILE,
) -> tuple[list[dict[str, Any]], bool]:
    ring: deque[dict[str, Any]] = deque(maxlen=max_lines)
    valid_count = 0
    byte_truncated = False
    try:
        file_size = path.stat().st_size
        with path.open("rb") as raw_handle:
            if file_size > max_bytes:
                byte_truncated = True
                raw_handle.seek(file_size - max_bytes)
                raw_handle.readline()
            for line_no, raw_bytes in enumerate(raw_handle, start=1):
                stripped = raw_bytes.decode("utf-8", errors="replace").strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except ValueError as exc:
                    stats["malformed_lines"] += 1
                    if len(stats["errors"]) < MAX_ERRORS_PER_SOURCE:
                        stats["errors"].append(f"{path}:{line_no}: invalid JSON ({exc})")
                    continue
                if not isinstance(value, dict):
                    stats["malformed_lines"] += 1
                    if len(stats["errors"]) < MAX_ERRORS_PER_SOURCE:
                        stats["errors"].append(f"{path}:{line_no}: JSON line is not an object")
                    continue
                valid_count += 1
                ring.append(value)
    except OSError as exc:
        _record_error(stats, f"{path}: {exc}")
        return [], False
    return list(ring), byte_truncated or (valid_count > max_lines)


def _canonical_codex_session_id(raw_stem: str) -> str:
    cleaned = raw_stem.strip()
    if len(cleaned) >= 36:
        tail = cleaned[-36:]
        if _is_uuid_or_hex_sha(tail) and tail.count("-") == 4:
            return tail
    return cleaned


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


def _extract_codex_text_blocks(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    all_parts: list[str] = []
    human_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in ("input_text", "output_text", "text"):
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        all_parts.append(text)
        if not _is_machine_message(text):
            human_parts.append(text)
    selected = human_parts if human_parts else all_parts
    return "\n".join(selected).strip()


def _extract_autopilot_tag(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.endswith(AUTOPILOT_COMMENT_CLOSE):
        return None
    search_end = len(stripped) - len(AUTOPILOT_COMMENT_CLOSE)
    while search_end >= 0:
        open_idx = stripped.rfind(AUTOPILOT_COMMENT_OPEN, 0, search_end)
        if open_idx < 0:
            return None
        inner = stripped[open_idx + len(AUTOPILOT_COMMENT_OPEN):-len(AUTOPILOT_COMMENT_CLOSE)].strip()
        if inner.lower().startswith("autopilot:"):
            payload = inner[len("autopilot:"):].strip()
            if payload.startswith("{") and payload.endswith("}"):
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    return None
                if not isinstance(parsed, dict):
                    return None
                scene = str(parsed.get("scene") or "").strip()
                if not scene or scene == "NONE":
                    return None
                return parsed
            return None
        search_end = open_idx
    return None


def _match_pending_tag(user_text: str, pending_tag: dict[str, Any] | None) -> str | None:
    if not isinstance(pending_tag, dict):
        return None
    scene = str(pending_tag.get("scene") or "").strip()
    if not scene or scene == "NONE":
        return None
    stripped = user_text.strip()
    for key in ("1", "2", "3"):
        candidate = str(pending_tag.get(key) or "").strip()
        if candidate and (
            stripped == candidate
            or stripped.startswith(candidate + "\n")
            or stripped.startswith(candidate + " ")
        ):
            return f"scene:{scene}"
    return None


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


def _inspect_installed_pipeline(gemini_dir: Path) -> dict[str, Any]:
    """Inspect the installed profile and scene contracts without guessing."""
    profile_path = gemini_dir / "config/autopilot/profile.json"
    project_path = gemini_dir / "config/autopilot/project.json"
    scenes_dir = gemini_dir / "config/autopilot/scenes"
    result: dict[str, Any] = {
        "profile_status": "absent" if not profile_path.is_file() else "ok",
        "scenes_status": "absent" if not scenes_dir.is_dir() else "ok",
        "mode": None,
        "max_consecutive_auto_turns": None,
        "deliver_strategy": None,
        "enabled_scenes": [],
        "missing_stages": list(SCENE_ORDER),
        "custom_scene_files": [],
        "incomplete_scene_contracts": [],
        "guardrail_count": 0,
        "redundant_guardrails": [],
    }
    specs = DEFAULT_SPECS
    if profile_path.is_file():
        try:
            raw_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            project_raw = (
                json.loads(project_path.read_text(encoding="utf-8"))
                if project_path.is_file()
                else None
            )
            profile = validate_profile(raw_profile, project=project_raw)
        except (OSError, ProfileError, json.JSONDecodeError) as exc:
            result["profile_status"] = "error"
            result["error"] = str(exc)
        else:
            specs = project_specs(project_raw)
            autopilot = profile["autopilot"]
            scenes = profile["scenes"]
            enabled_scenes = scenes["enabled_scenes"]
            result.update({
                "mode": autopilot["mode"],
                "max_consecutive_auto_turns": autopilot["max_consecutive_auto_turns"],
                "deliver_strategy": scenes["deliver_strategy"],
                "enabled_scenes": list(enabled_scenes),
                "missing_stages": [stage for stage in SCENE_ORDER if stage not in enabled_scenes],
                "guardrail_count": len(scenes["guardrails"]),
            })
            required_guardrails = [
                spec["required_guardrail"]
                for spec in specs["scene_specs"].values()
                if spec["required_guardrail"]
            ]
            result["redundant_guardrails"] = sorted({
                guardrail for guardrail in scenes["guardrails"]
                if any(
                    guardrail in required_guardrail or required_guardrail in guardrail
                    for required_guardrail in required_guardrails
                )
            })

    stage_by_file = {
        spec["file"]: stage for stage, spec in specs["scene_specs"].items()
    }
    if scenes_dir.is_dir():
        try:
            scene_files = sorted(path for path in scenes_dir.glob("*.md") if path.is_file())
            present_files = {path.name for path in scene_files}
            result["custom_scene_files"] = sorted(
                name for name in present_files if name not in stage_by_file
            )
            incomplete: set[str] = set()
            for path in scene_files:
                meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
                stage = stage_by_file.get(path.name)
                missing_guardrail = (
                    stage is not None
                    and specs["scene_specs"][stage]["required_guardrail"]
                    and not str(meta.get("required_guardrail") or "").strip()
                )
                if (
                    not str(meta.get("description") or "").strip()
                    or not str(meta.get("fallback_prompt") or "").strip()
                    or not body.strip()
                    or missing_guardrail
                ):
                    incomplete.add(path.name)
            if result["profile_status"] == "ok":
                for stage in result["enabled_scenes"]:
                    scene_file = specs["scene_specs"][stage]["file"]
                    if scene_file not in present_files:
                        incomplete.add(scene_file)
            result["incomplete_scene_contracts"] = sorted(incomplete)
        except (OSError, ValueError) as exc:
            result["scenes_status"] = "error"
            result.setdefault("error", str(exc))
    return result


def _analyze_pipeline_flow(
    all_records: list[dict[str, Any]],
    manual_records: list[dict[str, Any]],
    installed_pipeline: dict[str, Any],
) -> dict[str, Any]:
    stage_activity = {
        stage: {"autopilot_turns": 0, "manual_turns": 0, "total_turns": 0}
        for stage in SCENE_ORDER
    }
    session_turns: dict[tuple[str, str], list[tuple[float, str, bool]]] = {}
    for item in all_records:
        tagged = item.get("tag_provenance") or ""
        stage = tagged[6:] if tagged.startswith("scene:") and tagged[6:] in SCENE_ORDER else ""
        is_auto = bool(stage)
        if not stage:
            category, _ = _classify_pattern(
                _normalize_signature(item.get("text", "")), item.get("turn_role", "unknown"), 1
            )
            stage = {
                "plan_and_dispatch": "plan-and-orchestrate",
                "architecture_review": "architecture-re-review",
                "deliver_and_verify": "deliver-and-verify",
                "closeout_and_sync": "closeout-and-sync",
                "continue_nudge": "continue_nudge",
            }.get(category, "")
        if stage in stage_activity:
            key = "autopilot_turns" if is_auto else "manual_turns"
            stage_activity[stage][key] += 1
            stage_activity[stage]["total_turns"] += 1
        session_key = (str(item.get("agent") or ""), str(item.get("session_id") or ""))
        session_turns.setdefault(session_key, []).append((float(item.get("timestamp") or 0), stage, item.get("turn_role") == "unknown"))

    multi_turn_sessions = sum(len(turns) >= 2 for turns in session_turns.values())
    sessions_with_delivery = 0
    sessions_with_delivery_without_review = 0
    sessions_with_delivery_without_closeout = 0
    sessions_with_manual_nudges = 0
    for turns in session_turns.values():
        ordered = sorted(turns)
        truncated = any(turn[2] for turn in ordered)
        stages = [turn[1] for turn in ordered]
        if "continue_nudge" in stages:
            sessions_with_manual_nudges += 1
        if truncated or "deliver-and-verify" not in stages:
            continue
        sessions_with_delivery += 1
        delivery_index = stages.index("deliver-and-verify")
        if delivery_index > 0 and "architecture-re-review" not in stages[: delivery_index + 1]:
            sessions_with_delivery_without_review += 1
        if len(stages) >= 2 and "closeout-and-sync" not in stages[delivery_index:]:
            sessions_with_delivery_without_closeout += 1
    e2e_manual_turns = sum(
        "e2e" in str(item.get("text") or "").lower() or "端到端" in str(item.get("text") or "").lower()
        for item in manual_records
    )
    flow_metrics = {
        "multi_turn_sessions": multi_turn_sessions,
        "sessions_with_delivery": sessions_with_delivery,
        "sessions_with_delivery_without_review": sessions_with_delivery_without_review,
        "sessions_with_delivery_without_closeout": sessions_with_delivery_without_closeout,
        "sessions_with_manual_nudges": sessions_with_manual_nudges,
        "e2e_manual_turns": e2e_manual_turns,
    }
    findings: list[dict[str, Any]] = []
    def add(code: str, stage: str, target: str, evidence_count: int, action: str) -> None:
        contract = PIPELINE_STAGE_CONTRACTS[stage]
        findings.append({"code": code, "stage": stage, "target": target, "evidence_count": evidence_count,
                         "deliverable": contract["deliverable"], "quality_bar": contract["quality_bar"], "action": action})
    if (installed_pipeline.get("profile_status") == "ok" and installed_pipeline.get("mode") != "off"
            and installed_pipeline.get("enabled_scenes") and installed_pipeline.get("missing_stages")):
        add("missing_pipeline_stages", installed_pipeline["missing_stages"][0], "scenes.enabled_scenes",
            len(installed_pipeline["missing_stages"]), "Enable every canonical pipeline stage so transitions remain observable and complete.")
    over_count = len(installed_pipeline.get("custom_scene_files", [])) + len(installed_pipeline.get("redundant_guardrails", []))
    if over_count or installed_pipeline.get("guardrail_count", 0) > 4:
        add("over_engineered_pipeline", "plan-and-orchestrate", "scenes", max(over_count, installed_pipeline.get("guardrail_count", 0)),
            "Remove custom duplication and retain only guardrails with an independent safety or quality purpose.")
    if installed_pipeline.get("incomplete_scene_contracts"):
        incomplete_stage = next(
            (
                stage
                for stage in SCENE_ORDER
                if DEFAULT_SPECS["scene_specs"][stage]["file"]
                in installed_pipeline["incomplete_scene_contracts"]
            ),
            "plan-and-orchestrate",
        )
        add("incomplete_stage_contract", incomplete_stage, "scenes", len(installed_pipeline["incomplete_scene_contracts"]),
            "Complete each enabled scene description, fallback prompt, and required guardrail before relying on it.")
    if sessions_with_delivery_without_review:
        add("review_gate_bypassed", "architecture-re-review", "scenes.architecture-re-review", sessions_with_delivery_without_review,
            "Route delivery only after the full task receives an architecture re-review.")
    if sessions_with_delivery_without_closeout:
        add("closeout_stage_dropped", "closeout-and-sync", "scenes.closeout-and-sync", sessions_with_delivery_without_closeout,
            "Keep closeout and synchronization after delivery until all obligations and resources are settled.")
    if installed_pipeline.get("deliver_strategy") in ("merge_dev_only", "local_verify_only") and e2e_manual_turns >= 2:
        add("deliver_strategy_mismatch", "deliver-and-verify", "scenes.deliver_strategy", e2e_manual_turns,
            "Align the delivery strategy with repeated end-to-end verification demand.")
    if sessions_with_manual_nudges >= 2 or (installed_pipeline.get("profile_status") == "ok" and installed_pipeline.get("mode") == "gate"
            and (installed_pipeline.get("max_consecutive_auto_turns") or 0) < 10 and multi_turn_sessions >= 2 and sessions_with_manual_nudges >= 1):
        add("manual_pipeline_handoff", "plan-and-orchestrate", "autopilot.max_consecutive_auto_turns", sessions_with_manual_nudges,
            "Raise the automatic turn budget only after confirming the four stage contracts can advance without manual handoffs.")
    return {"stage_contracts": copy.deepcopy(PIPELINE_STAGE_CONTRACTS), "installed_pipeline": installed_pipeline,
            "stage_activity": stage_activity, "flow_metrics": flow_metrics, "findings": findings}


def _load_installed_shortcuts(gemini_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config_path = gemini_dir / "config/autopilot/config.json"
    if not config_path.is_file():
        return {"status": "absent", "path": str(config_path)}, []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "error", "path": str(config_path), "error": str(exc)}, []
    if not isinstance(config, dict):
        return {
            "status": "error",
            "path": str(config_path),
            "error": "config.json root must be a JSON object",
        }, []

    shortcuts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for section, label_prefix, is_entry in (
        ("entry_shortcuts", "entry:", True),
        ("comma_commands", "comma:,", False),
    ):
        if section not in config:
            continue
        section_obj = config.get(section)
        if not isinstance(section_obj, dict):
            return {
                "status": "error",
                "path": str(config_path),
                "error": f"config.json {section} must be a JSON object",
            }, []
        for key, spec in section_obj.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(spec, dict):
                return {
                    "status": "error",
                    "path": str(config_path),
                    "error": f"config.json {section}[{key!r}] must be a JSON object",
                }, []
            raw_canonical = spec.get("canonical_key", key)
            raw_command_id = spec.get("command_id", raw_canonical)
            raw_tmpl = spec.get("template", "")
            raw_requires_arg = spec.get("requires_arg", False)
            raw_aliases = spec.get("aliases", [])
            if (
                not isinstance(raw_canonical, str)
                or not raw_canonical.strip()
                or not isinstance(raw_command_id, str)
                or not raw_command_id.strip()
                or not isinstance(raw_tmpl, str)
                or not isinstance(raw_requires_arg, bool)
                or not isinstance(raw_aliases, list)
                or any(not isinstance(a, str) or not a.strip() for a in raw_aliases)
            ):
                return {
                    "status": "error",
                    "path": str(config_path),
                    "error": f"config.json {section}[{key!r}] has malformed fields",
                }, []
            canonical = raw_canonical.strip()
            command_id = raw_command_id.strip()
            if command_id in seen_ids:
                continue
            seen_ids.add(command_id)
            tmpl = raw_tmpl.strip()
            aliases = [a.strip().lower() for a in raw_aliases]
            if is_entry:
                aliases.append(canonical.lower())
                aliases.append(f",{canonical.lower()}")
            shortcuts.append({
                "label": f"{label_prefix}{canonical}",
                "canonical_key": canonical,
                "is_entry": is_entry,
                "requires_arg": raw_requires_arg,
                "aliases": sorted(set(aliases)),
                "template": tmpl,
            })
    return {"status": "ok", "path": str(config_path), "shortcuts_loaded": len(shortcuts)}, shortcuts


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
    raw = text[1:] if comma and text.startswith(",") else text
    if comma and (not text.startswith(",") or not raw or raw[0].isspace()):
        return "", ""
    key, arg = _command_parts(raw)
    checked = key.lower() if comma else key
    if not checked or not all(ch in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in checked):
        return "", ""
    if not comma and checked[0] not in "abcdefghijklmnopqrstuvwxyz":
        return "", ""
    return ("," + checked if comma else checked), arg


def _match_installed_autopilot(text: str, shortcuts: list[dict[str, Any]]) -> str | None:
    stripped = text.strip()
    if _named_command(
        stripped, {"worker", "agent"}, {"codex", "claude", "claude-code", "cc", "status"}, "status"
    ):
        return "command:,worker"
    if _named_command(stripped, {"mode"}, {"gate", "confirm", "off", "status"}):
        return "command:,mode"
    if _named_command(stripped, {"autopilot"}, {"recover"}):
        return "command:,autopilot"
    candidate, _ = _command_parts(stripped, comma=True)
    if candidate in {"1", "2", "3"}:
        return f"candidate:,{candidate}"
    alias, comma_arg = _shortcut_parts(stripped, comma=True)
    if alias:
        for item in shortcuts:
            if alias in item["aliases"]:
                if item["requires_arg"] and not comma_arg:
                    continue
                return item["label"]
    key, bare_arg = _shortcut_parts(stripped, comma=False)
    if key:
        for item in shortcuts:
            if item["is_entry"] and key == item["canonical_key"]:
                if item["requires_arg"] and not bare_arg:
                    continue
                return item["label"]
    return None


def _match_template_similarity(text: str, shortcuts: list[dict[str, Any]]) -> str | None:
    stripped = text.strip()
    for item in shortcuts:
        tmpl = item["template"]
        if tmpl and _matches_template(stripped, tmpl):
            return item["label"]
    return None


def _matches_template(text: str, template: str) -> bool:
    if "{arg}" not in template:
        return text == template
    raw_parts = template.split("{arg}")
    parts = [part.strip() for part in raw_parts if part.strip()]
    if not parts:
        return False
    must_anchor_start = not template.lstrip().startswith("{arg}")
    must_anchor_end = not template.rstrip().endswith("{arg}")
    if must_anchor_start and not text.startswith(parts[0]):
        return False
    if must_anchor_end and not text.endswith(parts[-1]):
        return False
    pos = 0
    for part in parts:
        found = text.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return True


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


def _resolve_turn_role(entry_count: int, followup_count: int, unknown_count: int) -> str:
    if entry_count > 0 and followup_count == 0 and unknown_count == 0:
        return "entry"
    if followup_count > 0 and entry_count == 0 and unknown_count == 0:
        return "followup"
    if unknown_count > 0 and entry_count == 0 and followup_count == 0:
        return "unknown"
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
        target = "shortcuts.custom_entry" if turn_role == "entry" else "scenes.plan-and-orchestrate"
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
    config_status: dict[str, Any],
    evidence_complete: bool,
    pipeline_assessment: dict[str, Any],
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

    for finding in pipeline_assessment.get("findings", []):
        recommendations.append({
            "category": "pipeline_optimization",
            "target": finding["target"],
            "evidence_count": finding["evidence_count"],
            "pattern_signature": finding["code"],
            "action": finding["action"],
        })

    if (
        evidence_complete
        and config_status.get("status") == "absent"
        and pipeline_assessment.get("installed_pipeline", {}).get("profile_status") != "error"
        and pipeline_assessment.get("installed_pipeline", {}).get("scenes_status") != "error"
        and not autopilot_counts
        and manual_records
    ):
        recommendations.insert(0, {
            "category": "onboarding",
            "target": "preset.recommended",
            "evidence_count": len(manual_records),
            "pattern_signature": "(no installed config.json or autopilot tags detected)",
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


def _stat_mtime(path: Path, stats: dict[str, Any]) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError as exc:
        _record_error(stats, f"{path}: {exc}")
        return None


def _parse_optional_timestamp(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
        if val <= 0:
            return None
        return val / 1000.0 if val > 1e11 else val
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None
