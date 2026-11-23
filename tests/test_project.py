"""Workspace shortcut isolation and universal compilation contracts.

resident-fact: workspace overlays are resolved per turn and global artifacts
stay project-neutral through install, upgrade, inspection, and uninstall.
"""
import copy
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor

from the_pilot import __version__
from the_pilot.compiler import compile_bundle, merge_hooks_json, split_frontmatter
from the_pilot.defaults import DEFAULT_SPECS, PIPELINE_STAGE_CONTRACTS, SCENE_ORDER, SCENES_BEGIN
from the_pilot.distill import distill_chat_history
from the_pilot.profile import resolve_profile
from the_pilot.project import ProfileError, normalize_project

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gemini = self.root / "gemini"
        self.base = json.loads((ROOT / "src/the_pilot/assets/profile.default.json").read_text())
        self.project = json.loads((FIXTURES / "alter-service.json").read_text())
        self.env = dict(os.environ, HOME=str(self.root / "home"),
                        THE_PILOT_GEMINI_DIR=str(self.gemini), THE_PILOT_DISABLE_AUTO_UPDATE="1")

    def cli(self, *args, cwd=None, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "bin/pilot"), *args,
                                 "--gemini-dir", str(self.gemini), "--json"],
                                cwd=cwd or self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return json.loads(result.stdout)

    def test_workspace_shortcuts_stay_out_of_global_artifacts(self):
        """Each turn sees its nearest overlay and no other workspace's entries."""
        repo_a, repo_b, repo_c = (self.root / name for name in ("alpha", "beta", "neutral"))
        child = repo_a / "nested/child"
        child.mkdir(parents=True)
        repo_c.mkdir()
        overlay_a = repo_a / ".pilot/project.json"
        overlay_a.parent.mkdir()
        overlay_a.write_text(json.dumps(self.project), encoding="utf-8")
        overlay_b = repo_b / ".pilot/project.json"
        overlay_b.parent.mkdir(parents=True)
        prod = dict(self.project["entry_specs"]["fb"], command_id="CMD_BETA_PROD",
                    template="Beta release {arg}")
        overlay_b.write_text(json.dumps({
            "schema": "the-pilot-project/v1", "projectId": "beta",
            "entry_order": ["prod"], "entry_specs": {"prod": prod},
        }), encoding="utf-8")
        home = self.root / "home"
        self.gemini = home / ".gemini"
        self.env["THE_PILOT_GEMINI_DIR"] = str(self.gemini)
        for path in (home / ".claude/CLAUDE.md", home / ".codex/AGENTS.md"):
            path.parent.mkdir(parents=True)
            path.write_text("Personal instructions\n", encoding="utf-8")
        self.cli("apply", "--project", str(repo_a), cwd=child)
        pilot = self.gemini / "config/the-pilot"
        paths = [self.gemini / "config/GEMINI.md", home / ".claude/CLAUDE.md",
                 home / ".codex/AGENTS.md", pilot / "config.json", pilot / "profile.json",
                 *sorted((pilot / "scenes").glob("*.md"))]
        before = {path: path.read_bytes() for path in paths}
        mtimes = {path: path.stat().st_mtime_ns for path in paths}
        for content in before.values():
            for marker in (b"peq.", b"alter-feedback", b"alter-service", b"pending-prod-publish"):
                self.assertFalse(marker in content, f"{marker!r} in a global artifact")
        self.assertFalse((pilot / "project.json").exists())
        self.assertEqual(self.cli("status", cwd=child)["drift"], [])
        self.assertEqual(self.cli("probe", cwd=child)["recommended_profile"]["shortcuts"]["enabled_entry"],
                         ["r", "rt", "d", "pull"])

        def pre(case):
            cid, workspace, command = case
            result = subprocess.run(
                [sys.executable, str(pilot / "runtime.py"), "hook", "--event", "PreInvocation"],
                input=json.dumps({"conversationId": cid, "lastUserInput": command,
                                  "workspacePaths": ["", str(workspace), str(child)], "cwd": str(repo_c)}),
                env=self.env, cwd=repo_c, text=True, capture_output=True, check=True,
            )
            return json.loads(result.stdout)

        with ThreadPoolExecutor(max_workers=3) as pool:
            actual = list(pool.map(pre, (
                ("a", child, "fb feedback-1"),
                ("b", child, ",fb feedback-2"),
                ("c", repo_b, "prod verify"),
                ("d", repo_b, ",fb hidden"),
                ("e", repo_c, "fb hidden"),
                ("f", repo_c, ",prod hidden"),
            )))
        self.assertIn("$alter-feedback", json.dumps(actual[0]))
        self.assertIn("feedback-2", json.dumps(actual[1]))
        self.assertIn("Beta release verify", json.dumps(actual[2]))
        self.assertEqual(actual[3:], [{}, {}, {}])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual({path: path.stat().st_mtime_ns for path in paths}, mtimes)

    def test_upgrade_ignores_legacy_feedback_and_removes_global_overlay(self):
        """Upgrade removes stale project state while retaining user shortcut overrides."""
        self.cli("apply")
        pilot = self.gemini / "config/the-pilot"
        profile_path = pilot / "profile.json"
        profile = json.loads(profile_path.read_text())
        profile["scenes"]["enable_feedback_email"] = True
        profile["shortcuts"]["enabled_entry"] = ["cq", "fb", "r", "pull"]
        profile["shortcuts"]["template_overrides"] = {",FB": "My feedback {arg}"}
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        stale = pilot / "project.json"
        stale.write_text("{malformed legacy snapshot", encoding="utf-8")
        self.cli("apply", "--upgrade")
        self.assertFalse(stale.exists())
        upgraded = json.loads(profile_path.read_text())
        self.assertNotIn("enable_feedback_email", upgraded["scenes"])
        self.assertEqual(upgraded["shortcuts"]["template_overrides"], {"fb": "My feedback {arg}"})
        config = json.loads((pilot / "config.json").read_text())
        self.assertEqual(set(config["entry_shortcuts"]), {"r", "pull"})
        overlay = self.root / ".pilot/project.json"
        overlay.parent.mkdir()
        overlay.write_text(json.dumps(self.project), encoding="utf-8")
        for i, command in enumerate(("fb A", ",fb A")):
            result = subprocess.run(
                [sys.executable, str(pilot / "runtime.py"), "hook", "--event", "PreInvocation"],
                input=json.dumps({"conversationId": f"override-{i}", "lastUserInput": command,
                                  "cwd": str(self.root)}),
                env=self.env, cwd=self.root, text=True, capture_output=True, check=True,
            )
            self.assertIn("My feedback A", result.stdout)
        stale.write_text("legacy", encoding="utf-8")
        self.cli("uninstall")
        self.assertFalse(stale.exists())

    def test_compiled_contract_and_parallel_project_isolation(self):
        """Compilation preserves the standalone runtime and overlay contracts."""
        runtime = (ROOT / "src/the_pilot/assets/runtime.py").read_text()
        tree = ast.parse(runtime)
        self.assertFalse(any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree)))
        for preset in ("recommended", "conservative"):
            for strategy in ("merge_dev_e2e", "merge_dev_only"):
                for feedback in (True, False):
                    profile = resolve_profile(self.base, preset=preset, project=self.project,
                                              overrides={"scenes": {"deliver_strategy": strategy,
                                                                    "enable_feedback_email": feedback}})
                    bundle = compile_bundle(profile, project=self.project)
                    expected_entries = self.project["entry_order"] if preset == "recommended" else ["r", "rt", "d", "pull"]
                    self.assertEqual(list(bundle.config_json["entry_shortcuts"]), expected_entries)
                    self.assertEqual(
                        set(bundle.config_json["comma_commands"]),
                        {((self.project.get("entry_specs") or {}).get(key)
                          or DEFAULT_SPECS["entry_specs"][key])["command_id"]
                         for key in expected_entries},
                    )
                    self.assertEqual(bundle.profile_json["pilot"]["mode"],
                                     "confirm" if preset == "conservative" else "gate")
                    self.assertEqual(bundle.profile_json["scenes"]["deliver_strategy"], strategy)
                    self.assertNotIn("enable_feedback_email", bundle.profile_json["scenes"])
                    self.assertEqual(bundle.config_json["entry_shortcuts"]["r"]["command_id"], "CMD_MOD_REVIEW")
                    self.assertEqual(bundle.config_json["entry_shortcuts"]["r"]["canonical_key"], "r")
                    self.assertEqual(bundle.config_json["entry_shortcuts"]["r"]["usage"], "r <module>")
                    self.assertEqual(set(bundle.scenes), {
                        DEFAULT_SPECS["scene_specs"][name]["file"]
                        for name in SCENE_ORDER
                    })
                    self.assertTrue(all("{{" not in text for text in (*bundle.scenes.values(), bundle.gemini_block)))
                    self.assertTrue(bundle.gemini_block.startswith("<!-- BEGIN THE_PILOT_PROFILE -->"))
                    self.assertIn("<!-- END THE_PILOT_SCENES -->", bundle.gemini_block)
                    self.assertIn("<!-- pilot: scene=NONE -->", bundle.gemini_block)
                    self.assertTrue(bundle.gemini_block.endswith("\n"))
                    hooks = merge_hooks_json({"other": {"hooks": []}}, "/pilot/runtime.py")
                    self.assertEqual(hooks["other"], {"hooks": []})
                    self.assertIn("the-pilot", hooks)
                    self.assertIn("/pilot/runtime.py", json.dumps(hooks["the-pilot"]))
        original = copy.deepcopy(self.project)
        def compile_for(project):
            profile = resolve_profile(self.base, project=project)
            return compile_bundle(profile, project=project).config_json["entry_shortcuts"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            generic, linked = list(pool.map(compile_for, (None, self.project)))
        self.assertEqual(list(generic), ["r", "rt", "d", "pull"])
        self.assertEqual(list(linked), self.project["entry_order"])
        self.assertEqual(self.project, original)

    def test_legacy_profile_and_project_sections_normalize_once(self):
        """Failure means an old profile or preset keeps a second owner for mode and turn cap."""
        legacy = copy.deepcopy(self.base)
        legacy["schema"] = "orca-autopilot-profile/v1"
        legacy["autopilot"] = legacy.pop("pilot")
        normalized = resolve_profile(legacy, preset=None)
        self.assertEqual(normalized["schema"], "the-pilot-profile/v1")
        self.assertIn("pilot", normalized)
        self.assertNotIn("autopilot", normalized)

        project = normalize_project({
            "schema": "the-pilot-project/v1",
            "projectId": "legacy-presets",
            "presets": {"recommended": {"autopilot": {"mode": "gate"}}},
        })
        self.assertIn("pilot", project["presets"]["recommended"])
        self.assertNotIn("autopilot", project["presets"]["recommended"])

        ambiguous = copy.deepcopy(legacy)
        ambiguous["pilot"] = copy.deepcopy(self.base["pilot"])
        with self.assertRaises(ProfileError):
            resolve_profile(ambiguous, preset=None)
        with self.assertRaises(ProfileError):
            normalize_project({
                "schema": "the-pilot-project/v1",
                "projectId": "dual-owner",
                "presets": {"recommended": {
                    "pilot": {"mode": "gate"},
                    "autopilot": {"mode": "gate"},
                }},
            })

    def test_universal_and_installed_runtime_commands(self):
        profile = resolve_profile(self.base)
        bundle = compile_bundle(profile)
        self.assertEqual(profile["shortcuts"]["enabled_entry"], ["r", "rt", "d", "pull"])
        for marker in ("alter-service", "alter-feedback", "Lark", "Source Hub", "WORKING.md",
                       "pending-prod-publish", "alert_transition.py", "cleanup-stale-worktrees"):
            self.assertNotIn(marker, bundle.gemini_block)
        self.cli("probe", "--no-project")
        self.cli("apply", "--no-project")
        self.assertEqual(self.cli("status")["drift"], [])
        installed = self.gemini / "config/the-pilot/runtime.py"
        self.assertEqual(installed.read_bytes(), (ROOT / "src/the_pilot/assets/runtime.py").read_bytes())
        for args in (("worker", "status", "--json"), ("mode", "status"), ("sync",), ("log",),
                     ("set", "r", "Review {arg}"), ("hook", "--event", "PreInvocation")):
            result = subprocess.run([sys.executable, str(ROOT / "bin/pilot"), *args],
                                    env=self.env, cwd=self.root, input='{}', text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.cli("uninstall")
        self.assertFalse(self.cli("status")["installed"])

    def test_universal_architecture_contracts_survive_install_and_distill(self):
        """Failure means a built-in workflow loses evidence, scope or rollout protections."""
        self.cli("apply", "--no-project")
        gemini_path = self.gemini / "config/GEMINI.md"
        gemini = gemini_path.read_text(encoding="utf-8")
        scenes = self.gemini / "config/the-pilot/scenes"
        required_clauses = {
            gemini_path: (
                "严禁主/子会话擅自改写已审定蓝图",
                "主会话调 `ask_question` 获用户确认",
            ),
            scenes / "01-plan-and-orchestrate.md": (
                "定稿前只读预检",
                "主键与唯一键粒度",
                "`ON DELETE`",
                "独立擦除或计费生命周期",
                "权威实体关系、唯一生命周期状态机与目标控制循环/泳道",
                "父表前缀列与次级状态列",
                "只剪外围附表",
                "冻结蓝图与预算",
                "涵盖退场项和本轮交付范围",
            ),
            scenes / "02-architecture-re-review.md": (
                "对照 Anchor 0、已审定蓝图与 `main`",
                "未经授权的蓝图改写或预算放宽",
                "父表列数或次级状态列增加",
                "AST 语句、状态枚举、控制循环与泳道未收敛",
            ),
            scenes / "03-deliver-and-verify.md": (
                "回填、收紧约束或删表删列",
                "合入或发布前",
                "只读核查目标环境",
                "孤儿行、延迟外键、历史枚举/版本值",
            ),
        }
        for document, clauses in required_clauses.items():
            if document == gemini_path:
                rendered = gemini.partition(SCENES_BEGIN)[0]
            else:
                rendered = document.read_text(encoding="utf-8")
            for clause in clauses:
                with self.subTest(document=document.name, clause=clause):
                    self.assertTrue(clause in rendered, f"Missing workflow clause in {document.name}")
                    self.assertTrue(clause in gemini, "Missing workflow clause in GEMINI.md")
            with self.subTest(document=document.name):
                self.assertNotIn("。。", rendered)
                self.assertNotIn("{{", rendered)

        assessment = self.cli("distill", "--home-dir", str(self.root / "home"))["pipeline_assessment"]
        installed = assessment["installed_pipeline"]
        self.assertEqual(installed["profile_status"], "ok")
        self.assertEqual(installed["scenes_status"], "ok")
        self.assertEqual(installed["missing_stages"], [])
        self.assertEqual(installed["incomplete_scene_contracts"], [])
        self.assertEqual(installed["redundant_guardrails"], [])
        contracts = assessment["stage_contracts"]
        self.assertIn("只读核对实际 Schema", contracts["plan-and-orchestrate"]["quality_bar"])
        self.assertIn("冻结审定蓝图与预算", contracts["plan-and-orchestrate"]["simplicity_rule"])
        self.assertIn("搬列合表", contracts["architecture-re-review"]["simplicity_rule"])
        self.assertIn("孤儿行、延迟外键、历史枚举/版本值", contracts["deliver-and-verify"]["quality_bar"])

    def test_conflicting_override_templates_fail_closed(self):
        """Distinct templates for one shortcut are never silently collapsed.

        Failure means `pilot apply` keeps whichever alias it reads last and
        overwrites the other, losing an installed customization instead of
        reporting the ambiguous `template_overrides`.
        """
        self.cli("apply", "--project", str(FIXTURES / "alter-service.json"))
        profile_path = self.gemini / "config/the-pilot/profile.json"
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["template_overrides"] = {
            "r": "Review A {arg}",
            ",r": "Review B {arg}",
        }
        profile_path.write_text(json.dumps(profile))
        installed = profile_path.read_text()

        error = self.cli(
            "apply", "--project", str(FIXTURES / "alter-service.json"), ok=False
        )
        self.assertIn("template_overrides", error["error"])
        self.assertEqual(profile_path.read_text(), installed)

        profile["shortcuts"]["template_overrides"] = {
            "r": "Review A {arg}",
            "CMD_MOD_REVIEW": "Review B {arg}",
        }
        profile_path.write_text(json.dumps(profile))
        error = self.cli("apply", "--no-project", ok=False)
        self.assertIn("template_overrides", error["error"])
        self.assertEqual(profile_path.read_text(), json.dumps(profile))

    def test_command_id_alias_never_shadows_a_canonical_entry_key(self):
        """A secondary command_id never outranks another shortcut's own key.

        Failure means `template_overrides` keyed `beta` or `,beta` binds to
        alpha when alpha's command_id literally names beta, so the override
        follows entry order and silently retargets alpha, and disabling beta
        hijacks alpha instead of failing closed.
        """

        def overlay(order, command_id):
            return {
                "schema": "the-pilot-project/v1",
                "projectId": "command-id-collision",
                "entry_order": [*order, "r", "rt", "d", "pull"],
                "entry_specs": {
                    "alpha": {
                        "command_id": command_id,
                        "description": "alpha",
                        "requires_arg": False,
                        "usage": "alpha",
                        "template": "alpha",
                    },
                    "beta": {
                        "command_id": "CMD_B",
                        "description": "beta",
                        "requires_arg": False,
                        "usage": "beta",
                        "template": "beta",
                    },
                },
                "presets": {
                    "recommended": {
                        "shortcuts": {
                            "enabled_entry": [*order, "r", "rt", "d", "pull"]
                        }
                    }
                },
            }

        for command_id in ("beta", ",BETA"):
            for order in (["alpha", "beta"], ["beta", "alpha"]):
                for raw_key in ("beta", ",beta"):
                    with self.subTest(
                        command_id=command_id, order=order, raw_key=raw_key
                    ):
                        profile = copy.deepcopy(self.base)
                        profile["shortcuts"]["enabled_entry"] = [
                            *order,
                            "r",
                            "rt",
                            "d",
                            "pull",
                        ]
                        profile["shortcuts"]["template_overrides"] = {
                            raw_key: "beta tpl"
                        }
                        resolved = resolve_profile(
                            profile,
                            preset=None,
                            project=overlay(order, command_id),
                        )
                        self.assertEqual(
                            resolved["shortcuts"]["template_overrides"],
                            {"beta": "beta tpl"},
                        )
                        bundle = compile_bundle(
                            resolved, project=overlay(order, command_id)
                        )
                        self.assertEqual(
                            bundle.config_json["entry_shortcuts"]["beta"]["template"],
                            "beta tpl",
                        )
                        self.assertEqual(
                            bundle.config_json["entry_shortcuts"]["alpha"]["template"],
                            "alpha",
                        )


    def test_conflicting_secondary_command_ids_fail_closed(self):
        """No two active shortcuts may share one command identity.

        Failure means an overlay entry and a custom shortcut with the same
        command_id are accepted, so `template_overrides` binds by section
        order and the compiler either double-registers or drops a command.
        """

        def overlay(order, command_id):
            return {
                "schema": "the-pilot-project/v1",
                "projectId": "shared-command-id",
                "entry_order": order,
                "entry_specs": {
                    "alpha": {
                        "command_id": command_id,
                        "description": "alpha",
                        "requires_arg": False,
                        "usage": "alpha",
                        "template": "alpha",
                    },
                },
                "presets": {
                    "recommended": {
                        "shortcuts": {
                            "enabled_entry": order
                        }
                    }
                },
            }

        orders = (
            ["alpha", "r", "rt", "d", "pull"],
            ["r", "rt", "d", "pull", "alpha"],
        )
        sections = (
            ("custom_entry", "CMD_CUSTOM_ENTRY_REQ"),
            ("custom_comma", "CMD_CUSTOM_COMMA_REQ"),
        )
        for section, command_id in sections:
            for order in orders:
                profile = copy.deepcopy(self.base)
                profile["shortcuts"]["enabled_entry"] = list(order)
                profile["shortcuts"][section] = {
                    "req": {"template": "request {arg}"}
                }
                profile["shortcuts"]["template_overrides"] = {
                    command_id: "shared tpl"
                }
                with self.subTest(section=section, order=order):
                    with self.assertRaises(ProfileError) as caught:
                        resolve_profile(
                            profile,
                            preset=None,
                            project=overlay(order, command_id),
                        )
                    self.assertIn(
                        f"命令身份冲突：{command_id}", str(caught.exception)
                    )


    def test_invalid_legacy_overlay_fields_fail_pure_validation(self):
        """Legacy compatibility must still reject invalid shortcut and scene contracts."""
        for mutation in ({"scene_specs": {"closeout-and-sync": {"file": "../../escape.md"}}},
                         {"entry_order": ["r", "r"]}, {"unknown": True},
                         {"entry_specs": {"r": {"requires_arg": "yes"}}},
                         {"presets": {"recommended": None}}):
            with self.subTest(mutation=mutation):
                bad = dict(self.project, **mutation)
                with self.assertRaises(ProfileError):
                    normalize_project(bad)

    def test_plugin_and_agent_manifests_match_package_metadata(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["name"], "the-pilot")
        self.assertEqual(pyproject["project"]["version"], __version__)
        claude_plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        cursor_plugin = json.loads((ROOT / ".cursor-plugin/plugin.json").read_text(encoding="utf-8"))
        expected_skills = ["./skills/the-pilot", "./skills/the-pilot-setup"]
        for manifest in (claude_plugin, cursor_plugin, marketplace["plugins"][0]):
            self.assertEqual(manifest["name"], "the-pilot")
            self.assertEqual(manifest["version"], __version__)
            self.assertEqual(manifest["description"], pyproject["project"]["description"])
            self.assertEqual(manifest["license"], pyproject["project"]["license"])
            self.assertEqual(manifest["keywords"], pyproject["project"]["keywords"])
        self.assertEqual(claude_plugin["skills"], expected_skills)
        self.assertEqual(cursor_plugin["skills"], expected_skills)
        for rel_skill in ("skills/the-pilot", "skills/the-pilot-setup"):
            skill_dir = ROOT / rel_skill
            self.assertTrue((skill_dir / "SKILL.md").is_file())
            openai_yaml = (skill_dir / "agents/openai.yaml").read_text(encoding="utf-8")
            self.assertIn("display_name:", openai_yaml)
            self.assertIn("short_description:", openai_yaml)
            self.assertIn("default_prompt:", openai_yaml)

    def test_distill_multi_agent_chat_history_and_recommendations(self):
        home = self.root / "home"
        now = time.time()
        recent_iso = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
        old_iso = datetime.fromtimestamp(now - 10 * 86400, tz=timezone.utc).isoformat()
        old_ts = now - 10 * 86400

        # 1. Antigravity: pre-window opener + in-window followups + assistant pilot tag provenance
        agy_recent = self.gemini / "antigravity-cli/brain/s1/.system_generated/logs/transcript.jsonl"
        agy_recent.parent.mkdir(parents=True)
        agy_recent.write_text("\n".join([
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": old_iso,
                "content": "<USER_REQUEST>\ninitial session opener before 7d window\n</USER_REQUEST>",
            }),
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": recent_iso,
                "content": "<USER_REQUEST>\nmid-session followup after pre-window opener\n</USER_REQUEST>",
            }),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "created_at": recent_iso,
                "content": 'Done.\n<!-- pilot: {"scene": "architecture-re-review", "gate": "auto", "1": "Audit whole task architecture"} -->',
            }),
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": recent_iso,
                "content": "<USER_REQUEST>\nAudit whole task architecture\n</USER_REQUEST>",
            }),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "created_at": recent_iso,
                "content": 'Stale suggestion.\n<!-- pilot: {"scene": "closeout-and-sync", "gate": "auto", "1": "Stale closeout prompt"} -->',
            }),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "created_at": recent_iso,
                "content": "Superseded by final response.\n<!-- pilot: scene=NONE -->",
            }),
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": recent_iso,
                "content": "<USER_REQUEST>\nStale closeout prompt\n</USER_REQUEST>",
            }),
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": recent_iso,
                "content": "<USER_REQUEST>\n,1\n</USER_REQUEST>",
            }),
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": recent_iso,
                "content": "<SYSTEM_MESSAGE>background task finished</SYSTEM_MESSAGE>",
            }),
        ]) + "\n")

        agy_entry = self.gemini / "antigravity-cli/brain/s2/.system_generated/logs/transcript.jsonl"
        agy_entry.parent.mkdir(parents=True)
        agy_entry.write_text(json.dumps({
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "created_at": recent_iso,
            "content": "<USER_REQUEST>\ntriage issue 95c4bb96-c616-4fdd-98c1-ad6b31d884ff in services/api/handler.py\n</USER_REQUEST>",
        }) + "\n")

        agy_old = self.gemini / "antigravity-cli/brain/s_old/.system_generated/logs/transcript.jsonl"
        agy_old.parent.mkdir(parents=True)
        agy_old.write_text(json.dumps({
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "created_at": old_iso,
            "content": "<USER_REQUEST>\nold message outside window\n</USER_REQUEST>",
        }) + "\n")
        os.utime(agy_old, (old_ts, old_ts))

        # 2. Claude Code: repeated turns in same session + pre-window turn + history mirror reconciliation
        claude_proj = home / ".claude/projects/demo/c1.jsonl"
        claude_proj.parent.mkdir(parents=True)
        claude_proj.write_text("\n".join([
            json.dumps({
                "type": "user",
                "timestamp": int(old_ts * 1000),
                "message": {"content": "triage issue e2931f0c-03bf-45fe-ac1b-d6b7edddd83e in services/auth/login.py"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int(old_ts * 1000),
                "message": {"content": "继续"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int((now - 30) * 1000),
                "message": {"content": "继续"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int(now * 1000),
                "message": {"content": "继续"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int(now * 1000),
                "message": {"content": [{"type": "text", "text": "合并当前分支到 main 并完成实跑验证"}]},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int(now * 1000),
                "message": {"content": "You are an independent Adversarial Architecture & Engineering Quality Reviewer."},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int(now * 1000),
                "message": {"content": "Generate a short git branch slug (2-4 lowercase hyphenated words) for: fix timeout."},
            }),
        ]) + "\n")
        claude_c2 = home / ".claude/projects/demo/c2.jsonl"
        claude_c2.write_text(json.dumps({
            "type": "user",
            "timestamp": int(now * 1000),
            "message": {"content": "triage issue e2931f0c-03bf-45fe-ac1b-d6b7edddd83e in services/auth/login.py"},
        }) + "\n")
        os.utime(claude_c2, (now - 30, now - 30))
        claude_hist = home / ".claude/history.jsonl"
        claude_hist.write_text("\n".join([
            json.dumps({
                "sessionId": "c1",
                "timestamp": int((now - 60) * 1000),
                "display": "继续",
            }),
            json.dumps({
                "sessionId": "c1",
                "timestamp": int(now * 1000),
                "display": "继续",
            }),
            json.dumps({
                "sessionId": "c2",
                "timestamp": int(now * 1000),
                "display": "triage issue e2931f0c-03bf-45fe-ac1b-d6b7edddd83e in services/auth/login.py",
            }),
            json.dumps({
                "sessionId": "c_extra",
                "timestamp": int((now - 120) * 1000),
                "display": "extra session in history",
            }),
        ]) + "\n")

        # 3. Codex: canonical UUID reconciliation + both mirror orders (event->response and response->event)
        codex_uuid = "01a1c8bb-6f9f-7162-a8f4-7fd61cb6ac69"
        codex_sess = home / f".codex/sessions/2026/11/11/rollout-2026-11-11T11-51-29-{codex_uuid}.jsonl"
        codex_sess.parent.mkdir(parents=True)
        codex_sess.write_text("\n".join([
            json.dumps({
                "type": "event_msg",
                "timestamp": recent_iso,
                "payload": {"type": "user_message", "message": "You are working inside Orca, a multi-agent IDE. You are a dispatched worker."},
            }),
            json.dumps({
                "type": "response_item",
                "timestamp": recent_iso,
                "payload": {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "合并当前分支到 main 并完成实跑验证"},
                        {"type": "input_text", "text": "<skill>\n<name>the-pilot</name>\n</skill>"},
                    ],
                },
            }),
            json.dumps({
                "type": "event_msg",
                "timestamp": recent_iso,
                "payload": {"type": "user_message", "message": "合并当前分支到 main 并完成实跑验证"},
            }),
        ]) + "\n")
        codex_hist = home / ".codex/history.jsonl"
        codex_hist.write_text(json.dumps({
            "session_id": codex_uuid,
            "ts": int(now - 60),
            "text": "合并当前分支到 main 并完成实跑验证",
        }) + "\n")

        # 4. Cursor: dated turn + undated turn + malformed line diagnostics
        cursor_sess = home / ".cursor/projects/demo/agent-transcripts/u1.jsonl"
        cursor_sess.parent.mkdir(parents=True)
        cursor_sess.write_text("\n".join([
            "{broken json line",
            json.dumps({"role": "user", "timestamp": recent_iso, "content": "继续"}),
            json.dumps({"role": "user", "content": "undated cursor turn"}),
        ]) + "\n")

        report = distill_chat_history(home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now)
        self.assertEqual(report["schema"], "the-pilot-distillation/v1")
        self.assertEqual(report["config_status"]["status"], "absent")
        self.assertEqual(report["sources"]["antigravity"]["sampled_files"], 2)
        # Claude c1 has 4 distinct in-window turns (1x "继续" from 24h ago in file, 1x "继续" from 30m ago in history, 1x "继续" at now mirrored in both, 1x deliver); c2 has 1; c_extra has 1
        self.assertEqual(report["sources"]["claude"]["human_messages"], 6)
        self.assertEqual(report["sources"]["claude"]["filtered_machine_messages"], 2)
        # Codex rollout event_msg + response_item + history.jsonl mirror of same UUID session counts once
        self.assertEqual(report["sources"]["codex"]["human_messages"], 1)
        self.assertEqual(report["sources"]["codex"]["filtered_machine_messages"], 1)
        # Cursor records malformed line and undated message explicitly
        self.assertEqual(report["sources"]["cursor"]["status"], "degraded")
        self.assertEqual(report["sources"]["cursor"]["malformed_lines"], 1)
        self.assertEqual(report["sources"]["cursor"]["undated_messages"], 1)
        self.assertEqual(report["sources"]["cursor"]["human_messages"], 1)
        self.assertEqual(report["pilot_activity"].get("candidate:,1"), 1)
        self.assertEqual(report["pilot_activity"].get("scene:architecture-re-review"), 1)
        self.assertIsNone(report["pilot_activity"].get("scene:closeout-and-sync"))

        # Pre-window opener in s1 means the first in-window message of s1 is classified as followup, not entry
        s1_sample = next(item for item in report["sampled_prompts"] if "mid-session followup" in item["preview"])
        self.assertEqual(s1_sample["turn_role"], "followup")

        by_sig = {item["signature"]: item for item in report["recurring_patterns"]}
        self.assertIn("继续", by_sig)
        self.assertEqual(by_sig["继续"]["count"], 4)
        self.assertEqual(by_sig["继续"]["category"], "continue_nudge")
        self.assertEqual(by_sig["继续"]["agents"], ["claude", "cursor"])

        triage_sig = "triage issue <id> in <path>"
        self.assertIn(triage_sig, by_sig)
        self.assertEqual(by_sig[triage_sig]["count"], 2)
        self.assertEqual(by_sig[triage_sig]["turn_role"], "entry")
        self.assertEqual(by_sig[triage_sig]["suggested_automation"]["target"], "shortcuts.custom_entry")

        deliver_sig = "合并当前分支到 main 并完成实跑验证"
        self.assertIn(deliver_sig, by_sig)
        self.assertEqual(by_sig[deliver_sig]["count"], 2)
        self.assertEqual(by_sig[deliver_sig]["category"], "deliver_and_verify")

        # 5. Enforce max_sessions_per_agent across session files + history.jsonl AND tail truncation
        capped = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=1, now_ts=now, max_lines_per_session=3
        )
        self.assertEqual(capped["sources"]["claude"]["sampled_files"], 1)
        self.assertEqual(capped["sources"]["claude"]["truncated_files"], 1)
        self.assertFalse(capped["evidence_complete"])

        # 6. Malformed or corrupt config.json surfaces error status and suppresses false onboarding recommendation
        cfg_path = self.gemini / "config/the-pilot/config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        for bad_payload in ("{corrupt", json.dumps({"entry_shortcuts": {"r": {"aliases": 123}}})):
            cfg_path.write_text(bad_payload, encoding="utf-8")
            degraded_cfg = distill_chat_history(home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now)
            self.assertEqual(degraded_cfg["config_status"]["status"], "error")
            self.assertFalse(any(r["category"] == "onboarding" for r in degraded_cfg["recommendations"]))

        # 7. Installed shortcut matching: confirmed entry shortcut vs uncertain template match
        cfg_path.write_text(json.dumps({
            "entry_shortcuts": {
                "ti": {
                    "canonical_key": "ti",
                    "command_id": "triage_issue",
                    "requires_arg": True,
                    "aliases": ["ti", ",ti"],
                    "template": "triage issue {arg}",
                }
            }
        }), encoding="utf-8")
        claude_c3 = home / ".claude/projects/demo/c3.jsonl"
        claude_c3.write_text("\n".join([
            json.dumps({
                "type": "user",
                "timestamp": int(now * 1000),
                "message": {"content": "ti auth-service"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int(now * 1000),
                "message": {"content": "ti billing-service"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int(now * 1000),
                "message": {"content": "ti"},
            }),
        ]) + "\n")
        with_shortcuts = distill_chat_history(home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now)
        self.assertEqual(with_shortcuts["config_status"]["status"], "ok")
        self.assertEqual(with_shortcuts["pilot_activity"].get("entry:ti"), 2)
        # Template-similar free-form prompts remain in manual_messages and surface in uncertain_template_matches
        self.assertEqual(with_shortcuts["uncertain_template_matches"].get("entry:ti"), 2)
        cfg_path.unlink()

        cli_out = self.cli("distill", "--home-dir", str(home), "--days", "7", "--max-sessions", "10")
        self.assertEqual(cli_out["schema"], "the-pilot-distillation/v1")
        self.assertTrue(cli_out["recommendations"])
        self.cli("distill", "--home-dir", str(home), "--days", "0", ok=False)

    def test_distill_pipeline_assessment_reports_stage_contract_gaps(self):
        home = self.root / "home"
        now = time.time()
        claude_dir = home / ".claude/projects/demo"
        claude_dir.mkdir(parents=True)
        (claude_dir / "p1.jsonl").write_text("\n".join([
            json.dumps({
                "type": "user",
                "timestamp": int((now - 180) * 1000),
                "message": {"content": "继续"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int((now - 60) * 1000),
                "message": {"content": "合并当前分支到 main 并完成实跑验证"},
            }),
        ]) + "\n")
        for index in range(2):
            (claude_dir / f"e2e{index}.jsonl").write_text(json.dumps({
                "type": "user",
                "timestamp": int((now - 30 - index) * 1000),
                "message": {"content": "再跑一遍 e2e 验证"},
            }) + "\n")

        pilot_dir = self.gemini / "config/the-pilot"
        scenes_dir = pilot_dir / "scenes"
        scenes_dir.mkdir(parents=True)
        project = {
            "schema": "the-pilot-project/v1",
            "projectId": "demo",
            "scene_specs": {
                "plan-and-orchestrate": {"order": 7, "title": "自定义方案阶段"},
            },
        }
        (pilot_dir / "project.json").write_text(json.dumps(project), encoding="utf-8")
        profile = copy.deepcopy(self.base)
        profile["scenes"]["enabled_scenes"] = ["plan-and-orchestrate", "deliver-and-verify"]
        profile["scenes"]["deliver_strategy"] = "merge_dev_only"
        (pilot_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
        (scenes_dir / "01-plan-and-orchestrate.md").write_text(
            "---\n"
            "name: plan-and-orchestrate\n"
            "description: Plan the task\n"
            f"required_guardrail: {DEFAULT_SPECS['scene_specs']['plan-and-orchestrate']['required_guardrail']}\n"
            "fallback_prompt: Draft the plan\n"
            "---\n"
            "Plan body.\n",
            encoding="utf-8",
        )
        (scenes_dir / "custom-extra.md").write_text(
            "---\n"
            "name: custom-extra\n"
            "description: Extra scene\n"
            "fallback_prompt: Do the extra step\n"
            "---\n"
            "Extra body.\n",
            encoding="utf-8",
        )

        report = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )
        assessment = report["pipeline_assessment"]
        contracts = assessment["stage_contracts"]
        self.assertEqual(list(contracts), list(SCENE_ORDER))
        for stage, contract in contracts.items():
            self.assertEqual(
                set(contract),
                {"order", "title", "deliverable", "quality_bar", "simplicity_rule"},
                stage,
            )
            for field in ("deliverable", "quality_bar", "simplicity_rule"):
                self.assertEqual(contract[field], PIPELINE_STAGE_CONTRACTS[stage][field], stage)
        self.assertEqual(contracts["plan-and-orchestrate"]["order"], 10)
        self.assertEqual(contracts["plan-and-orchestrate"]["title"],
                         DEFAULT_SPECS["scene_specs"]["plan-and-orchestrate"]["title"])
        self.assertEqual(
            contracts["closeout-and-sync"]["title"],
            DEFAULT_SPECS["scene_specs"]["closeout-and-sync"]["title"],
        )
        installed = assessment["installed_pipeline"]
        self.assertEqual(installed["profile_status"], "ok")
        self.assertEqual(installed["deliver_strategy"], "merge_dev_only")
        self.assertEqual(installed["missing_stages"], ["architecture-re-review", "closeout-and-sync"])
        self.assertEqual(installed["custom_scene_files"], ["custom-extra.md"])
        self.assertEqual(installed["duplicate_scene_names"], [])
        self.assertEqual(installed["redundant_guardrails"], [])
        self.assertEqual(installed["incomplete_scene_contracts"], ["03-deliver-and-verify.md"])
        findings = {finding["code"]: finding for finding in assessment["findings"]}
        self.assertEqual(
            set(findings),
            {
                "missing_pipeline_stages",
                "incomplete_stage_contract",
                "review_gate_bypassed",
                "closeout_stage_dropped",
                "deliver_strategy_mismatch",
            },
        )
        self.assertEqual(findings["missing_pipeline_stages"]["stage"], "architecture-re-review")
        self.assertEqual(findings["incomplete_stage_contract"]["stage"], "deliver-and-verify")
        self.assertEqual(findings["review_gate_bypassed"]["evidence_count"], 1)
        self.assertEqual(findings["closeout_stage_dropped"]["evidence_count"], 1)
        self.assertEqual(findings["deliver_strategy_mismatch"]["evidence_count"], 2)
        self.assertTrue(report["evidence_complete"])

        duplicate_guardrail_profile = copy.deepcopy(profile)
        duplicate_guardrail_profile["scenes"]["guardrails"] = [
            DEFAULT_SPECS["scene_specs"]["plan-and-orchestrate"]["required_guardrail"],
        ]
        (pilot_dir / "profile.json").write_text(
            json.dumps(duplicate_guardrail_profile), encoding="utf-8"
        )
        guardrail_assessment = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )["pipeline_assessment"]
        self.assertEqual(
            guardrail_assessment["installed_pipeline"]["redundant_guardrails"],
            [DEFAULT_SPECS["scene_specs"]["plan-and-orchestrate"]["required_guardrail"]],
        )
        self.assertIn(
            "over_engineered_pipeline",
            {finding["code"] for finding in guardrail_assessment["findings"]},
        )

        (scenes_dir / "custom-plan.md").write_text(
            "---\n"
            "name: plan-and-orchestrate\n"
            "description: Duplicate of the built-in planning stage\n"
            "fallback_prompt: Plan the task again\n"
            "---\n"
            "Duplicate body.\n",
            encoding="utf-8",
        )
        duplicate_name_assessment = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )["pipeline_assessment"]
        self.assertEqual(
            duplicate_name_assessment["installed_pipeline"]["duplicate_scene_names"],
            ["plan-and-orchestrate"],
        )
        self.assertIn(
            "over_engineered_pipeline",
            {finding["code"] for finding in duplicate_name_assessment["findings"]},
        )
        (scenes_dir / "custom-plan.md").unlink()
        (pilot_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")

        for path in claude_dir.glob("*.jsonl"):
            path.unlink()
        (claude_dir / "rework.jsonl").write_text("\n".join([
            json.dumps({
                "type": "user",
                "timestamp": int((now - 240) * 1000),
                "message": {"content": "先产出方案并用 Orca 编排实施"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int((now - 180) * 1000),
                "message": {"content": "整体 review 一遍代码"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int((now - 120) * 1000),
                "message": {"content": "先产出方案并用 Orca 编排实施"},
            }),
            json.dumps({
                "type": "user",
                "timestamp": int((now - 60) * 1000),
                "message": {"content": "合并当前分支到 main 并完成实跑验证"},
            }),
        ]) + "\n")
        rework = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )
        rework_findings = {
            finding["code"]: finding
            for finding in rework["pipeline_assessment"]["findings"]
        }
        self.assertEqual(rework_findings["review_gate_bypassed"]["evidence_count"], 1)

        (pilot_dir / "project.json").write_text('{"schema": "wrong"}', encoding="utf-8")
        bad_project = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )
        bad_installed = bad_project["pipeline_assessment"]["installed_pipeline"]
        self.assertEqual(bad_installed["profile_status"], "ok")
        self.assertEqual(bad_installed["custom_scene_files"], ["custom-extra.md"])

        (pilot_dir / "profile.json").write_text("{corrupt", encoding="utf-8")
        corrupt = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )
        corrupt_installed = corrupt["pipeline_assessment"]["installed_pipeline"]
        self.assertEqual(corrupt_installed["profile_status"], "error")
        self.assertTrue(corrupt_installed["error"])
        self.assertFalse(corrupt["evidence_complete"])
        self.assertNotIn(
            "missing_pipeline_stages",
            {finding["code"] for finding in corrupt["pipeline_assessment"]["findings"]},
        )


