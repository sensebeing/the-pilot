"""Sandboxed tests for the Orca Autopilot skill.

Every test runs inside a ``tempfile.TemporaryDirectory`` and passes an explicit
``--gemini-dir`` or ``ORCA_AUTOPILOT_GEMINI_DIR``; the real ``~/.gemini`` is
never read or written.

resident-fact: settlement: profile 是唯一安装真源，安装/卸载只改托管项；自动投递只在 gate=auto 且未达轮数上限时回车。
resident-fact: admission: 只有同回合已回答的同步提问才放行预填，题数只由提问工具声明（截断的调用一律等待）；异步提问与未回执/失败/空答案一律等待用户。
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


from functools import partial
from the_pilot import compiler as profile_compiler
from the_pilot.profile import resolve_profile as resolve_user_profile, normalize_comma_alias
from the_pilot.defaults import DEFAULT_SPECS, DELIVER_STRATEGIES, HARNESS_ORCHESTRATION_SENTENCE

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "src/the_pilot"
SETUP_SCRIPT = REPO_ROOT / "bin/pilot"
DEFAULT_PROFILE_PATH = SCRIPTS_DIR / "assets/profile.default.json"
PROJECT_PATH = REPO_ROOT / "tests/fixtures/alter-service.json"
PROJECT = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
resolve_profile = partial(resolve_user_profile, project=PROJECT)
compile_bundle = partial(profile_compiler.compile_bundle, project=PROJECT)
ENGINE_REL = Path("config/autopilot/autopilot.py")
SCENES_REL = Path("config/autopilot/scenes")
GEMINI_REL = Path("config/GEMINI.md")
HOOKS_REL = Path("config/hooks.json")
PROFILE_REL = Path("config/autopilot/profile.json")
CONFIG_REL = Path("config/autopilot/config.json")


FAKE_ORCA = """#!__PYTHON__
import json
import os
from pathlib import Path
import sys

