#!/usr/bin/env python3
"""Install, inspect, upgrade and remove The Pilot profile.

The whole install is compiled in memory first, then every file is replaced
atomically in the same directory. There is no install manifest and no pending
state: a re-run simply recompiles and writes again, while files that belong to
the teammate (other hooks, other GEMINI.md sections, and custom scene files)
are never touched.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

from . import compiler as profile_compiler
from .distill import DEFAULT_DAYS, DEFAULT_MAX_SESSIONS, distill_chat_history
from .assets.runtime import append_log_event
from .profile import resolve_profile, validate_profile
from .defaults import (
    DEFAULT_SPECS,
    DELIVER_STRATEGIES,
    ENGINE_NAME,
    HOOK_GROUP,
    LEGACY_AUTOPILOT_SUBDIR,
    LEGACY_ENGINE_NAME,
    LEGACY_HOOK_GROUP,
    LEGACY_LOCK_NAME,
    LOCK_NAME,
    MODES,
    PILOT_SUBDIR,
)


GEMINI_MD_SUBPATH = Path("config") / "GEMINI.md"
HOOKS_SUBPATH = Path("config") / "hooks.json"
GLOBAL_GUIDANCE_SUBPATHS = (Path(".claude") / "CLAUDE.md", Path(".codex") / "AGENTS.md")
ZSH_ENV_SUBPATH = Path(".zshenv")
SHELL_ENV_SUBPATHS = (ZSH_ENV_SUBPATH, Path(".bash_profile"), Path(".bashrc"), Path(".profile"))
SCENES_SUBDIR = "scenes"
LOG_NAME = "pilot.log"
AUTO_UPDATE_LOG_NAME = "auto_update.log"
LEGACY_LOG_NAME = "autopilot.log"
BUILTIN_SCENE_FILES = tuple(
    spec["file"] for spec in DEFAULT_SPECS["scene_specs"].values()
)
LEGACY_MANAGED_FILES = (
    LEGACY_ENGINE_NAME,
    "profile.json",
    "config.json",
    "project.json",
    LEGACY_LOCK_NAME,
    "__pycache__",
)


def _env_value(primary: str, legacy: str, env: Mapping[str, str] | None = None) -> str:
    """Read the canonical environment variable, then its legacy fallback."""
    eff_env = os.environ if env is None else env
    return eff_env.get(primary, "").strip() or eff_env.get(legacy, "").strip()


_BOOT_PACKAGE_DIR = globals().get("_BOOT_PACKAGE_DIR", Path(__file__).resolve().parent)
PACKAGE_DIR = _BOOT_PACKAGE_DIR
SCRIPTS_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SCRIPTS_DIR / "assets"
DEFAULT_PROFILE_PATH = ASSETS_DIR / "profile.default.json"
ENGINE_SOURCE_PATH = ASSETS_DIR / ENGINE_NAME
AUTO_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60
LOCAL_SYNC_INTERVAL_SECONDS = 30


class SetupError(Exception):
    """The requested install cannot be completed safely."""


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    delegated = maybe_delegate_to_checkout(raw_args)
    if delegated is not None:
        return delegated
    args = build_parser().parse_args(raw_args)
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
    if args.command == "apply":
        try:
            maybe_trigger_background_auto_update(gemini_dir_for(args))
        except (SetupError, OSError, subprocess.SubprocessError, ValueError) as exc:
            print(f"Pilot auto-update: {exc}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pilot",
        description="Install, inspect, upgrade or remove The Pilot in ~/.gemini.",
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

    apply_parser = subparsers.add_parser(
        "apply", help="编译并原子写入配置（默认保留已安装的个人偏好）"
    )
    add_common_arguments(apply_parser)
    apply_parser.add_argument(
        "--preset",
        choices=sorted(DEFAULT_SPECS["presets"]),
        help="显式重置为指定预设；会丢弃已安装 profile.json 中的个人偏好",
    )
    apply_parser.add_argument(
        "--profile",
        help="JSON 对象、JSON 文件路径或 @文件路径；会显式替换已安装的个人偏好",
    )
    apply_parser.add_argument(
        "--upgrade",
        action="store_true",
        help="以已安装 profile.json 为基线重编译（要求该文件已存在，不能与 --preset/--profile 同用）",
    )
    apply_parser.add_argument("--mode", choices=MODES)
    apply_parser.add_argument("--max-turns", type=int)
    apply_parser.add_argument("--worker-agents")
    apply_parser.add_argument("--review-agents")
    apply_parser.add_argument("--scenes")
    apply_parser.add_argument("--deliver-strategy", choices=DELIVER_STRATEGIES)
    apply_parser.add_argument(
        "--feedback-email", action=argparse.BooleanOptionalAction, help=argparse.SUPPRESS
    )
    apply_parser.add_argument("--entry-shortcuts")
    apply_parser.add_argument("--guardrail", action="append")

    status_parser = subparsers.add_parser("status", help="只读检查安装与漂移")
    add_common_arguments(status_parser)

    update_parser = subparsers.add_parser("self-update", help="更新 CLI、技能链接与已安装运行时")
    add_common_arguments(update_parser)
    update_parser.add_argument("--checkout-dir", help="本地 the-pilot checkout（测试或开发覆盖）")
    update_parser.add_argument("--home-dir", help="技能链接根目录")
    update_parser.add_argument("--workspace", help=argparse.SUPPRESS)
    update_mode = update_parser.add_mutually_exclusive_group()
    update_mode.add_argument("--project-only", action="store_true", help="执行本地 checkout 快速同步并安排后台更新")
    update_mode.add_argument("--local-only", action="store_true", help="仅同步本地 checkout，不执行远端检查")
    update_mode.add_argument("--background", action="store_true", help=argparse.SUPPRESS)

    uninstall_parser = subparsers.add_parser("uninstall", help="移除本工具写入的文件与 Hook 组")
    add_common_arguments(uninstall_parser)
    return parser


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gemini-dir",
        default=None,
        help="Antigravity 配置目录（默认 ~/.gemini；也可用 THE_PILOT_GEMINI_DIR 覆盖）",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--project", help=argparse.SUPPRESS)
    selection.add_argument("--no-project", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


def probe(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    pilot_dir = gemini_dir / PILOT_SUBDIR
    profile_path = _installed_path(gemini_dir, "profile.json")
    engine_path = pilot_dir / ENGINE_NAME
    scenes_dir = pilot_dir / SCENES_SUBDIR
    hooks_path = gemini_dir / HOOKS_SUBPATH
    gemini_md = gemini_dir / GEMINI_MD_SUBPATH
    hooks_group = False
    hooks_error = ""
    try:
        hooks_group = has_pilot_hook_group(hooks_path)
    except SetupError as exc:
        hooks_error = str(exc)
    report: dict[str, Any] = {
        "gemini_dir": str(gemini_dir),
        "clis": {
            name: shutil.which(name) or ""
            for name in ("orca", "claude", "codex", "agy")
        },
        "installed": {
            "pilot_dir": pilot_dir.is_dir(),
            "profile": profile_path.is_file(),
            "engine": engine_path.is_file(),
            "scenes": sorted(path.name for path in scenes_dir.glob("*.md"))
            if scenes_dir.is_dir()
            else [],
            "hooks_group": hooks_group,
            "gemini_block": profile_compiler.GEMINI_BEGIN in read_text(gemini_md),
        },
        "recommended_profile": resolve_profile(
            load_default_profile(), preset="recommended"
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
            report["installed_profile"] = validate_profile(installed_profile)
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
    pilot_dir = gemini_dir / PILOT_SUBDIR
    profile_path = pilot_dir / "profile.json"
    config_path = pilot_dir / "config.json"
    scenes_dir = pilot_dir / SCENES_SUBDIR
    engine_path = pilot_dir / ENGINE_NAME
    gemini_md = gemini_dir / GEMINI_MD_SUBPATH
    hooks_path = gemini_dir / HOOKS_SUBPATH

    installed_profile_path = _installed_path(gemini_dir, "profile.json")
    if args.upgrade:
        if args.preset is not None or args.profile:
            raise SetupError(
                "--upgrade 不能与 --preset 或 --profile 同用；重置预设或替换配置时请直接运行 pilot apply"
            )
        base = read_json_object(installed_profile_path, "已安装的 profile.json")
        if base is None:
            raise SetupError("--upgrade 需要已安装的 profile.json；未找到安装配置")
        reusing_installed = True
    elif args.preset is None and not args.profile and installed_profile_path.is_file():
        base = read_json_object(installed_profile_path, "已安装的 profile.json")
        if base is None:
            raise SetupError(
                "已安装的 profile.json 读取失败；请检查该文件或显式使用 --preset/--profile"
            )
        reusing_installed = True
    else:
        base = load_default_profile()
        reusing_installed = False
    preset = args.preset if args.preset is not None else (
        None if reusing_installed else "recommended"
    )
    profile = resolve_profile(
        base, preset=preset, overrides=overrides_from_args(args)
    )

    bundle = profile_compiler.compile_bundle(profile)
    current_gemini = read_text(gemini_md)
    hooks = read_json_object(hooks_path, "hooks.json") or {}
    engine_text = ENGINE_SOURCE_PATH.read_text(encoding="utf-8")

    merged_gemini = profile_compiler.merge_gemini_md(current_gemini, bundle.gemini_block)
    merged_hooks = profile_compiler.merge_hooks_json(hooks, str(engine_path))
    global_guidance = _merged_global_guidance(bundle.profile_json, gemini_dir)
    shell_env = _merged_shell_env(gemini_dir)
    stale_scenes = [
        name
        for name in BUILTIN_SCENE_FILES
        if name not in bundle.scenes and (scenes_dir / name).is_file()
    ]

    (pilot_dir / "project.json").unlink(missing_ok=True)
    atomic_write_text(profile_path, json_text(bundle.profile_json))
    atomic_write_text(config_path, json_text(bundle.config_json))
    for filename, text in bundle.scenes.items():
        atomic_write_text(scenes_dir / filename, text)
    atomic_write_text(engine_path, engine_text, mode=0o755)
    if merged_gemini != current_gemini or not gemini_md.is_file():
        atomic_write_text(gemini_md, merged_gemini)
    if merged_hooks != hooks or not hooks_path.is_file():
        atomic_write_text(hooks_path, json_text(merged_hooks))
    for guidance_path, guidance_text in global_guidance:
        atomic_write_user_file(guidance_path, guidance_text)
    for path, text in shell_env:
        if path.name == ZSH_ENV_SUBPATH.name and not path.exists() and not path.is_symlink():
            atomic_write_text(path, text, mode=0o600)
        else:
            atomic_write_user_file(path, text)
    for name in stale_scenes:
        (scenes_dir / name).unlink()

    _migrate_legacy_install(gemini_dir)

    return {
        "gemini_dir": str(gemini_dir),
        "preset": preset or "installed-profile",
        "mode": bundle.profile_json["pilot"]["mode"],
        "max_consecutive_auto_turns": bundle.profile_json["pilot"]["max_consecutive_auto_turns"],
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
        "global_guidance": [str(path) for path, _ in global_guidance],
        "shell_env": [str(path) for path, _ in shell_env],
        "preserved": [],
        "warnings": missing_cli_warnings(bundle.profile_json),
    }


def _installed_path(gemini_dir: Path, name: str) -> Path:
    """Prefer the canonical install path, falling back to the legacy one.

    Read-only callers use this before the first post-migration ``apply`` has
    moved ``config/autopilot`` onto ``config/the-pilot``.
    """
    canonical = gemini_dir / PILOT_SUBDIR / name
    if canonical.exists():
        return canonical
    legacy = gemini_dir / LEGACY_AUTOPILOT_SUBDIR / name
    return legacy if legacy.exists() else canonical


def default_install_home(gemini_dir: Path) -> Path | None:
    """Return the home dir of the resolved default install, else ``None``.

    Only the install rooted at ``$HOME/.gemini`` owns global guidance and the
    managed shell block. The decision is made from the resolved install root and
    the resolved user home, so every entry point (apply, status, uninstall,
    self-update, auto-upgrade, runtime ``,worker``) classifies the same
    installation identically. A redirected root such as ``--gemini-dir
    /tmp/sandbox`` does not own global guidance or shell startup files.
    An explicit ``self-update --home-dir`` authorizes tool and skill
    maintenance for that home while leaving managed-block ownership tied to Gemini.
    """
    home = Path.home().resolve()
    if gemini_dir.resolve() != (home / ".gemini").resolve():
        return None
    return home


def _shell_env_paths(gemini_dir: Path) -> tuple[Path, ...]:
    home = default_install_home(gemini_dir)
    return tuple(home / subpath for subpath in SHELL_ENV_SUBPATHS) if home is not None else ()


def _shell_env_change(path: Path, transform: Callable[[str], str]) -> tuple[str, str] | None:
    """Read a startup file and plan the change, or skip a missing optional file."""
    try:
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as handle:
                existing = handle.read()
        elif path.is_symlink() or path.exists():
            raise ValueError("必须是普通文件或指向普通文件的符号链接")
        elif path.name == ZSH_ENV_SUBPATH.name:
            existing = ""
        else:
            return None
        return existing, transform(existing)
    except (OSError, ValueError) as exc:
        raise SetupError(f"{path.name} 托管块异常：{exc}") from exc


def _merged_shell_env(gemini_dir: Path) -> list[tuple[Path, str]]:
    plans: list[tuple[Path, str]] = []
    for path in _shell_env_paths(gemini_dir):
        change = _shell_env_change(path, profile_compiler.merge_shell_env)
        if change is not None:
            existing, merged = change
            if merged != existing:
                plans.append((path, merged))
    return plans


def _stripped_shell_env(gemini_dir: Path) -> list[tuple[Path, str]]:
    plans: list[tuple[Path, str]] = []
    for path in _shell_env_paths(gemini_dir):
        change = _shell_env_change(path, profile_compiler.strip_shell_env)
        if change is not None:
            existing, stripped = change
            if stripped != existing:
                plans.append((path, stripped))
    return plans


def _shell_env_drift(gemini_dir: Path, *, installed: bool) -> list[str]:
    if installed:
        transform = profile_compiler.merge_shell_env
        finding = "托管块缺失或已漂移"
    else:
        transform = profile_compiler.strip_shell_env
        finding = "仍包含托管块"
    drift: list[str] = []
    for path in _shell_env_paths(gemini_dir):
        try:
            change = _shell_env_change(path, transform)
        except SetupError as exc:
            drift.append(str(exc))
        else:
            if change is not None and change[0] != change[1]:
                drift.append(f"{path.name} {finding}")
    return drift


def _merged_global_guidance(
    profile: dict[str, Any], gemini_dir: Path
) -> list[tuple[Path, str]]:
    """Render the managed orchestration block into existing agent files."""
    home_dir = default_install_home(gemini_dir)
    if home_dir is None:
        return []
    block = profile_compiler.render_orchestration_block(profile)
    merged_documents: list[tuple[Path, str]] = []
    for subpath in GLOBAL_GUIDANCE_SUBPATHS:
        path = home_dir / subpath
        if not path.is_file():
            continue
        existing = read_text(path)
        try:
            merged = profile_compiler.merge_orchestration_md(existing, block)
        except ValueError as exc:
            raise SetupError(f"{subpath.as_posix()} 托管块异常：{exc}") from exc
        if merged != existing:
            merged_documents.append((path, merged))
    return merged_documents


def _stripped_global_guidance(
    gemini_dir: Path,
) -> tuple[list[tuple[Path, str]], list[str]]:
    """Remove the managed orchestration block from existing agent files."""
    home_dir = default_install_home(gemini_dir)
    if home_dir is None:
        return [], []
    removals: list[tuple[Path, str]] = []
    removed: list[str] = []
    for subpath in GLOBAL_GUIDANCE_SUBPATHS:
        path = home_dir / subpath
        if not path.is_file():
            continue
        existing = read_text(path)
        try:
            stripped = profile_compiler.strip_orchestration_md(existing)
        except ValueError as exc:
            raise SetupError(f"{subpath.as_posix()} 托管块异常：{exc}") from exc
        if stripped != existing:
            removals.append((path, stripped))
            removed.append(f"{subpath.as_posix()}:managed-block")
    return removals, removed


def _global_guidance_drift(
    profile: dict[str, Any], gemini_dir: Path
) -> list[str]:
    """Report agent files whose managed orchestration block is stale."""
    home_dir = default_install_home(gemini_dir)
    if home_dir is None:
        return []
    block = profile_compiler.render_orchestration_block(profile)
    drift: list[str] = []
    for subpath in GLOBAL_GUIDANCE_SUBPATHS:
        path = home_dir / subpath
        if not path.is_file():
            continue
        existing = read_text(path)
        try:
            merged = profile_compiler.merge_orchestration_md(existing, block)
        except ValueError as exc:
            drift.append(f"{subpath.as_posix()} 托管块异常：{exc}")
        else:
            if merged != existing:
                drift.append(f"{subpath.as_posix()} 托管块缺失或已漂移")
    return drift


def _migrate_legacy_install(gemini_dir: Path) -> None:
    """Relocate a pre-migration ``config/autopilot`` install.

    The caller has already written every canonical managed file and the
    canonical hook group, so this only moves durable state and custom scenes,
    then removes managed legacy files. Unknown user files are left untouched
    instead of deleting the whole tree.
    """
    legacy_dir = gemini_dir / LEGACY_AUTOPILOT_SUBDIR
    if not legacy_dir.is_dir():
        return
    pilot_dir = gemini_dir / PILOT_SUBDIR
    _migrate_legacy_state(legacy_dir / "state", pilot_dir / "state")
    _migrate_legacy_scenes(legacy_dir / SCENES_SUBDIR, pilot_dir / SCENES_SUBDIR)
    for name in LEGACY_MANAGED_FILES:
        path = legacy_dir / name
        if name == "__pycache__":
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink()
    prune_empty_dir(legacy_dir / SCENES_SUBDIR)
    prune_empty_dir(legacy_dir / "state")
    prune_empty_dir(legacy_dir)


def _migrate_legacy_state(legacy_state: Path, state_dir: Path) -> None:
    if not legacy_state.is_dir():
        return
    for path in sorted(legacy_state.iterdir()):
        if not path.is_file():
            continue
        if path.name.endswith(".lock"):
            # Lock files carry no durable state and the legacy engine no longer
            # owns any hook, so remove them instead of moving stale locks.
            path.unlink(missing_ok=True)
            continue
        if path.name == LEGACY_LOG_NAME:
            _append_log_file(path, state_dir / LOG_NAME)
            continue
        target = state_dir / path.name
        if target.exists():
            continue
        state_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)


def _append_log_file(source: Path, target: Path) -> None:
    """Append the legacy log to the canonical log, then remove the source."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("ab") as writer:
        shutil.copyfileobj(reader, writer)
    source.unlink()


