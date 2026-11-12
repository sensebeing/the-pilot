"""Project compilation and installed overlay lifecycle contracts.

resident-fact: settlement: installed overlays survive cwd changes and upgrades;
explicit universal selection and uninstall remove only the owned project link.
resident-fact: admission: invalid overlays cannot escape scene paths or write an
installation; project compilation matches the reviewed structured-tag contract.
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
from the_pilot.compiler import compile_bundle, merge_hooks_json
from the_pilot.defaults import DEFAULT_SPECS, PIPELINE_STAGE_CONTRACTS, SCENE_ORDER
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
                        THE_PILOT_GEMINI_DIR=str(self.gemini))

    def cli(self, *args, cwd=None, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "bin/pilot"), *args,
                                 "--gemini-dir", str(self.gemini), "--json"],
                                cwd=cwd or self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return json.loads(result.stdout)

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
                    self.assertEqual(bundle.profile_json["scenes"]["enable_feedback_email"], feedback)
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

    def test_upgrade_promotes_colliding_custom_entry_and_enables_new_recommended(self):
        """Upgrade owns promotion of project entries while direct profiles stay strict."""
        repo = self.root / "repo"
        (repo / ".pilot").mkdir(parents=True)
        self.cli("apply", "--project", str(FIXTURES / "alter-service.json"))
        profile_path = self.gemini / "config/the-pilot/profile.json"
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["custom_entry"] = {"prod": "deploy {arg}", "req": "request {arg}"}
        profile_path.write_text(json.dumps(profile))
        upgraded = copy.deepcopy(self.project)
        upgraded["entry_order"] = ["prod", *upgraded["entry_order"]]
        upgraded["entry_specs"] = {"prod": {"command_id": "CMD_PROD", "description": "prod", "requires_arg": True, "usage": "prod <arg>", "template": "deploy {arg}"}, **upgraded["entry_specs"]}
        upgraded["presets"]["recommended"]["shortcuts"]["enabled_entry"] = ["prod", *upgraded["presets"]["recommended"]["shortcuts"]["enabled_entry"]]
        overlay = repo / ".pilot/project.json"
        overlay.write_text(json.dumps(upgraded))
        self.cli("apply", "--project", str(overlay), "--upgrade")
        result = json.loads(profile_path.read_text())
        self.assertIn("prod", result["shortcuts"]["enabled_entry"])
        self.assertIn("req", result["shortcuts"]["custom_entry"])
        self.assertNotIn("prod", result["shortcuts"]["custom_entry"])
        with self.assertRaises(ProfileError):
            resolve_profile(profile, project=upgraded)
        self.assertEqual(self.cli("status")["drift"], [])

        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["enabled_entry"].remove("fb")
        profile_path.write_text(json.dumps(profile))
        newer = copy.deepcopy(upgraded)
        newer["entry_order"] = ["prod", "shipcheck", *upgraded["entry_order"][1:]]
        newer["entry_specs"] = {
            "shipcheck": {"command_id": "CMD_SHIPCHECK", "description": "shipcheck", "requires_arg": False, "usage": "shipcheck", "template": "check shipment"},
            **upgraded["entry_specs"],
        }
        newer["presets"]["recommended"]["shortcuts"]["enabled_entry"] = [
            "prod", "shipcheck", *upgraded["presets"]["recommended"]["shortcuts"]["enabled_entry"][1:]
        ]
        overlay.write_text(json.dumps(newer))
        self.cli("apply", "--project", str(overlay), "--upgrade")
        result = json.loads(profile_path.read_text())
        self.assertIn("shipcheck", result["shortcuts"]["enabled_entry"])
        self.assertNotIn("fb", result["shortcuts"]["enabled_entry"])

    def test_reapply_switches_projects_without_resetting_preferences(self):
        """Detaching and reattaching an overlay preserves personal preferences.

        Failure means `pilot apply --no-project` or `--project` resets the
        installed profile, re-enables shortcuts the user disabled, or leaves
        retired project-only entries and template overrides behind so the next
        validation fails.
        """
        self.cli("apply", "--project", str(FIXTURES / "alter-service.json"))
        profile_path = self.gemini / "config/the-pilot/profile.json"
        link = self.gemini / "config/the-pilot/project.json"
        profile = json.loads(profile_path.read_text())
        profile["orchestration"]["worker_agents"] = ["codex", "claude"]
        profile["shortcuts"]["template_overrides"] = {
            "r": "通用 review {arg}",
            "fb": "项目反馈 {arg}",
        }
        profile_path.write_text(json.dumps(profile))

        self.cli("apply", "--no-project")
        detached = json.loads(profile_path.read_text())
        self.assertFalse(link.exists())
        self.assertEqual(detached["orchestration"]["worker_agents"], ["codex", "claude"])
        self.assertEqual(
            detached["shortcuts"]["enabled_entry"], ["r", "rt", "d", "pull"]
        )
        self.assertEqual(
            detached["shortcuts"]["template_overrides"], {"r": "通用 review {arg}"}
        )
        self.assertEqual(self.cli("status", "--no-project")["drift"], [])

        detached["shortcuts"]["enabled_entry"].remove("pull")
        profile_path.write_text(json.dumps(detached))
        self.cli("apply", "--project", str(FIXTURES / "alter-service.json"))
        reattached = json.loads(profile_path.read_text())
        self.assertEqual(
            reattached["orchestration"]["worker_agents"], ["codex", "claude"]
        )
        self.assertEqual(
            reattached["shortcuts"]["enabled_entry"], ["cq", "fb", "al", "r", "rt", "d"]
        )
        self.assertEqual(
            reattached["shortcuts"]["template_overrides"], {"r": "通用 review {arg}"}
        )
        self.assertEqual(self.cli("status")["drift"], [])

    def test_detach_keeps_overrides_shadowed_by_custom_shortcuts(self):
        """Retiring a project entry must not delete an override a custom shortcut owns.

        Failure means `pilot apply --no-project` removes a template override
        keyed like the retired entry even though a surviving custom comma
        command uses the same canonical name.
        """
        self.cli("apply", "--project", str(FIXTURES / "alter-service.json"))
        profile_path = self.gemini / "config/the-pilot/profile.json"
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["enabled_entry"].remove("fb")
        profile["shortcuts"]["custom_comma"] = {
            "fb": {"template": "自定义反馈流程 {arg}"}
        }
        profile["shortcuts"]["template_overrides"] = {"fb": "改后的反馈流程 {arg}"}
        profile_path.write_text(json.dumps(profile))

        self.cli("apply", "--no-project")
        detached = json.loads(profile_path.read_text())
        self.assertEqual(
            detached["shortcuts"]["template_overrides"], {"fb": "改后的反馈流程 {arg}"}
        )
        self.assertEqual(
            detached["shortcuts"]["custom_comma"]["fb"]["template"],
            "自定义反馈流程 {arg}",
        )
        config = json.loads((self.gemini / "config/the-pilot/config.json").read_text())
        command = next(
            item for item in config["comma_commands"].values() if ",fb" in item["aliases"]
        )
        self.assertEqual(command["template"], "改后的反馈流程 {arg}")

    def test_detach_prunes_unnormalized_retired_entry_overrides(self):
        """Hand-edited override keys resolve through the same aliases as validation.

        Failure means `pilot apply --no-project` keeps a raw `,FB ` override that
        validation resolves to the retired project entry, so clean detachment
        fails instead of dropping that project-only customization.
        """
        self.cli("apply", "--project", str(FIXTURES / "alter-service.json"))
        profile_path = self.gemini / "config/autopilot/profile.json"
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["template_overrides"] = {
            ",FB ": "手写反馈模板 {arg}",
            " r ": "通用 review {arg}",
        }
        profile_path.write_text(json.dumps(profile))

        self.cli("apply", "--no-project")
        detached = json.loads(profile_path.read_text())
        self.assertEqual(
            detached["shortcuts"]["template_overrides"], {"r": "通用 review {arg}"}
        )
        self.assertEqual(self.cli("status", "--no-project")["drift"], [])

    def test_switch_rekeys_overrides_when_entry_command_id_changes(self):
        """An override written for a shared entry key follows its new command identity.

        Failure means switching between overlays that keep an entry name but
        change its command_id rejects the installed override instead of
        re-keying it to the surviving entry.
        """
        alpha = dict(self.project, projectId="alpha")
        alpha["entry_order"] = ["deploy", *self.project["entry_order"]]
        alpha["entry_specs"] = {
            "deploy": {
                "command_id": "CMD_A_DEPLOY",
                "description": "alpha deploy",
                "requires_arg": True,
                "usage": "deploy <arg>",
                "template": "deploy alpha {arg}",
            },
            **self.project["entry_specs"],
        }
        alpha["presets"] = {
            "recommended": {
                "shortcuts": {
                    "enabled_entry": [
                        "deploy",
                        *self.project["presets"]["recommended"]["shortcuts"][
                            "enabled_entry"
                        ],
                    ]
                }
            }
        }
        beta = json.loads(json.dumps(alpha))
        beta["projectId"] = "beta"
        beta["entry_specs"]["deploy"] = {
            **alpha["entry_specs"]["deploy"],
            "command_id": "CMD_B_DEPLOY",
            "template": "deploy beta {arg}",
        }
        alpha_path = self.root / "alpha.json"
        beta_path = self.root / "beta.json"
        alpha_path.write_text(json.dumps(alpha))
        beta_path.write_text(json.dumps(beta))

        self.cli("apply", "--project", str(alpha_path))
        profile_path = self.gemini / "config/autopilot/profile.json"
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["template_overrides"] = {"CMD_A_DEPLOY": "我的部署 {arg}"}
        profile_path.write_text(json.dumps(profile))

        self.cli("apply", "--project", str(beta_path))
        migrated = json.loads(profile_path.read_text())
        self.assertEqual(
            migrated["shortcuts"]["template_overrides"], {"deploy": "我的部署 {arg}"}
        )
        config = json.loads((self.gemini / "config/autopilot/config.json").read_text())
        self.assertEqual(
            config["entry_shortcuts"]["deploy"]["template"], "我的部署 {arg}"
        )
        self.assertEqual(self.cli("status")["drift"], [])

    def test_detach_keeps_uppercase_custom_comma_override(self):
        """Uppercase custom_comma keys normalize like validation during detach.

        Failure means `pilot apply --no-project` treats a valid hand-edited
        `{"FB": ...}` custom command as absent, deletes the `fb` override as a
        retired entry, and silently loses the personal template.
        """
        self.cli("apply", "--project", str(FIXTURES / "alter-service.json"))
        profile_path = self.gemini / "config/autopilot/profile.json"
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["enabled_entry"].remove("fb")
        profile["shortcuts"]["custom_comma"] = {"FB": {"template": "custom {arg}"}}
        profile["shortcuts"]["template_overrides"] = {"fb": "personal {arg}"}
        profile_path.write_text(json.dumps(profile))

        self.cli("apply", "--no-project")
        detached = json.loads(profile_path.read_text())
        self.assertEqual(
            detached["shortcuts"]["template_overrides"], {"fb": "personal {arg}"}
        )
        self.assertEqual(
            detached["shortcuts"]["custom_comma"]["fb"]["template"], "custom {arg}"
        )
        config = json.loads((self.gemini / "config/autopilot/config.json").read_text())
        command = next(
            item for item in config["comma_commands"].values() if ",fb" in item["aliases"]
        )
        self.assertEqual(command["template"], "personal {arg}")

    def test_project_switch_follows_previous_override_ownership(self):
        """Overrides stay bound to the entry that owned their alias before the switch.

        Failure means swapped command_ids move templates to the wrong shortcut,
        or a retired entry's command_id hijacks a new entry that reuses it.
        """
        alpha = {
            "schema": "the-pilot-project/v1",
            "projectId": "alpha",
            "entry_order": ["alpha", "beta", "r", "rt", "d", "pull"],
            "entry_specs": {
                "alpha": {
                    "command_id": "CMD_1",
                    "description": "alpha",
                    "requires_arg": True,
                    "usage": "alpha <arg>",
                    "template": "alpha {arg}",
                },
                "beta": {
                    "command_id": "CMD_2",
                    "description": "beta",
                    "requires_arg": True,
                    "usage": "beta <arg>",
                    "template": "beta {arg}",
                },
            },
            "presets": {
                "recommended": {
                    "shortcuts": {
                        "enabled_entry": ["alpha", "beta", "r", "rt", "d", "pull"]
                    }
                }
            },
        }
        swapped = json.loads(json.dumps(alpha))
        swapped["projectId"] = "swapped"
        swapped["entry_specs"]["alpha"]["command_id"] = "CMD_2"
        swapped["entry_specs"]["beta"]["command_id"] = "CMD_1"
        retired = json.loads(json.dumps(alpha))
        retired["projectId"] = "retired"
        retired["entry_order"] = ["gamma", "beta", "r", "rt", "d", "pull"]
        retired["entry_specs"] = {
            "gamma": {**retired["entry_specs"]["alpha"], "template": "gamma {arg}"},
            "beta": retired["entry_specs"]["beta"],
        }
        retired["presets"]["recommended"]["shortcuts"]["enabled_entry"] = [
            "gamma",
            "beta",
            "r",
            "rt",
            "d",
            "pull",
        ]
        paths = {}
        for name, overlay in (("alpha", alpha), ("swapped", swapped), ("retired", retired)):
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(overlay))
            paths[name] = path
        profile_path = self.gemini / "config/autopilot/profile.json"

        self.cli("apply", "--project", str(paths["alpha"]))
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["template_overrides"] = {
            "CMD_1": "alpha tpl",
            "CMD_2": "beta tpl",
        }
        profile_path.write_text(json.dumps(profile))

        self.cli("apply", "--project", str(paths["swapped"]))
        self.assertEqual(
            json.loads(profile_path.read_text())["shortcuts"]["template_overrides"],
            {"alpha": "alpha tpl", "beta": "beta tpl"},
        )
        config = json.loads((self.gemini / "config/autopilot/config.json").read_text())
        self.assertEqual(config["entry_shortcuts"]["alpha"]["template"], "alpha tpl")
        self.assertEqual(config["entry_shortcuts"]["beta"]["template"], "beta tpl")

        self.cli("apply", "--project", str(paths["alpha"]))
        profile = json.loads(profile_path.read_text())
        profile["shortcuts"]["template_overrides"] = {"CMD_1": "alpha tpl"}
        profile_path.write_text(json.dumps(profile))
        self.cli("apply", "--project", str(paths["retired"]))
        self.assertEqual(
            json.loads(profile_path.read_text())["shortcuts"]["template_overrides"], {}
        )
        config = json.loads((self.gemini / "config/autopilot/config.json").read_text())
        self.assertEqual(config["entry_shortcuts"]["gamma"]["template"], "gamma {arg}")
        self.assertEqual(self.cli("status")["drift"], [])

    def test_discovery_persistence_explicit_override_and_uninstall(self):
        repo = self.root / "repo"
        (repo / ".pilot").mkdir(parents=True)
        (repo / ".pilot/project.json").write_text(json.dumps(self.project))
        child = repo / "deep/child"
        child.mkdir(parents=True)
        discovered = self.cli("probe", cwd=child)
        self.assertIn("cq", discovered["recommended_profile"]["shortcuts"]["enabled_entry"])
        self.assertFalse(self.gemini.exists())
        self.cli("apply", cwd=child)
        link = self.gemini / "config/the-pilot/project.json"
        self.assertEqual(json.loads(link.read_text()), self.project)
        # An installed link wins over another cwd's project.
        other = self.root / "other"
        (other / ".pilot").mkdir(parents=True)
        (other / ".pilot/project.json").write_text('{"schema":"wrong"}')
        self.assertEqual(self.cli("status", cwd=other)["drift"], [])
        self.cli("apply", "--upgrade", cwd=other)
        self.assertEqual(self.cli("status")["drift"], [])
        self.cli("apply", "--no-project", cwd=other)
        self.assertFalse(link.exists())
        self.assertEqual(self.cli("status", "--no-project")["drift"], [])
        self.cli("apply", "--project", str(repo))
        self.cli("apply", "--project", str(repo / ".pilot/project.json"), "--upgrade")
        self.assertEqual(self.cli("status")["drift"], [])
        self.cli("uninstall")
        self.assertFalse(link.exists())

    def test_invalid_overlay_has_no_install_effect(self):
        for mutation in ({"scene_specs": {"closeout-and-sync": {"file": "../../escape.md"}}},
                         {"entry_order": ["r", "r"]}, {"unknown": True},
                         {"entry_specs": {"r": {"requires_arg": "yes"}}},
                         {"presets": {"recommended": None}}):
            with self.subTest(mutation=mutation):
                bad = dict(self.project, **mutation)
                with self.assertRaises(ProfileError):
                    normalize_project(bad)
        path = self.root / "bad.json"
        path.write_text('{"schema":"wrong"}')
        self.cli("apply", "--project", str(path), ok=False)
        self.assertFalse((self.gemini / "config/the-pilot/profile.json").exists())
        self.cli("probe", "--project", str(self.root / "absent"), ok=False)

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
        self.assertEqual(contracts["plan-and-orchestrate"]["order"], 7)
        self.assertEqual(contracts["plan-and-orchestrate"]["title"], "自定义方案阶段")
        self.assertEqual(
            contracts["closeout-and-sync"]["title"],
            DEFAULT_SPECS["scene_specs"]["closeout-and-sync"]["title"],
        )
        installed = assessment["installed_pipeline"]
        self.assertEqual(installed["profile_status"], "ok")
        self.assertEqual(installed["project_status"], "ok")
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
        self.assertEqual(bad_installed["project_status"], "error")
        self.assertEqual(bad_installed["profile_status"], "error")
        self.assertEqual(bad_installed["custom_scene_files"], [])
        self.assertEqual(bad_installed["incomplete_scene_contracts"], [])
        self.assertFalse(bad_project["evidence_complete"])

        (pilot_dir / "profile.json").unlink()
        missing_profile = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )
        missing_installed = missing_profile["pipeline_assessment"]["installed_pipeline"]
        self.assertEqual(missing_installed["project_status"], "error")
        self.assertEqual(missing_installed["profile_status"], "absent")
        self.assertTrue(missing_installed["error"])
        self.assertFalse(missing_profile["evidence_complete"])

        (pilot_dir / "project.json").unlink()
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