root = Path(__ROOT__)
with (root / "orca-calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
args = sys.argv[1:]
if args[:2] == ["terminal", "list"]:
    worktree = os.environ.get("FAKE_WORKTREE_PATH", str(root / "ws"))
    count = int(os.environ.get("FAKE_TERMINAL_COUNT", "1"))
    terminals = [
        {
            "handle": "term_test" if index == 0 else f"term_test_{index}",
            "agentIdentity": "antigravity",
            "connected": True,
            "writable": True,
            "worktreePath": worktree,
        }
        for index in range(count)
    ]
    print(json.dumps({"ok": True, "result": {"terminals": terminals}}, ensure_ascii=False))
elif args[:2] == ["terminal", "send"]:
    if os.environ.get("FAKE_SEND_RESULT") == "invalid":
        print("not-json")
    else:
        accepted = os.environ.get("FAKE_SEND_ACCEPTED", "1") != "0"
        print(json.dumps({"ok": True, "result": {"send": {"accepted": accepted}}}, ensure_ascii=False))
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


def engine_env(root: Path, gemini: Path, state: Path, extra: dict | None = None) -> dict[str, str]:
    env = base_env(root)
    env["ORCA_AUTOPILOT_GEMINI_DIR"] = str(gemini)
    env["ORCA_AUTOPILOT_STATE_DIR"] = str(state)
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
    lines.append(f"<!-- autopilot: {json.dumps(tag, ensure_ascii=False)} -->")
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
        self.base = load_default_profile()

    def test_presets_cover_the_three_supported_profiles(self) -> None:
        self.assertEqual(
            set(DEFAULT_SPECS["presets"]),
            {"recommended", "conservative", "orchestration-only"},
        )
        recommended = resolve_profile(self.base, preset="recommended")
        self.assertEqual(recommended["autopilot"]["mode"], "gate")
        self.assertEqual(recommended["autopilot"]["max_consecutive_auto_turns"], 8)
        self.assertEqual(len(recommended["scenes"]["enabled_scenes"]), 4)
        self.assertEqual(len(recommended["shortcuts"]["enabled_entry"]), 7)
        config = compile_bundle(recommended).config_json
        feedback = config["entry_shortcuts"]["fb"]["template"]
        comma_feedback = next(command["template"] for command in config["comma_commands"].values() if ",fb" in command["aliases"])
        self.assertEqual(feedback, comma_feedback)
        for clause in ("$alter-feedback", "$debug", "直接执行 claim", "无需再询问", "端到端调用拓扑", "{arg}"):
            self.assertIn(clause, feedback)

        conservative = resolve_profile(self.base, preset="conservative")
        self.assertEqual(conservative["autopilot"]["mode"], "confirm")
        self.assertEqual(conservative["autopilot"]["max_consecutive_auto_turns"], 4)
        self.assertEqual(conservative["shortcuts"]["enabled_entry"], ["r", "rt", "d", "pull"])

        orchestration = resolve_profile(
            self.base, preset="orchestration-only"
        )
        self.assertEqual(orchestration["autopilot"]["mode"], "off")
        self.assertEqual(orchestration["scenes"]["enabled_scenes"], [])
        self.assertEqual(orchestration["shortcuts"]["enabled_entry"], [])
        bundle = compile_bundle(orchestration)
        self.assertEqual(bundle.scenes, {})

    def test_four_dimension_profile_compiles_into_config_scenes_and_block(self) -> None:
        profile = resolve_profile(
            self.base,
            preset="recommended",
            overrides={
                "orchestration": {"worker_agents": ["codex"], "review_agents": ["agy", "codex"]},
                "autopilot": {"mode": "confirm", "max_consecutive_auto_turns": 12},
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
        self.assertEqual(profile["autopilot"]["mode"], "confirm")
        self.assertEqual(profile["autopilot"]["max_consecutive_auto_turns"], 12)
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

    def test_feedback_email_closeout_respects_explicit_opt_in(self) -> None:
        # Failure means the generated workflow either stalls authorized email or sends without opt-in.
        for enabled in (False, True):
            with self.subTest(enable_feedback_email=enabled):
                bundle = compile_bundle(
                    resolve_profile(
                        self.base, preset="recommended",
                        overrides={"scenes": {"enable_feedback_email": enabled}},
                    )
                )
                for text in (bundle.scenes["04-closeout-and-sync.md"], bundle.gemini_block):
                    self.assertNotIn("发送前仍标 ", text)
                    self.assertNotIn("外部发信除外", text)
                    if enabled:
                        self.assertIn("全程标 `gate=auto` 自动闭环", text)
                        self.assertIn("review_verification_email", text)
                        self.assertIn("fill_batch_email_copy", text)
                        self.assertIn("本轮确认发送同样标 `gate=auto`", text)
                        self.assertIn("不用让我预览确认了", text)
                    else:
                        self.assertIn("不生成发信入口；发信需用户当轮明确授权", text)
                        self.assertNotIn("不用让我预览确认了", text)

    def test_orchestration_text_carries_required_rules(self) -> None:
        bundle = compile_bundle(
            resolve_profile(self.base, preset="recommended")
        )
        text = bundle.gemini_block
        self.assertIn(HARNESS_ORCHESTRATION_SENTENCE, text)
        self.assertIn("Agent 单一语义 Owner 三步判断模型", text)
        self.assertIn("不死板锁死文件清单", text)
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
        for preset in sorted(DEFAULT_SPECS["presets"]):
            for strategy in DELIVER_STRATEGIES:
                for feedback in (False, True):
                    with self.subTest(
                        preset=preset, strategy=strategy, feedback=feedback
                    ):
                        compiled = compile_bundle(
                            resolve_profile(
                                self.base,
                                preset=preset,
                                overrides={
                                    "scenes": {
                                        "deliver_strategy": strategy,
                                        "enable_feedback_email": feedback,
                                        "guardrails": ["必须补测试", "不得新增账本"],
                                    }
                                },
                            )
                        ).gemini_block
                        self.assertLess(len(compiled.encode("utf-8")), 19600)
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
        # growing GEMINI.md past the CLI limit and breaking `autopilot.py sync`.
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
        merged = profile_compiler.merge_hooks_json(existing, "/tmp/engine/autopilot.py")
        self.assertEqual(merged["orca-status"], existing["orca-status"])
        group = merged["orca-agy-autopilot"]
        self.assertEqual(group["PreInvocation"][0]["timeout"], 10)
        self.assertIn("/tmp/engine/autopilot.py", group["PreInvocation"][0]["command"])
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
                self.base, overrides={"autopilot": {"mode": "bogus"}}
            )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base, overrides={"autopilot": {"unknown": 1}}
            )
        with self.assertRaises(profile_compiler.ProfileError):
            resolve_profile(
                self.base, overrides={"shortcuts": {"custom_comma": {"mode": "x"}}}
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
        engine = '/tmp/engine $(touch nope) "quoted"/autopilot.py'
        merged = profile_compiler.merge_hooks_json({}, engine)
        command = merged["orca-agy-autopilot"]["PreInvocation"][0]["command"]
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

    def seed_user_files(self) -> None:
        config_dir = self.gemini / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "GEMINI.md").write_text(
            "# 用户规则\n\n保留这一段。\n", encoding="utf-8"
        )
        write_json(
            config_dir / "hooks.json",
            {"orca-status": {"Stop": [{"type": "command", "command": "true"}]}},
        )

    def test_apply_installs_every_artifact_and_preserves_user_files(self) -> None:
        self.seed_user_files()
        result = apply_profile(self.root, self.gemini, "--preset", "recommended")
        self.assertEqual(result["preset"], "recommended")
        engine = self.gemini / ENGINE_REL
        self.assertTrue(engine.is_file())
        self.assertEqual(engine.stat().st_mode & 0o777, 0o755)
        self.assertEqual(len(list((self.gemini / SCENES_REL).glob("*.md"))), 4)
        profile = read_json(self.gemini / PROFILE_REL)
        self.assertEqual(profile["autopilot"]["mode"], "gate")
        config = read_json(self.gemini / CONFIG_REL)
        self.assertEqual(
            set(config), {"entry_shortcuts", "comma_commands"}
        )
        gemini_text = (self.gemini / GEMINI_REL).read_text(encoding="utf-8")
        self.assertIn("保留这一段。", gemini_text)
        self.assertIn(profile_compiler.GEMINI_BEGIN, gemini_text)
        hooks = read_json(self.gemini / HOOKS_REL)
        self.assertIn("orca-status", hooks)
        self.assertIn("orca-agy-autopilot", hooks)
        self.assertIn(str(engine), hooks["orca-agy-autopilot"]["Stop"][0]["command"])

        status = run_setup(self.root, self.gemini, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertTrue(report["installed"])
        self.assertEqual(report["drift"], [])
        self.assertTrue(report["engine"]["up_to_date"])

    def test_apply_custom_profile_writes_selected_options(self) -> None:
        autopilot_dir = self.gemini / "config" / "autopilot"
        autopilot_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            autopilot_dir / "config.json",
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
        self.assertEqual(profile["autopilot"]["mode"], "confirm")
        self.assertEqual(profile["autopilot"]["max_consecutive_auto_turns"], 5)
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
        profile["autopilot"]["mode"] = "confirm"
        write_json(profile_path, profile)
        engine_before = (self.gemini / ENGINE_REL).read_bytes()

        apply_profile(self.root, self.gemini, "--upgrade")
        upgraded = read_json(profile_path)
        self.assertEqual(upgraded["orchestration"]["worker_agents"], ["codex"])
        self.assertEqual(upgraded["autopilot"]["mode"], "confirm")
        self.assertEqual((self.gemini / ENGINE_REL).read_bytes(), engine_before)

        conflict = run_setup(
            self.root, self.gemini, "apply", "--upgrade", "--profile", "{}"
        )
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("--upgrade", conflict.stderr)

    def test_upgrade_without_installed_profile_fails(self) -> None:
        result = run_setup(self.root, self.gemini, "apply", "--upgrade")
        self.assertEqual(result.returncode, 2)
        self.assertIn("profile.json", result.stderr)
        self.assertFalse((self.gemini / ENGINE_REL).is_file())

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
        self.assertNotIn("orca-agy-autopilot", hooks)

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
            '{"autopilot": {"mode": "bogus"}}',
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
        self.workspace.mkdir()
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
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
        self.assertIn("stop_delivery_frozen", log)

    def test_corrupt_or_unreadable_state_requires_explicit_recovery(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        path = state_path(self.state, "c-corrupt")
        path.write_text("{not-json", encoding="utf-8")
        self.stop("c-corrupt", suggestion("继续推进", gate="auto"))
        self.assertEqual(send_calls(self.root), [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
        self.assertIn("state_corrupt", log)
        self.assertIn("stop_state_corrupt", log)

        unreadable = state_path(self.state, "c-unreadable")
        unreadable.mkdir(parents=True)
        self.stop("c-unreadable", suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 0)
        self.assertTrue(unreadable.is_dir())
        failed = self.injection(self.pre("c-unreadable", ",autopilot recover"))
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
            [{"type": "USER_INPUT", "content": ",autopilot recover"}],
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
                lastUserInput=",autopilot recover",
                transcriptPath=str(partial_transcript),
                workspacePaths=[str(self.workspace)],
            ),
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(partial.read_text(encoding="utf-8"), "{broken")
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
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
            [{"type": "USER_INPUT", "content": ",autopilot recover"}],
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
                lastUserInput=",autopilot recover",
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
            (self.state / "autopilot.log").read_text(encoding="utf-8"),
        )
        self.stop(rebuild_id, suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 0)

        message = self.injection(self.pre("c-corrupt", ",autopilot recover"))
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
        self.assertEqual(read_json(self.gemini / PROFILE_REL)["autopilot"]["mode"], "confirm")

    def test_worker_chat_switch_and_reserved_aliases(self) -> None:
        for target in ("codex", "claude"):
            message = self.injection(self.pre("worker-chat", f",worker {target}"))
            self.assertIn(f"--agent {target}", message)
            self.assertIn("<!-- autopilot: scene=NONE -->", message)
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
        # Failure means a sandbox switch edits global guidance, or default switching leaves stale CLI examples.
        gemini = self.root / "home" / ".gemini"
        apply_profile(self.root, gemini, "--preset", "recommended")
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
        failed = run_engine(self.root, gemini, self.state, "worker", "claude", extra_env={"ORCA_AUTOPILOT_GEMINI_DIR": None})
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual((gemini / PROFILE_REL).read_bytes(), profile_before)
        self.assertEqual((gemini / GEMINI_REL).read_bytes(), guidance_before)
        self.assertEqual(paths[0].read_text(encoding="utf-8"), original)
        paths[1].write_text(original.replace("默认优先使用 ", "默认优先 "), encoding="utf-8")
        actual = run_engine(self.root, gemini, self.state, "worker", "codex", extra_env={"ORCA_AUTOPILOT_GEMINI_DIR": None})
        self.assertEqual(actual.returncode, 0, actual.stderr)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertIn("Orca Orchestration：Codex > Claude Code", text)
            self.assertIn("--name <name> --agent codex", text)
            self.assertIn("次选 `--agent claude`", text)
            self.assertIn("Codex（`--agent codex`），其次使用 Claude Code（`--agent claude`）", text)
            self.assertIn("保留其他用户规则", text)

    def test_worker_reload_reaches_open_and_unversioned_sessions_once(self) -> None:
        self.stop("worker-open", "<!-- autopilot: scene=NONE -->")
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
        self.stop(session, "<!-- autopilot: scene=NONE -->")
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
            read_json(self.gemini / PROFILE_REL)["autopilot"]["mode"], "confirm"
        )
        self.stop("c-mode", suggestion("继续推进", gate="auto"))
        sends = send_calls(self.root)
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--enter", sends[0])
        self.assertIn("confirm", self.injection(self.pre("c-mode", ",mode status")))

    def test_scene_none_and_pending_ask_question_clear_without_sending(self) -> None:
        self.stop(
            "c-none",
            "已完成。\n<!-- autopilot: scene=NONE -->",
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
        lock_path = self.gemini / "config" / "autopilot" / ".autopilot.lock"
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
            [sys.executable, str(SCRIPTS_DIR / "assets" / "autopilot.py"), "sync"],
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
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
        self.assertIn("auto_submit_rejected", log)

    def test_invalid_or_duplicate_tag_never_auto_submits(self) -> None:
        self.stop(
            "c-tag-missing",
            "方案已就绪。\n\n💡 下一步建议：继续推进\n"
            "<!-- autopilot: scene=plan-and-orchestrate -->",
        )
        self.stop(
            "c-tag-duplicate",
            "方案已就绪。\n\n💡 下一步建议：继续推进\n"
            "<!-- autopilot: scene=plan-and-orchestrate gate=auto -->\n"
            "<!-- autopilot: scene=plan-and-orchestrate gate=auto -->",
        )
        self.stop("c-tag-bad-gate", suggestion("继续推进", gate="maybe"))
        self.assertEqual(send_calls(self.root), [])

    def test_trailing_tag_ignores_markdown_and_earlier_tag_examples(self) -> None:
        # Failure means human formatting or documentation can override execution.
        prompt = "检查 `src/**/*.py`，保留 2**3 与内联 `code`"
        examples = (
            "正文提及 `<!-- autopilot: scene=NONE -->`。\n",
            "```html\n<!-- autopilot: scene=NONE -->\n```\n",
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
            f"<!-- autopilot: {tag} -->",
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
            "<!-- autopilot: scene=architecture-re-review, gate=auto, 1='继续推进实施：先补边界测试' -->",
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
            f"<!-- autopilot: {tag} -->",
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
        self.stop("c-tag-only", "<!-- autopilot: " + json.dumps(valid) + " -->")
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
            self.stop(session, "<!-- autopilot: " + json.dumps(fields) + " -->")
            self.assertEqual(len(send_calls(self.root)), 1)
            log = (self.state / "autopilot.log").read_text(encoding="utf-8")
            self.assertIn(f'"cid": "{session}", "reason": "{reason}"', log)
        for index, (body, reason) in enumerate((
            ('{"scene":"NONE","SCENE":"NONE"}', "duplicate_field"),
            ('{"scene":"NONE",}', "invalid_json"),
            ('scene=NONE scene=NONE', "invalid_json"),
            ('scene=plan-and-orchestrate gate=pause 1="推进"', "invalid_json"),
        )):
            session = f"c-malformed-{index}"
            self.stop(session, "<!-- autopilot: " + body + " -->")
            self.assertEqual(len(send_calls(self.root)), 1)
            self.assertEqual(read_state(self.state, session)["candidates"], [])
            log = (self.state / "autopilot.log").read_text(encoding="utf-8")
            self.assertIn(f'"cid": "{session}", "reason": "{reason}"', log)
        for index, body in enumerate(('{"scene":"NONE"}', 'scene=NONE')):
            self.stop(f"c-none-{index}", "<!-- autopilot: " + body + " -->")
            self.assertEqual(len(send_calls(self.root)), 1)

    def test_prompt_literals_survive_parsing(self) -> None:
        # Failure means structured text is modified by presentation cleanup or comment splitting.
        prompts = (
            "仅检查 src/**/*.py 与 2**3 的差异",
            "`继续推进`",
            "第一行\n第二行：\"原样\"",
            "Search for <!--secret--> and keep --> literal",
            '排查 <!-- autopilot: {"scene": "NONE"} --> 标签解析',
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
        self.stop("c-no-tag-prompt", "主推：不执行\n<!-- autopilot: scene=plan-and-orchestrate gate=pause -->")
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
                    log = (self.state / "autopilot.log").read_text(encoding="utf-8")
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
                log = (self.state / "autopilot.log").read_text(encoding="utf-8")
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
                    log = (self.state / "autopilot.log").read_text(encoding="utf-8")
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
                log = (self.state / "autopilot.log").read_text(encoding="utf-8")
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
            '<!-- autopilot: {"scene": "plan-and-orchestrate", "gate": "pause", '
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
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
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
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
        self.assertIn("terminal_workspace_missing", log)

    def test_duplicate_stop_for_same_turn_submits_once(self) -> None:
        transcript = write_transcript(
            self.root,
            "same-turn.jsonl",
            [
                {"type": "USER_INPUT", "content": "开始"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": suggestion("继续推进", gate="auto"),
                },
            ],
        )
        body = payload(
            "c-duplicate",
            transcriptPath=str(transcript),
            terminationReason="NO_TOOL_CALL",
            fullyIdle=True,
            workspacePaths=[str(self.workspace)],
        )
        first = run_engine(
            self.root, self.gemini, self.state, "hook", "--event", "Stop", stdin=body
        )
        second = run_engine(
            self.root, self.gemini, self.state, "hook", "--event", "Stop", stdin=body
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(send_calls(self.root)), 1)
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
        self.assertIn("stop_delivery_frozen", log)

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

        self.stop("c-unknown", suggestion("继续推进", gate="auto"))
        self.assertEqual(len(send_calls(self.root)), 2)

    def test_candidate_index_uses_structured_number(self) -> None:
        text = (
            "方案已就绪。\n\n💡 下一步建议：主推方案\n"
            "↳ 备选 ,3：第三方案\n"
            '<!-- autopilot: {"scene":"plan-and-orchestrate","gate":"pause","1":"主推方案","3":"第三方案"} -->'
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
        log = (self.state / "autopilot.log").read_text(encoding="utf-8")
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
        self.assertEqual(profile["autopilot"]["mode"], "confirm")
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

    def test_status_reports_active_mode_scenes_and_hook_group(self) -> None:
        status = run_engine(self.root, self.gemini, self.state, "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report["mode"], "gate")
        self.assertEqual(len(report["scenes"]), 4)
        self.assertTrue(report["hooks_group"])


if __name__ == "__main__":
    unittest.main()
