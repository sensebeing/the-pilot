"""Sandboxed tests for The Pilot.

Every test runs inside a ``tempfile.TemporaryDirectory`` and passes an explicit
``--gemini-dir`` or ``THE_PILOT_GEMINI_DIR``; the real ``~/.gemini`` is
never read or written.

resident-fact: settlement: profile 是唯一安装真源，安装/卸载只改托管项；自动投递只在 gate=auto 且未达轮数上限时回车。
resident-fact: admission: 只有同回合已回答的同步提问才放行预填，题数只由提问工具声明（截断的调用一律等待）；异步提问与未回执/失败/空答案一律等待用户。
"""
from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from functools import partial
from unittest import mock

from the_pilot import compiler as profile_compiler
from the_pilot import installer
from the_pilot.assets import runtime
from the_pilot.profile import resolve_profile as resolve_user_profile, normalize_comma_alias
from the_pilot.defaults import DEFAULT_SPECS, DELIVER_STRATEGIES, HARNESS_ORCHESTRATION_SENTENCE
from the_pilot.project import ProfileError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "src/the_pilot"
SETUP_SCRIPT = REPO_ROOT / "bin/pilot"
DEFAULT_PROFILE_PATH = SCRIPTS_DIR / "assets/profile.default.json"
PROJECT_PATH = REPO_ROOT / "tests/fixtures/alter-service.json"
PROJECT = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
resolve_profile = partial(resolve_user_profile, project=PROJECT)
compile_bundle = partial(profile_compiler.compile_bundle, project=PROJECT)
ENGINE_REL = Path("config/the-pilot/runtime.py")
SHELL_ENV_BLOCK = (
    '# BEGIN THE_PILOT_SHELL_ENV\n'
    'export AGY_CLI_OSC7501="${AGY_CLI_OSC7501:-off}"\n'
    '# END THE_PILOT_SHELL_ENV\n'
)
SCENES_REL = Path("config/the-pilot/scenes")
GEMINI_REL = Path("config/GEMINI.md")
HOOKS_REL = Path("config/hooks.json")
PROFILE_REL = Path("config/the-pilot/profile.json")
CONFIG_REL = Path("config/the-pilot/config.json")


FAKE_ORCA = """#!__PYTHON__
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__ROOT__)
with (root / "orca-calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
args = sys.argv[1:]
if args[:2] == ["terminal", "list"]:
    stop_pid = root / "stop-pid"
    if os.environ.get("FAKE_REQUIRE_SAME_HOOK") == "1" and not stop_pid.exists():
        stop_pid.write_text(str(os.getppid()), encoding="utf-8")
    mutation = os.environ.get("FAKE_LIST_MUTATION")
    if mutation:
        subprocess.run(
            json.loads(mutation),
            stdout=subprocess.DEVNULL, check=True, timeout=2,
        )
    transcript = os.environ.get("FAKE_LIST_TRANSCRIPT")
    if transcript:
        with Path(transcript).open("a", encoding="utf-8") as handle:
            step_type = os.environ["FAKE_LIST_STEP"]
            if step_type == "incomplete":
                handle.write('{"type":')
            else:
                handle.write(json.dumps({"type": step_type, "content": "another step"}) + "\\n")
    worktree = os.environ.get("FAKE_WORKTREE_PATH", str(root / "ws"))
    count = int(os.environ.get("FAKE_TERMINAL_COUNT", "1"))
    terminals = [
        {
            "handle": "term_test" if index == 0 else f"term_test_{index}",
            "agentIdentity": "antigravity",
            "connected": True,
            "writable": True,
            "worktreePath": worktree,
            "incarnationId": os.environ.get("FAKE_TERMINAL_INCARNATION", "agy-1"),
        }
        for index in range(count)
    ]
    print(json.dumps({"ok": True, "result": {"terminals": terminals}}, ensure_ascii=False))
elif args[:2] == ["terminal", "send"]:
    if os.environ.get("FAKE_REQUIRE_SAME_HOOK") == "1":
        if (root / "stop-pid").read_text(encoding="utf-8") != str(os.getppid()):
            sys.exit("Enter was sent by a different process than Stop's terminal lookup")
    result = os.environ.get("FAKE_SEND_RESULT")
    if result == "refused":
        print(json.dumps({
            "ok": False,
            "error": {"code": "agent_prompt_blocked"},
            "result": {"send": {"accepted": False, "refusedReason": "permission"}},
        }))
        sys.exit(1)
    if result == "invalid":
        print("not-json")
    else:
        receipt = {"accepted": os.environ.get("FAKE_SEND_ACCEPTED", "1") != "0"}
        if "FAKE_SEND_PROMPT" in os.environ:
            receipt["prompt"] = json.loads(os.environ["FAKE_SEND_PROMPT"])
        print(json.dumps({"ok": True, "result": {"send": receipt}}, ensure_ascii=False))
else:
    print(json.dumps({"ok": False}))
"""


def load_default_profile() -> dict:
    return json.loads(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def base_env(root: Path) -> dict[str, str]:
    return {
        "HOME": str(root / "home"),
        "PATH": f"{root / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(root / "tmp"),
        "THE_PILOT_DISABLE_AUTO_UPDATE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "en_US.UTF-8",
    }


def fake_orca(root: Path) -> Path:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    path = root / "bin" / "orca"
    script = FAKE_ORCA.replace("__PYTHON__", sys.executable).replace(
        "__ROOT__", repr(str(root))
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_setup(root: Path, gemini: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SETUP_SCRIPT),
            *args,
            "--project",
            str(PROJECT_PATH),
            "--gemini-dir",
            str(gemini),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=base_env(root),
        timeout=60,
    )


def apply_profile(root: Path, gemini: Path, *args: str) -> dict:
    result = run_setup(root, gemini, "apply", *args)
    if result.returncode != 0:
        raise AssertionError(f"apply failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def run_setup_default(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a default HOME install without redirecting ``~/.gemini``."""
    return subprocess.run(
        [
            sys.executable,
            str(SETUP_SCRIPT),
            *args,
            "--project",
            str(PROJECT_PATH),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=base_env(root),
        timeout=60,
    )


def engine_env(root: Path, gemini: Path, state: Path, extra: dict | None = None) -> dict[str, str]:
    env = base_env(root)
    env["THE_PILOT_GEMINI_DIR"] = str(gemini)
    env["THE_PILOT_STATE_DIR"] = str(state)
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def run_engine(
    root: Path,
    gemini: Path,
    state: Path,
    *args: str,
    stdin: str = "",
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(gemini / ENGINE_REL), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=engine_env(root, gemini, state, extra_env),
        timeout=30,
    )


def run_default_engine(
    root: Path, gemini: Path, state: Path, *args: str
) -> subprocess.CompletedProcess:
    """Run the installed engine where ``~/.gemini`` is the default install."""
    env = engine_env(root, gemini, state)
    env.pop("THE_PILOT_GEMINI_DIR", None)
    return subprocess.run(
        [sys.executable, str(gemini / ENGINE_REL), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def write_transcript(
    root: Path, name: str, steps: list[dict], *, append: bool = False
) -> Path:
    path = root / name
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        for step in steps:
            handle.write(json.dumps(step, ensure_ascii=False) + "\n")
    return path


def payload(conversation_id: str, **fields) -> str:
    body = {"conversationId": conversation_id}
    body.update(fields)
    return json.dumps(body, ensure_ascii=False)


def suggestion(
    prompt: str,
    *,
    alt: str = "",
    scene: str = "plan-and-orchestrate",
    gate: str = "auto",
) -> str:
    lines = [
        "方案已就绪。",
        "",
        f"> 💡 **下一步建议（已自动预填，按 Enter 发送）**：{prompt}",
    ]
    if alt:
        lines.append(f"↳ 备选 ,2：{alt}")
    tag = {"scene": scene, "gate": gate, "1": prompt}
    if alt:
        tag["2"] = alt
    lines.append(f"<!-- pilot: {json.dumps(tag, ensure_ascii=False)} -->")
    return "\n".join(lines)


ANSWER_HEADER = (
    "Created At: 2026-11-10 10:00:02\n"
    "Completed At: 2026-11-10 10:00:03\n\n"
)


def ask_args(count: int) -> dict:
    """Return a question call's `args` carrying `count` questions."""
    return {
        "args": {
            "questions": json.dumps(
                [{"question": f"Q{index}"} for index in range(1, count + 1)],
                ensure_ascii=False,
            )
        }
    }


def calls(root: Path) -> list[list[str]]:
    log = root / "orca-calls.jsonl"
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def send_calls(root: Path) -> list[list[str]]:
    return [item for item in calls(root) if item[:2] == ["terminal", "send"]]


def text_argument(argv: list[str]) -> str:
    try:
        return argv[argv.index("--text") + 1]
    except (ValueError, IndexError):
        return ""


def state_path(state: Path, conversation_id: str) -> Path:
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:24]
    return state / f"session-{digest}.json"


def read_state(state: Path, conversation_id: str) -> dict:
    path = state_path(state, conversation_id)
    return read_json(path) if path.is_file() else {}


class CompilerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(mock.patch.dict(
            os.environ, {"THE_PILOT_GEMINI_DIR": self.temp.name}
        ))
        self.base = load_default_profile()

    def test_render_and_merge_default_block(self) -> None:
        """The managed export must preserve explicit values and stay idempotent."""
        self.assertEqual(profile_compiler.render_shell_env_block(), SHELL_ENV_BLOCK)
        original = "# user shell settings\nalias ls='ls -h'\n"
        merged = profile_compiler.merge_shell_env(original)
        self.assertEqual(merged, original + "\n" + SHELL_ENV_BLOCK)
        self.assertEqual(profile_compiler.merge_shell_env(merged), merged)

        suffix = "export USER_SETTING=1\n\n"
        stale = SHELL_ENV_BLOCK.replace(":-off", ":-on")
        self.assertEqual(
            profile_compiler.merge_shell_env(original + stale + suffix),
            original + SHELL_ENV_BLOCK + suffix,
        )
        self.assertEqual(profile_compiler.merge_shell_env(merged, stale), original + "\n" + stale)

    def test_strip_preserves_user_content_and_tidies_seam(self) -> None:
        """Uninstall must remove only the block and excess blank lines at its seam."""
        before = "# user shell settings\r\nexport BEFORE=1\r\n"
        after = "export AFTER=1\r\n\r\n"
        strip = profile_compiler.strip_shell_env
        self.assertEqual(strip(SHELL_ENV_BLOCK), "")
        self.assertEqual(strip(before + "\r\n" + SHELL_ENV_BLOCK), before)
        self.assertEqual(
            strip(before + "\r\n\r\n" + SHELL_ENV_BLOCK + "\r\n" + after),
            before + "\r\n" + after,
        )
        self.assertEqual(strip(after), after)

    def test_malformed_markers_fail_closed(self) -> None:
        """An ambiguous marker range must never cause user shell text to be removed."""
        begin, _, end = SHELL_ENV_BLOCK.splitlines(keepends=True)
        for malformed in (
            begin, end, end + begin, begin + SHELL_ENV_BLOCK, SHELL_ENV_BLOCK * 2,
            "echo " + SHELL_ENV_BLOCK, SHELL_ENV_BLOCK.rstrip("\n") + " user comment\n",
        ):
            with self.subTest(malformed=malformed):
                for transform in (profile_compiler.merge_shell_env, profile_compiler.strip_shell_env):
                    with self.assertRaises(ValueError):
                        transform(malformed)

    def test_presets_cover_the_three_supported_profiles(self) -> None:
        self.assertEqual(
            set(DEFAULT_SPECS["presets"]),
            {"recommended", "conservative", "orchestration-only"},
        )
        recommended = resolve_profile(self.base, preset="recommended")
        self.assertEqual(recommended["pilot"]["mode"], "gate")
        self.assertEqual(recommended["pilot"]["max_consecutive_auto_turns"], 8)
        self.assertEqual(len(recommended["scenes"]["enabled_scenes"]), 4)
        self.assertEqual(len(recommended["shortcuts"]["enabled_entry"]), 7)
        config = compile_bundle(recommended).config_json
        feedback = config["entry_shortcuts"]["fb"]["template"]
        comma_feedback = next(command["template"] for command in config["comma_commands"].values() if ",fb" in command["aliases"])
        self.assertEqual(feedback, comma_feedback)
        for clause in ("$alter-feedback", "$debug", "直接执行 claim", "无需再询问", "端到端调用拓扑", "{arg}"):
            self.assertIn(clause, feedback)

        conservative = resolve_profile(self.base, preset="conservative")
        # resident-fact: admission: default delivery runs targeted dev E2E, retaining prod/suite gates.
        for preset in ("recommended", "conservative", "orchestration-only"):
            self.assertEqual(resolve_profile(self.base, preset=preset)["scenes"]["deliver_strategy"], "merge_dev_e2e")
        scene = compile_bundle(resolve_profile(self.base)).scenes["03-deliver-and-verify.md"]
        self.assertIn("部署后默认执行受影响链路的定向 dev E2E", scene)
        self.assertIn("当前环境不具备执行条件", scene)
        self.assertIn("全量/批量套件", scene)
        self.assertEqual(conservative["pilot"]["mode"], "confirm")
        self.assertEqual(conservative["pilot"]["max_consecutive_auto_turns"], 4)
        self.assertEqual(conservative["shortcuts"]["enabled_entry"], ["r", "rt", "d", "pull"])

        orchestration = resolve_profile(
            self.base, preset="orchestration-only"
        )
        self.assertEqual(orchestration["pilot"]["mode"], "off")
        self.assertEqual(orchestration["scenes"]["enabled_scenes"], [])
        self.assertEqual(orchestration["shortcuts"]["enabled_entry"], [])
        bundle = compile_bundle(orchestration)
        self.assertEqual(bundle.scenes, {})

    def test_legacy_profile_section_normalizes_once_and_dual_owners_fail(self) -> None:
        """Failure means an old profile keeps a second owner for mode and turn cap."""
        legacy = dict(self.base)
        legacy["schema"] = "orca-autopilot-profile/v1"
        legacy["autopilot"] = legacy.pop("pilot")
        legacy["orchestration"] = dict(self.base["orchestration"])
        normalized = resolve_profile(legacy, preset=None)
        self.assertEqual(normalized["schema"], "the-pilot-profile/v1")
        self.assertIn("pilot", normalized)
        self.assertNotIn("autopilot", normalized)
        self.assertEqual(normalized["pilot"], self.base["pilot"])

        ambiguous = dict(legacy)
        ambiguous["pilot"] = dict(self.base["pilot"])
        with self.assertRaises(ProfileError) as caught:
            resolve_profile(ambiguous, preset=None)
        self.assertIn("pilot", str(caught.exception))
        self.assertIn("autopilot", str(caught.exception))

    def test_four_dimension_profile_compiles_into_config_scenes_and_block(self) -> None:
        profile = resolve_profile(
            self.base,
            preset="recommended",
            overrides={
                "orchestration": {"worker_agents": ["codex"], "review_agents": ["agy", "codex"]},
                "pilot": {"mode": "confirm", "max_consecutive_auto_turns": 12},
                "scenes": {
                    "enabled_scenes": ["deliver-and-verify", "plan-and-orchestrate"],
                    "deliver_strategy": "merge_dev_e2e",
                    "enable_feedback_email": True,
                    "guardrails": ["必须补测试", "不得新增账本"],
                },
                "shortcuts": {
                    "enabled_entry": ["r", "d"],
                    "custom_entry": {
                        "go": {"template": "按 {arg} 推进", "requires_arg": True}
                    },
                    "custom_comma": {
                        "ts": {
                            "template": "先跑最小定向测试",
                            "aliases": [",t1"],
                            "scene": "deliver-and-verify",
                        }
                    },
                    "template_overrides": {
                        "r": "审查 {arg} 并报告",
                        ",ts": "覆盖后的测试模板",
                    },
                },
            },
        )
        self.assertEqual(profile["orchestration"]["worker_agents"], ["codex"])
        self.assertEqual(profile["orchestration"]["review_agents"], ["agy", "codex"])
        self.assertEqual(profile["pilot"]["mode"], "confirm")
        self.assertEqual(profile["pilot"]["max_consecutive_auto_turns"], 12)
        self.assertEqual(
            profile["scenes"]["enabled_scenes"],
            ["plan-and-orchestrate", "deliver-and-verify"],
        )
        bundle = compile_bundle(profile)
        self.assertEqual(
            sorted(bundle.scenes),
            ["01-plan-and-orchestrate.md", "03-deliver-and-verify.md"],
        )
        self.assertIn("merge_dev_e2e", bundle.scenes["03-deliver-and-verify.md"])
        self.assertIn("E2E", bundle.scenes["03-deliver-and-verify.md"])
        self.assertEqual(
            bundle.config_json["entry_shortcuts"]["r"]["template"], "审查 {arg} 并报告"
        )
        self.assertEqual(
            bundle.config_json["entry_shortcuts"]["go"]["template"], "按 {arg} 推进"
        )
        comma_templates = {
            alias: command["template"]
            for command in bundle.config_json["comma_commands"].values()
            for alias in command["aliases"]
        }
        self.assertEqual(comma_templates[",r"], "审查 {arg} 并报告")
        self.assertEqual(comma_templates[",go"], "按 {arg} 推进")
        self.assertEqual(comma_templates[",ts"], "覆盖后的测试模板")
        self.assertEqual(comma_templates[",t1"], "覆盖后的测试模板")
        self.assertEqual(
            bundle.config_json["entry_shortcuts"]["r"]["canonical_key"], "r"
        )
        ts_command = next(
            command
            for command in bundle.config_json["comma_commands"].values()
            if ",ts" in command["aliases"]
        )
        self.assertEqual(ts_command["canonical_key"], "ts")
        self.assertTrue(ts_command["command_id"].startswith("CMD_CUSTOM_COMMA_"))
        self.assertIn("必须补测试", bundle.gemini_block)
        self.assertIn("不得新增账本", bundle.gemini_block)

    def test_delivery_and_closeout_follow_the_active_repository_workflows(self) -> None:
        """Global scenes follow local repository rules and current authorization."""
        bundle = profile_compiler.compile_bundle(resolve_user_profile(self.base))
        delivery = bundle.scenes["03-deliver-and-verify.md"]
        closeout = bundle.scenes["04-closeout-and-sync.md"]
        for scene in (delivery, closeout):
            self.assertIn("AGENTS.md", scene)
            self.assertIn("CLAUDE.md", scene)
            self.assertIn("runbook", scene)
        self.assertIn("scenes.deliver_strategy", delivery)
        self.assertIn("发布交接", delivery)
        self.assertIn("台账", closeout)
        self.assertIn("当尚有已授权的结案", closeout)
        self.assertIn("外部发送仍遵循当前授权", closeout)
        self.assertIn("已授权的领域技能", closeout)
        self.assertIn("工作区生命周期脚本", closeout)

    def test_orchestration_text_carries_required_rules(self) -> None:
        bundle = compile_bundle(
            resolve_profile(self.base, preset="recommended")
        )
        text = bundle.gemini_block
        self.assertIn(HARNESS_ORCHESTRATION_SENTENCE, text)
        self.assertIn("Agent 单一语义 Owner 三步判断模型", text)
        self.assertIn("仅文件组织可调整，理由在 worker_done 汇报", text)
        self.assertIn("worker_done", text)
        self.assertIn("未经确认改变安全门禁、内部跨模块/外部合同及状态机边界", text)
        self.assertIn("不改变未获授权的安全门禁或跨模块/外部合同边界", text)
        self.assertNotIn("文件行数上限", text)
        self.assertIn("提问未答", text)
        self.assertNotIn("调用 `ask_question` 并输出 `scene=NONE`", text)
        self.assertNotIn("调用 `ask_question` 时必须 `gate=pause`", text)
        self.assertTrue(text.startswith(profile_compiler.GEMINI_BEGIN))
        self.assertLess(len(bundle.gemini_block.encode("utf-8")), 20000)
        self.assertIn("- **,1（主推）**：", bundle.gemini_block)
        self.assertNotIn("\n- 主推：", bundle.gemini_block)
        self.assertEqual(len(bundle.scenes), 4)
        for scene in bundle.scenes.values():
            self.assertIn("auto_in_gate_mode: true", scene)
        closeout = bundle.scenes["04-closeout-and-sync.md"]
        self.assertIn("alert_transition.py close", closeout)
        self.assertIn("prod 无需发版已核验", closeout)
        self.assertIn("gate=auto", closeout)
        for obligation in (
            "pending-prod-publish.md", "WORKING.md", "worker-release",
            "terminal-state reclaimable", "cleanup-stale-worktrees.py",
            "origin/main", "session.py release", "不删目录分支",
            "docs/backlog/*-complete.md", "scene=NONE",
        ):
            self.assertIn(obligation, closeout)
        self.assertEqual(
            text.index(profile_compiler.SCENES_BEGIN)
            < text.index(profile_compiler.SCENES_END)
            < text.index(profile_compiler.GEMINI_END),
            True,
        )

    def test_coordinator_agent_normalizes_and_legacy_profiles_default_to_agy(self) -> None:
        # Failure means an installed profile without coordinator_agent stops
        # validating, or a non-AGY value silently becomes the Tier-1 primary.
        legacy = copy.deepcopy(self.base)
        legacy["orchestration"].pop("coordinator_agent", None)
        normalized = resolve_profile(legacy, preset=None)
        self.assertEqual(normalized["orchestration"]["coordinator_agent"], "agy")
        for value in ("agy", "antigravity", " Antigravity "):
            with self.subTest(value=value):
                profile = resolve_profile(
                    self.base,
                    preset=None,
                    overrides={"orchestration": {"coordinator_agent": value}},
                )
                self.assertEqual(
                    profile["orchestration"]["coordinator_agent"], "agy"
                )
        for value in ("claude", "codex", "gemini", ""):
            with self.subTest(value=value):
                with self.assertRaises(ProfileError):
                    resolve_profile(
                        self.base,
                        preset=None,
                        overrides={"orchestration": {"coordinator_agent": value}},
                    )

    def test_two_tier_guidance_survives_worker_hot_switch(self) -> None:
        # Failure means generated guidance can send coordinators down an invalid
        # dispatch path, take mail before Orca wakes them, or drift on `,worker`.
        profile = resolve_profile(self.base, preset="recommended")
        section = profile_compiler.render_orchestration_section(profile)
        for required in (
            "Tier-1 主会话",
            "Tier-2 子会话",
            "agy --dangerously-skip-permissions",
            "agy-switch",
            "magpie agy",
            "trustedWorkspaces",
            "严禁以 `claude`/`codex` 启动或降级充当 Tier-1 主会话",
            'worker-start --spec "<任务契约>" --worktree current --agent claude --json',
            'worker-start --spec "<任务契约>" --worktree new-child --name <name> --agent claude --setup run --json',
            'worker-start --spec "<任务契约>" --worktree new-child --repo path:<目标仓库根> --name <name> --agent claude --setup run --json',
            "--spec-file <路径>",
            "--setup skip",
            "scene=NONE",
            "Orca 原生唤醒",
            "orca orchestration check --environment local --json",
            "自动标记已处理",
            "reply --id <msg_id>",
            'orca orchestration send --environment local --to dispatch:<dispatch_id> --subject "<主题>" --body-file <路径> --json',
            "worker_done",
            "仅文件组织可调整",
            "不得降低最简领域模型",
            "orca orchestration ask",
            "`reply` 不得默许",
            "双表/双状态机",
            "Python 封装或排版",
            "旁路表或状态机",
            "领域入口与核心实体真值",
            "对照业务目标与 Anchor 0 蓝图",
            "真实 diff 与改动后模块全貌",
            "重读起点、盘点现状、零基对账",
        ):
            self.assertIn(required, section)
        self.assertNotIn("check --wait", section)
        self.assertNotIn("--ack", section)
        self.assertNotIn(
            "worker-start --worktree path:<子工作区路径> --terminal <handle>", section
        )
        for initial, target in (
            (["claude", "codex"], ["codex", "claude"]),
            (["claude"], ["claude", "codex"]),
            (["codex"], ["codex", "claude"]),
        ):
            with self.subTest(initial=initial, target=target):
                block = profile_compiler.render_orchestration_block(
                    resolve_profile(
                        self.base,
                        preset=None,
                        overrides={"orchestration": {"worker_agents": initial}},
                    )
                )
                expected = profile_compiler.render_orchestration_block(
                    resolve_profile(
                        self.base,
                        preset=None,
                        overrides={"orchestration": {"worker_agents": target}},
                    )
                )
                self.assertEqual(
                    runtime.update_orchestration_guidance(block, target), expected
                )

    def test_orchestration_block_merge_preserves_prose_and_strips_cleanly(self) -> None:
        # Failure means install/uninstall rewrites unrelated global agent
        # prose, appends a second managed block, or guesses at malformed markers.
        block = profile_compiler.render_orchestration_block(
            resolve_profile(self.base, preset=None)
        )
        existing = "# 我的全局规则\n\n始终使用中文。\n"
        merged = profile_compiler.merge_orchestration_md(existing, block)
        self.assertTrue(merged.startswith(existing))
        self.assertIn(profile_compiler.ORCHESTRATION_BEGIN, merged)
        self.assertIn(profile_compiler.ORCHESTRATION_END, merged)
        self.assertEqual(profile_compiler.merge_orchestration_md(merged, block), merged)
        self.assertEqual(profile_compiler.strip_orchestration_md(merged), existing)
        self.assertEqual(profile_compiler.merge_orchestration_md("", block), block)
        embedded = "before\n\n" + block + "\nafter\n"
        self.assertEqual(
            profile_compiler.merge_orchestration_md(embedded, block), embedded
        )
        self.assertEqual(
            profile_compiler.strip_orchestration_md(embedded), "before\n\nafter\n"
        )
        for malformed in (
            profile_compiler.ORCHESTRATION_BEGIN + "\n",
            profile_compiler.ORCHESTRATION_END + "\n",
            profile_compiler.ORCHESTRATION_END
            + "\n"
            + profile_compiler.ORCHESTRATION_BEGIN
            + "\n",
            block + block,
        ):
            with self.subTest(malformed=malformed[:40]):
                with self.assertRaises(ValueError):
                    profile_compiler.merge_orchestration_md(malformed, block)
                with self.assertRaises(ValueError):
                    profile_compiler.strip_orchestration_md(malformed)

    def test_orchestration_merge_migrates_one_legacy_section(self) -> None:
        # Failure means an upgrade keeps the pre-managed orchestration section
        # beside the managed block, so `,worker` updates one copy while the
        # stale priority (including trailing item 6 prose) survives unnoticed.
        block = profile_compiler.render_orchestration_block(
            resolve_profile(self.base, preset=None)
        )
        heading = profile_compiler.LEGACY_ORCHESTRATION_HEADING
        legacy = (
            "# 我的全局规则\n\n始终使用中文。\n\n"
            f"{heading}AGY > Claude Code）\n\n"
            "5. 旧的交互闭环说明。\n\n"
            "6. 旧版收尾说明必须被迁移掉。\n\n"
            "## 其他章节\n\n这一段必须保留。\n"
        )
        merged = profile_compiler.merge_orchestration_md(legacy, block)
        self.assertEqual(merged.count(profile_compiler.ORCHESTRATION_BEGIN), 1)
        self.assertEqual(merged.count(profile_compiler.ORCHESTRATION_END), 1)
        self.assertEqual(merged.count(heading), 1)
        self.assertNotIn("6. 旧版收尾说明", merged)
        self.assertIn("始终使用中文。", merged)
        self.assertIn("## 其他章节\n\n这一段必须保留。", merged)
        self.assertEqual(profile_compiler.merge_orchestration_md(merged, block), merged)
        self.assertEqual(
            profile_compiler.strip_orchestration_md(merged),
            "# 我的全局规则\n\n始终使用中文。\n\n## 其他章节\n\n这一段必须保留。\n",
        )

        duplicated = legacy + "\n" + f"{heading}Codex > Claude Code）\n旧规则二。\n"
        with self.assertRaises(ValueError):
            profile_compiler.merge_orchestration_md(duplicated, block)

        fenced = (
            "# 我的全局规则\n\n```markdown\n"
            f"{heading}AGY > Claude Code）\n```\n"
        )
        merged_fenced = profile_compiler.merge_orchestration_md(fenced, block)
        self.assertIn(f"```markdown\n{heading}AGY > Claude Code）\n```", merged_fenced)
        self.assertEqual(merged_fenced.count(profile_compiler.ORCHESTRATION_BEGIN), 1)

    def test_compiled_guidance_separates_synchronous_and_async_questions(self) -> None:
        # Failure means the guidance again conflates an in-turn synchronous
        # question tool call with ending the turn to await an external answer,
        # or the universal block stops being project-neutral or exceeds the
        # byte budget the installed GEMINI.md must respect.
        profile = resolve_profile(self.base, preset="recommended")
        universal = profile_compiler.render_pilot_section(
            resolve_user_profile(self.base, preset="recommended"), 4
        )
        self.assertEqual(
            universal, profile_compiler.render_pilot_section(profile, 4)
        )
        self.assertNotIn("alter", universal.lower())
        self.assertNotIn("seabass", universal.lower())
        for clause in (
            "提问未答",
            "，同步澄清直接调 `ask_question`（同一步不写标签，禁止停在悬空 `：`），异步`提问未答`输出 `scene=NONE`；",
            '异步提问未答或结案输出 `<!-- pilot: {"scene": "NONE"} -->`',
        ):
            self.assertIn(clause, universal)

        bundle = compile_bundle(profile)
        plan_scene = bundle.scenes["01-plan-and-orchestrate.md"]
        self.assertIn(
            "同步提问直接调 `ask_question`（不写标签），异步待答输出 `scene=NONE`",
            plan_scene,
        )
        # Failure means diagnosis can skip design/review and trigger coding;
        # both universal and project guidance must preserve progression and the
        # same authorization boundary at every stage.
        for scene in (plan_scene, profile_compiler.compile_bundle(
            resolve_user_profile(self.base, preset="recommended")
        ).scenes["01-plan-and-orchestrate.md"]):
            steps = [scene.index(f"**进度 {step}") for step in ("①", "②", "③")]
            self.assertEqual(steps, sorted(steps))
            for clause in (
                "按当前实际进度单步递进，严禁跳步合并",
                "严禁直接编排子工作区改代码",
                "产出详细的工程架构修复/收敛方案",
                "严禁未经方案 review 直接启动实施",
                "方案级最简架构 Review",
                "按已审定方案通过 Orca 编排子工作区推进实施",
                "进度 ① → ② → ③ 均按",
                "A 类及已确认边界内的 B 类 `gate=auto` 单步递进",
                "B 类方案/裁决未确认 `gate=pause`",
                "对方案做最简架构 review 后先停下等我确认再进入实施",
            ):
                self.assertIn(clause, scene)
            self.assertNotIn("跳过方案复审", scene)
            self.assertNotIn("进度 ①、② 默认 `gate=auto`", scene)
        self.assertIn("B 类方案/裁决未确认标 `gate=pause`", universal)
        self.assertIn("已确认边界内 `gate=auto` 单步递进", universal)
        self.assertIn("定位 → 出详细方案 → 方案级架构 review → 编排实施", universal)
        self.assertIn("不得跳过方案设计或方案复审", universal)
        self.assertLess(len(bundle.gemini_block.encode("utf-8")), 20000)

    def test_compiled_scenes_require_zero_based_design_and_whole_module_review(self) -> None:
        # Failure means the agent can miss the domain model or skip whole-module
        # reconciliation, including when a project supplies its own scene prose.
        for project in (None, PROJECT):
            with self.subTest(project=(project or {}).get("projectId")):
                profile = resolve_user_profile(self.base, preset="recommended", project=project)
                bundle = profile_compiler.compile_bundle(profile, project=project)
                plan = bundle.scenes["01-plan-and-orchestrate.md"]
                review = bundle.scenes["02-architecture-re-review.md"]
                self.assertEqual(review.count("## 场景判断与构造说明"), 1)
                self.assertEqual(review.count("复审范围与治本判断"), 1)
                self.assertIn("1. **复审范围与治本判断**", review)
                for clause in (
                    "从业务目标零基推导改动后最简领域模型",
                    "唯一 owner、核心实体与状态机",
                    "写入原子收敛、读取直接投影",
                    "权威实体生命周期或读投影",
                    "不得新增平行状态机或旁路流程表",
                    "消除冗余表、拆分状态机与跨表副作用",
                ):
                    self.assertIn(clause, plan)
                    self.assertIn(clause, bundle.gemini_block)
                steps = (
                    "① 显式重读起点文档（Anchor 0",
                    "② 盘点改动后受影响模块全貌（Whole-Module Reality",
                    "③ 从业务目标做零基对账",
                )
                offsets = [review.index(step) for step in steps]
                self.assertEqual(offsets, sorted(offsets))
                for clause in (
                    "最初审查报告、原始业务需求或初版架构设计",
                    "严禁仅看子计划、指标表或凭记忆",
                    "存活表/Schema、状态机、子包职责、触发器、读写主链",
                    "严禁只看本轮 diff",
                    "每张表、每个状态机和调用链是否最简",
                    "扩展另起旁路表/平行状态机",
                    "只合函数不合表",
                    "压代码排版凑数",
                    "判未达标并打回源头重构",
                    "三步复审通过且架构与质量问题清零后才可进入交付（含 `,2`）",
                ):
                    self.assertIn(clause, review)
                    self.assertIn(clause, bundle.gemini_block)
                if project is not None:
                    self.assertIn("Dev 反馈/线上报警等修复任务", review)

    def test_compiled_byte_budget_across_presets_and_strategies(self) -> None:
        # Failure means generated instructions exceed the reserved GEMINI.md
        # budget once two guardrails and optional feedback guidance are present.
        for project, limit in ((None, 18_200), (PROJECT, 19_600 - 1)):
            for preset in DEFAULT_SPECS["presets"]:
                for strategy in DELIVER_STRATEGIES:
                    for feedback in (False, True):
                        with self.subTest(
                            project=(project or {}).get("projectId"), preset=preset,
                            strategy=strategy, feedback=feedback,
                        ):
                            profile = resolve_user_profile(
                                self.base, preset=preset, project=project,
                                overrides={"scenes": {
                                    "deliver_strategy": strategy,
                                    "enable_feedback_email": feedback,
                                    "guardrails": ["必须补测试", "不得新增账本"],
                                }},
                            )
                            bundle = profile_compiler.compile_bundle(profile, project=project)
                            self.assertLessEqual(len(bundle.gemini_block.encode("utf-8")), limit)

    def test_gemini_merge_is_non_destructive_and_idempotent(self) -> None:
        block = compile_bundle(
            resolve_profile(self.base)
        ).gemini_block
        existing = "# 我的规则\n\n这一段必须保留。\n"
        merged = profile_compiler.merge_gemini_md(existing, block)
        self.assertIn("这一段必须保留。", merged)
        self.assertIn(block, merged)
        self.assertEqual(profile_compiler.merge_gemini_md(merged, block), merged)
        stripped = profile_compiler.strip_gemini_md(merged)
        self.assertIn("这一段必须保留。", stripped)
        self.assertNotIn(profile_compiler.GEMINI_BEGIN, stripped)
        self.assertEqual(profile_compiler.strip_gemini_md(""), "")
        with self.assertRaises(ValueError):
            profile_compiler.merge_gemini_md(merged + merged, block)

    def test_gemini_merge_rejects_scene_markers_outside_the_managed_block(self) -> None:
        # Failure means a legacy manual scenes segment survives next to ours,
        # growing GEMINI.md past the CLI limit and breaking `runtime.py sync`.
        block = compile_bundle(
            resolve_profile(self.base)
        ).gemini_block
        self.assertIn(profile_compiler.SCENES_BEGIN, block)
        merged = profile_compiler.merge_gemini_md("# 用户规则\n", block)
        self.assertEqual(profile_compiler.merge_gemini_md(merged, block), merged)

        legacy = (
            "# 用户规则\n\n旧手工场景段：\n\n"
            f"{profile_compiler.SCENES_BEGIN}\n旧场景\n{profile_compiler.SCENES_END}\n"
        )
        for case, existing in (
            ("fresh", legacy),
            ("already-installed", merged + "\n" + legacy),
        ):
            with self.subTest(case=case):
                with self.assertRaises(ValueError) as caught:
                    profile_compiler.merge_gemini_md(existing, block)
                self.assertIn("未托管的场景标记", str(caught.exception))

    def test_hooks_merge_preserves_other_groups_and_strip_removes_only_ours(self) -> None:
        existing = {"orca-status": {"Stop": [{"type": "command", "command": "true"}]}}
        merged = profile_compiler.merge_hooks_json(existing, "/tmp/engine/runtime.py")
        self.assertEqual(merged["orca-status"], existing["orca-status"])
        group = merged["the-pilot"]
        self.assertEqual(group["PreInvocation"][0]["timeout"], 10)
        self.assertIn("/tmp/engine/runtime.py", group["PreInvocation"][0]["command"])
        self.assertIn("--event PreInvocation", group["PreInvocation"][0]["command"])
        self.assertIn("--event Stop", group["Stop"][0]["command"])
        stripped = profile_compiler.strip_hooks_json(merged)
        self.assertEqual(stripped, existing)

    def test_compile_produces_only_managed_shortcut_config(self) -> None:
        profile = resolve_profile(self.base)
        bundle = compile_bundle(profile)
        self.assertEqual(set(bundle.config_json), {"entry_shortcuts", "comma_commands"})

    def test_validation_rejects_unknown_fields_and_reserved_aliases(self) -> None:
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base, overrides={"pilot": {"mode": "bogus"}}
            )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base, overrides={"pilot": {"unknown": 1}}
            )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base, overrides={"shortcuts": {"custom_comma": {"mode": "x"}}}
            )
        for reserved in ("pilot", "autopilot"):
            with self.assertRaises(profile_compiler.ProfileError):
                resolve_profile(
                    self.base,
                    overrides={"shortcuts": {"custom_entry": {reserved: "x"}}},
                )
            with self.assertRaises(profile_compiler.ProfileError):
                resolve_profile(
                    self.base,
                    overrides={
                        "shortcuts": {"custom_comma": {reserved: {"template": "x"}}}
                    },
                )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base,
                overrides={
                    "shortcuts": {
                        "custom_comma": {
                            "ts": {"template": "x", "aliases": [",t1"]},
                            "t1": {"template": "y"},
                        }
                    }
                },
            )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base,
                overrides={"shortcuts": {"template_overrides": {"missing": "x"}}},
            )

        # Shortcut identities are ASCII tokens, not Unicode lookalikes or lines.
        for key in ("Ａbc", "a.b", "a\nb", "9entry"):
            with self.assertRaises(profile_compiler.ProfileError):
                resolve_profile(self.base, overrides={"shortcuts": {"custom_entry": {key: "x"}}})
        for alias in (",é", ",a.b", ",a\nb", ",-bad"):
            with self.assertRaises(profile_compiler.ProfileError):
                resolve_profile(self.base, overrides={"shortcuts": {"custom_comma": {"ok": {"template": "x", "aliases": [alias]}}}})

    def test_command_identity_collisions_and_reserved_entry_are_rejected(self) -> None:
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base,
                overrides={
                    "shortcuts": {
                        "custom_entry": {"a-b": "x", "a_b": "y"},
                    }
                },
            )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base,
                overrides={"shortcuts": {"custom_entry": {"mode": "x"}}},
            )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base,
                overrides={
                    "shortcuts": {
                        "custom_comma": {"a-b": "x", "a_b": "y"},
                    }
                },
            )

    def test_override_additional_alias_is_canonicalized(self) -> None:
        profile = resolve_profile(
            self.base,
            overrides={
                "shortcuts": {
                    "custom_comma": {
                        "ts": {
                            "template": "默认测试模板",
                            "aliases": [",t1"],
                        }
                    },
                    "template_overrides": {",t1": "别名覆盖"},
                }
            },
        )
        self.assertEqual(
            profile["shortcuts"]["template_overrides"], {"ts": "别名覆盖"}
        )
        bundle = compile_bundle(profile)
        command = next(
            item
            for item in bundle.config_json["comma_commands"].values()
            if ",ts" in item["aliases"]
        )
        self.assertEqual(command["template"], "别名覆盖")
        self.assertEqual(command["aliases"], [",ts", ",t1"])
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base,
                overrides={
                    "shortcuts": {
                        "custom_comma": {
                            "ts": {
                                "template": "默认测试模板",
                                "aliases": [",t1"],
                            }
                        },
                        "template_overrides": {",ts": "一", ",t1": "二"},
                    }
                },
            )

    def test_hook_command_shell_quotes_engine_path(self) -> None:
        engine = '/tmp/engine $(touch nope) "quoted"/runtime.py'
        merged = profile_compiler.merge_hooks_json({}, engine)
        command = merged["the-pilot"]["PreInvocation"][0]["command"]
        parts = shlex.split(command)
        self.assertEqual(parts[0], "python3")
        self.assertEqual(Path(parts[1]), Path(engine).expanduser().resolve())
        self.assertIn("--event PreInvocation", command)


class SetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.gemini = self.root / "gemini"
        fake_orca(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def seed_user_files(self, gemini: Path | None = None) -> None:
        config_dir = (gemini or self.gemini) / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "GEMINI.md").write_text(
            "# 用户规则\n\n保留这一段。\n", encoding="utf-8"
        )
        write_json(
            config_dir / "hooks.json",
            {"orca-status": {"Stop": [{"type": "command", "command": "true"}]}},
        )

    def seed_legacy_install(self, gemini: Path, *, unmanaged: bool = False) -> Path:
        """Create a populated pre-migration ``config/autopilot`` install."""
        self.seed_user_files(gemini)
        legacy_dir = gemini / "config" / "autopilot"
        (legacy_dir / "state").mkdir(parents=True)
        (legacy_dir / "scenes").mkdir(parents=True)
        (legacy_dir / "__pycache__").mkdir()
        profile = load_default_profile()
        profile["schema"] = "orca-autopilot-profile/v1"
        profile["autopilot"] = profile.pop("pilot")
        profile["orchestration"]["worker_agents"] = ["codex"]
        profile["autopilot"]["mode"] = "confirm"
        profile["autopilot"]["max_consecutive_auto_turns"] = 5
        write_json(legacy_dir / "profile.json", profile)
        write_json(legacy_dir / "config.json", {"entry_shortcuts": {}})
        write_json(legacy_dir / "project.json", PROJECT)
        (legacy_dir / "autopilot.py").write_text("# legacy engine\n", encoding="utf-8")
        (legacy_dir / ".autopilot.lock").write_text("", encoding="utf-8")
        (legacy_dir / "__pycache__" / "autopilot.cpython-311.pyc").write_text("old", encoding="utf-8")
        (legacy_dir / "state" / "autopilot.log").write_text(
            '{"event": "legacy_log"}\n', encoding="utf-8"
        )
        (legacy_dir / "state" / "session-abc.json").write_text(
            '{"conversation_id": "legacy"}\n', encoding="utf-8"
        )
        (legacy_dir / "state" / "session-abc.lock").write_text("", encoding="utf-8")
        (legacy_dir / "scenes" / "01-plan-and-orchestrate.md").write_text(
            "---\nname: plan-and-orchestrate\n---\nlegacy\n", encoding="utf-8"
        )
        (legacy_dir / "scenes" / "05-custom.md").write_text(
            "---\nname: custom\n---\nkeep custom\n", encoding="utf-8"
        )
        gemini_md = gemini / GEMINI_REL
        gemini_md.write_text(
            gemini_md.read_text(encoding="utf-8")
            + "\n"
            + profile_compiler.LEGACY_GEMINI_BEGIN
            + "\n## 个人研发状态机与 Turn Autopilot 规范\n\n旧指引\n\n"
            + profile_compiler.LEGACY_SCENES_BEGIN
            + "\n旧场景\n"
            + profile_compiler.LEGACY_SCENES_END
            + "\n"
            + profile_compiler.LEGACY_GEMINI_END
            + "\n",
            encoding="utf-8",
        )
        hooks_path = gemini / HOOKS_REL
        hooks = read_json(hooks_path)
        hooks["orca-agy-autopilot"] = {
            "Stop": [{"type": "command", "command": "python3 old/autopilot.py hook --event Stop"}]
        }
        write_json(hooks_path, hooks)
        if unmanaged:
            (legacy_dir / "notes.txt").write_text("keep", encoding="utf-8")
        return legacy_dir

    def test_apply_installs_every_artifact_and_preserves_user_files(self) -> None:
        self.seed_user_files()
        result = apply_profile(self.root, self.gemini, "--preset", "recommended")
        self.assertEqual(result["preset"], "recommended")
        engine = self.gemini / ENGINE_REL
        self.assertTrue(engine.is_file())
        self.assertEqual(engine.stat().st_mode & 0o777, 0o755)
        self.assertEqual(len(list((self.gemini / SCENES_REL).glob("*.md"))), 4)
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(profile["pilot"]["mode"], "gate")
        config = read_json(self.gemini / CONFIG_REL)
        self.assertEqual(
            set(config), {"entry_shortcuts", "comma_commands"}
        )
        gemini_text = (self.gemini / GEMINI_REL).read_text(encoding="utf-8")
        self.assertIn("保留这一段。", gemini_text)
        self.assertIn(profile_compiler.GEMINI_BEGIN, gemini_text)
        hooks = read_json(self.gemini / HOOKS_REL)
        self.assertIn("orca-status", hooks)
        self.assertIn("the-pilot", hooks)
        self.assertIn(str(engine), hooks["the-pilot"]["Stop"][0]["command"])

        status = run_setup(self.root, self.gemini, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertTrue(report["installed"])
        self.assertEqual(report["drift"], [])
        self.assertTrue(report["engine"]["up_to_date"])

    def test_apply_custom_profile_writes_selected_options(self) -> None:
        pilot_dir = self.gemini / "config" / "the-pilot"
        pilot_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            pilot_dir / "config.json",
            {"extra": 1},
        )
        custom = json.dumps(
            {
                "orchestration": {
                    "worker_agents": ["codex"],
                    "review_agents": ["agy", "codex"],
                }
            },
            ensure_ascii=False,
        )
        apply_profile(
            self.root,
            self.gemini,
            "--profile",
            custom,
            "--mode",
            "confirm",
            "--max-turns",
            "5",
            "--scenes",
            "plan-and-orchestrate",
            "--entry-shortcuts",
            "r",
            "--deliver-strategy",
            "local_verify_only",
            "--no-feedback-email",
        )
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(profile["orchestration"]["worker_agents"], ["codex"])
        self.assertEqual(profile["orchestration"]["review_agents"], ["agy", "codex"])
        self.assertEqual(profile["pilot"]["mode"], "confirm")
        self.assertEqual(profile["pilot"]["max_consecutive_auto_turns"], 5)
        self.assertEqual(profile["scenes"]["enabled_scenes"], ["plan-and-orchestrate"])
        self.assertEqual(
            sorted(path.name for path in (self.gemini / SCENES_REL).glob("*.md")),
            ["01-plan-and-orchestrate.md"],
        )
        config = read_json(self.gemini / CONFIG_REL)
        self.assertNotIn("extra", config)
        self.assertEqual(sorted(config["entry_shortcuts"]), ["r"])

    def test_upgrade_reuses_the_installed_profile(self) -> None:
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        profile_path = self.gemini / PROFILE_REL
        profile = read_json(profile_path)
        profile["orchestration"]["worker_agents"] = ["codex"]
        profile["pilot"]["mode"] = "confirm"
        write_json(profile_path, profile)
        engine_before = (self.gemini / ENGINE_REL).read_bytes()

        apply_profile(self.root, self.gemini, "--upgrade")
        upgraded = read_json(profile_path)
        self.assertEqual(upgraded["orchestration"]["worker_agents"], ["codex"])
        self.assertEqual(upgraded["pilot"]["mode"], "confirm")
        self.assertEqual((self.gemini / ENGINE_REL).read_bytes(), engine_before)

        conflict = run_setup(
            self.root, self.gemini, "apply", "--upgrade", "--profile", "{}"
        )
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("--upgrade", conflict.stderr)

        preset_conflict = run_setup(
            self.root, self.gemini, "apply", "--upgrade", "--preset", "conservative"
        )
        self.assertEqual(preset_conflict.returncode, 2)
        self.assertIn("--upgrade", preset_conflict.stderr)
        unchanged = read_json(profile_path)
        self.assertEqual(unchanged["orchestration"]["worker_agents"], ["codex"])
        self.assertEqual(unchanged["pilot"]["mode"], "confirm")

    def test_upgrade_without_installed_profile_fails(self) -> None:
        result = run_setup(self.root, self.gemini, "apply", "--upgrade")
        self.assertEqual(result.returncode, 2)
        self.assertIn("profile.json", result.stderr)
        self.assertFalse((self.gemini / ENGINE_REL).is_file())

    def test_upgrade_migrates_legacy_autopilot_install(self) -> None:
        """Failure means a pre-rename install keeps a second profile, hook group or state tree.

        resident-fact: settlement: one apply --upgrade moves managed legacy
        artifacts and durable state into config/the-pilot without data loss,
        and leaves unknown user files in config/autopilot untouched.
        """
        legacy_dir = self.seed_legacy_install(self.gemini)

        before = json.loads(run_setup(self.root, self.gemini, "status").stdout)
        self.assertTrue(
            any("旧版 config/autopilot" in item for item in before["drift"]),
            before["drift"],
        )

        apply_profile(self.root, self.gemini, "--upgrade")

        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(profile["schema"], "the-pilot-profile/v1")
        self.assertNotIn("autopilot", profile)
        self.assertEqual(profile["pilot"]["mode"], "confirm")
        self.assertEqual(profile["pilot"]["max_consecutive_auto_turns"], 5)
        self.assertTrue((self.gemini / ENGINE_REL).is_file())
        self.assertFalse((legacy_dir / "autopilot.py").exists())

        hooks = read_json(self.gemini / HOOKS_REL)
        self.assertIn("the-pilot", hooks)
        self.assertNotIn("orca-agy-autopilot", hooks)
        self.assertIn("orca-status", hooks)

        gemini_text = (self.gemini / GEMINI_REL).read_text(encoding="utf-8")
        self.assertIn("保留这一段。", gemini_text)
        self.assertEqual(gemini_text.count(profile_compiler.GEMINI_BEGIN), 1)
        self.assertNotIn(profile_compiler.LEGACY_GEMINI_BEGIN, gemini_text)
        self.assertNotIn(profile_compiler.LEGACY_SCENES_BEGIN, gemini_text)

        state_dir = self.gemini / "config/the-pilot/state"
        self.assertIn("legacy_log", (state_dir / "pilot.log").read_text(encoding="utf-8"))
        self.assertEqual(
            read_json(state_dir / "session-abc.json")["conversation_id"], "legacy"
        )
        self.assertFalse((legacy_dir / "state").exists())
        self.assertFalse((legacy_dir / "scenes" / "05-custom.md").exists())
        self.assertTrue((self.gemini / SCENES_REL / "05-custom.md").is_file())
        self.assertFalse(legacy_dir.exists())

        report = json.loads(run_setup(self.root, self.gemini, "status").stdout)
        self.assertEqual(report["drift"], [])

        other = self.root / "gemini-unmanaged"
        unmanaged_dir = self.seed_legacy_install(other, unmanaged=True)
        apply_profile(self.root, other, "--upgrade")
        self.assertEqual((unmanaged_dir / "notes.txt").read_text(encoding="utf-8"), "keep")
        self.assertFalse((unmanaged_dir / "profile.json").exists())
        self.assertFalse((unmanaged_dir / "autopilot.py").exists())
        self.assertEqual(
            json.loads(run_setup(self.root, other, "status").stdout)["drift"], []
        )

    def test_upgrade_mode_override_merges_into_legacy_profile(self) -> None:
        """Failure means apply --upgrade forks a second mode owner or drops the turn cap.

        resident-fact: settlement: overrides merge into a pre-rename profile
        after its autopilot section is normalized, so one canonical pilot
        section carries the new mode and the legacy max_consecutive_auto_turns.
        """
        self.seed_legacy_install(self.gemini)
        apply_profile(self.root, self.gemini, "--upgrade", "--mode", "gate")
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(profile["schema"], "the-pilot-profile/v1")
        self.assertNotIn("autopilot", profile)
        self.assertEqual(profile["pilot"]["mode"], "gate")
        self.assertEqual(profile["pilot"]["max_consecutive_auto_turns"], 5)

    def test_status_requires_canonical_profile_despite_legacy_tree(self) -> None:
        """Failure means status claims an install the runtime cannot read.

        resident-fact: inspection: status reports installed=false plus the
        legacy-tree drift when config/the-pilot/runtime.py exists alongside a
        legacy profile, while the runtime reads only config/the-pilot.
        """
        legacy_dir = self.seed_legacy_install(self.gemini)
        engine = self.gemini / ENGINE_REL
        engine.parent.mkdir(parents=True, exist_ok=True)
        engine.write_text("# canonical engine\n", encoding="utf-8")

        report = json.loads(run_setup(self.root, self.gemini, "status").stdout)

        self.assertFalse(report["installed"])
        self.assertTrue((legacy_dir / "profile.json").is_file())
        self.assertTrue(
            any("旧版 config/autopilot" in item for item in report["drift"]),
            report["drift"],
        )
        self.assertIn("profile.json 缺失", report["drift"])

    def test_reapply_preserves_installed_profile_without_upgrade(self) -> None:
        """Bare re-apply keeps personal preferences instead of factory defaults.

        Failure means `pilot apply` (also with `--project`) silently resets
        worker order, autopilot mode, turn cap, delivery policy, disabled
        entries, custom shortcuts and template overrides the user already chose.
        """
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        profile_path = self.gemini / PROFILE_REL
        profile = read_json(profile_path)
        profile["orchestration"]["worker_agents"] = ["codex", "claude"]
        profile["pilot"]["mode"] = "confirm"
        profile["pilot"]["max_consecutive_auto_turns"] = 5
        profile["scenes"]["deliver_strategy"] = "local_verify_only"
        profile["scenes"]["enable_feedback_email"] = True
        profile["shortcuts"]["enabled_entry"] = ["cq", "fb", "al", "r", "rt", "d"]
        profile["shortcuts"]["custom_entry"] = {"req": "request {arg}"}
        profile["shortcuts"]["custom_comma"] = {
            "ts": {"template": "先跑最小定向测试", "aliases": [",t1"]}
        }
        profile["shortcuts"]["template_overrides"] = {
            "r": "自定义 review {arg}",
            "req": "自定义 request {arg}",
        }
        write_json(profile_path, profile)

        result = apply_profile(self.root, self.gemini)
        applied = read_json(profile_path)
        self.assertEqual(result["preset"], "installed-profile")
        self.assertEqual(result["worker_agents"], ["codex", "claude"])
        self.assertEqual(result["mode"], "confirm")
        self.assertEqual(result["max_consecutive_auto_turns"], 5)
        self.assertEqual(result["deliver_strategy"], "local_verify_only")
        self.assertEqual(applied["orchestration"]["worker_agents"], ["codex", "claude"])
        self.assertEqual(
            applied["pilot"],
            {"mode": "confirm", "max_consecutive_auto_turns": 5},
        )
        self.assertEqual(applied["scenes"]["deliver_strategy"], "local_verify_only")
        self.assertNotIn("enable_feedback_email", applied["scenes"])
        self.assertEqual(
            applied["shortcuts"]["enabled_entry"],
            ["r", "rt", "d", "cq", "fb", "al"],
        )
        self.assertEqual(
            applied["shortcuts"]["custom_entry"]["req"]["template"], "request {arg}"
        )
        self.assertEqual(
            applied["shortcuts"]["custom_comma"]["ts"]["template"],
            "先跑最小定向测试",
        )
        self.assertEqual(
            applied["shortcuts"]["template_overrides"],
            {"r": "自定义 review {arg}", "req": "自定义 request {arg}"},
        )
        config = read_json(self.gemini / CONFIG_REL)
        self.assertEqual(config["entry_shortcuts"]["r"]["template"], "自定义 review {arg}")
        self.assertEqual(
            config["entry_shortcuts"]["req"]["template"], "自定义 request {arg}"
        )
        command = next(
            item
            for item in config["comma_commands"].values()
            if ",ts" in item["aliases"]
        )
        self.assertEqual(command["aliases"], [",ts", ",t1"])

    def test_reapply_preserves_legacy_autopilot_profile(self) -> None:
        """A pre-rename install keeps its preferences through one bare apply.

        resident-fact: settlement: bare `pilot apply` reads the legacy
        config/autopilot/profile.json before the one-way migration, so the
        canonical install keeps the installed mode, turn cap and worker order
        instead of resetting them to the recommended preset.
        """
        self.seed_legacy_install(self.gemini)

        result = apply_profile(self.root, self.gemini)
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(result["preset"], "installed-profile")
        self.assertEqual(result["worker_agents"], ["codex"])
        self.assertEqual(result["mode"], "confirm")
        self.assertEqual(result["max_consecutive_auto_turns"], 5)
        self.assertEqual(profile["pilot"]["mode"], "confirm")
        self.assertEqual(profile["pilot"]["max_consecutive_auto_turns"], 5)
        self.assertEqual(profile["orchestration"]["worker_agents"], ["codex"])
        self.assertFalse((self.gemini / "config/autopilot").exists())
        self.assertEqual(json.loads(run_setup(self.root, self.gemini, "status").stdout)["drift"], [])

    def test_explicit_profile_replaces_installed_preferences(self) -> None:
        """`--profile` is the documented replacement path for installed preferences.

        Failure means an explicit profile is merged onto stale installed state,
        so a replacement cannot remove worker agents, scenes or shortcuts the
        user previously chose.
        """
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        profile_path = self.gemini / PROFILE_REL
        profile = read_json(profile_path)
        profile["orchestration"]["worker_agents"] = ["codex", "claude"]
        profile["pilot"] = {"mode": "off", "max_consecutive_auto_turns": 12}
        profile["scenes"]["enable_feedback_email"] = True
        profile["shortcuts"]["enabled_entry"] = [
            "cq",
            "fb",
            "al",
            "r",
            "rt",
            "d",
            "pull",
        ]
        write_json(profile_path, profile)

        replacement = {
            "orchestration": {"worker_agents": ["claude"]},
            "pilot": {"mode": "confirm", "max_consecutive_auto_turns": 3},
            "scenes": {
                "enabled_scenes": ["plan-and-orchestrate"],
                "deliver_strategy": "local_verify_only",
                "enable_feedback_email": False,
                "guardrails": [],
            },
            "shortcuts": {"enabled_entry": ["r"]},
        }
        replacement_path = self.root / "replacement.json"
        replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
        result = apply_profile(
            self.root, self.gemini, "--profile", str(replacement_path)
        )
        replaced = read_json(profile_path)
        self.assertEqual(result["preset"], "recommended")
        self.assertEqual(result["worker_agents"], ["claude"])
        self.assertEqual(replaced["orchestration"]["worker_agents"], ["claude"])
        self.assertEqual(
            replaced["pilot"], {"mode": "confirm", "max_consecutive_auto_turns": 3}
        )
        self.assertEqual(replaced["scenes"]["enabled_scenes"], ["plan-and-orchestrate"])
        self.assertEqual(replaced["scenes"]["deliver_strategy"], "local_verify_only")
        self.assertNotIn("enable_feedback_email", replaced["scenes"])
        self.assertEqual(replaced["shortcuts"]["enabled_entry"], ["r"])

    def test_reapply_fails_closed_on_corrupt_installed_profile(self) -> None:
        """A corrupt installed profile is never treated as absent and overwritten.

        Failure means a bare `pilot apply` silently resets (and destroys) the
        user's preferences when profile.json cannot be parsed, instead of
        reporting the parse error while leaving the file untouched.
        """
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        profile_path = self.gemini / PROFILE_REL
        corrupt = '{"orchestration": '
        profile_path.write_text(corrupt, encoding="utf-8")

        blocked = run_setup(self.root, self.gemini, "apply")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("profile.json", blocked.stderr)
        self.assertEqual(profile_path.read_text(encoding="utf-8"), corrupt)

        recovered = apply_profile(self.root, self.gemini, "--preset", "recommended")
        self.assertEqual(recovered["preset"], "recommended")
        self.assertEqual(read_json(profile_path)["pilot"]["mode"], "gate")

    def test_explicit_preset_resets_installed_preferences(self) -> None:
        """Choosing a preset is the documented reset path for a customized install.

        Failure means users cannot return to a known preset because the
        installed profile always wins over the explicit `--preset` selection.
        """
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        profile_path = self.gemini / PROFILE_REL
        profile = read_json(profile_path)
        profile["orchestration"]["worker_agents"] = ["codex", "claude"]
        profile["pilot"] = {"mode": "off", "max_consecutive_auto_turns": 12}
        write_json(profile_path, profile)

        result = apply_profile(self.root, self.gemini, "--preset", "conservative")
        reset = read_json(profile_path)
        self.assertEqual(result["preset"], "conservative")
        self.assertEqual(reset["orchestration"]["worker_agents"], ["claude", "codex"])
        self.assertEqual(
            reset["pilot"],
            {"mode": "confirm", "max_consecutive_auto_turns": 4},
        )

    def test_uninstall_removes_only_managed_files(self) -> None:
        self.seed_user_files()
        custom_scene = self.gemini / SCENES_REL / "05-custom.md"
        custom_scene.parent.mkdir(parents=True, exist_ok=True)
        custom_scene.write_text("---\nname: custom\n---\nkeep\n", encoding="utf-8")
        apply_profile(self.root, self.gemini, "--preset", "recommended")

        result = run_setup(self.root, self.gemini, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.gemini / ENGINE_REL).is_file())
        self.assertFalse((self.gemini / PROFILE_REL).is_file())
        self.assertFalse((self.gemini / CONFIG_REL).is_file())
        self.assertEqual(
            sorted(path.name for path in (self.gemini / SCENES_REL).glob("*.md")),
            ["05-custom.md"],
        )
        self.assertIn(
            "保留这一段。", (self.gemini / GEMINI_REL).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            profile_compiler.GEMINI_BEGIN,
            (self.gemini / GEMINI_REL).read_text(encoding="utf-8"),
        )
        hooks = read_json(self.gemini / HOOKS_REL)
        self.assertIn("orca-status", hooks)
        self.assertNotIn("the-pilot", hooks)

        again = run_setup(self.root, self.gemini, "uninstall")
        self.assertEqual(again.returncode, 0)
        self.assertFalse(json.loads(again.stdout)["installed"])

    def test_status_reports_drift_after_config_tamper(self) -> None:
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        config_path = self.gemini / CONFIG_REL
        write_json(config_path, {"entry_shortcuts": {}})
        status = run_setup(self.root, self.gemini, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertTrue(any("config.json" in item for item in report["drift"]))
        apply_profile(self.root, self.gemini, "--upgrade")
        report = json.loads(run_setup(self.root, self.gemini, "status").stdout)
        self.assertEqual(report["drift"], [])

    def test_invalid_profile_writes_nothing(self) -> None:
        result = run_setup(
            self.root,
            self.gemini,
            "apply",
            "--profile",
            '{"pilot": {"mode": "bogus"}}',
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.gemini / ENGINE_REL).exists())
        self.assertFalse((self.gemini / PROFILE_REL).exists())

    def test_unmanaged_scene_markers_fail_apply_closed_and_surface_in_status(self) -> None:
        # Failure means apply appends a second scenes segment onto a legacy
        # manual one, or status dies instead of reporting that blocker as drift.
        legacy = (
            "\n# 用户规则\n\n旧手工场景段：\n\n"
            f"{profile_compiler.SCENES_BEGIN}\n旧手工场景\n{profile_compiler.SCENES_END}\n"
        )
        self.seed_user_files()
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        gemini_path = self.gemini / GEMINI_REL
        installed = gemini_path.read_text(encoding="utf-8")
        gemini_path.write_text(installed + legacy, encoding="utf-8")
        managed = {
            path: path.read_bytes()
            for path in (
                self.gemini / PROFILE_REL,
                self.gemini / CONFIG_REL,
                self.gemini / ENGINE_REL,
            )
        }

        applied = run_setup(self.root, self.gemini, "apply", "--preset", "recommended")
        self.assertEqual(applied.returncode, 2)
        self.assertIn("未托管的场景标记", applied.stderr)
        for path, content in managed.items():
            self.assertEqual(path.read_bytes(), content, path)
        self.assertEqual(gemini_path.read_text(encoding="utf-8"), installed + legacy)

        status = run_setup(self.root, self.gemini, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertTrue(report["ok"])
        self.assertTrue(
            any(
                item.startswith("GEMINI.md 托管块异常：")
                and "未托管的场景标记" in item
                for item in report["drift"]
            ),
            report["drift"],
        )

        fresh = self.root / "gemini-fresh"
        (fresh / "config").mkdir(parents=True)
        (fresh / GEMINI_REL).write_text(legacy, encoding="utf-8")
        blocked = run_setup(self.root, fresh, "apply", "--preset", "recommended")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("未托管的场景标记", blocked.stderr)
        self.assertFalse((fresh / ENGINE_REL).exists())
        self.assertFalse((fresh / PROFILE_REL).exists())
        self.assertFalse((fresh / CONFIG_REL).exists())


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.gemini = self.root / "gemini"
        self.state = self.root / "state"
        self.workspace = self.root / "ws"
        write_json(self.workspace / ".pilot/project.json", PROJECT)
        fake_orca(self.root)
        apply_profile(self.root, self.gemini, "--preset", "recommended")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def pre(
        self,
        conversation_id: str,
        text: str,
        *,
        steps: list[dict] | None = None,
        extra_env: dict | None = None,
    ) -> subprocess.CompletedProcess:
        transcript = write_transcript(
            self.root,
            f"transcript-{conversation_id}.jsonl",
            steps or [{"type": "USER_INPUT", "content": text}],
            append=True,
        )
        return run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "PreInvocation",
            stdin=payload(
                conversation_id,
                lastUserInput=text,
                transcriptPath=str(transcript),
                workspacePaths=[str(self.workspace)],
            ),
            extra_env=extra_env,
        )

    def stop(
        self,
        conversation_id: str,
        text: str,
        *,
        steps: list[dict] | None = None,
        extra_env: dict | None = None,
    ) -> subprocess.CompletedProcess:
        transcript_path = self.root / f"transcript-{conversation_id}.jsonl"
        if steps is not None:
            transcript_steps = steps
        elif transcript_path.is_file() and transcript_path.stat().st_size > 0:
            transcript_steps = [{"type": "PLANNER_RESPONSE", "content": text}]
        else:
            transcript_steps = [
                {"type": "USER_INPUT", "content": "开始"},
                {"type": "PLANNER_RESPONSE", "content": text},
            ]
        transcript = write_transcript(
            self.root,
            f"transcript-{conversation_id}.jsonl",
            transcript_steps,
            append=True,
        )
        return run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                conversation_id,
                transcriptPath=str(transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
                workspacePaths=[str(self.workspace)],
            ),
            extra_env=extra_env,
        )

    def stop_decision(
        self,
        conversation_id: str,
        steps: list[dict],
        *,
        continuation_active: bool | None = None,
        extra_env: dict | None = None,
    ) -> dict:
        """Run Stop on exactly ``steps`` and return the hook decision."""
        transcript = write_transcript(
            self.root, f"transcript-{conversation_id}.jsonl", steps
        )
        fields = {
            "transcriptPath": str(transcript),
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "workspacePaths": [str(self.workspace)],
        }
        if continuation_active is not None:
            fields["continuationActive"] = continuation_active
        result = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(conversation_id, **fields),
            extra_env=extra_env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout or "{}")

    def injection(self, result: subprocess.CompletedProcess) -> str:
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout or "{}")
        steps = body.get("injectSteps") or []
        return steps[0].get("ephemeralMessage", "") if steps else ""

    def stop_compact(
        self,
        session: str,
        *,
        tool: str,
        receipt: str,
        full_lines: list[str] | None,
        gate: str = "auto",
    ) -> None:
        """Stop on a `transcript.jsonl` whose question call is truncated.

        `full_lines` is the sibling `transcript_full.jsonl` content, or None to
        omit that file. The first line is any step; the second is the planner.
        """
        truncated_args = {"questions": '[{"question": "Q1"}, {"question": "Q2"'}
        compact_steps = [
            {"type": "USER_INPUT", "content": "选哪个方案"},
            {
                "type": "PLANNER_RESPONSE",
                "content": "需要你确认方案。",
                "tool_calls": [{"name": tool, "args": truncated_args}],
                "truncated_fields": ["tool_calls"],
            },
            {
                "type": "TOOL_RESPONSE",
                "status": "DONE",
                "content": ANSWER_HEADER + receipt,
            },
            {
                "type": "PLANNER_RESPONSE",
                "content": suggestion("按方案 A 推进", gate=gate),
            },
        ]
        directory = self.root / "compact"
        directory.mkdir(exist_ok=True)
        full_path = directory / "transcript_full.jsonl"
        if full_path.is_file():
            full_path.unlink()
        if full_lines is not None:
            full_path.write_text("\n".join(full_lines) + "\n", encoding="utf-8")
        transcript = write_transcript(
            self.root, "compact/transcript.jsonl", compact_steps
        )
        result = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                session,
                transcriptPath=str(transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
                workspacePaths=[str(self.workspace)],
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gate_auto_submits_and_keeps_the_auto_turn_boundary(self) -> None:
        result = self.stop(
            "c-auto",
            suggestion("继续推进实施", alt="先暂停确认", gate="auto"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertIn("--enter", sends[0])
        self.assertEqual(text_argument(sends[0]), "继续推进实施")
        state = read_state(self.state, "c-auto")
        self.assertEqual(state["consecutive_auto_turns"], 1)
        self.assertEqual(state["auto_submitted_text"], "继续推进实施")
        self.assertEqual(len(state["candidates"]), 2)

        self.pre("c-auto", "继续推进实施")
        state = read_state(self.state, "c-auto")
        self.assertEqual(state["consecutive_auto_turns"], 1)
        self.assertEqual(state["auto_submitted_text"], "")

    def test_stop_submits_once_and_settles_before_returning(self) -> None:
        """resident-fact: Stop submits from the same hook and settles before agy can block the pane for artifact review."""
        cid = "in-stop"
        result = self.stop(cid, suggestion("继续推进"), extra_env={"FAKE_REQUIRE_SAME_HOOK": "1"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"decision": "allow"})
        self.assertEqual(read_state(self.state, cid)["delivery_status"], "accepted")
        send, = send_calls(self.root)
        self.assertIn("--enter", send)
        self.assertNotIn("--wait-submit", send)

    def confirmation_env(self, cid: str, request_id: str = "R1") -> dict:
        return {
            "FAKE_SEND_PROMPT": json.dumps({
                "observation": "supported",
                "requestId": request_id,
                "processIncarnation": "agy-1",
                "stages": ["input_accepted"],
            }),
            "THE_PILOT_CONFIRM_TIMEOUT_MS": "20",
            "THE_PILOT_STOP_EXIT_TIMEOUT_MS": "20",
            "THE_PILOT_OSC_TARGET": self.root / f"osc-{cid}",
        }

    def deferred_confirmation(
        self, cid: str, prompt: str, env: dict, *,
        spawn_fails: bool = False, transcript: Path | None = None,
    ) -> list[str]:
        """Control the child launch so hooks can be ordered without timing guesses."""
        transcript = transcript or self.root / f"transcript-{cid}.jsonl"
        steps = [] if transcript.exists() else [{"type": "USER_INPUT", "content": "开始"}]
        steps.append({"type": "PLANNER_RESPONSE", "content": suggestion(prompt)})
        write_transcript(self.root, str(transcript.relative_to(self.root)), steps, append=True)
        command = []
        real_popen = subprocess.Popen

        def defer_child(argv, **options):
            if argv[2:3] == ["confirm-delivery"]:
                command.extend(argv[2:])
                if spawn_fails:
                    raise OSError("cannot launch confirm-delivery")
                return None
            return real_popen(argv, **options)

        pilot_dir = self.gemini / "config/the-pilot"
        with mock.patch.dict(os.environ, engine_env(self.root, self.gemini, self.state, env), clear=True), (
            mock.patch.multiple(
                runtime, GEMINI_DIR=self.gemini, PILOT_DIR=pilot_dir,
                PROFILE_PATH=self.gemini / PROFILE_REL, SCENES_DIR=self.gemini / SCENES_REL,
                STATE_DIR=self.state, LOG_PATH=self.state / "pilot.log",
            )
        ), mock.patch.object(subprocess, "Popen", side_effect=defer_child):
            result, frame = runtime._evaluate_stop({
                "conversationId": cid, "transcriptPath": str(transcript),
                "terminationReason": "NO_TOOL_CALL", "fullyIdle": True,
                "workspacePaths": [str(self.workspace)],
            })
            runtime.emit_osc_7501(frame)
        self.assertEqual(result, {"decision": "allow"})
        self.assertEqual(command[:1], ["confirm-delivery"], "Stop did not launch confirmation")
        return command

    def confirm_delivery(self, command: list[str], env: dict) -> None:
        command = command.copy()
        # This PID exceeds the host kernel's PID range, representing an exited Stop.
        command[command.index("--parent-pid") + 1] = str(2 ** 30)
        result = run_engine(self.root, self.gemini, self.state, *command, extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def delivery_event(self, cid: str, name: str) -> dict:
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        events = [json.loads(line) for line in log.splitlines()]
        event, = [item for item in events if item.get("cid") == cid and item["event"] == name]
        return event

    def test_stop_classifies_prompt_receipts_before_deciding_to_confirm(self) -> None:
        """Failure means a definitive receipt can claim the wrong status, retain a rejected turn, or start a redundant confirmation."""
        cases = (
            ("permission", ["input_accepted"], "rejected", runtime.OSC_7501_WAITING_QUESTION, None),
            ("incarnation_replaced", ["input_accepted"], "rejected", runtime.OSC_7501_DONE, None),
            ("unsupported", [], "accepted", "", False),
            ("supported", ["input_accepted", "turn_started"], "accepted", "", True),
        )
        for observation, stages, status, frame, verified in cases:
            with self.subTest(observation=observation):
                env = self.confirmation_env(observation)
                receipt = json.loads(env["FAKE_SEND_PROMPT"])
                env["FAKE_SEND_PROMPT"] = json.dumps(dict(receipt, observation=observation, stages=stages))
                result = self.stop(observation, suggestion("继续推进"), extra_env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                state = read_state(self.state, observation)
                target = env["THE_PILOT_OSC_TARGET"]
                self.assertEqual(state["delivery_status"], status)
                self.assertFalse(state.get("delivery_request_id"))
                self.assertEqual(target.read_text(encoding="utf-8") if target.exists() else "", frame)
                if status == "rejected":
                    self.assertEqual(state["consecutive_auto_turns"], 0)
                    self.assertEqual(self.delivery_event(observation, "auto_submit_rejected")["reason"], observation)
                else:
                    event = self.delivery_event(observation, "auto_submitted")
                    self.assertIs(event.get("verified"), verified)
                    if verified:
                        self.assertEqual(event["request_id"], "R1")
        self.assertEqual(len(send_calls(self.root)), len(cases))

    def test_repeated_permission_stop_keeps_waiting_until_new_input(self) -> None:
        """Failure means a duplicate Stop can release orchestration mail into an open permission prompt."""
        cid = "confirm-permission-duplicate"
        env = self.confirmation_env(cid)
        receipt = json.loads(env["FAKE_SEND_PROMPT"])
        env["FAKE_SEND_PROMPT"] = json.dumps(dict(receipt, observation="permission"))
        steps = [
            {"type": "USER_INPUT", "content": "开始"},
            {"type": "PLANNER_RESPONSE", "content": suggestion("继续推进")},
        ]
        for _ in range(2):
            self.assertEqual(self.stop_decision(cid, steps, extra_env=env), {"decision": "allow"})

        target = env["THE_PILOT_OSC_TARGET"]
        self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_WAITING_QUESTION * 2)
        self.assertEqual(len(send_calls(self.root)), 1)
        state = read_state(self.state, cid)
        self.assertEqual(state["delivery_status"], "rejected")
        self.assertEqual(state["rejected_reason"], "permission")
        self.assertEqual(self.pre(cid, "换个任务", extra_env=env).returncode, 0)
        self.assertFalse(read_state(self.state, cid).get("rejected_reason"))

    def test_confirm_delivery_recognizes_the_new_turn_without_another_send(self) -> None:
        """Failure means either local signal can leave a real turn unverified, emit a stale waiting frame, or replay Enter."""
        prompt = "继续推进"
        for signal in ("hook", "transcript"):
            with self.subTest(signal=signal):
                env = self.confirmation_env(signal)
                command = self.deferred_confirmation(signal, prompt, env)
                handoff = read_state(self.state, signal)
                self.assertEqual(handoff["delivery_status"], "accepted")
                self.assertEqual(handoff["delivery_request_id"], "R1")
                self.assertGreater(handoff["delivery_deadline_at"], handoff["updated_at"])
                self.assertEqual(self.delivery_event(signal, "auto_submit_accepted")["request_id"], "R1")
                if signal == "hook":
                    self.assertEqual(self.pre(signal, prompt, extra_env=env).returncode, 0)
                else:
                    write_transcript(self.root, f"transcript-{signal}.jsonl", [
                        {"type": "USER_INPUT", "content": f"<USER_REQUEST>{prompt}</USER_REQUEST>"},
                    ], append=True)
                before = calls(self.root)
                self.confirm_delivery(command, env)
                self.assertEqual(calls(self.root), before)
                state = read_state(self.state, signal)
                self.assertEqual(state["delivery_status"], "none" if signal == "hook" else "accepted")
                for field in ("delivery_request_id", "last_confirmed_request_id", "delivery_deadline_at"):
                    self.assertFalse(state[field])
                event = self.delivery_event(signal, "auto_submitted")
                self.assertEqual(event["request_id"], "R1")
                self.assertIs(event["verified"], True)
                target = env["THE_PILOT_OSC_TARGET"]
                if signal == "hook":
                    self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_WORKING)
                else:
                    self.assertFalse(target.exists())
        self.assertEqual(len(send_calls(self.root)), 2)

    def test_confirm_delivery_times_out_after_the_real_stop_exits(self) -> None:
        """Failure means the detached child cannot settle a live Stop handoff or releases mail after an unverified Enter."""
        cid = "confirm-timeout"
        env = self.confirmation_env(cid)
        result = self.stop(cid, suggestion("继续推进"), extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"decision": "allow"})
        target = env["THE_PILOT_OSC_TARGET"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if target.exists() and target.read_text(encoding="utf-8"):
                break
            time.sleep(0.01)
        self.assertTrue(target.exists(), "Confirmation did not emit a waiting frame")
        self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_WAITING_QUESTION)
        state = read_state(self.state, cid)
        self.assertEqual(state["delivery_status"], "unknown")
        self.assertEqual(state["unconfirmed_request_id"], "R1")
        self.assertEqual(state["delivery_request_id"], "")
        self.assertEqual(state["delivery_deadline_at"], 0.0)
        self.assertEqual(self.delivery_event(cid, "auto_submit_unconfirmed")["reason"], "turn_start_timeout")
        self.assertEqual([call[:2] for call in calls(self.root)], [
            ["terminal", "list"], ["terminal", "send"], ["terminal", "list"],
        ])

    def test_confirmation_uses_only_the_primary_transcript(self) -> None:
        """Failure means changes to hydrated content can masquerade as a new turn even though the primary transcript never advanced."""
        cid = "confirm-truncated-input"
        compact = write_transcript(self.root, "transcript.jsonl", [
            {"type": "USER_INPUT", "content": "开始", "truncated_fields": ["content"]},
        ])
        write_transcript(self.root, "transcript_full.jsonl", [
            {"type": "USER_INPUT", "content": "开始执行长任务"},
        ])
        env = self.confirmation_env(cid)
        command = self.deferred_confirmation(cid, "继续推进", env, transcript=compact)
        write_transcript(self.root, "transcript_full.jsonl", [
            {"type": "USER_INPUT", "content": "完整转录被替换"},
        ])
        self.confirm_delivery(command, env)
        self.assertEqual(read_state(self.state, cid)["delivery_status"], "unknown")
        self.assertEqual(self.delivery_event(cid, "auto_submit_unconfirmed")["reason"], "turn_start_timeout")
        self.assertEqual(len(send_calls(self.root)), 1)

    def test_manual_candidate_input_supersedes_confirmation(self) -> None:
        """Failure means an old confirmation can change a manually claimed turn or overwrite its working status."""
        cid = "confirm-manual"
        env = self.confirmation_env(cid)
        command = self.deferred_confirmation(cid, "继续推进", env)
        self.assertIn("继续推进", self.injection(self.pre(cid, ",1", extra_env=env)))
        state = read_state(self.state, cid)
        before = calls(self.root)
        self.confirm_delivery(command, env)
        self.assertEqual(calls(self.root), before)
        self.assertEqual(read_state(self.state, cid), state)
        self.assertEqual(env["THE_PILOT_OSC_TARGET"].read_text(encoding="utf-8"), runtime.OSC_7501_WORKING)
        self.assertEqual(self.delivery_event(cid, "auto_submit_superseded")["request_id"], "R1")

    def test_old_confirmation_cannot_settle_a_new_receipted_stop(self) -> None:
        """Failure means R1 can mutate the session or emit status after a new turn has handed off R2."""
        cid = "confirm-new-stop"
        env = self.confirmation_env(cid)
        first = self.deferred_confirmation(cid, "继续推进", env)
        self.assertEqual(self.pre(cid, "继续推进", extra_env=env).returncode, 0)
        self.deferred_confirmation(cid, "再继续", self.confirmation_env(cid, "R2"))
        state = read_state(self.state, cid)
        self.assertEqual(state["delivery_request_id"], "R2")
        before = calls(self.root)
        self.confirm_delivery(first, env)
        self.assertEqual(calls(self.root), before)
        self.assertEqual(read_state(self.state, cid), state)
        self.assertEqual(env["THE_PILOT_OSC_TARGET"].read_text(encoding="utf-8"), runtime.OSC_7501_WORKING)
        self.assertEqual(self.delivery_event(cid, "auto_submit_superseded")["request_id"], "R1")

    def test_unconfirmed_receipt_requires_proof_of_the_original_incarnation(self) -> None:
        """Failure means a delayed frame can mark a different or unverified process blocked while settlement needs reconciliation."""
        for cid, expected, actual in (
            ("confirm-replaced-incarnation", "agy-1", "agy-2"),
            ("confirm-missing-receipt-incarnation", "", "agy-1"),
            ("confirm-missing-live-incarnation", "agy-1", ""),
        ):
            with self.subTest(case=cid):
                env = self.confirmation_env(cid)
                receipt = json.loads(env["FAKE_SEND_PROMPT"])
                env["FAKE_SEND_PROMPT"] = json.dumps(dict(receipt, processIncarnation=expected))
                command = self.deferred_confirmation(cid, "继续推进", env)
                self.confirm_delivery(command, dict(env, FAKE_TERMINAL_INCARNATION=actual))
                state = read_state(self.state, cid)
                self.assertEqual(state["delivery_status"], "unknown")
                self.assertEqual(state["unconfirmed_request_id"], "R1")
                self.assertFalse(env["THE_PILOT_OSC_TARGET"].exists())
        self.assertEqual(len(send_calls(self.root)), 3)

    def test_late_matching_pre_invocation_recovers_receipted_delivery(self) -> None:
        """Failure means a late start of the original send freezes subsequent Stop hooks or repeats that send."""
        cid = "confirm-late"
        env = self.confirmation_env(cid)
        command = self.deferred_confirmation(cid, "继续推进", env)
        self.confirm_delivery(command, env)
        self.assertEqual(self.pre(cid, "继续推进", extra_env=env).returncode, 0)
        state = read_state(self.state, cid)
        self.assertEqual(state["delivery_status"], "none")
        self.assertEqual(state["unconfirmed_request_id"], "")
        event = self.delivery_event(cid, "auto_submitted")
        self.assertEqual(event["request_id"], "R1")
        self.assertIs(event["verified"], True)
        self.assertIs(event["late"], True)
        result = self.stop(cid, suggestion("下一次推进"), extra_env=env | {"FAKE_SEND_PROMPT": None})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([text_argument(call) for call in send_calls(self.root)], ["继续推进", "下一次推进"])
        self.assertEqual(env["THE_PILOT_OSC_TARGET"].read_text(encoding="utf-8"),
                         runtime.OSC_7501_WAITING_QUESTION + runtime.OSC_7501_WORKING)

    def test_failed_or_abandoned_confirmation_keeps_the_receipt_recoverable(self) -> None:
        """Failure means a lost confirmation can stay accepted forever or trigger another Enter on the same Stop."""
        for reason in ("confirm_spawn_failed", "confirm_abandoned"):
            with self.subTest(reason=reason):
                env = self.confirmation_env(reason)
                self.deferred_confirmation(reason, "继续推进", env, spawn_fails=reason == "confirm_spawn_failed")
                target = env["THE_PILOT_OSC_TARGET"]
                if reason == "confirm_abandoned":
                    path = state_path(self.state, reason)
                    transcript = self.root / f"transcript-{reason}.jsonl"
                    steps = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
                    self.assertEqual(self.stop_decision(reason, steps, extra_env=env), {"decision": "allow"})
                    self.assertFalse(target.exists())
                    write_json(path, read_json(path) | {"delivery_deadline_at": 0.0})
                    self.assertEqual(self.stop_decision(reason, steps, extra_env=env), {"decision": "allow"})
                state = read_state(self.state, reason)
                self.assertEqual(state["delivery_status"], "unknown")
                self.assertEqual(state["unconfirmed_request_id"], "R1")
                self.assertEqual(state["delivery_request_id"], "")
                self.assertEqual(state["delivery_deadline_at"], 0.0)
                self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_WAITING_QUESTION)
                self.assertEqual(self.delivery_event(reason, "auto_submit_unconfirmed")["reason"], reason)
        self.assertEqual(len(send_calls(self.root)), 2)

    def test_stop_uses_mode_committed_during_terminal_lookup(self) -> None:
        """resident-fact: A mode change can commit during terminal lookup and governs the ensuing send handoff."""
        for initial_mode, mode in (
            ("gate", "off"), ("gate", "confirm"), ("confirm", "off"), ("confirm", "gate"),
        ):
            with self.subTest(initial_mode=initial_mode, mode=mode):
                apply_profile(self.root, self.gemini, "--mode", initial_mode)
                cid = f"mode-during-lookup-{initial_mode}-{mode}"
                previous_sends = len(send_calls(self.root))
                mutation = [sys.executable, str(self.gemini / ENGINE_REL), "mode", mode]
                result = self.stop(cid, suggestion("继续推进"), extra_env={"FAKE_LIST_MUTATION": json.dumps(mutation)})

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(read_json(self.gemini / PROFILE_REL)["pilot"]["mode"], mode)
                state = read_state(self.state, cid)
                self.assertEqual(state.get("consecutive_auto_turns", 0), 0)
                sends = send_calls(self.root)[previous_sends:]
                if mode == "off":
                    self.assertEqual(sends, [])
                    self.assertEqual(state["candidates"], [])
                    self.assertEqual(state["delivery_status"], "none")
                else:
                    send, = sends
                    self.assertNotIn("--enter", send)
                    self.assertEqual(state["delivery_status"], "prefilled")

    def test_stop_does_not_prefill_after_uninstall_during_terminal_lookup(self) -> None:
        """resident-fact: Uninstall takes effect before a stale Stop can launch a prefill or retain its suggestion."""
        apply_profile(self.root, self.gemini, "--mode", "confirm")
        cid = "uninstall-during-lookup"
        first = self.stop(cid, suggestion("继续推进"))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(read_state(self.state, cid)["delivery_status"], "prefilled")

        mutation = [sys.executable, str(SETUP_SCRIPT), "uninstall", "--gemini-dir", str(self.gemini), "--json"]
        result = self.stop(cid, suggestion("下一步"), extra_env={"FAKE_LIST_MUTATION": json.dumps(mutation)})

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stderr)["error"], "config_missing")
        self.assertFalse((self.gemini / PROFILE_REL).exists())
        self.assertEqual(read_state(self.state, cid)["candidates"], [])
        self.assertEqual(len(send_calls(self.root)), 1)

    def test_stop_cancels_delivery_if_transcript_changes_during_lookup(self) -> None:
        """resident-fact: A changed or incomplete transcript blocks stale delivery and preserves the pane status."""
        for step_type in ("USER_INPUT", "CHECKPOINT", "incomplete"):
            with self.subTest(step_type=step_type):
                cid = f"transcript-during-lookup-{step_type}"
                started = self.pre(cid, "开始")
                self.assertEqual(started.returncode, 0, started.stderr)
                previous_state = read_state(self.state, cid)
                target = self.root / f"osc-{cid}"
                result = self.stop(cid, suggestion("继续推进"), extra_env={
                    "FAKE_LIST_TRANSCRIPT": self.root / f"transcript-{cid}.jsonl",
                    "FAKE_LIST_STEP": step_type,
                    "THE_PILOT_OSC_TARGET": target,
                })

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(send_calls(self.root), [])
                self.assertEqual(read_state(self.state, cid), previous_state)
                self.assertFalse(target.exists())

    def test_stop_logs_structured_orca_refusal(self) -> None:
        """resident-fact: A nonzero Orca reply exposes the stdout error code and send refusal reason in Pilot's log."""
        cid = "orca-refusal"
        result = self.stop(cid, suggestion("继续推进"), extra_env={"FAKE_SEND_RESULT": "refused"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_state(self.state, cid)["delivery_status"], "rejected")
        entries = [
            json.loads(line)
            for line in (self.state / "pilot.log").read_text(encoding="utf-8").splitlines()
        ]
        refusal, = [entry for entry in entries if entry["event"] == "send_rejected"]
        self.assertEqual(refusal["handle"], "term_test")
        self.assertIs(refusal["enter"], True)
        self.assertEqual(refusal["returncode"], 1)
        self.assertEqual(refusal["stderr"], "")
        self.assertEqual(refusal["error_code"], "agent_prompt_blocked")
        self.assertEqual(refusal["refused_reason"], "permission")
        self.assertIs(json.loads(refusal["stdout"])["result"]["send"]["accepted"], False)

    def test_stop_crash_keeps_matching_input_frozen_until_manual_input(self) -> None:
        """resident-fact: A persisted pending send holds mail and suppresses another Enter until manual input."""
        cid = "crashed-stop"
        prompt = "继续推进"
        first = self.stop(cid, suggestion(prompt))
        self.assertEqual(first.returncode, 0, first.stderr)
        pending = read_state(self.state, cid)
        pending["delivery_status"] = "pending"
        write_json(state_path(self.state, cid), pending)

        matching = self.pre(cid, prompt)
        self.assertEqual(matching.returncode, 0, matching.stderr)
        target = self.root / "osc-crashed-stop"
        following = self.stop(cid, suggestion(prompt), extra_env={"THE_PILOT_OSC_TARGET": target})
        self.assertEqual(following.returncode, 0, following.stderr)
        self.assertEqual(read_state(self.state, cid)["delivery_status"], "pending")
        self.assertEqual(len(send_calls(self.root)), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_WAITING_QUESTION)

        manual = self.pre(cid, "我自己接手")
        self.assertEqual(manual.returncode, 0, manual.stderr)
        self.assertEqual(read_state(self.state, cid)["delivery_status"], "cleared")

    def test_gate_pause_prefills_without_enter(self) -> None:
        self.stop("c-pause", suggestion("继续推进实施", gate="pause"))
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--enter", sends[0])
        state = read_state(self.state, "c-pause")
        self.assertEqual(state.get("consecutive_auto_turns", 0), 0)
        self.assertEqual(state["gate_action"], "pause")

    def test_delivery_and_closeout_respect_explicit_gate(self) -> None:
        for scene, gate in [
            ("deliver-and-verify", "auto"),
            ("closeout-and-sync", "auto"),
            ("deliver-and-verify", "pause"),
            ("closeout-and-sync", "pause"),
        ]:
            with self.subTest(scene=scene, gate=gate):
                session = f"c-{scene}-{gate}"
                self.stop(session, suggestion("完成当前交付步骤", scene=scene, gate=gate))
                sent = send_calls(self.root)[-1]
                self.assertEqual("--enter" in sent, gate == "auto")
                self.assertEqual(text_argument(sent), "完成当前交付步骤")

    def test_confirm_mode_never_presses_enter(self) -> None:
        apply_profile(self.root, self.gemini, "--mode", "confirm")
        self.stop("c-confirm", suggestion("继续推进实施", gate="auto"))
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--enter", sends[0])

    def test_off_mode_does_not_prefill(self) -> None:
        apply_profile(self.root, self.gemini, "--mode", "off")
        self.stop("c-off", suggestion("继续推进实施", gate="auto"))
        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(read_state(self.state, "c-off")["candidates"], [])

    def test_consecutive_auto_cap_falls_back_to_prefill(self) -> None:
        apply_profile(self.root, self.gemini, "--max-turns", "1")
        self.stop("c-cap", suggestion("继续推进实施", gate="auto"))
        self.pre("c-cap", "继续推进实施")
        self.stop("c-cap", suggestion("继续推进实施", gate="auto"))
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 2)
        self.assertIn("--enter", sends[0])
        self.assertNotIn("--enter", sends[1])

    def test_repeated_stop_after_cap_never_promotes_to_auto(self) -> None:
        apply_profile(self.root, self.gemini, "--max-turns", "1")
        self.stop("c-cap-repeat", suggestion("第一轮", gate="auto"))
        self.pre("c-cap-repeat", "第一轮")
        self.stop("c-cap-repeat", suggestion("第二轮", gate="auto"))
        transcript = self.root / "transcript-c-cap-repeat.jsonl"
        body = payload(
            "c-cap-repeat",
            transcriptPath=str(transcript),
            terminationReason="NO_TOOL_CALL",
            fullyIdle=True,
            workspacePaths=[str(self.workspace)],
        )
        repeated = run_engine(
            self.root, self.gemini, self.state, "hook", "--event", "Stop", stdin=body
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 2)
        self.assertIn("--enter", sends[0])
        self.assertNotIn("--enter", sends[1])
        self.assertEqual(
            read_state(self.state, "c-cap-repeat")["consecutive_auto_turns"], 1
        )
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("stop_delivery_frozen", log)

    def test_corrupt_or_unreadable_state_requires_explicit_recovery(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        path = state_path(self.state, "c-corrupt")
        path.write_text("{not-json", encoding="utf-8")
        self.stop("c-corrupt", suggestion("继续推进", gate="auto"))
        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("state_corrupt", log)
        self.assertIn("stop_state_corrupt", log)

        unreadable = state_path(self.state, "c-unreadable")
        unreadable.mkdir(parents=True)
        self.stop("c-unreadable", suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 0)
        self.assertTrue(unreadable.is_dir())
        failed = self.injection(self.pre("c-unreadable", ",pilot recover"))
        self.assertIn("恢复失败", failed)
        self.assertTrue(unreadable.is_dir())
        self.assertEqual(
            list(self.state.glob(f"{unreadable.name}.corrupt-*")), []
        )
        self.stop("c-unreadable", suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 0)

        # Ordinary input and auto-continuation are indistinguishable from a
        # rebuilt state's perspective, so neither may recover.
        self.pre("c-corrupt", "继续推进")
        self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")
        self.assertEqual(list(self.state.glob(f"{path.name}.corrupt-*")), [])

        partial = state_path(self.state, "c-partial")
        partial.write_text("{broken", encoding="utf-8")
        partial_transcript = write_transcript(
            self.root,
            "partial-recovery.jsonl",
            [{"type": "USER_INPUT", "content": ",pilot recover"}],
        )
        with partial_transcript.open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")
        blocked = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "PreInvocation",
            stdin=payload(
                "c-partial",
                lastUserInput=",pilot recover",
                transcriptPath=str(partial_transcript),
                workspacePaths=[str(self.workspace)],
            ),
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(partial.read_text(encoding="utf-8"), "{broken")
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("state_recovery_blocked", log)
        self.assertIn("transcript_complete", log)

        # Rebuild failure: the evidence copy succeeds, but the atomic replace
        # cannot be written. The corrupt main file must stay in place and no
        # success may be reported.
        rebuild_id = "c-rebuild-fail"
        rebuild_path = state_path(self.state, rebuild_id)
        rebuild_path.write_text("{rebuild-broken", encoding="utf-8")
        digest = hashlib.sha256(rebuild_id.encode("utf-8")).hexdigest()[:24]
        lock_path = self.state / f"session-{digest}.lock"
        rebuild_transcript = write_transcript(
            self.root,
            "rebuild-failure.jsonl",
            [{"type": "USER_INPUT", "content": ",pilot recover"}],
        )
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(self.gemini / ENGINE_REL),
                    "hook",
                    "--event",
                    "PreInvocation",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=engine_env(self.root, self.gemini, self.state),
            )
            (self.state / f".{rebuild_path.name}.{process.pid}.tmp").mkdir()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        stdout, stderr = process.communicate(
            payload(
                rebuild_id,
                lastUserInput=",pilot recover",
                transcriptPath=str(rebuild_transcript),
                workspacePaths=[str(self.workspace)],
            ),
            timeout=30,
        )
        self.assertEqual(process.returncode, 0, stderr)
        steps = (json.loads(stdout or "{}").get("injectSteps") or [])
        message = steps[0].get("ephemeralMessage", "") if steps else ""
        self.assertIn("恢复失败", message)
        self.assertEqual(rebuild_path.read_text(encoding="utf-8"), "{rebuild-broken")
        rebuild_backups = list(self.state.glob(f"{rebuild_path.name}.corrupt-*"))
        self.assertEqual(len(rebuild_backups), 1)
        self.assertEqual(
            rebuild_backups[0].read_text(encoding="utf-8"), "{rebuild-broken"
        )
        self.assertNotIn(
            '"event": "state_recovered", "cid": "c-rebuild-fail"',
            (self.state / "pilot.log").read_text(encoding="utf-8"),
        )
        self.stop(rebuild_id, suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 0)

        message = self.injection(self.pre("c-corrupt", ",pilot recover"))
        self.assertIn("已恢复", message)
        state = read_json(path)
        self.assertEqual(state["conversation_id"], "c-corrupt")
        backups = list(self.state.glob(f"{path.name}.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not-json")
        self.assertEqual(send_calls(self.root), [])

    def test_manual_interruption_resets_the_auto_count(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        write_json(
            state_path(self.state, "c-reset"),
            {
                "conversation_id": "c-reset",
                "consecutive_auto_turns": 3,
                "auto_submitted_text": "自动推进",
                "candidates": [],
            },
        )
        self.pre("c-reset", "换一个方向")
        self.assertEqual(read_state(self.state, "c-reset")["consecutive_auto_turns"], 0)

    def test_worker_preamble_is_silent(self) -> None:
        result = self.pre(
            "c-worker",
            "继续推进",
            extra_env={"ORCA_TASK_ID": "task_abc123"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertFalse(state_path(self.state, "c-worker").exists())

        worker_steps = [
            {"type": "USER_INPUT", "content": "Task ID: task_abc123\nDispatch ID: disp_abc123"},
            {"type": "PLANNER_RESPONSE", "content": suggestion("继续推进", gate="auto")},
        ]
        self.stop("c-worker-stop", "", steps=worker_steps)
        self.assertEqual(send_calls(self.root), [])

    def test_candidate_switch_expands_and_clears(self) -> None:
        self.stop("c-cand", suggestion("主推方案", alt="稳妥方案", gate="pause"))
        message = self.injection(self.pre("c-cand", ",2 先看边界"))
        self.assertIn("稳妥方案", message)
        self.assertIn("先看边界", message)
        self.assertEqual(read_state(self.state, "c-cand")["candidates"], [])
        self.assertEqual(json.loads(self.pre("c-cand", ",2").stdout), {})

    def test_invalid_workspace_overlay_fails_closed_without_disabling_universal_commands(self) -> None:
        """Universal control and candidate expansion survive bad workspace shortcut data."""
        reserved = dict(PROJECT["entry_specs"]["fb"], command_id="CMD_RESERVED")
        invalid = dict(PROJECT, entry_order=[*PROJECT["entry_order"], "mode"],
                       entry_specs={**PROJECT["entry_specs"], "mode": reserved})
        for name, content in (("malformed", "{"), ("reserved", json.dumps(invalid))):
            with self.subTest(name=name):
                (self.workspace / ".pilot/project.json").write_text(content, encoding="utf-8")
                self.assertEqual(json.loads(self.pre(f"{name}-fb", ",fb private").stdout), {})
                self.assertIn("Pilot 状态", self.injection(self.pre(f"{name}-mode", ",mode status")))
                self.assertIn("worker", self.injection(self.pre(f"{name}-worker", ",worker status")).lower())
                candidate = f"{name}-candidate"
                self.stop(candidate, suggestion("等待确认", gate="pause"))
                self.assertIn("等待确认", self.injection(self.pre(candidate, ",1")))
        events = [json.loads(line) for line in (self.state / "pilot.log").read_text().splitlines()]
        self.assertTrue({"malformed-fb", "reserved-fb"} <= {
            item["cid"] for item in events if item["event"] == "project_overlay_invalid"
        })

    def test_entry_shortcut_and_custom_comma_expand(self) -> None:
        apply_profile(
            self.root,
            self.gemini,
            "--profile",
            json.dumps(
                {"shortcuts": {"custom_comma": {"ts": "先跑最小定向测试再继续"}}},
                ensure_ascii=False,
            ),
        )
        message = self.injection(self.pre("c-entry", "r auth"))
        self.assertIn("auth", message)
        self.assertIn("review", message)
        message = self.injection(self.pre("c-entry", ",ts"))
        self.assertIn("先跑最小定向测试再继续", message)
        message = self.injection(self.pre("c-entry", "pull"))
        self.assertIn("origin/main", message)

    def test_quality_shortcuts_export_frozen_evidence_with_all_coordinates(self) -> None:
        coordinates = "ses_one --env prod --date 2026-11-09"
        for shortcut in ("cq", ",cq"):
            with self.subTest(shortcut=shortcut):
                message = self.injection(self.pre("c-quality", f"{shortcut} {coordinates}"))
                self.assertIn("alter_quality_review.py export " + coordinates, message)
                self.assertNotIn("alter_quality_review.py claim", message)
                self.assertNotIn("alter_quality_review.py resolve", message)
                self.assertNotIn("--expected-version", message)
                self.assertNotIn("--dispatch-request-id", message)
                self.assertEqual(read_state(self.state, "c-quality")["workflow_type"], "quality")

    def test_alert_shortcuts_delegate_claim_and_verified_close(self) -> None:
        for shortcut in ("al", ",al"):
            with self.subTest(shortcut=shortcut):
                message = self.injection(self.pre("c-alert", f"{shortcut} ae_aaaaaaaaaaaaaaaaaaaa"))
                self.assertIn("$alter-alert", message)
                self.assertIn("alert_transition.py claim", message)
                self.assertIn("alert_transition.py close", message)
                self.assertIn("/ship 发布 prod 前暂停确认一次", message)

    def test_worker_cli_persists_without_drift_through_upgrade(self) -> None:
        # Failure means switching priority is lost by upgrades or disagrees with install output.
        for target in ("codex", "claude", "cc", "claude-code"):
            result = run_engine(self.root, self.gemini, self.state, "worker", target, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = ["codex", "claude"] if target == "codex" else ["claude", "codex"]
            self.assertEqual(json.loads(result.stdout)["worker_agents"], expected)
            self.assertEqual(json.loads(run_setup(self.root, self.gemini, "status").stdout)["drift"], [])
            apply_profile(self.root, self.gemini, "--upgrade")
            status = run_engine(self.root, self.gemini, self.state, "worker", "status", "--json")
            self.assertEqual(json.loads(status.stdout)["worker_agents"], expected)
        mode = run_engine(self.root, self.gemini, self.state, "mode", "confirm")
        self.assertEqual(mode.returncode, 0, mode.stderr)
        self.assertEqual(mode.stdout.strip(), "Pilot mode: confirm")
        self.assertEqual(read_json(self.gemini / PROFILE_REL)["pilot"]["mode"], "confirm")

    def test_worker_chat_switch_and_reserved_aliases(self) -> None:
        for target in ("codex", "claude"):
            message = self.injection(self.pre("worker-chat", f",worker {target}"))
            self.assertIn(f"--agent {target}", message)
            self.assertIn("<!-- pilot: scene=NONE -->", message)
            self.assertEqual(json.loads(run_setup(self.root, self.gemini, "status").stdout)["drift"], [])
        self.assertIn("Claude Code > Codex", self.injection(self.pre("worker-chat", ",worker")))
        self.assertIn("Claude Code > Codex", self.injection(self.pre("worker-chat", ",agent status")))
        for alias in ("worker", "agent"):
            with self.assertRaisesRegex(profile_compiler.ProfileError, "保留别名"):
                normalize_comma_alias(alias)

    def test_worker_switch_reports_missing_profile_and_unreadable_guidance(self) -> None:
        # Failure means a failed preference write is reported as a successful switch.
        original_profile = (self.gemini / PROFILE_REL).read_bytes()
        (self.gemini / GEMINI_REL).write_bytes(b"\xff")
        result = run_engine(self.root, self.gemini, self.state, "worker", "codex", "--json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual((self.gemini / PROFILE_REL).read_bytes(), original_profile)
        (self.gemini / GEMINI_REL).write_text("no managed section", encoding="utf-8")
        result = run_engine(self.root, self.gemini, self.state, "worker", "codex", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual((self.gemini / PROFILE_REL).read_bytes(), original_profile)
        (self.gemini / PROFILE_REL).unlink()
        result = run_engine(self.root, self.gemini, self.state, "worker", "claude", "--json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_corrupt_session_cannot_switch_preferences(self) -> None:
        # Failure means an untrusted session silently changes durable preferences.
        path = self.state / ("session-" + hashlib.sha256(b"worker-corrupt").hexdigest()[:24] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{broken")
        original = (self.gemini / PROFILE_REL).read_bytes()
        for command in (",mode confirm", ",worker codex"):
            self.assertEqual(json.loads(self.pre("worker-corrupt", command).stdout), {})
            self.assertEqual((self.gemini / PROFILE_REL).read_bytes(), original)
            self.assertEqual(path.read_bytes(), b"{broken")

    def test_worker_switch_from_single_agent_has_no_drift(self) -> None:
        # Failure means runtime slot replacement diverges from the canonical compiler.
        for initial in ("claude", "codex"):
            apply_profile(self.root, self.gemini, "--worker-agents", initial)
            for target in ("codex", "claude"):
                result = run_engine(self.root, self.gemini, self.state, "worker", target)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(run_setup(self.root, self.gemini, "status").stdout)
                self.assertEqual(report["drift"], [])

    def test_worker_global_guidance_updates_only_for_default_install(self) -> None:
        # Failure means a sandbox switch edits global guidance, a default switch
        # rewrites markerless user prose, or the managed block keeps stale CLI
        # examples after the worker order changes.
        gemini = self.root / "home" / ".gemini"
        apply_profile(self.root, gemini, "--preset", "recommended")
        block = profile_compiler.render_orchestration_block(
            read_json(gemini / PROFILE_REL)
        )
        original = (
            "## Orca Orchestration：Claude Code > Codex）\n"
            "执行器优先级：Claude Code > Codex）\n"
            "默认优先使用 Claude Code（`--agent claude`），其次使用 Codex（`--agent codex`）\n"
            '`worker-start --worktree new-child --name <name> --agent claude --json`（次选 `--agent codex`）\n'
            "保留其他用户规则\n"
        )
        paths = [self.root / "home/.claude/CLAUDE.md", self.root / "home/.codex/AGENTS.md"]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(original, encoding="utf-8")
        isolated = run_engine(self.root, gemini, self.state, "worker", "codex")
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        for path in paths:
            self.assertEqual(path.read_text(encoding="utf-8"), original)
        profile_before = (gemini / PROFILE_REL).read_bytes()
        guidance_before = (gemini / GEMINI_REL).read_bytes()
        paths[1].write_bytes(b"\xff")
        failed = run_engine(self.root, gemini, self.state, "worker", "claude", extra_env={"THE_PILOT_GEMINI_DIR": None})
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual((gemini / PROFILE_REL).read_bytes(), profile_before)
        self.assertEqual((gemini / GEMINI_REL).read_bytes(), guidance_before)
        self.assertEqual(paths[0].read_text(encoding="utf-8"), original)
        for path in paths:
            path.write_text(original, encoding="utf-8")
        markerless = run_engine(
            self.root, gemini, self.state, "worker", "codex",
            extra_env={"THE_PILOT_GEMINI_DIR": None},
        )
        self.assertEqual(markerless.returncode, 0, markerless.stderr)
        for path in paths:
            self.assertEqual(path.read_text(encoding="utf-8"), original)
        seeded = "保留其他用户规则\n\n" + block
        for path in paths:
            path.write_text(seeded, encoding="utf-8")
        actual = run_engine(self.root, gemini, self.state, "worker", "codex", extra_env={"THE_PILOT_GEMINI_DIR": None})
        self.assertEqual(actual.returncode, 0, actual.stderr)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(profile_compiler.ORCHESTRATION_BEGIN), 1)
            self.assertIn("Orca Orchestration：Codex > Claude Code", text)
            self.assertIn("--name <name> --agent codex", text)
            self.assertIn("次选 `--agent claude`", text)
            self.assertIn("Codex（`--agent codex`），其次使用 Claude Code（`--agent claude`）", text)
            self.assertIn("保留其他用户规则", text)

    def test_worker_reload_reaches_open_and_unversioned_sessions_once(self) -> None:
        self.stop("worker-open", "<!-- pilot: scene=NONE -->")
        legacy = self.state / ("session-" + hashlib.sha256(b"worker-old").hexdigest()[:24] + ".json")
        write_json(legacy, {"last_scene": "kept"})
        corrupt = self.state / "broken.json"
        corrupt.write_bytes(b"{broken")
        legacy_before = legacy.read_bytes()
        result = run_engine(self.root, self.gemini, self.state, "worker", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(legacy.read_bytes(), legacy_before)
        self.assertEqual(corrupt.read_bytes(), b"{broken")
        self.assertEqual(read_json(legacy)["last_scene"], "kept")
        for session, text in (("worker-open", "继续普通任务"), ("worker-old", ",r")):
            message = self.injection(self.pre(session, text))
            self.assertIn("<orca_worker_agent_hot_reload>", message)
            self.assertIn("--agent codex", message)
            self.assertEqual(json.loads(self.pre(session, "下一轮普通输入").stdout), {})
        self.assertEqual(json.loads(self.pre("worker-new", "首次输入").stdout), {})

    def test_worker_stop_cannot_claim_uninjected_priority(self) -> None:
        # Failure means Stop masks an upgrade-time priority change without telling the model.
        session = "worker-stop-first"
        path = self.state / ("session-" + hashlib.sha256(session.encode()).hexdigest()[:24] + ".json")
        write_json(path, {"last_scene": "kept"})
        result = run_engine(self.root, self.gemini, self.state, "worker", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.stop(session, "<!-- pilot: scene=NONE -->")
        self.assertNotIn("loaded_worker_agents", read_json(path))
        self.assertIn("<orca_worker_agent_hot_reload>", self.injection(self.pre(session, "继续")))
        self.assertEqual(json.loads(self.pre(session, "普通输入").stdout), {})

    def test_worker_status_and_failed_switch_preserve_pending_reload(self) -> None:
        # Failure means a command consumes the pending priority update without injecting it.
        for session in ("worker-status-pending", "worker-failed-pending"):
            self.pre(session, "开始")
        result = run_engine(self.root, self.gemini, self.state, "worker", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        message = self.injection(self.pre("worker-status-pending", ",worker status"))
        self.assertTrue(message.startswith("<orca_worker_agent_hot_reload>"))
        (self.gemini / GEMINI_REL).write_text("missing managed section", encoding="utf-8")
        message = self.injection(self.pre("worker-failed-pending", ",worker claude"))
        self.assertTrue(message.startswith("<orca_worker_agent_hot_reload>"))
        self.assertIn("切换失败", message)
        for session in ("worker-status-pending", "worker-failed-pending"):
            self.assertEqual(json.loads(self.pre(session, "普通输入").stdout), {})

    def test_worker_checkpoint_reinjects_changed_priority_once(self) -> None:
        # Failure means compaction restores frozen startup rules or needlessly reinjects unchanged rules.
        self.pre("worker-compacted", "开始")
        self.assertIn("<orca_worker_agent_hot_reload>", self.injection(self.pre("worker-compacted", ",worker codex")))
        self.pre("worker-unchanged", "开始")
        for session in ("worker-compacted", "worker-unchanged"):
            result = self.pre(session, "压缩后继续", steps=[
                {"type": "CHECKPOINT", "content": "摘要"},
                {"type": "USER_INPUT", "content": "压缩后继续"},
            ])
            if session == "worker-compacted":
                message = self.injection(result)
                self.assertIn("<orca_worker_agent_hot_reload>", message)
                self.assertIn("--agent codex", message)
            else:
                self.assertEqual(json.loads(result.stdout), {})
            self.assertEqual(json.loads(self.pre(session, "再下一轮").stdout), {})

    def test_mode_command_updates_profile_and_delivery(self) -> None:
        message = self.injection(self.pre("c-mode", ",mode confirm"))
        self.assertIn("confirm", message)
        self.assertEqual(
            read_json(self.gemini / PROFILE_REL)["pilot"]["mode"], "confirm"
        )
        self.stop("c-mode", suggestion("继续推进", gate="auto"))
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--enter", sends[0])
        self.assertIn("confirm", self.injection(self.pre("c-mode", ",mode status")))

    def test_mode_write_normalizes_a_legacy_profile(self) -> None:
        """Failure means a runtime write leaves a second section or legacy schema.

        resident-fact: runtime: the mode writer rewrites a pre-rename profile
        as one canonical pilot section and the-pilot-profile/v1 schema.
        """
        profile_path = self.gemini / PROFILE_REL
        profile = read_json(profile_path)
        profile["schema"] = "orca-autopilot-profile/v1"
        profile["autopilot"] = profile.pop("pilot")
        write_json(profile_path, profile)

        message = self.injection(self.pre("c-legacy", ",mode confirm"))

        self.assertIn("confirm", message)
        updated = read_json(profile_path)
        self.assertEqual(updated["schema"], "the-pilot-profile/v1")
        self.assertNotIn("autopilot", updated)
        self.assertEqual(updated["pilot"]["mode"], "confirm")

    def test_scene_none_and_pending_ask_question_clear_without_sending(self) -> None:
        self.stop(
            "c-none",
            "已完成。\n<!-- pilot: scene=NONE -->",
        )
        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(read_state(self.state, "c-none")["candidates"], [])

        steps = [
            {"type": "USER_INPUT", "content": "怎么办"},
            {
                "type": "PLANNER_RESPONSE",
                "content": suggestion("继续推进", gate="auto"),
                "tool_calls": [{"name": "ask_question", **ask_args(1)}],
            },
        ]
        self.stop("c-ask", "", steps=steps)
        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(read_state(self.state, "c-ask")["candidates"], [])

    def test_unknown_scene_is_never_auto_submitted(self) -> None:
        self.stop(
            "c-unknown",
            suggestion("继续推进", scene="made-up", gate="auto"),
        )
        self.assertEqual(send_calls(self.root), [])

    def test_sync_refreshes_scenes_after_edit_and_preserves_user_text(self) -> None:
        before_sync = (self.gemini / GEMINI_REL).read_bytes()
        first = run_engine(self.root, self.gemini, self.state, "sync")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual((self.gemini / GEMINI_REL).read_bytes(), before_sync)
        status = run_setup(self.root, self.gemini, "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["drift"], [])
        self.assertEqual(
            (self.gemini / GEMINI_REL).read_text(encoding="utf-8").count(
                profile_compiler.SCENES_BEGIN
            ),
            1,
        )
        gemini_path = self.gemini / GEMINI_REL
        original = gemini_path.read_text(encoding="utf-8")
        gemini_path.write_text(original + "\n用户尾部规则。\n", encoding="utf-8")
        scene_path = self.gemini / SCENES_REL / "01-plan-and-orchestrate.md"
        scene_path.write_text(
            scene_path.read_text(encoding="utf-8") + "\n自定义场景说明 XYZ。\n",
            encoding="utf-8",
        )
        updated = run_engine(self.root, self.gemini, self.state, "sync")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        text = gemini_path.read_text(encoding="utf-8")
        self.assertIn("自定义场景说明 XYZ。", text)
        self.assertIn("用户尾部规则。", text)

    def test_sync_is_serialized_with_uninstall(self) -> None:
        gemini_md = self.gemini / GEMINI_REL
        gemini_md.write_text(
            gemini_md.read_text(encoding="utf-8") + "\n用户规则。\n", encoding="utf-8"
        )
        before_sync = gemini_md.read_text(encoding="utf-8")
        lock_path = self.gemini / "config" / "the-pilot" / ".pilot.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            process = subprocess.Popen(
                [sys.executable, str(self.gemini / ENGINE_REL), "sync"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=engine_env(self.root, self.gemini, self.state),
            )
            # The engine must take this lock before reading any input; deleting
            # the installation while we hold it exposes a sync that only reads
            # (or writes) outside the lock.
            (self.gemini / PROFILE_REL).unlink()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn("未安装", stderr)
        self.assertEqual(gemini_md.read_text(encoding="utf-8"), before_sync)

        apply_profile(self.root, self.gemini, "--preset", "recommended")
        result = run_setup(self.root, self.gemini, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        after_uninstall = gemini_md.read_text(encoding="utf-8")
        # The installed engine is gone after uninstall; any still-running copy
        # (here the skill asset) must refuse rather than rewrite the block.
        sync = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "assets" / "runtime.py"), "sync"],
            capture_output=True,
            text=True,
            env=engine_env(self.root, self.gemini, self.state),
            timeout=30,
        )
        self.assertEqual(sync.returncode, 2, sync.stderr)
        self.assertIn("未安装", sync.stderr)
        self.assertEqual(gemini_md.read_text(encoding="utf-8"), after_uninstall)

    def test_set_updates_profile_and_command_table(self) -> None:
        result = run_engine(
            self.root, self.gemini, self.state, "set", "r", "新模板 {arg}"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            read_json(self.gemini / PROFILE_REL)["shortcuts"]["template_overrides"]["r"],
            "新模板 {arg}",
        )
        config = read_json(self.gemini / CONFIG_REL)
        self.assertEqual(config["entry_shortcuts"]["r"]["template"], "新模板 {arg}")
        comma = next(
            item
            for item in config["comma_commands"].values()
            if ",r" in item["aliases"]
        )
        self.assertEqual(comma["template"], "新模板 {arg}")
        message = self.injection(self.pre("c-set", "r auth"))
        self.assertIn("新模板 auth", message)

        result = run_engine(
            self.root, self.gemini, self.state, "set", ",r", "第二模板 {arg}"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = read_json(self.gemini / CONFIG_REL)
        self.assertEqual(config["entry_shortcuts"]["r"]["template"], "第二模板 {arg}")
        comma = next(
            item
            for item in config["comma_commands"].values()
            if ",r" in item["aliases"]
        )
        self.assertEqual(comma["template"], "第二模板 {arg}")

    def test_rejected_send_does_not_increment(self) -> None:
        self.stop(
            "c-fail",
            suggestion("继续推进", gate="auto"),
            extra_env={"FAKE_SEND_ACCEPTED": "0"},
        )
        state = read_state(self.state, "c-fail")
        self.assertEqual(state["consecutive_auto_turns"], 0)
        self.assertEqual(state["auto_submitted_text"], "")
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("auto_submit_rejected", log)

    def test_invalid_or_duplicate_tag_never_auto_submits(self) -> None:
        self.stop(
            "c-tag-missing",
            "方案已就绪。\n\n💡 下一步建议：继续推进\n"
            "<!-- pilot: scene=plan-and-orchestrate -->",
        )
        self.stop(
            "c-tag-duplicate",
            "方案已就绪。\n\n💡 下一步建议：继续推进\n"
            "<!-- pilot: scene=plan-and-orchestrate gate=auto -->\n"
            "<!-- pilot: scene=plan-and-orchestrate gate=auto -->",
        )
        self.stop("c-tag-bad-gate", suggestion("继续推进", gate="maybe"))
        self.assertEqual(send_calls(self.root), [])

    def test_unasked_question_lead_in_continues_once_and_settles_after_recovery(self) -> None:
        # Failure means a long answer that stops on a question lead-in without
        # the tool call strands the user, or the continuation outlives one user
        # turn and can loop instead of clearing once the tag is valid again.
        session = "c-unasked"
        lead_in = "架构方案已经梳理完毕。\n\n请确认以下 2 个架构与契约边界取舍："
        first = self.stop_decision(
            session,
            [
                {"type": "USER_INPUT", "content": "请给我完整方案"},
                {"type": "PLANNER_RESPONSE", "content": lead_in},
            ],
        )
        self.assertEqual(first["decision"], "continue")
        self.assertIn("ask_question", str(first["reason"]))
        self.assertEqual(send_calls(self.root), [])
        state = read_state(self.state, session)
        continued_turn = state["continued_unasked_turn_id"]
        self.assertTrue(continued_turn)
        log_path = self.state / "pilot.log"
        continue_event = (
            f'"event": "stop_continue_unasked_question", "cid": "{session}"'
        )
        self.assertIn(continue_event, log_path.read_text(encoding="utf-8"))

        # Replaying the same Stop without AGY's continuation flag is absorbed
        # by the durable per-user-turn guard and never continues twice.
        replay = self.stop_decision(
            session,
            [
                {"type": "USER_INPUT", "content": "请给我完整方案"},
                {"type": "PLANNER_RESPONSE", "content": lead_in},
            ],
        )
        self.assertEqual(replay["decision"], "allow")
        self.assertEqual(
            log_path.read_text(encoding="utf-8").count(continue_event), 1
        )

        # A caller that honors AGY's re-entry flag never continues on it.
        guarded_session = "c-unasked-active"
        guarded = self.stop_decision(
            guarded_session,
            [
                {"type": "USER_INPUT", "content": "请给我完整方案"},
                {"type": "PLANNER_RESPONSE", "content": lead_in},
            ],
            continuation_active=True,
        )
        self.assertEqual(guarded["decision"], "allow")
        self.assertNotIn(
            "continued_unasked_turn_id", read_state(self.state, guarded_session)
        )

        # The same lead-in shape is recognized with an ASCII colon.
        ascii_decision = self.stop_decision(
            "c-unasked-ascii",
            [
                {"type": "USER_INPUT", "content": "give me the plan"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "方案已就绪。\nConfirm these 2 contract boundaries:",
                },
            ],
        )
        self.assertEqual(ascii_decision["decision"], "continue")
        self.assertEqual(send_calls(self.root), [])

        # The same user turn then answers the question and emits a valid tag:
        # normal structured prefill/submit resumes despite the old turn guard.
        recovered = self.stop_decision(
            session,
            [
                {"type": "USER_INPUT", "content": "请给我完整方案"},
                {"type": "PLANNER_RESPONSE", "content": lead_in},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "先向你确认边界。",
                    "tool_calls": [{"name": "ask_question", **ask_args(1)}],
                },
                {
                    "type": "TOOL_RESPONSE",
                    "status": "DONE",
                    "content": ANSWER_HEADER + "A1: 按方案 A 取舍",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("按确认的边界推进实施", gate="auto"),
                },
            ],
        )
        self.assertEqual(recovered["decision"], "allow")
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertIn("--enter", sends[0])
        self.assertEqual(text_argument(sends[0]), "按确认的边界推进实施")

        # A new USER_INPUT is a new user turn: PreInvocation advances the turn
        # identity, so a later dangling lead-in is protected again.
        previous_turn = read_state(self.state, session)["continued_unasked_turn_id"]
        self.pre(session, "再确认一个边界")
        later = self.stop(
            session,
            "",
            steps=[
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "继续说明。\n\n请确认以下 1 个问题：",
                }
            ],
        )
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertEqual(json.loads(later.stdout)["decision"], "continue")
        self.assertNotEqual(
            read_state(self.state, session)["continued_unasked_turn_id"],
            previous_turn,
        )

    def test_unasked_question_guard_allows_plain_and_tagged_prose(self) -> None:
        # Failure means ordinary prose or a valid tagged suggestion is
        # mistaken for a dangling question lead-in and force-continued.
        plain = self.stop_decision(
            "c-plain-prose",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {"type": "PLANNER_RESPONSE", "content": "方案已就绪。"},
            ],
        )
        self.assertEqual(plain["decision"], "allow")
        self.assertIn(
            '"event": "stop_invalid_tag", "cid": "c-plain-prose", "reason": "missing_tag"',
            (self.state / "pilot.log").read_text(encoding="utf-8"),
        )

        tagged = self.stop_decision(
            "c-tagged-colon",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "范围说明：只改本模块。\n\n"
                    + suggestion("继续推进实施", gate="pause"),
                },
            ],
        )
        self.assertEqual(tagged["decision"], "allow")
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--enter", sends[0])
        self.assertEqual(text_argument(sends[0]), "继续推进实施")
        self.assertNotIn(
            '"event": "stop_continue_unasked_question"',
            (self.state / "pilot.log").read_text(encoding="utf-8"),
        )

        # An unclosed fenced block ending in `:` is code, not a lead-in.
        fenced = self.stop_decision(
            "c-unclosed-fence",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "代码如下：\n```python\nif ready:",
                },
            ],
        )
        self.assertEqual(fenced["decision"], "allow")
        self.assertNotIn(
            "continued_unasked_turn_id", read_state(self.state, "c-unclosed-fence")
        )
        self.assertIn(
            '"event": "stop_invalid_tag", "cid": "c-unclosed-fence", "reason": "missing_tag"',
            (self.state / "pilot.log").read_text(encoding="utf-8"),
        )

    def test_unasked_question_guard_fails_closed_when_state_cannot_persist(self) -> None:
        # Failure means an unwritable state directory loses the durable
        # per-user-turn guard yet still returns "continue", allowing a loop.
        session = "c-state-fail"
        steps = [
            {"type": "USER_INPUT", "content": "请给我完整方案"},
            {"type": "PLANNER_RESPONSE", "content": "请确认以下 2 个问题："},
        ]
        seeded = self.stop_decision(session, steps, continuation_active=True)
        self.assertEqual(seeded["decision"], "allow")
        self.assertTrue(state_path(self.state, session).is_file())

        # The lock and log files already exist and stay writable; only new
        # files (the state temp file) fail, so the hook can still log why.
        self.state.chmod(0o555)
        try:
            decision = self.stop_decision(session, steps)
        finally:
            self.state.chmod(0o755)
        self.assertEqual(decision["decision"], "allow")
        self.assertNotIn(
            "continued_unasked_turn_id", read_state(self.state, session)
        )
        self.assertIn(
            f'"event": "stop_continue_state_failed", "cid": "{session}"',
            (self.state / "pilot.log").read_text(encoding="utf-8"),
        )

    def test_unasked_question_guard_fails_closed_without_complete_content(self) -> None:
        # Failure means incomplete or malformed transcript evidence (a
        # truncated field or non-list tool_calls) is trusted as proof that the
        # model meant to ask, fabricating a continuation from an unknown state.
        cases = (
            (
                "content",
                {
                    "content": "请确认以下 2 个问题：",
                    "truncated_fields": ["content"],
                },
            ),
            (
                "tool-calls-empty",
                {
                    "content": "请确认以下 2 个问题：",
                    "tool_calls": [],
                    "truncated_fields": ["tool_calls"],
                },
            ),
            (
                "tool-calls-omitted",
                {
                    "content": "请确认以下 2 个问题：",
                    "truncated_fields": ["tool_calls"],
                },
            ),
            (
                "tool-calls-string",
                {
                    "content": "请确认以下 2 个问题：",
                    "tool_calls": "truncated",
                    "truncated_fields": ["tool_calls"],
                },
            ),
            (
                "tool-calls-empty-string",
                {
                    "content": "请确认以下 2 个问题：",
                    "tool_calls": "",
                },
            ),
        )
        log_path = self.state / "pilot.log"
        for label, fields in cases:
            with self.subTest(case=label):
                session = f"c-truncated-{label}"
                decision = self.stop_decision(
                    session,
                    [
                        {"type": "USER_INPUT", "content": "开始"},
                        {"type": "PLANNER_RESPONSE", **fields},
                    ],
                )
                self.assertEqual(decision["decision"], "allow")
                self.assertNotIn(
                    "continued_unasked_turn_id", read_state(self.state, session)
                )
                self.assertIn(
                    f'"event": "stop_invalid_tag", "cid": "{session}", '
                    '"reason": "missing_tag"',
                    log_path.read_text(encoding="utf-8"),
                )

    def test_unasked_question_guard_is_silent_for_dispatched_workers(self) -> None:
        # Failure means a worker turn ending on a colon is force-continued or
        # writes session state even though workers never run the pilot.
        steps = [
            {
                "type": "USER_INPUT",
                "content": "Task ID: task_abc123\nDispatch ID: disp_abc123",
            },
            {"type": "PLANNER_RESPONSE", "content": "请确认以下 2 个问题："},
        ]
        for label, conversation_id, extra_env in (
            ("preamble", "c-worker-lead", None),
            ("env", "c-worker-env", {"ORCA_TASK_ID": "task_abc123"}),
        ):
            with self.subTest(case=label):
                decision = self.stop_decision(
                    conversation_id, steps, extra_env=extra_env
                )
                self.assertEqual(decision["decision"], "allow")
                self.assertFalse(state_path(self.state, conversation_id).exists())
        log_path = self.state / "pilot.log"
        if log_path.is_file():
            self.assertNotIn(
                "stop_continue_unasked_question",
                log_path.read_text(encoding="utf-8"),
            )

    def test_unasked_question_guard_respects_mode_off(self) -> None:
        # Failure means switching the pilot off still force-continues a turn.
        self.pre("c-mode-off-lead", ",mode off")
        result = self.stop(
            "c-mode-off-lead",
            "",
            steps=[{"type": "PLANNER_RESPONSE", "content": "请确认以下 2 个问题："}],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"decision": "allow"})
        self.assertNotIn(
            "continued_unasked_turn_id", read_state(self.state, "c-mode-off-lead")
        )
        self.assertNotIn(
            '"event": "stop_continue_unasked_question", "cid": "c-mode-off-lead"',
            (self.state / "pilot.log").read_text(encoding="utf-8"),
        )

    def test_trailing_tag_ignores_markdown_and_earlier_tag_examples(self) -> None:
        # Failure means human formatting or documentation can override execution.
        prompt = "检查 `src/**/*.py`，保留 2**3 与内联 `code`"
        examples = (
            "正文提及 `<!-- pilot: scene=NONE -->`。\n",
            "```html\n<!-- pilot: scene=NONE -->\n```\n",
            "**下一步建议：**\n- **,1**：忽略这段展示文本\n- **,2**：展示备选\n",
        )
        for index, prose in enumerate(examples):
            session = f"c-prose-{index}"
            self.stop(session, prose + suggestion(prompt, alt="暂停确认", gate="pause"))
            sends = send_calls(self.root)
            self.assertEqual(len(sends), index + 1)
            self.assertEqual(text_argument(sends[-1]), prompt)
            self.assertNotIn("--enter", sends[-1])
            state = read_state(self.state, session)
            self.assertEqual([(item["index"], item["prompt"]) for item in state["candidates"]],
                             [(1, prompt), (2, "暂停确认")])
            self.assertIn("暂停确认", self.injection(self.pre(session, ",2")))

    def test_runtime_accepts_legacy_tags_and_recovery_command(self) -> None:
        """Failure means a session opened before the rename stalls on its old prompt.

        resident-fact: admission: during the one-way rename, the installed
        runtime still accepts pre-migration ``autopilot`` suggestion tags and
        the ``,autopilot recover`` command.
        """
        legacy = suggestion("继续推进实施").replace("<!-- pilot:", "<!-- autopilot:")
        self.stop("c-legacy-tag", legacy)
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertIn("--enter", sends[0])
        self.assertEqual(text_argument(sends[0]), "继续推进实施")

        self.state.mkdir(parents=True, exist_ok=True)
        for index, command in enumerate((",pilot recover", ",autopilot recover")):
            conversation = f"c-recover-{index}"
            path = state_path(self.state, conversation)
            path.write_text("{broken", encoding="utf-8")
            message = self.injection(self.pre(conversation, command))
            self.assertIn("已恢复", message)
            self.assertEqual(read_json(path)["conversation_id"], conversation)

    def test_structured_json_tag_selects_candidates(self) -> None:
        tag = json.dumps(
            {
                "scene": "plan-and-orchestrate",
                "gate": "auto",
                "1": "继续推进实施",
                "2": "先暂停确认",
            },
            ensure_ascii=False,
        )
        self.stop(
            "c-json-tag",
            "方案已就绪。\n\n"
            "- **,1（主推）**：继续推进实施\n"
            "- **,2（备选）**：先暂停确认\n"
            f"<!-- pilot: {tag} -->",
        )
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertIn("--enter", sends[0])
        self.assertEqual(text_argument(sends[0]), "继续推进实施")
        state = read_state(self.state, "c-json-tag")
        self.assertEqual(state["scene"], "plan-and-orchestrate")
        self.assertEqual(
            [(item["index"], item["prompt"]) for item in state["candidates"]],
            [(1, "继续推进实施"), (2, "先暂停确认")],
        )

    def test_comma_separated_tag_and_primary_candidate_expansion(self) -> None:
        # Legacy key=value candidate tags are rejected; only JSON tags execute candidates.
        self.stop(
            "c-legacy-kv-rejected",
            "<!-- pilot: scene=architecture-re-review, gate=auto, 1='继续推进实施：先补边界测试' -->",
        )
        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(read_state(self.state, "c-legacy-kv-rejected")["candidates"], [])

        tag = json.dumps(
            {
                "scene": "architecture-re-review",
                "gate": "auto",
                "1": "继续推进实施：先补边界测试",
            },
            ensure_ascii=False,
        )
        self.stop(
            "c-primary-expand",
            "复审完成。\n\n"
            "💡 **下一步建议 ,1（已自动预填，按 Enter 或输 ,1 发送；"
            "输 ,2 切备选）**：继续推进实施：先补边界测试\n"
            f"<!-- pilot: {tag} -->",
        )
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertEqual(text_argument(sends[0]), "继续推进实施：先补边界测试")
        state = read_state(self.state, "c-primary-expand")
        self.assertEqual(state["scene"], "architecture-re-review")
        self.assertEqual(state["gate_action"], "auto")

        message = self.injection(self.pre("c-primary-expand", ",1 先补边界测试"))
        self.assertIn("继续推进实施：先补边界测试", message)
        self.assertIn("先补边界测试", message)
        self.assertEqual(read_state(self.state, "c-primary-expand")["candidates"], [])

    def test_tag_candidates_are_authoritative_and_aliases_fail_closed(self) -> None:
        # Failure means a conflicting alias or malformed payload becomes executable.
        valid = {"scene": "plan-and-orchestrate", "gate": "pause", "1": "推进", "2": "推进", "3": "暂停"}
        self.stop("c-tag-only", "<!-- pilot: " + json.dumps(valid) + " -->")
        self.assertEqual(text_argument(send_calls(self.root)[0]), "推进")
        self.assertEqual([(item["index"], item["prompt"]) for item in read_state(self.state, "c-tag-only")["candidates"]],
                         [(1, "推进"), (3, "暂停")])
        cases = [
            ({**valid, "prompt": "另一个主推"}, "conflicting_prompt"),
            ({**valid, "alt": "另一个备选"}, "duplicate_alternative"),
            ({**valid, "1": ""}, "missing_prompt"),
            ({**valid, "1": 7}, "invalid_json"),
            ({**valid, "1": ["推进"]}, "invalid_json"),
            ({**valid, "stage": "other"}, "conflicting_scene"),
            ({**valid, "action": "auto"}, "conflicting_gate"),
            ({**valid, "unexpected": "value"}, "unknown_field"),
        ]
        for index, (fields, reason) in enumerate(cases):
            session = f"c-invalid-fields-{index}"
            self.stop(session, "<!-- pilot: " + json.dumps(fields) + " -->")
            self.assertEqual(len(send_calls(self.root)), 1)
            log = (self.state / "pilot.log").read_text(encoding="utf-8")
            self.assertIn(f'"cid": "{session}", "reason": "{reason}"', log)
        for index, (body, reason) in enumerate((
            ('{"scene":"NONE","SCENE":"NONE"}', "duplicate_field"),
            ('{"scene":"NONE",}', "invalid_json"),
            ('scene=NONE scene=NONE', "invalid_json"),
            ('scene=plan-and-orchestrate gate=pause 1="推进"', "invalid_json"),
        )):
            session = f"c-malformed-{index}"
            self.stop(session, "<!-- pilot: " + body + " -->")
            self.assertEqual(len(send_calls(self.root)), 1)
            self.assertEqual(read_state(self.state, session)["candidates"], [])
            log = (self.state / "pilot.log").read_text(encoding="utf-8")
            self.assertIn(f'"cid": "{session}", "reason": "{reason}"', log)
        for index, body in enumerate(('{"scene":"NONE"}', 'scene=NONE')):
            self.stop(f"c-none-{index}", "<!-- pilot: " + body + " -->")
            self.assertEqual(len(send_calls(self.root)), 1)

    def test_prompt_literals_survive_parsing(self) -> None:
        # Failure means structured text is modified by presentation cleanup or comment splitting.
        prompts = (
            "仅检查 src/**/*.py 与 2**3 的差异",
            "`继续推进`",
            "第一行\n第二行：\"原样\"",
            "Search for <!--secret--> and keep --> literal",
            '排查 <!-- pilot: {"scene": "NONE"} --> 标签解析',
        )
        for index, prompt in enumerate(prompts):
            self.stop(f"c-literal-{index}", suggestion(prompt, alt="备选 <!--alt--> -->", gate="pause") + " \n")
            sends = send_calls(self.root)
            self.assertEqual(len(sends), index + 1)
            self.assertEqual(text_argument(sends[-1]), prompt)
            state = read_state(self.state, f"c-literal-{index}")
            self.assertEqual(
                [(item["index"], item["prompt"]) for item in state["candidates"]],
                [(1, prompt), (2, "备选 <!--alt--> -->")],
            )
        self.stop("c-not-trailing", suggestion("不执行") + "\n这是后续正文")
        self.stop("c-no-tag-prompt", "主推：不执行\n<!-- pilot: scene=plan-and-orchestrate gate=pause -->")
        self.assertEqual(len(send_calls(self.root)), len(prompts))

    def test_earlier_ask_question_blocks_later_suggestion(self) -> None:
        steps = [
            {"type": "USER_INPUT", "content": "怎么办"},
            {
                "type": "PLANNER_RESPONSE",
                "content": "需要确认。",
                "tool_calls": [{"name": "ask_question", **ask_args(1)}],
            },
            {
                "type": "PLANNER_RESPONSE",
                "content": suggestion("继续推进", gate="auto"),
            },
        ]
        self.stop("c-ask-earlier", "", steps=steps)
        self.assertEqual(send_calls(self.root), [])

    def test_same_turn_answered_ask_question_allows_the_suggestion(self) -> None:
        # Failure means a synchronous question answered inside the same turn
        # still freezes the suggestion the model produced after the answer.
        cases = (
            (
                "ask-question",
                [{"name": "ask_question", **ask_args(2)}],
                [
                    {
                        "type": "TOOL_RESPONSE",
                        "status": "DONE",
                        "content": ANSWER_HEADER
                        + "A1: 选方案 A\n补充：先补回归测试\nA2: 同意继续",
                    }
                ],
            ),
            (
                "ask-user-question",
                [{"name": "AskUserQuestion", **ask_args(1)}],
                [
                    {
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": ANSWER_HEADER + '{"answers": {"q1": "选方案 A"}}',
                    }
                ],
            ),
            (
                "request-user-input",
                [{"name": "request_user_input", **ask_args(1)}],
                [
                    {
                        "type": "TOOL_RESPONSE",
                        "status": "DONE",
                        "content": ANSWER_HEADER
                        + '{"answers": {"q1": {"answers": ["选方案 A"]}}}',
                    }
                ],
            ),
            (
                "parallel",
                [{"name": "run_command"}, {"name": "ask_question", **ask_args(1)}],
                [
                    {"type": "GENERIC", "status": "DONE", "content": "OK"},
                    {
                        "type": "TOOL_RESPONSE",
                        "status": "DONE",
                        "content": ANSWER_HEADER + "A1: 选方案 A",
                    },
                ],
            ),
        )
        for label, tool_calls, responses in cases:
            for gate, enter in (("pause", False), ("auto", True)):
                with self.subTest(case=label, gate=gate):
                    session = f"c-answered-{label}-{gate}"
                    steps = [
                        {"type": "USER_INPUT", "content": "选哪个方案"},
                        {
                            "type": "PLANNER_RESPONSE",
                            "content": "需要你确认方案。",
                            "tool_calls": tool_calls,
                        },
                        *responses,
                        {
                            "type": "PLANNER_RESPONSE",
                            "content": suggestion(
                                "按方案 A 推进", alt="先补回归测试", gate=gate
                            ),
                        },
                    ]
                    before = len(send_calls(self.root))
                    self.stop(session, "", steps=steps)
                    sends = send_calls(self.root)[before:]
                    self.assertEqual(len(sends), 1)
                    self.assertEqual("--enter" in sends[0], enter)
                    self.assertEqual(text_argument(sends[0]), "按方案 A 推进")
                    state = read_state(self.state, session)
                    self.assertEqual(
                        [item["index"] for item in state["candidates"]], [1, 2]
                    )
                    self.assertEqual(state["gate_action"], gate)
                    log = (self.state / "pilot.log").read_text(encoding="utf-8")
                    self.assertNotIn(
                        f'"event": "stop_waiting_ask_question", "cid": "{session}"',
                        log,
                    )

    def test_compact_transcript_hydrates_truncated_tool_calls(self) -> None:
        # Failure means a compact transcript hides the question count, so a
        # partial answer is settled as if every question had been answered.
        user_line = json.dumps(
            {"type": "USER_INPUT", "content": "选哪个方案"}, ensure_ascii=False
        )

        def full_lines(tool: str) -> list[str]:
            return [
                user_line,
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "content": "需要你确认方案。",
                        "tool_calls": [{"name": tool, "args": ask_args(2)["args"]}],
                    },
                    ensure_ascii=False,
                ),
            ]

        partial_cases = (
            ("ask-question", "ask_question", "A1: 选方案 A"),
            (
                "ask-user-question",
                "AskUserQuestion",
                '{"answers": {"q1": "选方案 A"}}',
            ),
        )
        for label, tool, receipt in partial_cases:
            with self.subTest(case=label, hydration="partial"):
                session = f"c-compact-partial-{label}"
                self.stop_compact(
                    session,
                    tool=tool,
                    receipt=receipt,
                    full_lines=full_lines(tool),
                    gate="auto",
                )
                self.assertEqual(send_calls(self.root), [])
                log = (self.state / "pilot.log").read_text(encoding="utf-8")
                self.assertIn(
                    f'"event": "stop_waiting_ask_question", "cid": "{session}", '
                    '"reason": "invalid_answer_receipt"',
                    log,
                )

        answered_cases = (
            ("ask-question", "ask_question", "A1: 选方案 A\nA2: 同意继续"),
            (
                "ask-user-question",
                "AskUserQuestion",
                '{"answers": {"q1": "选方案 A", "q2": "同意继续"}}',
            ),
        )
        for index, (label, tool, receipt) in enumerate(answered_cases, start=1):
            with self.subTest(case=label, hydration="complete"):
                self.stop_compact(
                    f"c-compact-answered-{label}",
                    tool=tool,
                    receipt=receipt,
                    full_lines=full_lines(tool),
                    gate="pause",
                )
                sends = send_calls(self.root)
                self.assertEqual(len(sends), index)
                self.assertNotIn("--enter", sends[-1])
                self.assertEqual(text_argument(sends[-1]), "按方案 A 推进")

    def test_failed_compact_hydration_keeps_partial_answers_unsettled(self) -> None:
        # Failure means a question count the full transcript cannot supply is
        # inferred from the answer, so a partial answer auto-submits.
        user_line = json.dumps(
            {"type": "USER_INPUT", "content": "选哪个方案"}, ensure_ascii=False
        )
        failures = (
            ("missing", None, "transcript_full_missing"),
            ("corrupt", [user_line, "{not-json"], "transcript_full_corrupt_line"),
            (
                "mismatch",
                [user_line, json.dumps({"type": "GENERIC", "content": "x"})],
                "transcript_full_step_mismatch",
            ),
            ("short", [user_line], "transcript_full_short"),
        )
        receipts = (
            ("ask_question", "A1: 选方案 A"),
            ("AskUserQuestion", '{"answers": {"q1": "选方案 A"}}'),
        )
        for tool, receipt in receipts:
            for label, lines, event in failures:
                with self.subTest(tool=tool, case=label):
                    session = f"c-hydrate-{tool}-{label}"
                    before = len(send_calls(self.root))
                    self.stop_compact(
                        session,
                        tool=tool,
                        receipt=receipt,
                        full_lines=lines,
                        gate="auto",
                    )
                    self.assertEqual(len(send_calls(self.root)), before)
                    self.assertEqual(read_state(self.state, session)["candidates"], [])
                    log = (self.state / "pilot.log").read_text(encoding="utf-8")
                    self.assertIn(f'"event": "{event}"', log)
                    self.assertIn(
                        f'"event": "stop_waiting_ask_question", "cid": "{session}", '
                        '"reason": "tool_calls_truncated"',
                        log,
                    )

    def test_unsettled_ask_question_still_blocks_the_suggestion(self) -> None:
        # Failure means a question the user did not positively answer admits
        # prefill: every branch names its deterministic unsettled reason.
        done = {"type": "TOOL_RESPONSE", "status": "DONE"}
        numbered = ANSWER_HEADER + "A1: 选方案 A"
        ask = {"name": "ask_question", **ask_args(1)}
        structured = {"name": "AskUserQuestion", **ask_args(1)}
        cases = (
            ("missing-response", [ask], [], "missing_response"),
            (
                "status-running",
                [ask],
                [dict(done, content=numbered, status="RUNNING")],
                "status_not_done",
            ),
            (
                "status-error",
                [ask],
                [dict(done, content=numbered, status="ERROR")],
                "status_not_done",
            ),
            (
                "status-invalid",
                [ask],
                [dict(done, content=numbered, status="INVALID")],
                "status_not_done",
            ),
            (
                "missing-status",
                [ask],
                [{"type": "TOOL_RESPONSE", "content": numbered}],
                "status_not_done",
            ),
            (
                "response-error",
                [ask],
                [dict(done, content=numbered, error="boom")],
                "response_error",
            ),
            ("header-only-body", [ask], [dict(done, content=ANSWER_HEADER)], "empty_answer"),
            ("empty-body", [ask], [dict(done, content="")], "empty_answer"),
            (
                "dict-content",
                [ask],
                [dict(done, content={"answers": {"q1": "选方案 A"}})],
                "non_string_content",
            ),
            ("numeric-content", [ask], [dict(done, content=1)], "non_string_content"),
            (
                "prose-cancelled",
                [ask],
                [dict(done, content=ANSWER_HEADER + "用户取消了提问")],
                "invalid_answer_receipt",
            ),
            (
                "prose-answer",
                [structured],
                [dict(done, content=ANSWER_HEADER + "选方案 A")],
                "invalid_answer_receipt",
            ),
            (
                "empty-answers",
                [structured],
                [dict(done, content=ANSWER_HEADER + '{"answers": {}}')],
                "invalid_answer_receipt",
            ),
            (
                "empty-string-answer",
                [structured],
                [dict(done, content=ANSWER_HEADER + '{"answers": {"q1": ""}}')],
                "invalid_answer_receipt",
            ),
            (
                "empty-list-answer",
                [{"name": "request_user_input", **ask_args(1)}],
                [dict(done, content=ANSWER_HEADER + '{"answers": {"q1": []}}')],
                "invalid_answer_receipt",
            ),
            (
                "cancelled-receipt",
                [structured],
                [
                    dict(
                        done,
                        content=ANSWER_HEADER
                        + '{"cancelled": true, "answers": {"q1": "选方案 A"}}',
                    )
                ],
                "invalid_answer_receipt",
            ),
            (
                "pending-status-receipt",
                [structured],
                [
                    dict(
                        done,
                        content=ANSWER_HEADER
                        + '{"status": "pending", "answers": {"q1": "选方案 A"}}',
                    )
                ],
                "invalid_answer_receipt",
            ),
            (
                "structured-count-mismatch",
                [{"name": "AskUserQuestion", **ask_args(2)}],
                [dict(done, content=ANSWER_HEADER + '{"answers": {"q1": "选方案 A"}}')],
                "invalid_answer_receipt",
            ),
            (
                "numbered-count-mismatch",
                [{"name": "ask_question", **ask_args(2)}],
                [dict(done, content=numbered)],
                "invalid_answer_receipt",
            ),
            (
                "numbered-gap",
                [{"name": "ask_question", **ask_args(2)}],
                [dict(done, content=ANSWER_HEADER + "A1: 选方案 A\nA3: 同意继续")],
                "invalid_answer_receipt",
            ),
            (
                "content-truncated",
                [ask],
                [dict(done, content=numbered, truncated_fields=["content"])],
                "content_truncated",
            ),
            (
                "second-call-unanswered",
                [{"name": "run_command"}, {"name": "AskUserQuestion", **ask_args(1)}],
                [dict(done, content=numbered)],
                "missing_response",
            ),
            (
                "async-receipt",
                [{"name": "request_user_input_async", **ask_args(1)}],
                [dict(done, content=numbered)],
                "async_question",
            ),
            (
                "tool-calls-truncated",
                [{"name": "ask_question", **ask_args(2)}],
                [dict(done, content=numbered)],
                "tool_calls_truncated",
            ),
            (
                "missing-question-args",
                [{"name": "ask_question"}],
                [dict(done, content=numbered)],
                "invalid_question_args",
            ),
        )
        for label, tool_calls, responses, reason in cases:
            with self.subTest(case=label):
                session = f"c-unsettled-{label}"
                planner: dict = {
                    "type": "PLANNER_RESPONSE",
                    "content": "需要你确认方案。",
                    "tool_calls": tool_calls,
                }
                if label == "tool-calls-truncated":
                    planner["truncated_fields"] = ["tool_calls"]
                steps = [
                    {"type": "USER_INPUT", "content": "选哪个方案"},
                    planner,
                    *responses,
                    {
                        "type": "PLANNER_RESPONSE",
                        "content": suggestion("按方案 A 推进", gate="auto"),
                    },
                ]
                before = len(send_calls(self.root))
                self.stop(session, "", steps=steps)
                self.assertEqual(len(send_calls(self.root)), before)
                self.assertEqual(read_state(self.state, session)["candidates"], [])
                log = (self.state / "pilot.log").read_text(encoding="utf-8")
                self.assertIn(
                    f'"event": "stop_waiting_ask_question", "cid": "{session}", '
                    f'"reason": "{reason}"',
                    log,
                )

    def test_single_candidate_tag_may_omit_the_alternative(self) -> None:
        # Failure means the documented single-candidate tag shape is rejected
        # instead of pre-filling the structured primary.
        self.stop(
            "c-single-tag",
            "方案已就绪。\n\n"
            "💡 **下一步建议 ,1（已自动预填，按 Enter 或输 ,1 发送）**：按方案 A 推进\n"
            '<!-- pilot: {"scene": "plan-and-orchestrate", "gate": "pause", '
            '"1": "按方案 A 推进"} -->',
        )
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--enter", sends[0])
        self.assertEqual(text_argument(sends[0]), "按方案 A 推进")
        state = read_state(self.state, "c-single-tag")
        self.assertEqual([item["index"] for item in state["candidates"]], [1])

    def test_incomplete_transcript_never_auto_submits(self) -> None:
        transcript = self.root / "bad-transcript.jsonl"
        transcript.write_text(
            "{not-json}\n"
            + json.dumps({"type": "USER_INPUT", "content": "开始"})
            + "\n"
            + json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        result = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-bad-transcript",
                transcriptPath=str(transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
                workspacePaths=[str(self.workspace)],
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(send_calls(self.root), [])
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("stop_transcript_incomplete", log)

    def test_ambiguous_or_mismatched_terminal_never_writes(self) -> None:
        self.stop(
            "c-terminal-ambiguous",
            suggestion("继续推进", gate="auto"),
            extra_env={"FAKE_TERMINAL_COUNT": "2"},
        )
        self.stop(
            "c-terminal-mismatch",
            suggestion("继续推进", gate="auto"),
            extra_env={"FAKE_WORKTREE_PATH": str(self.root / "other-workspace")},
        )
        self.assertEqual(send_calls(self.root), [])

    def test_explicit_handle_without_workspace_never_writes(self) -> None:
        transcript = write_transcript(
            self.root,
            "no-workspace.jsonl",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
            ],
        )
        result = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-no-workspace",
                transcriptPath=str(transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            ),
            extra_env={"ORCA_TERMINAL_HANDLE": "term_test"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(send_calls(self.root), [])
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("terminal_workspace_missing", log)

    def test_duplicate_stop_for_same_turn_submits_once(self) -> None:
        """Failure means a repeated Stop can repeat the Enter for the same turn.

        resident-fact: settlement: both accepted and rejected sends consume
        the unique delivery attempt for their Stop turn.
        """
        for accepted in ("1", "0"):
            with self.subTest(accepted=accepted):
                cid = f"same-turn-{accepted}"
                transcript = write_transcript(
                    self.root,
                    f"transcript-{cid}.jsonl",
                    [
                        {"type": "USER_INPUT", "content": "开始"},
                        {
                            "type": "PLANNER_RESPONSE",
                            "content": suggestion("继续推进", gate="auto"),
                        },
                    ],
                )
                body = payload(
                    cid,
                    transcriptPath=str(transcript),
                    terminationReason="NO_TOOL_CALL",
                    fullyIdle=True,
                    workspacePaths=[str(self.workspace)],
                )
                previous_sends = len(send_calls(self.root))
                for _ in range(2):
                    result = run_engine(
                        self.root, self.gemini, self.state, "hook", "--event", "Stop",
                        stdin=body, extra_env={"FAKE_SEND_ACCEPTED": accepted},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(send_calls(self.root)), previous_sends + 1)
                expected_status = "accepted" if accepted == "1" else "rejected"
                self.assertEqual(read_state(self.state, cid)["delivery_status"], expected_status)

    def test_unknown_delivery_freezes_until_manual_input(self) -> None:
        self.stop(
            "c-unknown",
            suggestion("继续推进", gate="auto"),
            extra_env={"FAKE_SEND_RESULT": "invalid"},
        )
        state = read_state(self.state, "c-unknown")
        self.assertEqual(state["delivery_status"], "unknown")
        self.assertEqual(state["consecutive_auto_turns"], 1)

        self.stop("c-unknown", suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 1)

        self.pre("c-unknown", "我自己接手")
        state = read_state(self.state, "c-unknown")
        self.assertEqual(state["delivery_status"], "cleared")
        self.assertEqual(state["consecutive_auto_turns"], 0)
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("delivery_state_cleared", log)

        self.stop("c-unknown", suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 2)

    def test_candidate_index_uses_structured_number(self) -> None:
        text = (
            "方案已就绪。\n\n💡 下一步建议：主推方案\n"
            "↳ 备选 ,3：第三方案\n"
            '<!-- pilot: {"scene":"plan-and-orchestrate","gate":"pause","1":"主推方案","3":"第三方案"} -->'
        )
        self.pre("c-cand-missing", "开始")
        self.stop("c-cand-missing", text)
        self.assertEqual(self.injection(self.pre("c-cand-missing", ",2")), "")
        self.assertEqual(read_state(self.state, "c-cand-missing")["candidates"], [])

        self.pre("c-cand-third", "开始")
        self.stop("c-cand-third", text)
        self.assertIn("第三方案", self.injection(self.pre("c-cand-third", ",3")))

    def test_plain_input_invalidates_candidates(self) -> None:
        self.pre("c-clear", "开始")
        self.stop("c-clear", suggestion("主推", alt="备选", gate="pause"))
        self.assertEqual(json.loads(self.pre("c-clear", "换一个方向").stdout), {})
        self.assertEqual(read_state(self.state, "c-clear")["candidates"], [])
        self.assertEqual(json.loads(self.pre("c-clear", ",2").stdout), {})

    def test_set_alias_updates_canonical_command_and_survives_upgrade(self) -> None:
        apply_profile(
            self.root,
            self.gemini,
            "--profile",
            json.dumps(
                {
                    "shortcuts": {
                        "custom_comma": {
                            "ts": {
                                "template": "先跑最小定向测试",
                                "aliases": [",t1"],
                            }
                        }
                    }
                },
                ensure_ascii=False,
            ),
        )
        result = run_engine(
            self.root, self.gemini, self.state, "set", ",t1", "改后的测试模板"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(
            profile["shortcuts"]["template_overrides"]["ts"], "改后的测试模板"
        )
        config = read_json(self.gemini / CONFIG_REL)
        command = next(
            item
            for item in config["comma_commands"].values()
            if ",ts" in item["aliases"]
        )
        self.assertEqual(command["template"], "改后的测试模板")

        apply_profile(self.root, self.gemini, "--upgrade")
        config = read_json(self.gemini / CONFIG_REL)
        command = next(
            item
            for item in config["comma_commands"].values()
            if ",ts" in item["aliases"]
        )
        self.assertEqual(command["template"], "改后的测试模板")
        self.assertEqual(command["canonical_key"], "ts")

    def test_stale_stop_after_new_pre_invocation_never_sends(self) -> None:
        old_transcript = write_transcript(
            self.root,
            "old-turn.jsonl",
            [
                {"type": "USER_INPUT", "content": "turn1"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
            ],
        )
        self.pre("c-stale", "turn1")
        self.pre("c-stale", "turn2")
        result = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-stale",
                transcriptPath=str(old_transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
                workspacePaths=[str(self.workspace)],
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(send_calls(self.root), [])
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("stop_stale_turn", log)

    def test_stop_requires_explicit_idle_evidence(self) -> None:
        transcript = write_transcript(
            self.root,
            "idle-evidence.jsonl",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
            ],
        )
        base = {
            "transcriptPath": str(transcript),
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "workspacePaths": [str(self.workspace)],
        }
        missing_idle = dict(base)
        missing_idle.pop("fullyIdle")
        missing_termination = dict(base)
        missing_termination.pop("terminationReason")
        for conversation_id, fields in (
            ("c-no-idle", missing_idle),
            ("c-no-termination", missing_termination),
        ):
            result = run_engine(
                self.root,
                self.gemini,
                self.state,
                "hook",
                "--event",
                "Stop",
                stdin=payload(conversation_id, **fields),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(send_calls(self.root), [])

    def test_concurrent_profile_writers_preserve_both_changes(self) -> None:
        transcript = write_transcript(
            self.root,
            "concurrent-mode.jsonl",
            [{"type": "USER_INPUT", "content": ",mode confirm"}],
        )
        env = engine_env(self.root, self.gemini, self.state)
        engine = str(self.gemini / ENGINE_REL)
        mode_process = subprocess.Popen(
            [sys.executable, engine, "hook", "--event", "PreInvocation"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        set_process = subprocess.Popen(
            [sys.executable, engine, "set", "r", "并发模板 {arg}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        mode_stdout, mode_stderr = mode_process.communicate(
            payload(
                "c-concurrent",
                lastUserInput=",mode confirm",
                transcriptPath=str(transcript),
                workspacePaths=[str(self.workspace)],
            ),
            timeout=30,
        )
        set_stdout, set_stderr = set_process.communicate(timeout=30)
        self.assertEqual(mode_process.returncode, 0, mode_stderr or mode_stdout)
        self.assertEqual(set_process.returncode, 0, set_stderr or set_stdout)
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(profile["pilot"]["mode"], "confirm")
        self.assertEqual(
            profile["shortcuts"]["template_overrides"]["r"], "并发模板 {arg}"
        )

    def test_upgrade_and_template_set_are_serialized(self) -> None:
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        setup_process = subprocess.Popen(
            [
                sys.executable,
                str(SETUP_SCRIPT),
                "apply",
                "--upgrade",
                "--project",
            str(PROJECT_PATH),
            "--gemini-dir",
                str(self.gemini),
                "--json",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=base_env(self.root),
        )
        set_process = subprocess.Popen(
            [
                sys.executable,
                str(self.gemini / ENGINE_REL),
                "set",
                "r",
                "升级并发模板 {arg}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=engine_env(self.root, self.gemini, self.state),
        )
        setup_stdout, setup_stderr = setup_process.communicate(timeout=60)
        set_stdout, set_stderr = set_process.communicate(timeout=60)
        self.assertEqual(
            setup_process.returncode, 0, setup_stderr or setup_stdout
        )
        self.assertEqual(set_process.returncode, 0, set_stderr or set_stdout)
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(
            profile["shortcuts"]["template_overrides"]["r"], "升级并发模板 {arg}"
        )

    def test_corrupt_profile_fails_closed_across_commands_and_hooks(self) -> None:
        """Verify that corrupt profile.json fails closed with exit code 2 and diagnostics across CLI commands and hooks.

        If this test fails, a corrupt profile may be treated as empty
        preferences or a hook may act without surfacing the failure.
        resident-fact: fail-closed: a corrupt profile.json makes status, mode,
        worker, sync and both hooks exit 2 with diagnostics, while the
        session's loaded worker list stays intact and nothing is sent.
        """
        self.pre("c-corrupt-profile", "初始化会话")
        loaded = read_state(self.state, "c-corrupt-profile")["loaded_worker_agents"]
        profile_path = self.gemini / PROFILE_REL
        profile_path.write_bytes(b"{broken")
        gemini_md = self.gemini / GEMINI_REL
        gemini_before = gemini_md.read_bytes()

        status = run_engine(self.root, self.gemini, self.state, "status", "--json")
        self.assertEqual(status.returncode, 2, status.stderr)
        report = json.loads(status.stdout)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("profile.json" in item for item in report["errors"]),
            report["errors"],
        )

        mode = run_engine(self.root, self.gemini, self.state, "mode", "status")
        self.assertEqual(mode.returncode, 2)
        self.assertIn("profile.json 损坏", mode.stderr)

        worker = run_engine(
            self.root, self.gemini, self.state, "worker", "status", "--json"
        )
        self.assertEqual(worker.returncode, 2)
        worker_report = json.loads(worker.stdout)
        self.assertFalse(worker_report["ok"])
        self.assertEqual(worker_report["worker_agents"], [])
        self.assertIn("profile.json", worker_report["error"])

        sync = run_engine(self.root, self.gemini, self.state, "sync")
        self.assertEqual(sync.returncode, 2)
        self.assertIn("profile.json 损坏", sync.stderr)
        self.assertEqual(gemini_md.read_bytes(), gemini_before)

        pre = self.pre("c-corrupt-profile", "继续任务")
        self.assertEqual(pre.returncode, 2)
        self.assertEqual(pre.stdout, "")
        self.assertEqual(json.loads(pre.stderr)["error"], "config_corrupt")

        stop = self.stop("c-corrupt-profile", suggestion("继续推进", gate="auto"))
        self.assertEqual(stop.returncode, 2)
        self.assertEqual(stop.stdout, "")
        self.assertEqual(json.loads(stop.stderr)["error"], "config_corrupt")

        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(
            read_state(self.state, "c-corrupt-profile")["loaded_worker_agents"],
            loaded,
        )
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("pre_invocation_config_corrupt", log)
        self.assertIn("stop_config_corrupt", log)

    def test_corrupt_config_fails_closed_in_status_set_and_pre_invocation(self) -> None:
        """Verify that corrupt config.json fails closed with exit code 2 and diagnostics in status, set and PreInvocation.

        If this test fails, a corrupt config may be treated as default
        shortcuts or a failed write may proceed against unreadable data.
        resident-fact: fail-closed: a corrupt config.json makes status, set and
        PreInvocation exit 2 with diagnostics, leaves the installed config
        untouched, and preserves the session's loaded worker list.
        """
        self.pre("c-corrupt-config", "初始化会话")
        loaded = read_state(self.state, "c-corrupt-config")["loaded_worker_agents"]
        config_path = self.gemini / CONFIG_REL
        config_path.write_bytes(b"{broken")

        status = run_engine(self.root, self.gemini, self.state, "status", "--json")
        self.assertEqual(status.returncode, 2, status.stderr)
        report = json.loads(status.stdout)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("config.json" in item for item in report["errors"]),
            report["errors"],
        )

        set_result = run_engine(
            self.root, self.gemini, self.state, "set", "r", "新模板 {arg}"
        )
        self.assertEqual(set_result.returncode, 2)
        self.assertIn("config.json 损坏", set_result.stderr)
        self.assertEqual(config_path.read_bytes(), b"{broken")

        pre = self.pre("c-corrupt-config", "继续任务")
        self.assertEqual(pre.returncode, 2)
        self.assertEqual(pre.stdout, "")
        self.assertEqual(json.loads(pre.stderr)["error"], "config_corrupt")
        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(
            read_state(self.state, "c-corrupt-config")["loaded_worker_agents"],
            loaded,
        )
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("pre_invocation_config_corrupt", log)

    def test_missing_profile_and_config_are_distinct_from_empty_object_and_fail_closed(self) -> None:
        """Verify that absent profile/config files are reported as missing, not as empty configuration.

        If this test fails, status, mode, worker or the hooks may silently treat
        absent installed configuration as valid empty defaults.
        resident-fact: fail-closed: an on-disk {} profile is valid ("ok"), while
        a missing profile or config reports "missing", exits 2 with null mode,
        and refuses hooks with config_missing and zero terminal sends.
        """
        profile_path = self.gemini / PROFILE_REL
        config_path = self.gemini / CONFIG_REL
        valid_profile = profile_path.read_bytes()
        write_json(profile_path, {})

        empty_profile = run_engine(
            self.root, self.gemini, self.state, "status", "--json"
        )
        self.assertEqual(empty_profile.returncode, 0, empty_profile.stderr)
        empty_report = json.loads(empty_profile.stdout)
        self.assertEqual(empty_report["profile_status"], "ok")
        self.assertTrue(empty_report["ok"])

        profile_path.unlink()
        missing_profile = run_engine(
            self.root, self.gemini, self.state, "status", "--json"
        )
        self.assertEqual(missing_profile.returncode, 2)
        report = json.loads(missing_profile.stdout)
        self.assertFalse(report["ok"])
        self.assertFalse(report["installed"])
        self.assertEqual(report["profile_status"], "missing")
        self.assertIsNone(report["mode"])
        self.assertEqual(report["worker_agents"], [])
        self.assertIsNone(report["max_consecutive_auto_turns"])

        mode = run_engine(self.root, self.gemini, self.state, "mode", "status")
        self.assertEqual(mode.returncode, 2)
        self.assertIn("未安装（缺少 profile.json）", mode.stderr)

        worker = run_engine(
            self.root, self.gemini, self.state, "worker", "status", "--json"
        )
        self.assertEqual(worker.returncode, 2)
        worker_report = json.loads(worker.stdout)
        self.assertFalse(worker_report["ok"])
        self.assertEqual(worker_report["status"], "missing")

        pre = self.pre("c-missing-profile", "继续任务")
        self.assertEqual(pre.returncode, 2)
        self.assertEqual(pre.stdout, "")
        self.assertEqual(json.loads(pre.stderr)["error"], "config_missing")
        self.assertFalse(state_path(self.state, "c-missing-profile").exists())

        stop = self.stop("c-missing-profile", suggestion("继续推进", gate="auto"))
        self.assertEqual(stop.returncode, 2)
        self.assertEqual(stop.stdout, "")
        self.assertEqual(json.loads(stop.stderr)["error"], "config_missing")
        self.assertEqual(send_calls(self.root), [])

        profile_path.write_bytes(valid_profile)
        config_path.unlink()
        missing_config = run_engine(
            self.root, self.gemini, self.state, "status", "--json"
        )
        self.assertEqual(missing_config.returncode, 2)
        config_report = json.loads(missing_config.stdout)
        self.assertFalse(config_report["installed"])
        self.assertEqual(config_report["config_status"], "missing")
        self.assertEqual(config_report["entry_shortcuts"], [])
        self.assertEqual(config_report["comma_commands"], [])

        pre = self.pre("c-missing-config", "继续任务")
        self.assertEqual(pre.returncode, 2)
        self.assertEqual(json.loads(pre.stderr)["error"], "config_missing")
        self.assertEqual(send_calls(self.root), [])

    def test_corrupt_session_recovery_requires_valid_installed_configuration(self) -> None:
        """Verify that ,pilot recover refuses a corrupt session while the installed profile is corrupt.

        If this test fails, recovery may certify automatic delivery against an
        unreadable profile or overwrite and back up a corrupt session file anyway.
        resident-fact: fail-closed: config corruption is reported before session
        recovery, leaves the corrupt session untouched, and recovery succeeds
        once the profile is valid again.
        """
        conversation_id = "c-recover-corrupt"
        session = state_path(self.state, conversation_id)
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_bytes(b"{broken")
        profile_path = self.gemini / PROFILE_REL
        valid_profile = profile_path.read_bytes()
        profile_path.write_bytes(b"{broken")

        refused = self.pre(conversation_id, ",pilot recover")
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stdout, "")
        self.assertEqual(json.loads(refused.stderr)["error"], "config_corrupt")
        self.assertEqual(session.read_bytes(), b"{broken")
        self.assertEqual(
            list(session.parent.glob(session.name + ".corrupt-*")), []
        )

        profile_path.write_bytes(valid_profile)
        recovered = self.pre(conversation_id, ",pilot recover")
        self.assertIn("状态已恢复", self.injection(recovered))
        self.assertNotEqual(session.read_bytes(), b"{broken")

    def test_unreadable_scene_fails_closed_across_commands_and_hooks(self) -> None:
        """Verify that an unreadable scene file fails closed with exit code 2 across CLI commands and hooks.

        If this test fails, routing may proceed with an incomplete scene set or
        an unreadable scene may be silently omitted from status and sync.
        resident-fact: fail-closed: an unreadable scene file makes status, sync,
        PreInvocation (including `,mode status`) and Stop exit 2, and Stop
        refuses to deliver against an incomplete scene set.
        """
        self.pre("c-scene-corrupt", "初始化会话")
        (self.gemini / SCENES_REL / "99-broken.md").mkdir()
        gemini_md = self.gemini / GEMINI_REL
        gemini_before = gemini_md.read_bytes()

        status = run_engine(self.root, self.gemini, self.state, "status", "--json")
        self.assertEqual(status.returncode, 2, status.stderr)
        report = json.loads(status.stdout)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("99-broken.md" in item for item in report["errors"]),
            report["errors"],
        )

        sync = run_engine(self.root, self.gemini, self.state, "sync")
        self.assertEqual(sync.returncode, 2)
        self.assertIn("场景文件不可读", sync.stderr)
        self.assertEqual(gemini_md.read_bytes(), gemini_before)

        pre = self.pre("c-scene-corrupt", "继续任务")
        self.assertEqual(pre.returncode, 2)
        self.assertEqual(pre.stdout, "")
        self.assertEqual(json.loads(pre.stderr)["error"], "config_corrupt")

        mode = self.pre("c-scene-corrupt", ",mode status")
        self.assertEqual(mode.returncode, 2)
        self.assertEqual(mode.stdout, "")
        self.assertEqual(json.loads(mode.stderr)["error"], "config_corrupt")

        stop = self.stop("c-scene-corrupt", suggestion("继续推进", gate="auto"))
        self.assertEqual(stop.returncode, 2)
        self.assertEqual(stop.stdout, "")
        self.assertEqual(json.loads(stop.stderr)["error"], "config_corrupt")
        self.assertEqual(send_calls(self.root), [])
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("pre_invocation_config_corrupt", log)
        self.assertIn("stop_config_corrupt", log)

    def test_hook_rejects_invalid_payload_and_internal_errors(self) -> None:
        """Verify that malformed hook payloads and internal failures exit 2 with structured JSON errors.

        If this test fails, a broken hook input or runtime bug may exit 0 as a
        fake allow/no-op instead of surfacing the failure.
        resident-fact: observability: invalid hook payloads and internal
        exceptions exit 2 with a structured JSON error instead of a fake
        allow, and both shapes are recorded in the runtime log.
        """
        malformed = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "PreInvocation",
            stdin="{not json",
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(malformed.stdout, "")
        failure = json.loads(malformed.stderr)
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["event"], "PreInvocation")
        self.assertEqual(failure["error"], "invalid_payload")

        not_object = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin="[]",
        )
        self.assertEqual(not_object.returncode, 2)
        self.assertEqual(not_object.stdout, "")
        self.assertEqual(
            json.loads(not_object.stderr)["detail"],
            "hook payload must be a JSON object",
        )
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("payload_invalid", log)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stderr = io.StringIO()
            with mock.patch.object(runtime, "STATE_DIR", root), mock.patch.object(
                runtime, "LOG_PATH", root / "pilot.log"
            ), mock.patch.object(
                runtime.sys, "stdin", io.StringIO(payload("c-internal"))
            ), mock.patch.object(
                runtime.sys, "stderr", stderr
            ), mock.patch.object(
                runtime, "handle_pre_invocation", side_effect=RuntimeError("boom")
            ):
                code = runtime.run_hook("PreInvocation")
            self.assertEqual(code, 2)
            internal = json.loads(stderr.getvalue())
            self.assertEqual(internal["error"], "internal_error")
            self.assertIn("boom", internal["detail"])
            self.assertIn("hook_failed", (root / "pilot.log").read_text(encoding="utf-8"))

    def test_osc_pre_invocation_reports_working_once(self) -> None:
        """A turn start reports working, including for dispatched workers.

        Failure means Orca keeps the pane in an unknown state while agy is
        already working, or the frame leaks into the hook's JSON stdout.
        resident-fact: status: PreInvocation 只在控制终端发出一次
        `state=working`，与是否由 Pilot 投递无关。
        """
        target = self.root / "osc-working.log"
        result = self.pre(
            "c-osc-pre", "开始", extra_env={"THE_PILOT_OSC_TARGET": str(target)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_WORKING)
        self.assertNotIn("\x1b]7501;", result.stdout)

        worker_target = self.root / "osc-worker.log"
        worker = self.pre(
            "c-osc-worker",
            "任务",
            extra_env={
                "THE_PILOT_OSC_TARGET": str(worker_target),
                "ORCA_TASK_ID": "task_osc",
            },
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(
            worker_target.read_text(encoding="utf-8"), runtime.OSC_7501_WORKING
        )

    def test_osc_pre_invocation_reexec_does_not_duplicate_working(self) -> None:
        """The hot-reexec engine child stays silent so the outer hook owns the frame.

        Failure means one turn emits two working frames, which can regress the
        program activity Orca derives from the control terminal.
        resident-fact: status: 引擎热重执行时只有外层进程发出 `state=working`。
        """
        target = self.root / "osc-reexec.log"
        shim = self.root / "pilot-shim.py"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib\n"
            "engine = pathlib.Path(os.environ['THE_PILOT_GEMINI_DIR']) / 'config/the-pilot/runtime.py'\n"
            "engine.write_text(engine.read_text() + '\\n# upgraded\\n')\n"
            "print('{\"local_source_status\": \"updated\"}')\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        result = self.pre(
            "c-osc-reexec",
            "开始",
            extra_env={
                "THE_PILOT_OSC_TARGET": str(target),
                "THE_PILOT_BIN": str(shim),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_WORKING)

    def test_osc_stop_reports_done_for_settled_turns(self) -> None:
        """A settled turn with no picker or prefilled draft frees Orca mail.

        Failure means an idle agy at its composer stays invisible to Orca, so
        orchestration mail is never delivered until the user types.
        resident-fact: status: scene=NONE、mode=off、dispatched worker 与带 error
        的空闲 Stop 都发出 `state=done`。
        """
        target = self.root / "osc-done.log"
        env = {"THE_PILOT_OSC_TARGET": str(target)}

        none = self.stop(
            "c-osc-none",
            '结论 <!-- pilot: {"scene": "NONE"} -->',
            extra_env=env,
        )
        self.assertEqual(none.returncode, 0, none.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_DONE)

        apply_profile(self.root, self.gemini, "--mode", "off")
        off = self.stop(
            "c-osc-off", suggestion("继续推进实施", gate="auto"), extra_env=env
        )
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_DONE * 2
        )

        worker = self.stop(
            "c-osc-stop-worker",
            suggestion("继续推进实施", gate="auto"),
            extra_env={**env, "ORCA_TASK_ID": "task_osc"},
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_DONE * 3
        )

        error = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-osc-error",
                transcriptPath=str(self.root / "missing.jsonl"),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
                error="provider failed",
            ),
            extra_env=env,
        )
        self.assertEqual(error.returncode, 0, error.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_DONE * 4
        )

    def test_osc_stop_accepts_fully_idle_snake_case(self) -> None:
        """An idle stop is recognized from either payload spelling.

        Failure means Antigravity's snake_case idle signal is ignored and the
        pane never reports done.
        resident-fact: status: `fullyIdle` 缺失时 `fully_idle=True` 同样证明回合
        已静止并发出 `state=done`。
        """
        target = self.root / "osc-snake.log"
        transcript = write_transcript(
            self.root,
            "osc-snake.jsonl",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": '结论 <!-- pilot: {"scene": "NONE"} -->',
                },
            ],
        )
        result = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-osc-snake",
                transcriptPath=str(transcript),
                terminationReason="NO_TOOL_CALL",
                fully_idle=True,
            ),
            extra_env={"THE_PILOT_OSC_TARGET": str(target)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), runtime.OSC_7501_DONE)

    def test_osc_stop_stays_working_while_the_turn_continues(self) -> None:
        """Running, continuing and auto-submitted turns never claim an idle terminal.

        Failure means a turn that is still running is advertised as done, which
        lets orchestration mail collide with the running turn or the next one.
        resident-fact: status: fullyIdle=False、continue 决策与回车成功的自动投递
        都不发出 OSC 帧。
        """
        target = self.root / "osc-working-stop.log"
        env = {"THE_PILOT_OSC_TARGET": str(target)}
        transcript = write_transcript(
            self.root,
            "osc-running.jsonl",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
            ],
        )
        running = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-osc-running",
                transcriptPath=str(transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=False,
                workspacePaths=[str(self.workspace)],
            ),
            extra_env=env,
        )
        self.assertEqual(running.returncode, 0, running.stderr)
        self.assertFalse(target.exists())

        continued = self.stop(
            "c-osc-continue",
            "",
            steps=[
                {"type": "USER_INPUT", "content": "开始"},
                {"type": "PLANNER_RESPONSE", "content": "请确认以下 2 个问题："},
            ],
            extra_env=env,
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertEqual(json.loads(continued.stdout)["decision"], "continue")
        self.assertFalse(target.exists())

        accepted = self.stop(
            "c-osc-auto", suggestion("继续推进", gate="auto"), extra_env=env
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("--enter", send_calls(self.root)[-1])
        self.assertFalse(target.exists())

    def test_osc_stop_holds_mail_for_questions_and_prefilled_drafts(self) -> None:
        """A picker or an unconfirmed composer draft reports blocked:question.

        Failure means Orca may append orchestration mail plus Enter into an open
        question picker or a gate=pause draft and submit it without consent.
        resident-fact: status: 未结算的 ask_question 与 enter=False 的预填草稿都
        发出 `state=blocked:kind=question`。
        """
        target = self.root / "osc-blocked.log"
        env = {"THE_PILOT_OSC_TARGET": str(target)}

        question = self.stop(
            "c-osc-question",
            "",
            steps=[
                {"type": "USER_INPUT", "content": "选哪个方案"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "需要你确认方案。",
                    "tool_calls": [{"name": "ask_question", **ask_args(1)}],
                },
            ],
            extra_env=env,
        )
        self.assertEqual(question.returncode, 0, question.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_WAITING_QUESTION
        )
        self.assertEqual(send_calls(self.root), [])

        pause = self.stop(
            "c-osc-pause",
            suggestion("继续推进实施", gate="pause"),
            extra_env=env,
        )
        self.assertEqual(pause.returncode, 0, pause.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            runtime.OSC_7501_WAITING_QUESTION * 2,
        )
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--enter", sends[0])

    def test_osc_stop_holds_mail_when_delivery_is_ambiguous(self) -> None:
        """An unknown send receipt never claims idle, and a rejection does.

        Failure means an ambiguous delivery is treated as a free composer, so
        arriving mail may be appended to the same draft, or a rejected prefill
        is held forever.
        resident-fact: status: 投递回执 unknown 时发出 blocked:question，
        明确拒绝时发出 `state=done`。
        """
        target = self.root / "osc-ambiguous.log"
        env = {"THE_PILOT_OSC_TARGET": str(target)}

        unknown = self.stop(
            "c-osc-unknown",
            suggestion("继续推进", gate="auto"),
            extra_env={**env, "FAKE_SEND_RESULT": "invalid"},
        )
        self.assertEqual(unknown.returncode, 0, unknown.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_WAITING_QUESTION
        )

        rejected = self.stop(
            "c-osc-rejected",
            suggestion("继续推进", gate="auto"),
            extra_env={**env, "FAKE_SEND_ACCEPTED": "0"},
        )
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            runtime.OSC_7501_WAITING_QUESTION + runtime.OSC_7501_DONE,
        )

    def test_osc_stale_stop_never_overwrites_active_working_status(self) -> None:
        """An obsolete Stop leaves the newer turn's working status untouched.

        Failure means a late Stop from a previous transcript can flip the pane
        to done while the newer turn is still running.
        resident-fact: status: stop_stale_turn 返回空帧，绝不覆盖新回合的
        `state=working`。
        """
        target = self.root / "osc-stale.log"
        env = {"THE_PILOT_OSC_TARGET": str(target)}
        old_transcript = write_transcript(
            self.root,
            "osc-stale-old.jsonl",
            [
                {"type": "USER_INPUT", "content": "turn1"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
            ],
        )
        self.pre("c-osc-stale", "turn1", extra_env=env)
        self.pre("c-osc-stale", "turn2", extra_env=env)
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_WORKING * 2
        )

        stale = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-osc-stale",
                transcriptPath=str(old_transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
                workspacePaths=[str(self.workspace)],
            ),
            extra_env=env,
        )
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_WORKING * 2
        )
        log = (self.state / "pilot.log").read_text(encoding="utf-8")
        self.assertIn("stop_stale_turn", log)

    def test_osc_duplicate_stop_after_auto_submit_stays_working(self) -> None:
        """A duplicate Stop on an accepted auto-submitted turn stays silent.

        Failure means the duplicate Stop overwrites the working state of the
        next turn that the accepted Enter already started.
        resident-fact: status: `delivery_status=accepted` 的重复 Stop 返回空帧。
        """
        target = self.root / "osc-dup-auto.log"
        env = {"THE_PILOT_OSC_TARGET": str(target)}
        transcript = write_transcript(
            self.root,
            "osc-dup-auto.jsonl",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
            ],
        )
        body = payload(
            "c-osc-dup-auto",
            transcriptPath=str(transcript),
            terminationReason="NO_TOOL_CALL",
            fullyIdle=True,
            workspacePaths=[str(self.workspace)],
        )
        for attempt in ("initial", "duplicate"):
            result = run_engine(
                self.root,
                self.gemini,
                self.state,
                "hook",
                "--event",
                "Stop",
                stdin=body,
                extra_env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(target.exists(), attempt)
        state = read_state(self.state, "c-osc-dup-auto")
        self.assertEqual(state["delivery_status"], "accepted")
        self.assertEqual(len(send_calls(self.root)), 1)

    def test_osc_repeated_stop_reports_each_prefill_outcome(self) -> None:
        """Every prefill outcome persists a status that a repeated Stop preserves.

        Failure means a duplicate Stop reports done while an unconfirmed draft
        still owns the composer, or holds mail after an explicit rejection.
        resident-fact: status: prefilled/prefill_unknown 保持 blocked:question；
        rejected/none 报告 done，重复 Stop 与首次一致。
        """
        cases = (
            ("accepted", {}, runtime.OSC_7501_WAITING_QUESTION),
            (
                "unknown",
                {"FAKE_SEND_RESULT": "invalid"},
                runtime.OSC_7501_WAITING_QUESTION,
            ),
            ("rejected", {"FAKE_SEND_ACCEPTED": "0"}, runtime.OSC_7501_DONE),
            ("no-terminal", {"FAKE_TERMINAL_COUNT": "2"}, runtime.OSC_7501_DONE),
        )
        for label, extra, expected in cases:
            with self.subTest(outcome=label):
                session = f"c-osc-repeat-{label}"
                target = self.root / f"osc-repeat-{label}.log"
                transcript = write_transcript(
                    self.root,
                    f"transcript-{session}.jsonl",
                    [
                        {"type": "USER_INPUT", "content": "开始"},
                        {
                            "type": "PLANNER_RESPONSE",
                            "content": suggestion("继续推进实施", gate="pause"),
                        },
                    ],
                )
                body = payload(
                    session,
                    transcriptPath=str(transcript),
                    terminationReason="NO_TOOL_CALL",
                    fullyIdle=True,
                    workspacePaths=[str(self.workspace)],
                )
                env = {"THE_PILOT_OSC_TARGET": str(target), **extra}
                for attempt, expected_frames in (
                    ("initial", expected),
                    ("repeated", expected * 2),
                ):
                    result = run_engine(
                        self.root,
                        self.gemini,
                        self.state,
                        "hook",
                        "--event",
                        "Stop",
                        stdin=body,
                        extra_env=env,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        target.read_text(encoding="utf-8"),
                        expected_frames,
                        attempt,
                    )

    def test_osc_new_turn_prefill_after_auto_submit_holds_mail(self) -> None:
        """A prior accepted auto-submit never masks the next turn's prefill.

        Failure means the persisted `accepted` status from the previous turn
        makes the new turn's initial and repeated Stop look like a working
        turn, so Orca presses Enter into the unconfirmed draft.
        resident-fact: status: 上一回合 accepted 自动投递后，新回合 gate=pause
        预填的首次与重复 Stop 都报告 blocked:question。
        """
        target = self.root / "osc-handoff.log"
        env = {"THE_PILOT_OSC_TARGET": str(target)}

        auto = self.stop(
            "c-osc-handoff",
            suggestion("继续推进实施", gate="auto"),
            extra_env=env,
        )
        self.assertEqual(auto.returncode, 0, auto.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(
            read_state(self.state, "c-osc-handoff")["delivery_status"], "accepted"
        )

        self.pre("c-osc-handoff", "继续推进实施", extra_env=env)
        self.assertEqual(
            read_state(self.state, "c-osc-handoff")["delivery_status"], "none"
        )
        self.assertEqual(
            target.read_text(encoding="utf-8"), runtime.OSC_7501_WORKING
        )

        first = self.stop(
            "c-osc-handoff",
            suggestion("继续推进实施", gate="pause"),
            extra_env=env,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            read_state(self.state, "c-osc-handoff")["delivery_status"], "prefilled"
        )
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            runtime.OSC_7501_WORKING + runtime.OSC_7501_WAITING_QUESTION,
        )

        transcript = self.root / "transcript-c-osc-handoff.jsonl"
        repeated = run_engine(
            self.root,
            self.gemini,
            self.state,
            "hook",
            "--event",
            "Stop",
            stdin=payload(
                "c-osc-handoff",
                transcriptPath=str(transcript),
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
                workspacePaths=[str(self.workspace)],
            ),
            extra_env=env,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            runtime.OSC_7501_WORKING + runtime.OSC_7501_WAITING_QUESTION * 2,
        )

    def test_status_reports_active_mode_scenes_and_hook_group(self) -> None:
        status = run_engine(self.root, self.gemini, self.state, "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report["mode"], "gate")
        self.assertEqual(len(report["scenes"]), 4)
        self.assertTrue(report["hooks_group"])


class OscTargetResolutionTest(unittest.TestCase):
    """Control-terminal facts for the OSC 7501 program-status channel."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def isolated_env(self, **values: str):
        """Patch the process environment down to the OSC-relevant keys."""
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "THE_PILOT_GEMINI_DIR",
                "ORCA_AUTOPILOT_GEMINI_DIR",
                "THE_PILOT_OSC_TARGET",
                "ORCA_PANE_KEY",
                "ORCA_TERMINAL_HANDLE",
            }
        }
        env.update(values)
        return mock.patch.dict(os.environ, env, clear=True)

    @staticmethod
    def ps_result(stdout: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["ps"], 0, stdout, "")

    def resolve_from(self, first: str, second: str | None = None):
        responses = [first] if second is None else [first, second]

        def fake_run(command, **kwargs):
            stdout = responses.pop(0) if responses else ""
            return self.ps_result(stdout)

        with self.isolated_env(ORCA_PANE_KEY="pane-1"), mock.patch.object(
            runtime.os, "getppid", return_value=5150
        ), mock.patch.object(runtime.subprocess, "run", side_effect=fake_run):
            return runtime._resolve_osc_target()

    def test_frames_match_orca_program_status_contract(self) -> None:
        """The exact wire frames Orca strips and maps to program activity.

        Failure means Orca no longer recognizes the emitted sequence and the
        pane status regresses to unknown.
        resident-fact: status: OSC 7501 帧文本是 Orca
        `terminal.program-status.v1` 的外部协议。
        """
        self.assertEqual(
            runtime.OSC_7501_WORKING,
            "\x1b]7501;state=working:app=antigravity\x07",
        )
        self.assertEqual(
            runtime.OSC_7501_DONE, "\x1b]7501;state=done:app=antigravity\x07"
        )
        self.assertEqual(
            runtime.OSC_7501_WAITING_QUESTION,
            "\x1b]7501;state=blocked:kind=question:app=antigravity\x07",
        )

    def test_shell_wrapper_walks_up_to_the_agy_process(self) -> None:
        """Only the ancestor agy's tty names Orca's pane, not a child PTY.

        Failure means the hook writes status into an internal PTY created by
        agy, where Orca never sees it.
        resident-fact: status: `sh -> agy` 祖先链解析到 agy 的控制终端。
        """
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[2] == "5150":
                return self.ps_result("  4800 ttys999 -/bin/zsh\n")
            if command[2] == "4800":
                return self.ps_result("  4700 ttys999 /opt/homebrew/bin/agy\n")
            return subprocess.CompletedProcess(command, 1, "", "")

        with self.isolated_env(ORCA_PANE_KEY="pane-1"), mock.patch.object(
            runtime.os, "getppid", return_value=5150
        ), mock.patch.object(runtime.subprocess, "run", side_effect=fake_run):
            resolved = runtime._resolve_osc_target()
        self.assertEqual(resolved, (Path("/dev/ttys999"), False))
        self.assertEqual([command[2] for command in calls], ["5150", "4800"])

    def test_non_agy_lineage_and_invalid_ttys_are_rejected(self) -> None:
        """A shell without an agy parent or a detached tty never targets a device.

        Failure means status could be written to an unrelated process's
        terminal or a non-terminal `ps` placeholder.
        resident-fact: status: 非 agy 祖先、`?`/`??`/`-` tty 与越界路径一律拒绝。
        """
        cases = (
            (
                "shell-to-python",
                "  4800 ttys999 /bin/bash\n",
                "  4700 ttys999 /usr/bin/python3\n",
            ),
            ("direct-python", "  4700 ttys999 /usr/bin/python3\n", None),
            ("detached-tty", "  4700 ?? /opt/homebrew/bin/agy\n", None),
            ("unknown-tty", "  4700 - /opt/homebrew/bin/agy\n", None),
            ("parent-gone", "", None),
            (
                "tty-traversal",
                "  4700 ../../etc/passwd /opt/homebrew/bin/agy\n",
                None,
            ),
        )
        for label, first, second in cases:
            with self.subTest(case=label):
                self.assertIsNone(self.resolve_from(first, second))

    def test_gemini_isolation_never_probes_processes_or_devices(self) -> None:
        """Isolated unit tests and explicit installs resolve no control terminal.

        Failure means the installed runtime probes host processes or writes to
        host `/dev/tty*` during a test run inside a live Orca terminal.
        resident-fact: status: 设置 THE_PILOT_GEMINI_DIR 且无显式目标时解析立即
        返回 None，绝不运行 `ps`。
        """
        with self.isolated_env(
            THE_PILOT_GEMINI_DIR=str(self.root / "gemini"),
            ORCA_PANE_KEY="pane-1",
        ), mock.patch.object(runtime.os, "getppid") as getppid, mock.patch.object(
            runtime.subprocess, "run"
        ) as run:
            self.assertIsNone(runtime._resolve_osc_target())
        run.assert_not_called()
        getppid.assert_not_called()

        with self.isolated_env(), mock.patch.object(
            runtime.os, "getppid"
        ) as getppid, mock.patch.object(runtime.subprocess, "run") as run:
            self.assertIsNone(runtime._resolve_osc_target())
        run.assert_not_called()
        getppid.assert_not_called()

    def test_explicit_override_wins_and_none_disables_emission(self) -> None:
        """An explicit target overrides isolation; empty or `none` disables it.

        Failure means tests or users cannot redirect the status channel, or a
        disabled channel silently falls back to probing the host.
        resident-fact: status: THE_PILOT_OSC_TARGET 优先于一切隔离，空值与
        `none` 表示不输出。
        """
        with self.isolated_env(
            THE_PILOT_OSC_TARGET=str(self.root / "osc.log"),
            THE_PILOT_GEMINI_DIR=str(self.root / "gemini"),
        ):
            self.assertEqual(
                runtime._resolve_osc_target(), (self.root / "osc.log", True)
            )
        for value in ("", "   ", "none", "NONE"):
            with self.subTest(value=value):
                with self.isolated_env(
                    THE_PILOT_OSC_TARGET=value, ORCA_PANE_KEY="pane-1"
                ):
                    self.assertIsNone(runtime._resolve_osc_target())

    def test_explicit_file_target_receives_frames_with_private_mode(self) -> None:
        """The explicit target appends raw frames and creates missing parents.

        Failure means the observable status sequence cannot be asserted under
        test isolation without touching a host terminal.
        resident-fact: status: 显式文件目标按帧追加且权限为 0600。
        """
        target = self.root / "nested" / "osc.log"
        log = self.root / "state" / "pilot.log"
        with self.isolated_env(THE_PILOT_OSC_TARGET=str(target)), mock.patch.object(
            runtime, "STATE_DIR", log.parent
        ), mock.patch.object(runtime, "LOG_PATH", log):
            self.assertTrue(runtime.emit_osc_7501(runtime.OSC_7501_DONE))
            self.assertTrue(
                runtime.emit_osc_7501(runtime.OSC_7501_WAITING_QUESTION)
            )
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            runtime.OSC_7501_DONE + runtime.OSC_7501_WAITING_QUESTION,
        )
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_unwritable_explicit_target_is_logged_not_raised(self) -> None:
        """A failed status write stays out of hook JSON and leaves a log record.

        Failure means a status write error can break the hook's stdout JSON or
        disappear without evidence.
        resident-fact: status: 写入失败只记录 `osc_status_write_failed` 并返回
        False。
        """
        target = self.root / "blocked"
        target.mkdir()
        log = self.root / "state" / "pilot.log"
        with self.isolated_env(THE_PILOT_OSC_TARGET=str(target)), mock.patch.object(
            runtime, "STATE_DIR", log.parent
        ), mock.patch.object(runtime, "LOG_PATH", log):
            self.assertFalse(runtime.emit_osc_7501(runtime.OSC_7501_DONE))
        self.assertIn("osc_status_write_failed", log.read_text(encoding="utf-8"))

    def test_production_target_must_be_an_owned_character_device(self) -> None:
        """A non-tty path can never receive program status.

        Failure means production could write escape sequences into a regular
        file or a device owned by another user.
        resident-fact: status: 生产目标只接受当前用户拥有的字符设备。
        """
        target = self.root / "regular"
        target.write_text("", encoding="utf-8")
        log = self.root / "state" / "pilot.log"
        with self.isolated_env(), mock.patch.object(
            runtime, "_resolve_osc_target", return_value=(target, False)
        ), mock.patch.object(runtime, "STATE_DIR", log.parent), mock.patch.object(
            runtime, "LOG_PATH", log
        ):
            self.assertFalse(runtime.emit_osc_7501(runtime.OSC_7501_DONE))
        self.assertIn("osc_status_target_rejected", log.read_text(encoding="utf-8"))

        with mock.patch.object(runtime, "_resolve_osc_target") as resolve:
            self.assertFalse(runtime.emit_osc_7501(""))
        resolve.assert_not_called()

    def test_production_target_rechecks_the_opened_descriptor(self) -> None:
        """A target swapped between stat and open is rejected after opening.

        Failure means a replaced path could let escape bytes reach a regular
        file or another user's device even though the pre-open check passed.
        resident-fact: status: 生产目标在 `os.open` 后用 `fstat` 复核字符设备与
        所有者，不一致时记录 `osc_status_target_rejected`、关闭描述符且不写入。
        """
        target = self.root / "swapped-tty"
        log = self.root / "state" / "pilot.log"
        owned_char = os.stat_result(
            (stat.S_IFCHR | 0o600, 0, 0, 0, os.getuid(), 0, 0, 0, 0, 0)
        )
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(target):
                return owned_char
            return real_stat(path, *args, **kwargs)

        cases = (
            (
                "not-character",
                os.stat_result(
                    (stat.S_IFREG | 0o600, 0, 0, 0, os.getuid(), 0, 0, 0, 0, 0)
                ),
            ),
            (
                "wrong-owner",
                os.stat_result(
                    (stat.S_IFCHR | 0o600, 0, 0, 0, os.getuid() + 1, 0, 0, 0, 0, 0)
                ),
            ),
        )
        for label, opened in cases:
            with self.subTest(case=label):
                if log.exists():
                    log.unlink()
                with self.isolated_env(), mock.patch.object(
                    runtime, "_resolve_osc_target", return_value=(target, False)
                ), mock.patch.object(
                    runtime, "STATE_DIR", log.parent
                ), mock.patch.object(
                    runtime, "LOG_PATH", log
                ), mock.patch.object(
                    runtime.os, "stat", side_effect=fake_stat
                ), mock.patch.object(
                    runtime.os, "open", return_value=99
                ), mock.patch.object(
                    runtime.os, "fstat", return_value=opened
                ), mock.patch.object(
                    runtime.os, "close"
                ) as close, mock.patch.object(
                    runtime.os, "write"
                ) as write:
                    self.assertFalse(runtime.emit_osc_7501(runtime.OSC_7501_DONE))
                write.assert_not_called()
                close.assert_called_once_with(99)
                self.assertIn(
                    "osc_status_target_rejected", log.read_text(encoding="utf-8")
                )

    def test_delayed_frame_requires_the_bound_tty_device_and_inode(self) -> None:
        """Failure means a reused pane path can receive stale waiting bytes, or a valid binding rejects the frame."""
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        os.set_blocking(master, False)
        opened = os.fstat(slave)
        log = self.root / "state" / "pilot.log"
        frame = runtime.OSC_7501_WAITING_QUESTION
        cases = (
            (str(opened.st_dev + 1), str(opened.st_ino)),
            (str(opened.st_dev), str(opened.st_ino + 1)),
            ("invalid", str(opened.st_ino)),
            ("", str(opened.st_ino)),
        )
        env = {
            "THE_PILOT_GEMINI_DIR": str(self.root / "gemini"),
            "THE_PILOT_OSC_TARGET": os.ttyname(slave),
            "THE_PILOT_OSC_TARGET_IS_PANE": "1",
        }
        with mock.patch.multiple(runtime, STATE_DIR=log.parent, LOG_PATH=log):
            for dev, ino in cases:
                with self.subTest(dev=dev, ino=ino), self.isolated_env(
                    **env, THE_PILOT_OSC_DEV=dev, THE_PILOT_OSC_INO=ino,
                ):
                    self.assertFalse(runtime.emit_osc_7501(frame))
                    with self.assertRaises(BlockingIOError):
                        os.read(master, 4096)
            with self.isolated_env(
                **env, THE_PILOT_OSC_DEV=str(opened.st_dev), THE_PILOT_OSC_INO=str(opened.st_ino),
            ):
                self.assertTrue(runtime.emit_osc_7501(frame))
                self.assertEqual(os.read(master, 4096), frame.encode())
        self.assertEqual(log.read_text(encoding="utf-8").count("osc_status_target_rejected"), len(cases))

    def test_zero_byte_write_is_logged_not_spun(self) -> None:
        """A write that reports zero bytes fails instead of looping forever.

        Failure means a degenerate terminal write can spin the hook and block
        every turn end.
        resident-fact: status: `os.write` 返回 0 时立即以 `short_osc_write`
        失败并记录 `osc_status_write_failed`，不重试自旋。
        """
        target = self.root / "short-write.log"
        log = self.root / "state" / "pilot.log"
        with self.isolated_env(THE_PILOT_OSC_TARGET=str(target)), mock.patch.object(
            runtime, "STATE_DIR", log.parent
        ), mock.patch.object(runtime, "LOG_PATH", log), mock.patch.object(
            runtime.os, "write", return_value=0
        ) as write:
            self.assertFalse(runtime.emit_osc_7501(runtime.OSC_7501_DONE))
        self.assertEqual(write.call_count, 1)
        record = log.read_text(encoding="utf-8")
        self.assertIn("osc_status_write_failed", record)
        self.assertIn("short_osc_write", record)

    def test_transient_eagain_after_partial_write_drains_the_frame(self) -> None:
        """A full PTY output buffer delays bytes instead of truncating the frame.

        Failure means a multi-KB agy redraw at turn end makes the status frame
        drop with `EAGAIN` instead of draining once the slave is writable.
        resident-fact: status: 非阻塞 PTY 的瞬时 `EAGAIN` 在
        `OSC_WRITE_TIMEOUT_SECONDS` 内经 `select` 等待可写并补齐整帧。
        """
        target = self.root / "osc.log"
        log = self.root / "state" / "pilot.log"
        frame = runtime.OSC_7501_DONE
        data = frame.encode("utf-8")
        chunks: list[bytes] = []
        attempts: list[bytes] = []
        selected: list[tuple] = []

        def fake_write(fd, payload):
            attempts.append(payload)
            if len(attempts) == 1:
                chunks.append(payload[:4])
                return 4
            if len(attempts) == 2:
                raise BlockingIOError(
                    errno.EAGAIN, "Resource temporarily unavailable"
                )
            chunks.append(payload)
            return len(payload)

        def fake_select(readable, writable, exceptional, timeout):
            selected.append((readable, writable, exceptional, timeout))
            return ([], writable, [])

        with self.isolated_env(THE_PILOT_OSC_TARGET=str(target)), mock.patch.object(
            runtime, "STATE_DIR", log.parent
        ), mock.patch.object(runtime, "LOG_PATH", log), mock.patch.object(
            runtime.os, "write", side_effect=fake_write
        ) as write, mock.patch.object(
            runtime.select, "select", side_effect=fake_select
        ):
            self.assertTrue(runtime.emit_osc_7501(frame))
        self.assertEqual(b"".join(chunks), data)
        self.assertEqual(attempts[2], data[4:])
        self.assertEqual(write.call_count, 3)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][1], [write.call_args_list[0].args[0]])
        self.assertGreater(selected[0][3], 0)
        self.assertLessEqual(selected[0][3], runtime.OSC_WRITE_TIMEOUT_SECONDS)
        self.assertFalse(log.exists())

    def test_persistent_eagain_fails_closed_and_closes_the_descriptor(self) -> None:
        """When the PTY never drains, the frame fails closed and frees the fd.

        Failure means a full slave can spin the hook or leak a descriptor
        instead of recording `osc_status_write_failed` and returning False.
        resident-fact: status: `select` 超时仍不可写时重新抛出 `EAGAIN`、关闭
        描述符、记录 `osc_status_write_failed` 并返回 False。
        """
        target = self.root / "osc.log"
        log = self.root / "state" / "pilot.log"
        opened: list[int] = []
        selected: list[tuple] = []

        def fake_write(fd, payload):
            opened.append(fd)
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

        def fake_select(readable, writable, exceptional, timeout):
            selected.append((readable, writable, exceptional, timeout))
            return ([], [], [])

        with self.isolated_env(THE_PILOT_OSC_TARGET=str(target)), mock.patch.object(
            runtime, "STATE_DIR", log.parent
        ), mock.patch.object(runtime, "LOG_PATH", log), mock.patch.object(
            runtime.os, "write", side_effect=fake_write
        ), mock.patch.object(runtime.select, "select", side_effect=fake_select):
            self.assertFalse(runtime.emit_osc_7501(runtime.OSC_7501_DONE))
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][1], [opened[0]])
        self.assertGreater(selected[0][3], 0)
        self.assertLessEqual(selected[0][3], runtime.OSC_WRITE_TIMEOUT_SECONDS)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        record = log.read_text(encoding="utf-8")
        self.assertIn("osc_status_write_failed", record)
        self.assertIn("Resource temporarily unavailable", record)

    def test_nested_shell_wrappers_resolve_only_through_three_hops(self) -> None:
        """Nested shims still reach agy; runaway shell chains resolve to None.

        Failure means a doubly wrapped hook loses the pane status, or a shell
        chain without agy is mistaken for the owning terminal.
        resident-fact: status: 祖先链最多跨过 3 层 shell wrapper，之后第一个
        非 shell 进程必须是 `agy`，否则解析为 None。
        """

        def resolve_with(responses: dict[int, str]):
            def fake_run(command, **kwargs):
                stdout = responses.get(int(command[2]))
                if stdout is None:
                    return subprocess.CompletedProcess(command, 1, "", "")
                return self.ps_result(stdout)

            with self.isolated_env(ORCA_PANE_KEY="pane-1"), mock.patch.object(
                runtime.os, "getppid", return_value=5150
            ), mock.patch.object(runtime.subprocess, "run", side_effect=fake_run):
                return runtime._resolve_osc_target()

        self.assertEqual(
            resolve_with(
                {
                    5150: "  5140 ttys010 /bin/sh\n",
                    5140: "  5130 ttys010 /bin/bash\n",
                    5130: "  5100 ttys042 /opt/homebrew/bin/agy\n",
                }
            ),
            (Path("/dev/ttys042"), False),
        )
        self.assertIsNone(
            resolve_with(
                {
                    5150: "  5140 ttys010 /bin/sh\n",
                    5140: "  5130 ttys010 /bin/bash\n",
                    5130: "  5120 ttys010 /bin/zsh\n",
                    5120: "  5100 ttys010 /bin/dash\n",
                }
            )
        )
        self.assertIsNone(
            resolve_with(
                {
                    5150: "  5140 ttys010 /bin/sh\n",
                    5140: "  5130 ttys010 /bin/bash\n",
                    5130: "  5100 ttys042 /usr/bin/python3\n",
                }
            )
        )


    def test_reexecuted_hook_can_resolve_its_original_pane(self) -> None:
        """Failure means an authorized re-exec can lose its original Orca pane.

        resident-fact: status: an authorized re-exec can cross its one Python
        parent while retaining the agy ancestor requirement.
        """
        processes = {
            5150: (5140, "ttys010", "/usr/bin/python3"),
            5140: (5130, "ttys010", "/bin/sh"),
            5130: (5120, "ttys010", "/bin/bash"),
            5120: (5100, "ttys010", "/bin/zsh"),
            5100: (5000, "ttys042", "/opt/homebrew/bin/agy"),
        }
        for reexec, expected in (("0", None), ("1", (Path("/dev/ttys042"), False))):
            with self.subTest(reexec=reexec), self.isolated_env(
                HOME=str(self.root), ORCA_PANE_KEY="pane-1", THE_PILOT_REEXEC=reexec,
                ORCA_AUTOPILOT_REEXEC="0",
            ), mock.patch.object(runtime.os, "getppid", return_value=5150), mock.patch.object(
                runtime, "_ps_process", side_effect=processes.get
            ):
                self.assertEqual(runtime._resolve_osc_target(), expected)


class GlobalGuidanceTest(unittest.TestCase):
    """Default-install ownership of user guidance and shell startup blocks."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.home = self.root / "home"
        self.gemini = self.home / ".gemini"
        self.state = self.root / "state"
        fake_orca(self.root)
        self.guidance = {
            self.home
            / ".claude/CLAUDE.md": "# Claude 全局规则\n\n保留 Claude 用户规则。\n",
            self.home
            / ".codex/AGENTS.md": "# Codex 全局规则\n\n保留 Codex 用户规则。\n",
        }

    def seed_guidance(self) -> None:
        for path, text in self.guidance.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def default_apply(self, *args: str) -> dict:
        result = run_setup_default(self.root, "apply", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_install_creates_private_zshenv_and_uninstall_empties_it(self) -> None:
        """The default install owns the export from apply until uninstall strips it."""
        zshenv = self.home / ".zshenv"
        self.assertFalse(zshenv.exists())
        result = run_setup_default(self.root, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["drift"], ["profile.json 缺失"])

        report = self.default_apply("--preset", "recommended")
        self.assertEqual(report["shell_env"], [str(zshenv.resolve())])
        self.assertEqual(zshenv.read_text(encoding="utf-8"), SHELL_ENV_BLOCK)
        self.assertEqual(stat.S_IMODE(zshenv.stat().st_mode), 0o600)

        result = run_setup_default(self.root, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["drift"], [])

        result = run_setup_default(self.root, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".zshenv:managed-block", json.loads(result.stdout)["removed"])
        self.assertTrue(zshenv.is_file())
        self.assertEqual(zshenv.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(zshenv.stat().st_mode), 0o600)

        result = run_setup_default(self.root, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["drift"], ["profile.json 缺失"])

    def test_existing_profile_is_updated_without_creating_bash_startup_files(self) -> None:
        """Apply updates existing shell files and reports only the ones it changed."""
        self.home.mkdir()
        profile = self.home / ".profile"
        original = b"export USER_SETTING=1\r\n"
        profile.write_bytes(original)
        report = self.default_apply("--preset", "recommended")
        self.assertEqual(report["shell_env"], [
            str((self.home / ".zshenv").resolve()), str(profile.resolve()),
        ])
        self.assertTrue(profile.read_bytes().startswith(original))
        self.assertIn(SHELL_ENV_BLOCK.encode(), profile.read_bytes())
        for name in (".bash_profile", ".bashrc"):
            self.assertFalse((self.home / name).exists())
        self.assertEqual(self.default_apply()["shell_env"], [])

        result = run_setup_default(self.root, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile.read_bytes(), original)

    def test_status_reports_residual_shell_blocks_without_profile(self) -> None:
        """A missing profile must leave residual shell blocks and invalid markers visible."""
        self.home.mkdir()
        bashrc = self.home / ".bashrc"
        bashrc.touch()
        self.default_apply("--preset", "recommended")
        (self.gemini / PROFILE_REL).unlink()
        (self.home / ".profile").write_text(SHELL_ENV_BLOCK.splitlines()[0] + "\n", encoding="utf-8")

        result = run_setup_default(self.root, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        drift = json.loads(result.stdout)["drift"]
        self.assertIn("profile.json 缺失", drift)
        for name in (".zshenv", ".bashrc"):
            self.assertIn(f"{name} 仍包含托管块", drift)
            self.assertEqual((self.home / name).read_text(encoding="utf-8"), SHELL_ENV_BLOCK)
        self.assertTrue(any(item.startswith(".profile 托管块异常：") for item in drift), drift)

    def test_shell_env_writes_preserve_symlinks_and_target_modes(self) -> None:
        """Apply and uninstall must preserve the symlinks and private modes of dotfiles."""
        self.home.mkdir()
        files = {}
        for name in (".zshenv", ".bashrc"):
            path = self.home / name
            target = self.home / f"{name}.target"
            target.touch(mode=0o600)
            path.symlink_to(target.name)
            files[path] = target

        self.default_apply("--preset", "recommended")
        for path, target in files.items():
            self.assertEqual(path.readlink(), Path(target.name))
            self.assertEqual(target.read_text(encoding="utf-8"), SHELL_ENV_BLOCK)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

        result = run_setup_default(self.root, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, target in files.items():
            self.assertEqual(path.readlink(), Path(target.name))
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_broken_shell_env_targets_reject_apply_and_surface_in_status(self) -> None:
        """An invalid target must be reported even when no profile could be installed."""
        self.home.mkdir()
        for name in (".zshenv", ".bashrc"):
            for kind in ("dangling", "directory"):
                with self.subTest(name=name, kind=kind):
                    path = self.home / name
                    if kind == "dangling":
                        path.symlink_to("missing-target")
                    else:
                        path.mkdir()
                    result = run_setup_default(self.root, "apply")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"{name} 托管块异常", result.stderr)
                    for artifact in (PROFILE_REL, CONFIG_REL, GEMINI_REL, HOOKS_REL):
                        self.assertFalse((self.gemini / artifact).exists())
                    if name != ".zshenv":
                        self.assertFalse((self.home / ".zshenv").exists())

                    result = run_setup_default(self.root, "status")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    drift = json.loads(result.stdout)["drift"]
                    self.assertTrue(any(item.startswith(f"{name} 托管块异常：") for item in drift), drift)
                    if kind == "dangling":
                        self.assertEqual(path.readlink(), Path("missing-target"))
                        path.unlink()
                    else:
                        path.rmdir()

    def test_uninstall_preflights_every_shell_file_before_writing(self) -> None:
        """An invalid or malformed shell file must leave all installed artifacts in place."""
        self.default_apply("--preset", "recommended")
        zshenv = self.home / ".zshenv"
        bashrc = self.home / ".bashrc"
        artifacts = (PROFILE_REL, CONFIG_REL, GEMINI_REL, HOOKS_REL)
        paths = [zshenv, *(self.gemini / rel for rel in artifacts)]
        before = {path: path.read_bytes() for path in paths}
        for kind in ("dangling", "malformed"):
            with self.subTest(kind=kind):
                if kind == "dangling":
                    bashrc.symlink_to("missing-target")
                else:
                    bashrc.write_text(SHELL_ENV_BLOCK.splitlines()[0] + "\n", encoding="utf-8")
                commands = ("uninstall", "apply") if kind == "malformed" else ("uninstall",)
                for command in commands:
                    result = run_setup_default(self.root, command)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(".bashrc 托管块异常", result.stderr)
                    for path, original in before.items():
                        self.assertEqual(path.read_bytes(), original)
                result = run_setup_default(self.root, "status")
                self.assertEqual(result.returncode, 0, result.stderr)
                drift = json.loads(result.stdout)["drift"]
                self.assertTrue(any(item.startswith(".bashrc 托管块异常：") for item in drift), drift)
                bashrc.unlink()

    def test_shell_env_drift_is_repaired_by_upgrade_and_self_update(self) -> None:
        """Drift must be visible and repairable from the existing upgrade and update paths."""
        self.default_apply("--preset", "recommended")
        zshenv = self.home / ".zshenv"
        zshenv.write_text(SHELL_ENV_BLOCK.replace(":-off", ":-on"), encoding="utf-8")
        result = run_setup_default(self.root, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".zshenv 托管块缺失或已漂移", json.loads(result.stdout)["drift"])
        self.default_apply("--upgrade")
        self.assertEqual(zshenv.read_text(encoding="utf-8"), SHELL_ENV_BLOCK)

        zshenv.unlink()
        result = run_setup_default(self.root, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".zshenv 托管块缺失或已漂移", json.loads(result.stdout)["drift"])
        checkout = self.make_checkout()
        skill_root = self.root / "skill-root"
        skill_root.mkdir()
        (skill_root / ".zshenv").symlink_to("missing-target")
        result = run_setup_default(
            self.root, "self-update", "--local-only", "--checkout-dir", str(checkout),
            "--home-dir", str(skill_root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(zshenv.read_text(encoding="utf-8"), SHELL_ENV_BLOCK)
        self.assertEqual((skill_root / ".zshenv").readlink(), Path("missing-target"))
        self.assertFalse((skill_root / "missing-target").exists())
        result = run_setup_default(self.root, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["drift"], [])

    def test_shell_env_is_untouched_by_redirected_installations(self) -> None:
        """Both Gemini-root overrides and the skill-only home must leave shell files alone."""
        homes = (self.home, self.root / "skill-root", self.root / "sandbox")
        for home in homes:
            home.mkdir()
            (home / ".zshenv").symlink_to("missing-target")
            (home / ".bashrc").write_text(SHELL_ENV_BLOCK, encoding="utf-8")

        skill_root, sandbox = homes[1:]
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                env = base_env(self.root)
                root_args = []
                if explicit:
                    root_args = ["--gemini-dir", str(sandbox)]
                else:
                    env["THE_PILOT_GEMINI_DIR"] = str(sandbox)
                for command, options in (
                    ("apply", []),
                    ("status", []),
                    ("self-update", ["--local-only", "--home-dir", str(skill_root)]),
                    ("uninstall", []),
                ):
                    result = subprocess.run(
                        [sys.executable, str(SETUP_SCRIPT), command, *options, *root_args, "--no-project", "--json"],
                        capture_output=True, text=True, env=env, timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    if command == "apply":
                        self.assertEqual(report["shell_env"], [])
                    elif command == "status":
                        self.assertEqual(report["drift"], [])
                    elif command == "uninstall":
                        self.assertNotIn(".bashrc:managed-block", report["removed"])
                for home in homes:
                    self.assertEqual((home / ".zshenv").readlink(), Path("missing-target"))
                    self.assertFalse((home / "missing-target").exists())
                    self.assertEqual((home / ".bashrc").read_text(encoding="utf-8"), SHELL_ENV_BLOCK)

    def test_default_install_syncs_and_uninstalls_managed_blocks(self) -> None:
        # Failure means a default install leaves stale global guidance behind,
        # status hides that drift, or uninstall deletes unrelated user prose.
        self.seed_guidance()
        self.default_apply("--preset", "recommended")
        for path, original in self.guidance.items():
            text = path.read_text(encoding="utf-8")
            self.assertIn(original, text)
            self.assertIn(profile_compiler.ORCHESTRATION_BEGIN, text)
            self.assertIn(profile_compiler.ORCHESTRATION_END, text)
            self.assertIn("Tier-1 主会话", text)
            self.assertIn("agy --dangerously-skip-permissions", text)
        report = json.loads(run_setup_default(self.root, "status").stdout)
        self.assertEqual(report["drift"], [], report["drift"])

        tampered = next(iter(self.guidance))
        text = tampered.read_text(encoding="utf-8")
        tampered.write_text(
            text.replace("Tier-1 主会话", "Tier-1 已被篡改"), encoding="utf-8"
        )
        drift = json.loads(run_setup_default(self.root, "status").stdout)["drift"]
        self.assertTrue(any(".claude/CLAUDE.md" in item for item in drift), drift)

        result = run_setup_default(self.root, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, original in self.guidance.items():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(profile_compiler.ORCHESTRATION_BEGIN, text)
            self.assertNotIn(profile_compiler.ORCHESTRATION_END, text)
            self.assertEqual(text, original)

    def test_worker_switch_refreshes_managed_blocks_without_drift(self) -> None:
        # Failure means `,worker codex` leaves stale Tier-1/Tier-2 slots in the
        # managed global blocks, or runtime rewriting disagrees with recompiling.
        self.seed_guidance()
        self.default_apply("--preset", "recommended")
        result = run_default_engine(
            self.root, self.gemini, self.state, "worker", "codex", "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["worker_agents"], ["codex", "claude"])
        for path, original in self.guidance.items():
            text = path.read_text(encoding="utf-8")
            self.assertIn(original, text)
            self.assertIn("Orca Orchestration：Codex > Claude Code", text)
            self.assertIn(
                'worker-start --spec "<任务契约>" --worktree new-child --repo '
                "path:<目标仓库根> --name <name> --agent codex --setup run --json",
                text,
            )
        report = json.loads(run_setup_default(self.root, "status").stdout)
        self.assertEqual(report["drift"], [], report["drift"])

    def test_explicit_gemini_dir_leaves_global_guidance_untouched(self) -> None:
        # Failure means a sandboxed apply, uninstall, or status reads or edits
        # the user's real Claude Code / Codex global guidance.
        self.seed_guidance()
        sandbox = self.root / "sandbox-gemini"
        apply_profile(self.root, sandbox, "--preset", "recommended")
        for path, original in self.guidance.items():
            self.assertEqual(path.read_text(encoding="utf-8"), original)
        report = json.loads(run_setup(self.root, sandbox, "status").stdout)
        self.assertEqual(report["drift"], [], report["drift"])
        result = run_setup(self.root, sandbox, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, original in self.guidance.items():
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_gemini_dir_env_override_leaves_global_guidance_untouched(self) -> None:
        # Failure means an environment-redirected root is mistaken for the
        # default install and edits the user's global agent guidance.
        self.seed_guidance()
        sandbox = self.root / "env-gemini"
        env = base_env(self.root)
        env["THE_PILOT_GEMINI_DIR"] = str(sandbox)
        result = subprocess.run(
            [
                sys.executable,
                str(SETUP_SCRIPT),
                "apply",
                "--preset",
                "recommended",
                "--project",
                str(PROJECT_PATH),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, original in self.guidance.items():
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_upgrade_normalizes_profile_without_coordinator_agent(self) -> None:
        # Failure means an installed profile written before the two-tier
        # contract cannot upgrade, or its coordinator agent stays undefined.
        self.default_apply("--preset", "recommended")
        profile_path = self.gemini / PROFILE_REL
        profile = read_json(profile_path)
        profile["orchestration"].pop("coordinator_agent", None)
        write_json(profile_path, profile)
        self.default_apply("--upgrade")
        upgraded = read_json(profile_path)
        self.assertEqual(upgraded["orchestration"]["coordinator_agent"], "agy")

    def strip_managed_blocks(self) -> None:
        for path in self.guidance:
            path.write_text(
                profile_compiler.strip_orchestration_md(path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )

    def seed_legacy_guidance(self) -> None:
        heading = profile_compiler.LEGACY_ORCHESTRATION_HEADING
        for path in self.guidance:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# 用户规则\n\n保留段落。\n\n"
                f"{heading}AGY > Claude Code）\n\n"
                "5. 旧交互说明。\n\n6. 旧收尾说明。\n\n"
                "## 用户后续章节\n\n也必须保留。\n",
                encoding="utf-8",
            )

    def make_checkout(self) -> Path:
        checkout = self.root / "checkout"
        subprocess.run(
            ["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True
        )
        for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
            subprocess.run(["git", "-C", str(checkout), "config", key, value], check=True)
        launcher = checkout / "bin/pilot"
        launcher.parent.mkdir()
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
            "from the_pilot.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "commit", "-m", "checkout"],
            check=True,
            capture_output=True,
        )
        return checkout

    def test_legacy_unmanaged_heading_migrates_on_apply_and_worker_switch(self) -> None:
        # Failure means an upgraded default install keeps the stale unmanaged
        # orchestration section beside the managed block, so a later ,worker
        # switch leaves two diverging owners and status hides the drift.
        self.seed_legacy_guidance()
        self.default_apply("--preset", "recommended")
        for path in self.guidance:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(
                text.count(profile_compiler.LEGACY_ORCHESTRATION_HEADING), 1, text
            )
            self.assertNotIn("6. 旧收尾说明", text)
            self.assertIn("保留段落。", text)
            self.assertIn("## 用户后续章节\n\n也必须保留。", text)
        result = run_default_engine(
            self.root, self.gemini, self.state, "worker", "codex", "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.guidance:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(
                text.count(profile_compiler.LEGACY_ORCHESTRATION_HEADING), 1, text
            )
            self.assertIn("Orca Orchestration：Codex > Claude Code", text)
            self.assertNotIn("Orca Orchestration：AGY > Claude Code", text)
        report = json.loads(run_setup_default(self.root, "status").stdout)
        self.assertEqual(report["drift"], [], report["drift"])

    def test_worker_switch_does_not_rewrite_markerless_global_files(self) -> None:
        # Failure means the runtime still owns whole legacy files, editing user
        # prose instead of reporting the unmanaged file as drift.
        self.seed_guidance()
        self.default_apply("--preset", "recommended")
        self.strip_managed_blocks()
        before = {path: path.read_text(encoding="utf-8") for path in self.guidance}
        result = run_default_engine(
            self.root, self.gemini, self.state, "worker", "codex", "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.guidance:
            self.assertEqual(path.read_text(encoding="utf-8"), before[path])
        drift = json.loads(run_setup_default(self.root, "status").stdout)["drift"]
        self.assertTrue(any(".claude/CLAUDE.md" in item for item in drift), drift)

    def test_managed_writes_preserve_existing_file_mode(self) -> None:
        # Failure means a default install widens a user's 0600 global guidance
        # file to installer-artifact mode across apply, ,worker, or uninstall.
        self.seed_guidance()
        for path in self.guidance:
            path.chmod(0o600)
        self.default_apply("--preset", "recommended")
        run_default_engine(self.root, self.gemini, self.state, "worker", "codex", "--json")
        result = run_setup_default(self.root, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, original in self.guidance.items():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_managed_writes_preserve_symlinked_guidance_targets(self) -> None:
        # Failure means the default install replaces a user's symlinked dotfile
        # with a regular file instead of rewriting its resolved target.
        targets: dict[Path, Path] = {}
        for path, original in self.guidance.items():
            target = self.root / "dotfiles" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(original, encoding="utf-8")
            target.chmod(0o600)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
            targets[path] = target
        self.default_apply("--preset", "recommended")
        run_default_engine(self.root, self.gemini, self.state, "worker", "codex", "--json")
        for path, target in targets.items():
            self.assertTrue(path.is_symlink())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertIn(
                profile_compiler.ORCHESTRATION_BEGIN,
                target.read_text(encoding="utf-8"),
            )
        result = run_setup_default(self.root, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, target in targets.items():
            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), self.guidance[path])

    def test_uninstall_keeps_empty_pre_existing_guidance_file(self) -> None:
        # Failure means uninstall deletes a user-created empty dotfile that the
        # installer never created.
        for path in self.guidance:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        self.default_apply("--preset", "recommended")
        result = run_setup_default(self.root, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.guidance:
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_explicit_default_gemini_dir_owns_global_guidance(self) -> None:
        # Failure means `--gemini-dir $HOME/.gemini` is misclassified as a
        # sandbox, so a re-apply, status, or uninstall silently skips the blocks.
        self.seed_guidance()
        apply_profile(self.root, self.gemini, "--preset", "recommended")
        for path in self.guidance:
            self.assertIn(
                profile_compiler.ORCHESTRATION_BEGIN,
                path.read_text(encoding="utf-8"),
            )
        report = json.loads(run_setup(self.root, self.gemini, "status").stdout)
        self.assertEqual(report["drift"], [], report["drift"])
        result = run_setup(self.root, self.gemini, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, original in self.guidance.items():
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_bare_self_update_without_checkout_syncs_global_guidance(self) -> None:
        # Failure means a default install updated without a local checkout
        # skips the managed blocks, leaving stale priority guidance behind.
        self.seed_guidance()
        self.default_apply("--preset", "recommended")
        self.strip_managed_blocks()
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "THE_PILOT_GEMINI_DIR": "",
                "ORCA_AUTOPILOT_GEMINI_DIR": "",
            },
        ), mock.patch.object(
            installer, "_discover_checkout", return_value=(None, False)
        ):
            args = installer.build_parser().parse_args(
                ["self-update", "--local-only", "--json"]
            )
            report = installer.self_update(args)
        self.assertEqual(report["status"], "skipped_no_source", report)
        for path in self.guidance:
            self.assertIn(
                profile_compiler.ORCHESTRATION_BEGIN,
                path.read_text(encoding="utf-8"),
            )

    def test_self_update_home_dir_is_not_a_guidance_root(self) -> None:
        # Failure means `self-update --home-dir` (documented as the skill-link
        # root) reclassifies the same default install, so guidance is skipped
        # or redirected away from the resolved default home.
        self.seed_guidance()
        self.default_apply("--preset", "recommended")
        self.strip_managed_blocks()
        skill_root = self.root / "skill-root"
        skill_root.mkdir()
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "THE_PILOT_GEMINI_DIR": "",
                "ORCA_AUTOPILOT_GEMINI_DIR": "",
            },
        ), mock.patch.object(
            installer, "_discover_checkout", return_value=(None, False)
        ):
            args = installer.build_parser().parse_args(
                ["self-update", "--local-only", "--home-dir", str(skill_root), "--json"]
            )
            report = installer.self_update(args)
        self.assertEqual(report["status"], "skipped_no_source", report)
        for path in self.guidance:
            self.assertIn(
                profile_compiler.ORCHESTRATION_BEGIN,
                path.read_text(encoding="utf-8"),
            )
        self.assertFalse((skill_root / ".claude/CLAUDE.md").exists())
        self.assertFalse((skill_root / ".codex/AGENTS.md").exists())

    def test_self_update_with_checkout_syncs_global_guidance(self) -> None:
        # Failure means the checkout re-apply passes --gemini-dir explicitly and
        # is then mistaken for a sandbox, skipping the managed blocks.
        self.seed_guidance()
        self.default_apply("--preset", "recommended")
        self.strip_managed_blocks()
        checkout = self.make_checkout()
        result = run_setup_default(
            self.root, "self-update", "--local-only", "--checkout-dir", str(checkout)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.guidance:
            self.assertIn(
                profile_compiler.ORCHESTRATION_BEGIN,
                path.read_text(encoding="utf-8"),
            )
        report = json.loads(run_setup_default(self.root, "status").stdout)
        self.assertEqual(report["drift"], [], report["drift"])

if __name__ == "__main__":
    unittest.main()
