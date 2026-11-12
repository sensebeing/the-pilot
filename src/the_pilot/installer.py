#!/usr/bin/env python3
"""Install, inspect, upgrade and remove the Orca Autopilot profile.

The whole install is compiled in memory first, then every file is replaced
atomically in the same directory. There is no install manifest and no pending
state: a re-run simply recompiles and writes again, while files that belong to
the teammate (other hooks, other GEMINI.md sections, and custom scene files)
are never touched.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from . import compiler as profile_compiler
from .distill import DEFAULT_DAYS, DEFAULT_MAX_SESSIONS, distill_chat_history
from .profile import resolve_profile, validate_profile
from .project import ProfileError, normalize_project, project_specs
from .defaults import DEFAULT_SPECS, MODES, DELIVER_STRATEGIES


AUTOPILOT_SUBDIR = Path("config") / "autopilot"
GEMINI_MD_SUBPATH = Path("config") / "GEMINI.md"
HOOKS_SUBPATH = Path("config") / "hooks.json"
SCENES_SUBDIR = "scenes"
ENGINE_NAME = "autopilot.py"
BUILTIN_SCENE_FILES = tuple(
    spec["file"] for spec in DEFAULT_SPECS["scene_specs"].values()
)

SCRIPTS_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SCRIPTS_DIR / "assets"
DEFAULT_PROFILE_PATH = ASSETS_DIR / "profile.default.json"
ENGINE_SOURCE_PATH = ASSETS_DIR / ENGINE_NAME


class SetupError(Exception):
    """The requested install cannot be completed safely."""


class _OverlayHistoryUnknown(Exception):
    """Git history was inconclusive, so overlay synchronization must not write."""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "probe":
            result = probe(args)
        elif args.command == "distill":
            result = distill(args)
        elif args.command == "apply":
            result = apply(args)
        elif args.command == "status":
            result = status(args)
        elif args.command == "self-update":
            result = self_update(args)
        else:
            result = uninstall(args)
    except (SetupError, profile_compiler.ProfileError, OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    else:
        print_human(args.command, result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pilot",
        description="Install, inspect, upgrade or remove Orca Autopilot in ~/.gemini.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe_parser = subparsers.add_parser("probe", help="只读探测本机环境与已有安装")
    add_common_arguments(probe_parser)

    distill_parser = subparsers.add_parser(
        "distill",
        help="只读扫描近期多 Agent 聊天记录，提炼重复输入模式与场景自动化建议",
    )
    add_common_arguments(distill_parser)
    distill_parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"扫描最近 N 天的聊天记录（默认 {DEFAULT_DAYS}）",
    )
    distill_parser.add_argument(
        "--max-sessions",
        type=int,
        default=DEFAULT_MAX_SESSIONS,
        help=f"每个 Agent 产品最多抽样的最近会话数（默认 {DEFAULT_MAX_SESSIONS}）",
    )
    distill_parser.add_argument(
        "--home-dir",
        default=str(Path.home()),
        help="用户主目录（用于定位 ~/.claude、~/.codex、~/.cursor 与 ~/.gemini）",
    )

    apply_parser = subparsers.add_parser("apply", help="编译并原子写入配置")
    add_common_arguments(apply_parser)
    apply_parser.add_argument("--preset", choices=sorted(DEFAULT_SPECS["presets"]))
    apply_parser.add_argument("--profile", help="JSON 对象、JSON 文件路径或 @文件路径")
    apply_parser.add_argument("--upgrade", action="store_true", help="以已安装 profile.json 为基线重编译")
    apply_parser.add_argument("--mode", choices=MODES)
    apply_parser.add_argument("--max-turns", type=int)
    apply_parser.add_argument("--worker-agents")
    apply_parser.add_argument("--review-agents")
    apply_parser.add_argument("--scenes")
    apply_parser.add_argument("--deliver-strategy", choices=DELIVER_STRATEGIES)
    apply_parser.add_argument(
        "--feedback-email", action=argparse.BooleanOptionalAction, default=None
    )
    apply_parser.add_argument("--entry-shortcuts")
    apply_parser.add_argument("--guardrail", action="append")

    status_parser = subparsers.add_parser("status", help="只读检查安装与漂移")
    add_common_arguments(status_parser)

    update_parser = subparsers.add_parser("self-update", help="更新 CLI、技能链接与已安装运行时")
    add_common_arguments(update_parser)
    update_parser.add_argument("--checkout-dir", help="本地 the-pilot checkout（测试或开发覆盖）")
    update_parser.add_argument("--home-dir", default=str(Path.home()), help="技能链接根目录")
    update_parser.add_argument("--workspace", help="workspace directory for project overlay synchronization")
    update_parser.add_argument("--project-only", action="store_true", help="仅同步 workspace project overlay")

    uninstall_parser = subparsers.add_parser("uninstall", help="移除本工具写入的文件与 Hook 组")
    add_common_arguments(uninstall_parser)
    return parser


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gemini-dir",
        default=os.environ.get("ORCA_AUTOPILOT_GEMINI_DIR", str(Path.home() / ".gemini")),
        help="Antigravity 配置目录（默认 ~/.gemini）",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--project", help="Repository root or project JSON path")
    selection.add_argument("--no-project", action="store_true", help="Use universal defaults")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


def probe(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    project = discover_project(args, gemini_dir)
    autopilot_dir = gemini_dir / AUTOPILOT_SUBDIR
    profile_path = autopilot_dir / "profile.json"
    engine_path = autopilot_dir / ENGINE_NAME
    scenes_dir = autopilot_dir / SCENES_SUBDIR
    hooks_path = gemini_dir / HOOKS_SUBPATH
    gemini_md = gemini_dir / GEMINI_MD_SUBPATH
    hooks_group = False
    hooks_error = ""
    try:
        hooks_group = has_autopilot_hook_group(hooks_path)
    except SetupError as exc:
        hooks_error = str(exc)
    report: dict[str, Any] = {
        "gemini_dir": str(gemini_dir),
        "clis": {
            name: shutil.which(name) or ""
            for name in ("orca", "claude", "codex", "agy")
        },
        "installed": {
            "autopilot_dir": autopilot_dir.is_dir(),
            "profile": profile_path.is_file(),
            "engine": engine_path.is_file(),
            "scenes": sorted(path.name for path in scenes_dir.glob("*.md"))
            if scenes_dir.is_dir()
            else [],
            "hooks_group": hooks_group,
            "gemini_block": profile_compiler.GEMINI_BEGIN in read_text(gemini_md),
        },
        "recommended_profile": resolve_profile(
            load_default_profile(), preset="recommended", project=project
        ),
    }
    if hooks_error:
        report["hooks_error"] = hooks_error
    try:
        installed_profile = read_json_object(profile_path, "profile.json")
    except SetupError as exc:
        report["installed_profile_error"] = str(exc)
        installed_profile = None
    if installed_profile is not None:
        try:
            report["installed_profile"] = validate_profile(installed_profile, project=project)
        except profile_compiler.ProfileError as exc:
            report["installed_profile_error"] = str(exc)
    return report


def distill(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    home_dir = Path(os.path.expanduser(args.home_dir)).resolve()
    return distill_chat_history(
        home_dir,
        gemini_dir,
        days=args.days,
        max_sessions_per_agent=args.max_sessions,
    )


def apply(args: argparse.Namespace) -> dict[str, Any]:
    with profile_lock(gemini_dir_for(args)):
        return _apply_unlocked(args)


def _apply_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    project = discover_project(args, gemini_dir)
    autopilot_dir = gemini_dir / AUTOPILOT_SUBDIR
    profile_path = autopilot_dir / "profile.json"
    config_path = autopilot_dir / "config.json"
    scenes_dir = autopilot_dir / SCENES_SUBDIR
    engine_path = autopilot_dir / ENGINE_NAME
    gemini_md = gemini_dir / GEMINI_MD_SUBPATH
    hooks_path = gemini_dir / HOOKS_SUBPATH

    if args.upgrade:
        if args.profile:
            raise SetupError("--upgrade 不能与 --profile 同用；请先完成定制，再单独升级")
        base = read_json_object(profile_path, "已安装的 profile.json")
        if base is None:
            raise SetupError("--upgrade 需要已安装的 profile.json；未找到安装配置")
    else:
        base = load_default_profile()
    installed_project = None
    if args.upgrade and project is not None:
        installed_project_path = autopilot_dir / "project.json"
        if installed_project_path.is_file():
            installed_project = normalize_project(
                read_json_object(installed_project_path, "project.json")
            )
        base = _migrate_upgraded_project_shortcuts(
            base,
            installed_project,
            project,
            explicit_entry_override=args.entry_shortcuts is not None,
        )
    preset = args.preset if args.preset is not None else (None if args.upgrade else "recommended")
    profile = resolve_profile(
        base, preset=preset, overrides=overrides_from_args(args), project=project
    )

    bundle = profile_compiler.compile_bundle(profile, project=project)
    current_gemini = read_text(gemini_md)
    hooks = read_json_object(hooks_path, "hooks.json") or {}
    engine_text = ENGINE_SOURCE_PATH.read_text(encoding="utf-8")

    merged_gemini = profile_compiler.merge_gemini_md(current_gemini, bundle.gemini_block)
    merged_hooks = profile_compiler.merge_hooks_json(hooks, str(engine_path))
    stale_scenes = [
        name
        for name in BUILTIN_SCENE_FILES
        if name not in bundle.scenes and (scenes_dir / name).is_file()
    ]

    project_path = autopilot_dir / "project.json"
    if project is not None:
        atomic_write_text(project_path, json_text(project))
    elif project_path.exists():
        project_path.unlink()
    atomic_write_text(profile_path, json_text(bundle.profile_json))
    atomic_write_text(config_path, json_text(bundle.config_json))
    for filename, text in bundle.scenes.items():
        atomic_write_text(scenes_dir / filename, text)
    atomic_write_text(engine_path, engine_text, mode=0o755)
    if merged_gemini != current_gemini or not gemini_md.is_file():
        atomic_write_text(gemini_md, merged_gemini)
    if merged_hooks != hooks or not hooks_path.is_file():
        atomic_write_text(hooks_path, json_text(merged_hooks))
    for name in stale_scenes:
        (scenes_dir / name).unlink()

    return {
        "gemini_dir": str(gemini_dir),
        "preset": preset or "installed-profile",
        "mode": bundle.profile_json["autopilot"]["mode"],
        "max_consecutive_auto_turns": bundle.profile_json["autopilot"]["max_consecutive_auto_turns"],
        "deliver_strategy": bundle.profile_json["scenes"]["deliver_strategy"],
        "worker_agents": bundle.profile_json["orchestration"]["worker_agents"],
        "review_agents": bundle.profile_json["orchestration"]["review_agents"],
        "scenes": sorted(bundle.scenes),
        "removed_scenes": sorted(stale_scenes),
        "profile": str(profile_path),
        "config": str(config_path),
        "engine": str(engine_path),
        "gemini_md": str(gemini_md),
        "hooks": str(hooks_path),
        "preserved": [],
        "warnings": missing_cli_warnings(bundle.profile_json),
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    project = discover_project(args, gemini_dir)
    autopilot_dir = gemini_dir / AUTOPILOT_SUBDIR
    profile_path = autopilot_dir / "profile.json"
    config_path = autopilot_dir / "config.json"
    scenes_dir = autopilot_dir / SCENES_SUBDIR
    engine_path = autopilot_dir / ENGINE_NAME
    gemini_md = gemini_dir / GEMINI_MD_SUBPATH
    hooks_path = gemini_dir / HOOKS_SUBPATH

    profile = read_json_object(profile_path, "profile.json")
    engine_up_to_date = engine_path.is_file() and (
        engine_path.read_text(encoding="utf-8") == ENGINE_SOURCE_PATH.read_text(encoding="utf-8")
    )
    report: dict[str, Any] = {
        "gemini_dir": str(gemini_dir),
        "installed": bool(profile is not None and engine_path.is_file()),
        "profile": str(profile_path),
        "engine": {"path": str(engine_path), "up_to_date": engine_up_to_date},
        "hooks": {
            "path": str(hooks_path),
            "group": has_autopilot_hook_group(hooks_path),
        },
        "gemini_md": str(gemini_md),
        "drift": [],
    }
    state_dir = _state_dir_for(autopilot_dir)
    auto_update_path = state_dir / "auto_update.json"
    if auto_update_path.is_file():
        try:
            report["auto_update"] = json.loads(auto_update_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report["auto_update"] = {"error": str(exc)}
    if profile is None:
        report["drift"].append("profile.json 缺失")
        return report

    existing_config = read_json_object(config_path, "config.json") or {}
    bundle = profile_compiler.compile_bundle(profile, project=project)
    report.update(
        {
            "mode": bundle.profile_json["autopilot"]["mode"],
            "max_consecutive_auto_turns": bundle.profile_json["autopilot"][
                "max_consecutive_auto_turns"
            ],
            "scenes": sorted(bundle.scenes),
            "deliver_strategy": bundle.profile_json["scenes"]["deliver_strategy"],
            "preserved": [],
        }
    )
    if existing_config != bundle.config_json:
        report["drift"].append("config.json 与 profile.json 编译结果不一致")

    expected_scenes = set(bundle.scenes)
    disk_scenes = {
        path.name: path.read_text(encoding="utf-8")
        for path in scenes_dir.glob("*.md")
    } if scenes_dir.is_dir() else {}
    for filename, text in bundle.scenes.items():
        if disk_scenes.get(filename) != text:
            report["drift"].append(f"场景文件缺失或已漂移：{filename}")
    for filename in BUILTIN_SCENE_FILES:
        if filename not in expected_scenes and filename in disk_scenes:
            report["drift"].append(f"已停用的内置场景仍在：{filename}")

    current_gemini = read_text(gemini_md)
    # `status` is read-only: a GEMINI.md the installer refuses to merge is a
    # drift finding, not a reason to fail the whole inspection.
    try:
        merged = profile_compiler.merge_gemini_md(current_gemini, bundle.gemini_block)
    except ValueError as exc:
        report["drift"].append(f"GEMINI.md 托管块异常：{exc}")
    else:
        if merged != current_gemini:
            report["drift"].append("GEMINI.md 托管块缺失或已漂移")

    hooks = read_json_object(hooks_path, "hooks.json") or {}
    expected_hooks = profile_compiler.merge_hooks_json(hooks, str(engine_path))
    report["hooks"]["up_to_date"] = (
        hooks.get("orca-agy-autopilot") == expected_hooks.get("orca-agy-autopilot")
    )
    if not hooks_path.is_file():
        report["hooks"]["up_to_date"] = False
    if not report["hooks"]["up_to_date"]:
        report["drift"].append("hooks.json 的 orca-agy-autopilot 组缺失或已漂移")
    if not engine_up_to_date:
        report["drift"].append("autopilot.py 缺失或与当前技能版本不一致")
    return report


def _discover_checkout(explicit: str | None, *, home_dir: Path | None = None) -> tuple[Path | None, bool]:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not (path / ".git").exists():
            raise SetupError(f"Checkout directory is not a Git repository: {path}")
        return path, True
    if home_dir is not None and home_dir.expanduser().resolve() != Path.home().resolve():
        candidates = [home_dir.expanduser() / "the-pilot"]
    else:
        candidates = [SCRIPTS_DIR.parents[1], Path.home() / "the-pilot"]
    for path in candidates:
        if (path / ".git").exists():
            return path, False
    return None, False


def _compute_source_tree_digest(src_root: Path) -> str:
    """Hash source Python files and bundled assets deterministically."""
    digest = hashlib.sha256()
    if not src_root.is_dir():
        return digest.hexdigest()
    files = []
    for path in src_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(src_root)
        if path.suffix == ".py" or relative.parts[:1] == ("assets",):
            files.append((relative.as_posix(), path))
    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _installed_uv_package_dir(home_dir: Path) -> Path | None:
    root = home_dir / ".local/share/uv/tools/the-pilot"
    matches = sorted(root.glob("lib/python*/site-packages/the_pilot"))
    return matches[0] if matches else None


def _load_json_object(path: Path) -> dict[str, Any]:
    value = read_json_object(path, path.name)
    return value if isinstance(value, dict) else {}


def _state_dir_for(autopilot_dir: Path) -> Path:
    return Path(
        os.environ.get("ORCA_AUTOPILOT_STATE_DIR", "").strip()
        or str(autopilot_dir / "state")
    ).expanduser()


def _find_ancestor_overlay(start: Path) -> Path | None:
    return next(
        (
            parent / ".pilot/project.json"
            for parent in (start, *start.parents)
            if (parent / ".pilot/project.json").is_file()
        ),
        None,
    )


def _migrate_upgraded_project_shortcuts(
    base: dict[str, Any],
    installed_project: dict[str, Any] | None,
    project: dict[str, Any],
    *,
    explicit_entry_override: bool,
) -> dict[str, Any]:
    """Migrate user shortcut state when an installed overlay gains entries."""
    migrated = copy.deepcopy(base)
    shortcuts = migrated.get("shortcuts")
    if not isinstance(shortcuts, dict):
        return migrated
    custom_entry = shortcuts.get("custom_entry")
    effective_specs = project_specs(project)
    entry_order = effective_specs["entry_order"]
    if isinstance(custom_entry, dict):
        for key in list(custom_entry):
            if key not in entry_order:
                continue
            del custom_entry[key]
            enabled_entry = shortcuts.get("enabled_entry")
            if (
                not explicit_entry_override
                and isinstance(enabled_entry, list)
                and key not in enabled_entry
            ):
                enabled_entry.append(key)
    enabled_entry = shortcuts.get("enabled_entry")
    if installed_project is not None and not explicit_entry_override and isinstance(enabled_entry, list):
        previous_order = set(installed_project.get("entry_order", ()))
        recommended_enabled = effective_specs["presets"]["recommended"]["shortcuts"].get(
            "enabled_entry", ()
        )
        for key in recommended_enabled:
            if key in entry_order and key not in previous_order and key not in enabled_entry:
                enabled_entry.append(key)
    return migrated


def _is_older_committed_overlay(
    overlay: Path, installed_project: dict[str, Any]
) -> bool:
    """Return whether a clean worktree overlay predates installed content."""
    repo_root = overlay.parent.parent
    if not (repo_root / ".git").exists():
        return False

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return subprocess.CompletedProcess([], 1, "", "")

    status = git("status", "--porcelain", "--", ".pilot/project.json")
    if status.returncode != 0:
        raise _OverlayHistoryUnknown("无法检查 workspace overlay 工作区状态")
    if status.stdout.strip():
        return False
    overlay_commit = git("log", "-n", "1", "--format=%H", "HEAD", "--", ".pilot/project.json")
    overlay_sha = overlay_commit.stdout.strip()
    if overlay_commit.returncode != 0 or not overlay_sha:
        raise _OverlayHistoryUnknown("无法定位 workspace overlay 提交")
    commits = git("log", "--all", "-n", "50", "--format=%H", "--", ".pilot/project.json")
    if commits.returncode != 0:
        raise _OverlayHistoryUnknown("无法读取 workspace overlay Git 历史")
    commit_list = [item.strip() for item in commits.stdout.splitlines() if item.strip()]
    if len(commit_list) >= 50:
        exhaustive = git("log", "--all", "--format=%H", "--", ".pilot/project.json")
        if exhaustive.returncode != 0:
            raise _OverlayHistoryUnknown("无法完整读取 workspace overlay Git 历史")
        commit_list = [item.strip() for item in exhaustive.stdout.splitlines() if item.strip()]
    if not commit_list:
        raise _OverlayHistoryUnknown("workspace overlay 没有可验证的 Git 历史")
    seen_blobs: dict[str, dict[str, Any] | None] = {}
    for commit in commit_list:
        if not commit or commit == overlay_sha:
            continue
        blob = git("rev-parse", f"{commit}:.pilot/project.json")
        blob_sha = blob.stdout.strip()
        if blob.returncode != 0:
            raise _OverlayHistoryUnknown("无法读取 workspace overlay 提交对象")
        if not blob_sha:
            continue
        if blob_sha not in seen_blobs:
            content = git("show", blob_sha)
            if content.returncode != 0:
                raise _OverlayHistoryUnknown("无法读取 workspace overlay Git 对象")
            else:
                try:
                    parsed = json.loads(content.stdout)
                    seen_blobs[blob_sha] = normalize_project(parsed)
                except (ValueError, ProfileError):
                    seen_blobs[blob_sha] = None
        if seen_blobs[blob_sha] != installed_project:
            continue
        ancestry = git("merge-base", "--is-ancestor", overlay_sha, commit)
        if ancestry.returncode == 0:
            return True
        if ancestry.returncode != 1:
            raise _OverlayHistoryUnknown("无法验证 workspace overlay 提交关系")
    return False


def _sync_workspace_overlay(workspace_arg: str | None, gemini_dir: Path, autopilot_dir: Path) -> str:
    if not workspace_arg:
        return "none"
    installed_path = autopilot_dir / "project.json"
    overlay = _find_ancestor_overlay(Path(workspace_arg).expanduser().resolve())
    if overlay is None:
        return "none"
    with profile_lock(gemini_dir):
        if not installed_path.is_file():
            return "none"
        installed_project = normalize_project(read_json_object(installed_path, "project.json"))
        workspace_project = normalize_project(read_json_object(overlay, "project.json"))
        if workspace_project["projectId"] != installed_project["projectId"]:
            return "skipped_unmatched_project"
        if workspace_project == installed_project:
            return "up_to_date"
        try:
            older = _is_older_committed_overlay(overlay, installed_project)
        except _OverlayHistoryUnknown:
            return "skipped_unknown_worktree"
        if older:
            return "skipped_older_worktree"
        apply_args = argparse.Namespace(
            command="apply", gemini_dir=str(gemini_dir), project=str(overlay), no_project=False,
            json=False, preset=None, profile=None, upgrade=True, mode=None, max_turns=None,
            worker_agents=None, review_agents=None, scenes=None, deliver_strategy=None,
            feedback_email=None, entry_shortcuts=None, guardrail=None,
        )
        _apply_unlocked(apply_args)
    return "updated"


def self_update(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    autopilot_dir = gemini_dir / AUTOPILOT_SUBDIR
    state_dir = _state_dir_for(autopilot_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if getattr(args, "project_only", False):
        project_status = _sync_workspace_overlay(getattr(args, "workspace", None), gemini_dir, autopilot_dir)
        result = {
            "status": project_status,
            "source_status": "skipped_project_only",
            "project_status": project_status,
            "local_source_status": "none",
            "head_sha": None,
            "synced_skills": [],
            "error": None,
        }
        home_dir = Path(args.home_dir).expanduser()
        with _file_lock(state_dir / "auto_update.lock"):
            try:
                checkout, _ = _discover_checkout(getattr(args, "checkout_dir", None), home_dir=home_dir)
                if checkout is not None and (autopilot_dir / "profile.json").is_file():
                    status = subprocess_run(["git", "-C", str(checkout), "status", "--porcelain"], 2)
                    branch = subprocess_run(["git", "-C", str(checkout), "branch", "--show-current"], 2).strip()
                    if not status.strip() and branch == "main":
                        checkout_digest = _compute_source_tree_digest(checkout / "src/the_pilot")
                        existing = _load_json_object(state_dir / "auto_update.json")
                        source_engine = checkout / "src/the_pilot/assets/autopilot.py"
                        installed_engine = autopilot_dir / "autopilot.py"
                        differs = (
                            existing.get("installed_tree_digest") != checkout_digest
                            or (source_engine.is_file() and installed_engine.is_file() and source_engine.read_bytes() != installed_engine.read_bytes())
                        )
                        if differs:
                            project_path = autopilot_dir / "project.json"
                            project_args = ["--project", str(project_path)] if project_path.is_file() else ["--no-project"]
                            apply_command = [sys.executable, str(checkout / "bin/pilot"), "apply", "--upgrade", "--gemini-dir", str(gemini_dir), *project_args, "--json"]
                            subprocess_run(apply_command, 30)
                            result["local_source_status"] = "updated"
                            sha = subprocess_run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 2).strip()
                            result["head_sha"] = sha
                            uv_available = bool(shutil.which("uv"))
                            pilot_path = home_dir / ".local/bin/pilot"
                            is_real_home = home_dir.resolve() == Path.home().resolve()
                            has_installed_pilot = pilot_path.exists() or (is_real_home and bool(shutil.which("pilot")))
                            existing.update({"installed_sha": sha, "installed_tree_digest": checkout_digest})
                            atomic_write_text(state_dir / "auto_update.json", json_text(existing))
                            if uv_available and has_installed_pilot:
                                try:
                                    elapsed = time.time() - float(existing.get("last_checked_at") or 0)
                                except (TypeError, ValueError):
                                    elapsed = 30
                                if elapsed >= 30:
                                    existing["last_checked_at"] = time.time()
                                    atomic_write_text(state_dir / "auto_update.json", json_text(existing))
                                    try:
                                        subprocess.Popen([sys.executable, str(checkout / "bin/pilot"), "self-update", "--gemini-dir", str(gemini_dir), "--home-dir", str(home_dir), "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                                    except OSError as exc:
                                        result["error"] = str(exc)
                        else:
                            # No uv bundle is present; the runtime files are already current.
                            pass
            except (SetupError, OSError, subprocess.SubprocessError, ValueError) as exc:
                result["error"] = str(exc)
        return result
    existing_metadata = _load_json_object(state_dir / "auto_update.json")
    report: dict[str, Any] = {
        "last_checked_at": time.time(), "last_updated_at": None,
        "status": "failed", "source_status": "failed", "project_status": "none",
        "head_sha": None, "synced_skills": [], "error": None,
    }
    for key in ("last_updated_at", "installed_sha", "installed_tree_digest"):
        if key in existing_metadata:
            report[key] = existing_metadata[key]
    with _file_lock(state_dir / "auto_update.lock"):
        try:
            report["project_status"] = _sync_workspace_overlay(
                getattr(args, "workspace", None), gemini_dir, autopilot_dir
            )
            home_dir = Path(args.home_dir).expanduser()
            checkout, explicit = _discover_checkout(getattr(args, "checkout_dir", None), home_dir=home_dir)
            if checkout is not None:
                status = subprocess_run(["git", "-C", str(checkout), "status", "--porcelain"], 20)
                branch = subprocess_run(["git", "-C", str(checkout), "branch", "--show-current"], 20).strip()
                if status.strip():
                    report["source_status"] = "skipped_dirty_checkout"
                elif branch != "main":
                    report["source_status"] = "skipped_non_main_branch"
                else:
                    before_sha = subprocess_run(
                        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20
                    ).strip()
                    subprocess_run(["git", "-C", str(checkout), "pull", "--ff-only"], 20)
                    after_sha = subprocess_run(
                        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20
                    ).strip()
                    checkout_digest = _compute_source_tree_digest(checkout / "src/the_pilot")
                    package_dir = _installed_uv_package_dir(home_dir)
                    uv_needs_reinstall = (
                        after_sha != before_sha
                        or existing_metadata.get("installed_sha") != after_sha
                        or existing_metadata.get("installed_tree_digest") != checkout_digest
                        or (package_dir is not None and _compute_source_tree_digest(package_dir) != checkout_digest)
                    )
                    if after_sha != before_sha:
                        report["source_status"] = "updated"
                    else:
                        report["source_status"] = "up_to_date"
                    pilot_bin = home_dir / ".local/bin/pilot"
                    is_real_home = home_dir.resolve() == Path.home().resolve()
                    has_installed_pilot = pilot_bin.exists() or (is_real_home and bool(shutil.which("pilot")))
                    installed_ok = not uv_needs_reinstall
                    if uv_needs_reinstall and not explicit and shutil.which("uv") and has_installed_pilot:
                        subprocess_run(["uv", "tool", "install", "--force", str(checkout)], 30)
                        installed_ok = True
                    if installed_ok:
                        report["installed_sha"] = after_sha
                        report["installed_tree_digest"] = checkout_digest
                report["head_sha"] = subprocess_run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20).strip()
            elif not explicit and shutil.which("uv"):
                uv_out = subprocess_run(["uv", "tool", "upgrade", "the-pilot"], 30)
                report["source_status"] = (
                    "up_to_date" if "nothing to upgrade" in uv_out.lower() else "updated"
                )
            else:
                report["source_status"] = "skipped_no_source"

            if checkout is not None:
                for skill_name in ("the-pilot", "the-pilot-setup"):
                    source = checkout / "skills" / skill_name
                    if not source.exists():
                        continue
                    parents = (home_dir / ".agents/skills", home_dir / ".claude/skills", Path(args.gemini_dir).expanduser() / "config/skills")
                    for parent in parents:
                        target = parent / skill_name
                        if not parent.is_dir() or (target.exists() and not target.is_symlink()):
                            continue
                        if target.is_symlink() or not target.exists():
                            target.unlink(missing_ok=True)
                            target.symlink_to(source)
                            report["synced_skills"].append(str(target))

            profile_path = autopilot_dir / "profile.json"
            if profile_path.is_file():
                project_path = autopilot_dir / "project.json"
                apply_args = argparse.Namespace(**vars(args))
                apply_args.command = "apply"
                apply_args.upgrade = True
                apply_args.profile = None
                apply_args.preset = apply_args.mode = apply_args.max_turns = None
                apply_args.worker_agents = apply_args.review_agents = apply_args.scenes = None
                apply_args.deliver_strategy = apply_args.entry_shortcuts = None
                apply_args.feedback_email = None
                apply_args.guardrail = None
                apply_args.project = str(project_path) if project_path.is_file() else None
                apply_args.no_project = False
                if checkout is not None and (checkout / "bin" / "pilot").is_file():
                    project_args = (
                        ["--project", str(project_path)]
                        if project_path.is_file()
                        else ["--no-project"]
                    )
                    subprocess_run(
                        [
                            sys.executable,
                            str(checkout / "bin" / "pilot"),
                            "apply",
                            "--upgrade",
                            "--gemini-dir",
                            str(gemini_dir),
                            *project_args,
                            "--json",
                        ],
                        30,
                    )
                else:
                    apply(apply_args)
            report["last_updated_at"] = time.time()
            if report["source_status"] in {"updated", "up_to_date", "skipped_dirty_checkout", "skipped_non_main_branch", "skipped_no_source"}:
                report["status"] = report["source_status"]
        except Exception as exc:
            report["error"] = str(exc)
            report["status"] = "failed"
        atomic_write_text(state_dir / "auto_update.json", json_text(report))
        log_path = state_dir / "autopilot.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "auto_update_failed" if report["status"] == "failed" else "auto_update_completed", **report}, ensure_ascii=False) + "\n")
        if report["status"] == "failed":
            raise SetupError(str(report["error"]))
    return report


def subprocess_run(command: list[str], timeout: int) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout or result.stderr


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def uninstall(args: argparse.Namespace) -> dict[str, Any]:
    with profile_lock(gemini_dir_for(args)):
        return _uninstall_unlocked(args)


def _uninstall_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    autopilot_dir = gemini_dir / AUTOPILOT_SUBDIR
    profile_path = autopilot_dir / "profile.json"
    config_path = autopilot_dir / "config.json"
    scenes_dir = autopilot_dir / SCENES_SUBDIR
    engine_path = autopilot_dir / ENGINE_NAME
    gemini_md = gemini_dir / GEMINI_MD_SUBPATH
    hooks_path = gemini_dir / HOOKS_SUBPATH

    installed = profile_path.is_file() or engine_path.is_file()
    removed: list[str] = []
    hooks = read_json_object(hooks_path, "hooks.json")
    if hooks is not None and "orca-agy-autopilot" in hooks:
        atomic_write_text(hooks_path, json_text(profile_compiler.strip_hooks_json(hooks)))
        removed.append("hooks.json:orca-agy-autopilot")

    current_gemini = read_text(gemini_md)
    stripped = profile_compiler.strip_gemini_md(current_gemini)
    if stripped != current_gemini:
        if stripped:
            atomic_write_text(gemini_md, stripped)
        else:
            gemini_md.unlink()
        removed.append("GEMINI.md:managed-block")

    for path, label in (
        (engine_path, "autopilot.py"),
        (profile_path, "profile.json"),
        (config_path, "config.json"),
        (autopilot_dir / "project.json", "project.json"),
    ):
        if path.is_file():
            path.unlink()
            removed.append(label)

    preserved_scenes = []
    for path in sorted(scenes_dir.glob("*.md")) if scenes_dir.is_dir() else []:
        if path.name in BUILTIN_SCENE_FILES:
            path.unlink()
            removed.append(f"scenes/{path.name}")
        else:
            preserved_scenes.append(path.name)

    prune_empty_dir(scenes_dir)
    prune_empty_dir(autopilot_dir)
    preserved = [f"scenes/{name}" for name in preserved_scenes]
    return {
        "gemini_dir": str(gemini_dir),
        "installed": installed,
        "removed": removed,
        "preserved": preserved,
    }


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if args.profile:
        overrides = read_profile_argument(args.profile)
    if args.mode:
        overrides.setdefault("autopilot", {})["mode"] = args.mode
    if args.max_turns is not None:
        overrides.setdefault("autopilot", {})["max_consecutive_auto_turns"] = args.max_turns
    if args.worker_agents is not None:
        overrides.setdefault("orchestration", {})["worker_agents"] = split_list(args.worker_agents)
    if args.review_agents is not None:
        overrides.setdefault("orchestration", {})["review_agents"] = split_list(args.review_agents)
    if args.scenes is not None:
        overrides.setdefault("scenes", {})["enabled_scenes"] = split_list(
            args.scenes, allow_empty=True
        )
    if args.deliver_strategy:
        overrides.setdefault("scenes", {})["deliver_strategy"] = args.deliver_strategy
    if args.feedback_email is not None:
        overrides.setdefault("scenes", {})["enable_feedback_email"] = args.feedback_email
    if args.entry_shortcuts is not None:
        overrides.setdefault("shortcuts", {})["enabled_entry"] = split_list(
            args.entry_shortcuts, allow_empty=True
        )
    if args.guardrail:
        overrides.setdefault("scenes", {})["guardrails"] = list(args.guardrail)
    return overrides


def read_profile_argument(raw: str) -> dict[str, Any]:
    text = raw.strip()
    candidate: Path | None = None
    if text.startswith("@"):
        candidate = Path(text[1:]).expanduser()
    elif "\n" not in text and Path(text).expanduser().is_file():
        candidate = Path(text).expanduser()
    if candidate is not None:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise SetupError(f"无法读取 profile 文件 {candidate}: {exc}") from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise SetupError(f"--profile 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise SetupError("--profile 必须是 JSON 对象")
    return parsed


def load_default_profile() -> dict[str, Any]:
    try:
        raw = json.loads(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"无法读取默认 profile {DEFAULT_PROFILE_PATH}: {exc}") from exc
    return validate_profile(raw)


def discover_project(args: argparse.Namespace, gemini_dir: Path) -> dict[str, Any] | None:
    """Explicit choice, installed link, then nearest ancestor project."""
    if args.no_project:
        return None
    if args.project:
        path = Path(args.project).expanduser().resolve()
        if path.is_dir():
            path = path / ".pilot/project.json"
        if not path.is_file():
            raise SetupError(f"Project overlay not found: {path}")
    else:
        path = gemini_dir / AUTOPILOT_SUBDIR / "project.json"
        if not path.is_file():
            path = _find_ancestor_overlay(Path.cwd())
            if path is None:
                return None
    return normalize_project(read_json_object(path, "project.json"))


def gemini_dir_for(args: argparse.Namespace) -> Path:
    return Path(args.gemini_dir).expanduser().resolve()


@contextmanager
def profile_lock(gemini_dir: Path):
    with _file_lock(gemini_dir / AUTOPILOT_SUBDIR / ".autopilot.lock"):
        yield


def read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def read_json_object(path: Path, label: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"{label} 无法解析（{path}）：{exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"{label} 必须是 JSON 对象（{path}）")
    return value


def atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write next to the target and replace it, so readers never see a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def has_autopilot_hook_group(hooks_path: Path) -> bool:
    hooks = read_json_object(hooks_path, "hooks.json") or {}
    return "orca-agy-autopilot" in hooks


def missing_cli_warnings(profile: dict[str, Any]) -> list[str]:
    warnings = []
    agents = profile["orchestration"]["worker_agents"] + profile["orchestration"]["review_agents"]
    for name in dict.fromkeys(agents):
        if not shutil.which(name):
            warnings.append(f"未在 PATH 上找到 `{name}`；编排/审查命令需要队友自行安装")
    if not shutil.which("orca"):
        warnings.append("未在 PATH 上找到 `orca`；自动预填不会发送，请先安装 Orca CLI")
    return warnings


def split_list(raw: str, *, allow_empty: bool = False) -> list[str]:
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items and not allow_empty:
        raise SetupError("列表参数不能为空")
    return items


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def prune_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def print_human(command: str, result: dict[str, Any]) -> None:
    if command == "probe":
        print(f"gemini_dir: {result['gemini_dir']}")
        print("CLIs: " + ", ".join(
            f"{name}={'yes' if path else 'no'}" for name, path in result["clis"].items()
        ))
        installed = result["installed"]
        print(
            "已安装: "
            f"profile={installed['profile']} engine={installed['engine']} "
            f"hooks={installed['hooks_group']} gemini_block={installed['gemini_block']}"
        )
        print("推荐预设: recommended")
    elif command == "apply":
        print(f"已应用预设：{result['preset']}（mode={result['mode']}）")
        print("场景：" + ", ".join(result["scenes"]))
        if result["removed_scenes"]:
            print("已移除停用场景：" + ", ".join(result["removed_scenes"]))
        if result["preserved"]:
            print("已保留：" + ", ".join(result["preserved"]))
        for warning in result["warnings"]:
            print(f"注意：{warning}")
    elif command == "status":
        print(f"installed: {result['installed']}")
        auto_update = result.get("auto_update")
        if isinstance(auto_update, dict) and "status" in auto_update:
            print(f"auto_update: {auto_update['status']}")
        if "mode" in result:
            print(f"mode: {result['mode']}  max_turns: {result['max_consecutive_auto_turns']}")
            print("scenes: " + (", ".join(result['scenes']) or "（无）"))
        print("drift: " + ("none" if not result["drift"] else "; ".join(result["drift"])))
    elif command == "self-update":
        print(
            f"status: {result['status']}  source: {result['source_status']}  "
            f"head: {result.get('head_sha') or 'none'}"
        )
    elif command == "distill":
        totals = result["totals"]
        print(
            f"window_days: {result['window_days']}  "
            f"human_messages: {totals['human_messages']}  "
            f"manual_messages: {totals['manual_messages']}"
        )
        print("sources: " + ", ".join(
            f"{agent}(sampled={info['sampled_files']}/{info['discovered_files']}, msgs={info['human_messages']})"
            for agent, info in result["sources"].items()
        ))
        findings = result.get("pipeline_assessment", {}).get("findings", [])
        if findings:
            print("pipeline findings: " + ", ".join(
                f"{item['code']}({item['stage']})" for item in findings
            ))
        for item in result["recurring_patterns"][:10]:
            print(f"[{item['count']}x][{item['category']}] {item['signature']}")
    else:
        print("removed: " + (", ".join(result["removed"]) or "（无）"))
        if result["preserved"]:
            print("preserved: " + ", ".join(result["preserved"]))


if __name__ == "__main__":
    raise SystemExit(main())