def _migrate_legacy_scenes(legacy_scenes: Path, scenes_dir: Path) -> None:
    if not legacy_scenes.is_dir():
        return
    for path in sorted(legacy_scenes.glob("*.md")):
        if path.name in BUILTIN_SCENE_FILES:
            path.unlink()
            continue
        target = scenes_dir / path.name
        if target.exists():
            continue
        scenes_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)


def status(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    pilot_dir = gemini_dir / PILOT_SUBDIR
    profile_path = pilot_dir / "profile.json"
    config_path = pilot_dir / "config.json"
    scenes_dir = pilot_dir / SCENES_SUBDIR
    engine_path = pilot_dir / ENGINE_NAME
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
            "group": has_pilot_hook_group(hooks_path),
        },
        "gemini_md": str(gemini_md),
        "drift": [],
    }
    report["drift"].extend(_shell_env_drift(gemini_dir, installed=profile is not None))
    project_path = pilot_dir / "project.json"
    if project_path.exists() or project_path.is_symlink():
        report["drift"].append("检测到旧版 project.json，请运行 pilot apply --upgrade 移除")
    legacy_dir = gemini_dir / LEGACY_AUTOPILOT_SUBDIR
    if any((legacy_dir / name).exists() for name in LEGACY_MANAGED_FILES):
        report["drift"].append(
            "检测到旧版 config/autopilot 托管残留，请运行 pilot apply --upgrade 完成迁移"
        )
    state_dir = _state_dir_for(pilot_dir)
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
    bundle = profile_compiler.compile_bundle(profile)
    report["drift"].extend(_global_guidance_drift(bundle.profile_json, gemini_dir))
    report.update(
        {
            "mode": bundle.profile_json["pilot"]["mode"],
            "max_consecutive_auto_turns": bundle.profile_json["pilot"][
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
        hooks.get(HOOK_GROUP) == expected_hooks.get(HOOK_GROUP)
        and LEGACY_HOOK_GROUP not in hooks
    )
    if not hooks_path.is_file():
        report["hooks"]["up_to_date"] = False
    if LEGACY_HOOK_GROUP in hooks:
        report["drift"].append(
            f"hooks.json 仍包含旧版 {LEGACY_HOOK_GROUP} 组，请运行 pilot apply --upgrade 完成迁移"
        )
    if not report["hooks"]["up_to_date"] and LEGACY_HOOK_GROUP not in hooks:
        report["drift"].append(f"hooks.json 的 {HOOK_GROUP} 组缺失或已漂移")
    if not engine_up_to_date:
        report["drift"].append("runtime.py 缺失或与当前技能版本不一致")
    return report


def _in_test_harness(env: Mapping[str, str] | None = None) -> bool:
    eff_env = os.environ if env is None else env
    return "PYTEST_CURRENT_TEST" in eff_env or "unittest" in sys.modules or "pytest" in sys.modules


def _auto_update_disabled(env: Mapping[str, str] | None = None) -> bool:
    return _env_value("THE_PILOT_DISABLE_AUTO_UPDATE", "ORCA_AUTOPILOT_DISABLE_AUTO_UPDATE", env) == "1"


def _automatic_update_allowed(env: Mapping[str, str]) -> bool:
    return not _auto_update_disabled(env) and (
        not _in_test_harness(env) or env.get("THE_PILOT_ENABLE_AUTO_UPDATE", "").strip() == "1"
    )


def _discover_checkout(explicit: str | None, *, home_dir: Path | None = None) -> tuple[Path | None, bool]:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not (path / ".git").exists():
            raise SetupError(f"Checkout directory is not a Git repository: {path}")
        return path, True
    real_home = Path.home().resolve()
    custom_home = home_dir is not None and home_dir.expanduser().resolve() != real_home
    if custom_home:
        candidates = [home_dir.expanduser() / "the-pilot"]
    elif not _in_test_harness() or os.environ.get("THE_PILOT_ENABLE_AUTO_UPDATE", "").strip() == "1":
        candidates = [real_home / "the-pilot", SCRIPTS_DIR.parents[1]]
    else:
        candidates = []
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


def _manages_uv_tool(home_dir: Path, *, explicit: bool = False) -> bool:
    if explicit or not shutil.which("uv"):
        return False
    is_real_home = home_dir.expanduser().resolve() == Path.home().resolve()
    if _in_test_harness():
        return not is_real_home and (home_dir / ".local/bin/pilot").exists()
    return is_real_home or (home_dir / ".local/bin/pilot").exists()


def _uv_tool_stale(
    home_dir: Path, sha: str, digest: str, metadata: dict[str, Any], *, managed: bool,
) -> bool:
    package = _installed_uv_package_dir(home_dir)
    return (
        (managed and not (home_dir / ".local/bin/pilot").exists())
        or metadata.get("installed_sha") != sha
        or metadata.get("installed_tree_digest") != digest
        or (package is not None and _compute_source_tree_digest(package) != digest)
    )


def _load_json_object(path: Path) -> dict[str, Any]:
    value = read_json_object(path, path.name)
    return value if isinstance(value, dict) else {}


def _state_dir_for(pilot_dir: Path, *, env: Mapping[str, str] | None = None) -> Path:
    return Path(
        _env_value("THE_PILOT_STATE_DIR", "ORCA_AUTOPILOT_STATE_DIR", env)
        or str(pilot_dir / "state")
    ).expanduser()


def _installed_vcs_commit(package_dir: Path) -> str | None:
    if package_dir.parent.name != "site-packages":
        return None
    for direct_url in sorted(package_dir.parent.glob("the_pilot-*.dist-info/direct_url.json")):
        vcs_info = _load_json_object(direct_url).get("vcs_info")
        if isinstance(vcs_info, dict):
            commit = vcs_info.get("commit_id")
            if isinstance(commit, str) and commit.strip():
                return commit.strip()
    return None


def _is_same_git_repo(path_a: Path, path_b: Path) -> bool:
    path_a, path_b = path_a.expanduser().resolve(), path_b.expanduser().resolve()
    if path_a == path_b:
        return True
    args = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    common_a = subprocess_run(["git", "-C", str(path_a), *args], 5).strip()
    common_b = subprocess_run(["git", "-C", str(path_b), *args], 5).strip()
    return Path(common_a).resolve() == Path(common_b).resolve()


def _checkout_state(checkout: Path, timeout: int) -> str:
    git = ["git", "--no-optional-locks", "-C", str(checkout)]
    if subprocess_run([*git, "status", "--porcelain"], timeout).strip():
        return "skipped_dirty_checkout"
    if subprocess_run([*git, "branch", "--show-current"], timeout).strip() != "main":
        return "skipped_non_main_branch"
    return "clean_main"


def _newer_checkout_package(
    *,
    home_dir: Path | None = None,
    gemini_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    eff_env = os.environ if env is None else env
    if not _automatic_update_allowed(eff_env) or eff_env.get("THE_PILOT_DELEGATED", "").strip() == "1":
        return None
    resolved_home = home_dir.expanduser().resolve() if home_dir is not None else Path.home().resolve()
    checkout = resolved_home / "the-pilot"
    package = (checkout / "src/the_pilot").resolve()
    if not package.is_dir() or package == PACKAGE_DIR.resolve() or not (checkout / ".git").exists():
        return None

    try:
        git = ["git", "-C", str(checkout)]
        if _checkout_state(checkout, 5) != "clean_main":
            return None
        checkout_sha = subprocess_run([*git, "rev-parse", "HEAD"], 5).strip()
        current_repo = PACKAGE_DIR.parents[1]
        allow_same_commit = False
        if (current_repo / ".git").exists():
            if _is_same_git_repo(current_repo, checkout):
                return None
            baseline = subprocess_run(["git", "-C", str(current_repo), "rev-parse", "HEAD"], 5).strip()
        else:
            baseline = _installed_vcs_commit(PACKAGE_DIR)
            if baseline is None:
                resolved_gemini = _resolve_gemini_dir(gemini_dir, resolved_home, eff_env)
                state = _state_dir_for(resolved_gemini / PILOT_SUBDIR, env=eff_env)
                metadata = _load_json_object(state / "auto_update.json")
                installed_sha = metadata.get("installed_sha")
                baseline = installed_sha or metadata.get("head_sha")
                allow_same_commit = bool(installed_sha) and PACKAGE_DIR.parent.name == "site-packages"
        if not isinstance(baseline, str) or not baseline.strip():
            return None
        baseline_sha = subprocess_run(
            [*git, "rev-parse", "--verify", "--end-of-options", f"{baseline.strip()}^{{commit}}"], 5
        ).strip()
        if baseline_sha == checkout_sha:
            if not allow_same_commit:
                return None
        else:
            subprocess_run([*git, "merge-base", "--is-ancestor", baseline_sha, checkout_sha], 5)
        if _compute_source_tree_digest(package) == _compute_source_tree_digest(PACKAGE_DIR):
            return None
    except (SetupError, OSError, ValueError, subprocess.SubprocessError):
        # A checkout without verifiable provenance cannot replace the running package.
        return None
    return package


def maybe_activate_newer_checkout_package(package_path: list[str]) -> bool:
    package = _newer_checkout_package(env=os.environ)
    if package is None or str(package) in package_path:
        return False
    package_path.insert(0, str(package))
    boot_package_dir = PACKAGE_DIR
    for name in ("assets.runtime", "defaults", "project", "profile", "compiler", "distill", "runtime", "installer"):
        module = sys.modules.get(f"the_pilot.{name}")
        if module is not None:
            active = importlib.reload(module)
            if name == "installer":
                active._BOOT_PACKAGE_DIR = active.PACKAGE_DIR = boot_package_dir
    return True


def _argv_option(argv: list[str], option: str) -> str | None:
    value = None
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token.startswith(f"{option}="):
            value = token.partition("=")[2]
        elif token == option and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            value = argv[index + 1]
    return value


def _with_package_on_pythonpath(env: Mapping[str, str], package: Path) -> dict[str, str]:
    paths = [str(package.parent)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    return {**env, "PYTHONPATH": os.pathsep.join(paths), "PYTHONSAFEPATH": "1"}


def maybe_delegate_to_checkout(
    argv: list[str],
    *,
    home_dir: str | Path | None = None,
    gemini_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int | None:
    if not argv or argv[0] in {"self-update", "-h", "--help", "--version"} or any(
        token == "--checkout-dir" or token.startswith("--checkout-dir=") for token in argv
    ):
        return None
    eff_env = os.environ if env is None else env
    home = home_dir if home_dir is not None else _argv_option(argv, "--home-dir")
    gemini = gemini_dir if gemini_dir is not None else _argv_option(argv, "--gemini-dir")
    package = _newer_checkout_package(
        home_dir=Path(home) if home else None, gemini_dir=Path(gemini) if gemini else None, env=eff_env,
    )
    if package is None:
        return None
    child_env = _with_package_on_pythonpath(eff_env, package)
    child_env["THE_PILOT_DELEGATED"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "the_pilot.cli", *argv], env=child_env, check=False,
    ).returncode


def _cooldown_elapsed(metadata: dict[str, Any], key: str, interval: int) -> bool:
    try:
        return time.time() - float(metadata.get(key) or 0) >= interval
    except (TypeError, ValueError):
        return True


def _log_auto_update(state_dir: Path, event: str, **details: Any) -> None:
    append_log_event(state_dir / AUTO_UPDATE_LOG_NAME, event, **details)


def _clean_local_source(checkout: Path | None) -> tuple[Path, str, str] | None:
    if checkout is None or _checkout_state(checkout, 2) != "clean_main":
        return None
    sha = subprocess_run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 2).strip()
    return checkout, sha, _compute_source_tree_digest(checkout / "src/the_pilot")


def _schedule_background_update(
    gemini_dir: Path,
    state_dir: Path,
    maintenance_home: Path,
    local_source: tuple[Path, str, str] | None,
    *,
    allow_remote: bool,
) -> str:
    """Choose and record one updater while the caller holds auto_update.lock."""
    metadata_path = state_dir / "auto_update.json"
    metadata = _load_json_object(metadata_path)
    update_args = ["--gemini-dir", str(gemini_dir), "--json"]
    if maintenance_home != default_install_home(gemini_dir):
        update_args.extend(["--home-dir", str(maintenance_home)])
    command = None
    child_env = os.environ
    status = "none"
    if local_source is not None:
        checkout, sha, digest = local_source
        managed = _manages_uv_tool(maintenance_home)
        stale = _uv_tool_stale(maintenance_home, sha, digest, metadata, managed=managed)
        if stale and not managed:
            status = "skipped_unmanaged"
        elif managed and (stale or os.environ.get("THE_PILOT_DELEGATED") == "1"):
            if _cooldown_elapsed(metadata, "last_local_sync_at", LOCAL_SYNC_INTERVAL_SECONDS):
                command = [sys.executable, str(checkout / "bin/pilot"), "self-update", "--local-only", *update_args]
                status, timestamp = "spawned_local_sync", "last_local_sync_at"
            else:
                status = "cooldown"
    if command is None and allow_remote and _cooldown_elapsed(metadata, "last_checked_at", AUTO_UPDATE_INTERVAL_SECONDS):
        command = [sys.executable, "-m", "the_pilot.cli", "self-update", *update_args]
        child_env = _with_package_on_pythonpath(os.environ, SCRIPTS_DIR)
        status, timestamp = "spawned_remote_check", "last_checked_at"
    if command is None:
        return status
    try:
        subprocess.Popen(
            command, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        metadata.update(status="failed", error=str(exc), last_failed_at=time.time())
        atomic_write_text(metadata_path, json_text(metadata))
        _log_auto_update(state_dir, "auto_update_spawn_failed", error=str(exc))
        raise SetupError(f"Could not start Pilot auto-update: {exc}") from exc
    metadata[timestamp] = time.time()
    atomic_write_text(metadata_path, json_text(metadata))
    return status


def maybe_trigger_background_auto_update(gemini_dir: Path) -> str:
    maintenance_home = default_install_home(gemini_dir)
    if maintenance_home is None or not _automatic_update_allowed(os.environ):
        return "none"
    state_dir = _state_dir_for(gemini_dir / PILOT_SUBDIR)
    with _file_lock(state_dir / "auto_update.lock", blocking=False) as locked:
        if not locked:
            return "busy"
        checkout, _ = _discover_checkout(None, home_dir=maintenance_home)
        return _schedule_background_update(
            gemini_dir, state_dir, maintenance_home, _clean_local_source(checkout), allow_remote=True,
        )


def _checkout_artifact_command(checkout: Path, command: str, gemini_dir: Path, *options: str) -> str:
    return subprocess_run(
        [
            sys.executable, str(checkout / "bin/pilot"), command, *options,
            "--gemini-dir", str(gemini_dir), "--no-project", "--json",
        ],
        30,
        env={**os.environ, "THE_PILOT_DISABLE_AUTO_UPDATE": "1"},
    )


def _has_installed_artifact_drift(checkout: Path, checkout_digest: str, gemini_dir: Path) -> bool:
    if _compute_source_tree_digest(SCRIPTS_DIR) != checkout_digest:
        report = json.loads(_checkout_artifact_command(checkout, "status", gemini_dir))
    else:
        args = argparse.Namespace(
            command="status", gemini_dir=str(gemini_dir), json=True,
        )
        report = status(args)
    return bool(report["drift"])


def self_update(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    if getattr(args, "background", False):
        return {
            "status": maybe_trigger_background_auto_update(gemini_dir),
            "source_status": "scheduled_check",
        }
    requested_home = getattr(args, "home_dir", None)
    maintenance_home = Path(requested_home).expanduser().resolve() if requested_home else default_install_home(gemini_dir)
    checkout_override = getattr(args, "checkout_dir", None)
    home_dir = maintenance_home or Path.home()
    check_remote = not getattr(args, "local_only", False) and (
        maintenance_home is not None or bool(checkout_override)
    )
    pilot_dir = gemini_dir / PILOT_SUBDIR
    state_dir = _state_dir_for(pilot_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if getattr(args, "project_only", False):
        result = {
            "status": "none",
            "source_status": "local_sync",
            "local_source_status": "none",
            "head_sha": None,
            "synced_skills": [],
            "cli_status": "none",
            "error": None,
        }
        with _file_lock(state_dir / "auto_update.lock"):
            try:
                checkout, explicit = _discover_checkout(checkout_override, home_dir=home_dir)
                local_source = _clean_local_source(checkout)
                if local_source is not None and _installed_path(gemini_dir, "profile.json").is_file():
                    checkout, sha, checkout_digest = local_source
                    metadata_path = state_dir / "auto_update.json"
                    existing = _load_json_object(metadata_path)
                    source_engine = checkout / "src/the_pilot/assets" / ENGINE_NAME
                    installed_engine = pilot_dir / ENGINE_NAME
                    runtime_differs = (
                        existing.get("runtime_tree_digest") != checkout_digest
                        or (source_engine.is_file() and (not installed_engine.is_file() or source_engine.read_bytes() != installed_engine.read_bytes()))
                    )
                    if runtime_differs or _has_installed_artifact_drift(checkout, checkout_digest, gemini_dir):
                        _checkout_artifact_command(checkout, "apply", gemini_dir, "--upgrade")
                        result["status"] = result["local_source_status"] = "updated"
                        result["head_sha"] = sha
                        existing["runtime_tree_digest"] = checkout_digest
                        atomic_write_text(metadata_path, json_text(existing))
                if maintenance_home is not None and not explicit and _automatic_update_allowed(os.environ):
                    result["cli_status"] = _schedule_background_update(
                        gemini_dir, state_dir, maintenance_home, local_source,
                        allow_remote=not requested_home,
                    )
            except (SetupError, OSError, subprocess.SubprocessError, ValueError) as exc:
                result["error"] = str(exc)
        return result
    report: dict[str, Any] = {
        "last_checked_at": time.time() if check_remote else None, "last_updated_at": None,
        "status": "failed", "source_status": "failed",
        "head_sha": None, "synced_skills": [], "error": None,
    }
    with _file_lock(state_dir / "auto_update.lock"):
        try:
            existing_metadata = _load_json_object(state_dir / "auto_update.json")
            for key in ("last_updated_at", "installed_sha", "installed_tree_digest", "runtime_tree_digest", "last_local_sync_at"):
                if key in existing_metadata:
                    report[key] = existing_metadata[key]
            if not check_remote:
                report["last_checked_at"] = existing_metadata.get("last_checked_at")
            checkout_digest: str | None = None
            checkout, explicit = _discover_checkout(checkout_override, home_dir=home_dir)
            manages_uv_tool = maintenance_home is not None and _manages_uv_tool(home_dir, explicit=explicit)
            if checkout is not None:
                checkout_state = _checkout_state(checkout, 20)
                report["source_status"] = checkout_state
                if checkout_state == "clean_main":
                    before_sha = subprocess_run(
                        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20
                    ).strip()
                    if check_remote:
                        subprocess_run(["git", "-C", str(checkout), "pull", "--ff-only"], 20)
                    after_sha = subprocess_run(
                        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20
                    ).strip()
                    checkout_digest = _compute_source_tree_digest(checkout / "src/the_pilot")
                    if after_sha != before_sha:
                        report["source_status"] = "updated"
                    else:
                        report["source_status"] = "up_to_date"
                    if manages_uv_tool:
                        if after_sha != before_sha or _uv_tool_stale(
                            home_dir, after_sha, checkout_digest, existing_metadata, managed=True,
                        ):
                            subprocess_run(["uv", "tool", "install", "--force", str(checkout)], 30)
                        report["installed_sha"] = after_sha
                        report["installed_tree_digest"] = checkout_digest
                report["head_sha"] = subprocess_run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20).strip()
            elif check_remote and manages_uv_tool:
                uv_out = subprocess_run(["uv", "tool", "upgrade", "the-pilot"], 30)
                report["source_status"] = (
                    "up_to_date" if "nothing to upgrade" in uv_out.lower() else "updated"
                )
            else:
                report["source_status"] = "skipped_no_source"

            if checkout is not None:
                parents = [gemini_dir / "config/skills"]
                if maintenance_home is not None:
                    parents.extend([maintenance_home / ".agents/skills", maintenance_home / ".claude/skills"])
                for skill_name in ("the-pilot", "the-pilot-setup"):
                    source = checkout / "skills" / skill_name
                    if not source.exists():
                        continue
                    for parent in parents:
                        target = parent / skill_name
                        if not parent.is_dir() or (target.exists() and not target.is_symlink()):
                            continue
                        if target.is_symlink() or not target.exists():
                            target.unlink(missing_ok=True)
                            target.symlink_to(source)
                            report["synced_skills"].append(str(target))

            profile_path = _installed_path(gemini_dir, "profile.json")
            if profile_path.is_file():
                apply_args = argparse.Namespace(**vars(args))
                apply_args.command = "apply"
                apply_args.upgrade = True
                apply_args.profile = None
                apply_args.preset = apply_args.mode = apply_args.max_turns = None
                apply_args.worker_agents = apply_args.review_agents = apply_args.scenes = None
                apply_args.deliver_strategy = apply_args.entry_shortcuts = None
                apply_args.guardrail = None
                if checkout is not None and (checkout / "bin" / "pilot").is_file():
                    _checkout_artifact_command(checkout, "apply", gemini_dir, "--upgrade")
                else:
                    apply(apply_args)
                if checkout_digest is not None and report["source_status"] in {"updated", "up_to_date"}:
                    report["runtime_tree_digest"] = checkout_digest
            report["last_updated_at"] = time.time()
            if report["source_status"] in {"updated", "up_to_date", "skipped_dirty_checkout", "skipped_non_main_branch", "skipped_no_source"}:
                report["status"] = report["source_status"]
        except Exception as exc:
            report["error"] = str(exc)
            report["status"] = "failed"
        atomic_write_text(state_dir / "auto_update.json", json_text(report))
        event = "auto_update_failed" if report["status"] == "failed" else "auto_update_completed"
        _log_auto_update(state_dir, event, **report)
        if report["status"] == "failed":
            raise SetupError(str(report["error"]))
    return report


def subprocess_run(command: list[str], timeout: int, *, env: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout, env=env)
    return result.stdout or result.stderr


@contextmanager
def _file_lock(path: Path, *, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if blocking:
                raise
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def uninstall(args: argparse.Namespace) -> dict[str, Any]:
    with profile_lock(gemini_dir_for(args)):
        return _uninstall_unlocked(args)


def _uninstall_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    gemini_dir = gemini_dir_for(args)
    pilot_dir = gemini_dir / PILOT_SUBDIR
    legacy_dir = gemini_dir / LEGACY_AUTOPILOT_SUBDIR
    profile_path = pilot_dir / "profile.json"
    config_path = pilot_dir / "config.json"
    scenes_dir = pilot_dir / SCENES_SUBDIR
    engine_path = pilot_dir / ENGINE_NAME
    gemini_md = gemini_dir / GEMINI_MD_SUBPATH
    hooks_path = gemini_dir / HOOKS_SUBPATH

    installed = (
        profile_path.is_file()
        or engine_path.is_file()
        or (legacy_dir / "profile.json").is_file()
        or (legacy_dir / LEGACY_ENGINE_NAME).is_file()
    )
    removed: list[str] = []
    global_guidance, global_labels = _stripped_global_guidance(gemini_dir)
    shell_env = _stripped_shell_env(gemini_dir)
    hooks = read_json_object(hooks_path, "hooks.json")
    if hooks is not None and (HOOK_GROUP in hooks or LEGACY_HOOK_GROUP in hooks):
        for group in (HOOK_GROUP, LEGACY_HOOK_GROUP):
            if group in hooks:
                removed.append(f"hooks.json:{group}")
        atomic_write_text(hooks_path, json_text(profile_compiler.strip_hooks_json(hooks)))

    current_gemini = read_text(gemini_md)
    stripped = profile_compiler.strip_gemini_md(current_gemini)
    if stripped != current_gemini:
        if stripped:
            atomic_write_text(gemini_md, stripped)
        else:
            gemini_md.unlink()
        removed.append("GEMINI.md:managed-block")

    for guidance_path, guidance_text in global_guidance:
        atomic_write_user_file(guidance_path, guidance_text)
    removed.extend(global_labels)
    for path, text in shell_env:
        atomic_write_user_file(path, text)
        removed.append(f"{path.name}:managed-block")

    for path, label in (
        (engine_path, ENGINE_NAME),
        (profile_path, "profile.json"),
        (config_path, "config.json"),
        (pilot_dir / "project.json", "project.json"),
        (legacy_dir / LEGACY_ENGINE_NAME, f"legacy/{LEGACY_ENGINE_NAME}"),
        (legacy_dir / "profile.json", "legacy/profile.json"),
        (legacy_dir / "config.json", "legacy/config.json"),
        (legacy_dir / "project.json", "legacy/project.json"),
        (legacy_dir / LEGACY_LOCK_NAME, f"legacy/{LEGACY_LOCK_NAME}"),
    ):
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(label)

    preserved_scenes = _uninstall_scenes(scenes_dir, removed, prefix="scenes/")
    preserved_scenes += _uninstall_scenes(
        legacy_dir / SCENES_SUBDIR, removed, prefix="legacy/scenes/"
    )
    shutil.rmtree(legacy_dir / "__pycache__", ignore_errors=True)
    prune_empty_dir(legacy_dir / SCENES_SUBDIR)
    prune_empty_dir(legacy_dir)
    prune_empty_dir(scenes_dir)
    prune_empty_dir(pilot_dir)
    return {
        "gemini_dir": str(gemini_dir),
        "installed": installed,
        "removed": removed,
        "preserved": preserved_scenes,
    }


def _uninstall_scenes(
    scenes_dir: Path, removed: list[str], *, prefix: str
) -> list[str]:
    preserved: list[str] = []
    for path in sorted(scenes_dir.glob("*.md")) if scenes_dir.is_dir() else []:
        if path.name in BUILTIN_SCENE_FILES:
            path.unlink()
            removed.append(f"{prefix}{path.name}")
        else:
            preserved.append(f"{prefix}{path.name}")
    return preserved


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if args.profile:
        overrides = read_profile_argument(args.profile)
    if args.mode:
        overrides.setdefault("pilot", {})["mode"] = args.mode
    if args.max_turns is not None:
        overrides.setdefault("pilot", {})["max_consecutive_auto_turns"] = args.max_turns
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


def gemini_dir_for(args: argparse.Namespace) -> Path:
    return _resolve_gemini_dir(getattr(args, "gemini_dir", None), Path.home())


def _resolve_gemini_dir(
    explicit: str | Path | None, home_dir: Path, env: Mapping[str, str] | None = None,
) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = _env_value("THE_PILOT_GEMINI_DIR", "ORCA_AUTOPILOT_GEMINI_DIR", env)
    if configured:
        return Path(configured).expanduser().resolve()
    return (home_dir / ".gemini").resolve()


@contextmanager
def profile_lock(gemini_dir: Path):
    with _file_lock(gemini_dir / PILOT_SUBDIR / LOCK_NAME):
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


def atomic_write_user_file(path: Path, content: str) -> None:
    """Atomically rewrite an existing user-owned file in place.

    Follows symlinks so the link itself survives, preserves the existing
    permission bits, and fails closed on a missing target instead of creating a
    file the user never had.
    """
    target = path.resolve(strict=True)
    mode = stat.S_IMODE(target.stat().st_mode)
    atomic_write_text(target, content, mode=mode)


def has_pilot_hook_group(hooks_path: Path) -> bool:
    hooks = read_json_object(hooks_path, "hooks.json") or {}
    return HOOK_GROUP in hooks


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
