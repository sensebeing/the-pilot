"""Project compilation and installed overlay lifecycle contracts.

resident-fact: settlement: installed overlays survive cwd changes and upgrades;
explicit universal selection and uninstall remove only the owned project link.
resident-fact: admission: invalid overlays cannot escape scene paths or write an
installation; project compilation matches the reviewed structured-tag contract.
"""
import copy
from datetime import datetime, timezone
import hashlib
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


def artifact_hashes(bundle):
    values = {
        "profile_json": bundle.profile_json, "config_json": bundle.config_json,
        "scenes": bundle.scenes, "gemini_block": bundle.gemini_block,
        "hooks": merge_hooks_json({"other": {"hooks": []}}, "/pilot-contract/config/autopilot/autopilot.py"),
    }
    return {
        key: hashlib.sha256((value if isinstance(value, str) else
                            json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest()
        for key, value in values.items()
    }


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gemini = self.root / "gemini"
        self.base = json.loads((ROOT / "src/the_pilot/assets/profile.default.json").read_text())
        self.project = json.loads((FIXTURES / "alter-service.json").read_text())
        self.env = dict(os.environ, HOME=str(self.root / "home"),
                        ORCA_AUTOPILOT_GEMINI_DIR=str(self.gemini))

    def cli(self, *args, cwd=None, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "bin/pilot"), *args,
                                 "--gemini-dir", str(self.gemini), "--json"],
                                cwd=cwd or self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return json.loads(result.stdout)

    def test_compiled_contract_and_parallel_project_isolation(self):
        baseline = json.loads((FIXTURES / "legacy-compilation.json").read_text())
        self.assertEqual(hashlib.sha256((ROOT / "src/the_pilot/assets/autopilot.py").read_bytes()).hexdigest(),
                         baseline["runtime_sha256"])
        for case in baseline["cases"]:
            with self.subTest(preset=case["preset"], overrides=case["overrides"]):
                profile = resolve_profile(self.base, preset=case["preset"],
                                          overrides=case["overrides"], project=self.project)
                bundle = compile_bundle(profile, project=self.project)
                self.assertEqual(artifact_hashes(bundle), case["sha256"])
        original = copy.deepcopy(self.project)
        def compile_for(project):
            profile = resolve_profile(self.base, project=project)
            return compile_bundle(profile, project=project).config_json["entry_shortcuts"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            generic, linked = list(pool.map(compile_for, (None, self.project)))
        self.assertEqual(list(generic), ["r", "rt", "d", "pull"])
        self.assertEqual(list(linked), self.project["entry_order"])
        self.assertEqual(self.project, original)

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
        installed = self.gemini / "config/autopilot/autopilot.py"
        self.assertEqual(installed.read_bytes(), (ROOT / "src/the_pilot/assets/autopilot.py").read_bytes())
        for args in (("worker", "status", "--json"), ("mode", "status"), ("sync",), ("log",),
                     ("set", "r", "Review {arg}"), ("hook", "--event", "PreInvocation")):
            result = subprocess.run([sys.executable, str(ROOT / "bin/pilot"), *args],
                                    env=self.env, cwd=self.root, input='{}', text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.cli("uninstall")
        self.assertFalse(self.cli("status")["installed"])

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
        link = self.gemini / "config/autopilot/project.json"
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
        self.assertFalse((self.gemini / "config/autopilot/profile.json").exists())
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

        # 1. Antigravity: pre-window opener + in-window followups + assistant autopilot tag provenance
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
                "content": 'Done.\n<!-- autopilot: {"scene": "architecture-re-review", "gate": "auto", "1": "Audit whole task architecture"} -->',
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
                "content": 'Stale suggestion.\n<!-- autopilot: {"scene": "closeout-and-sync", "gate": "auto", "1": "Stale closeout prompt"} -->',
            }),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "created_at": recent_iso,
                "content": "Superseded by final response.\n<!-- autopilot: scene=NONE -->",
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
        self.assertEqual(report["autopilot_activity"].get("candidate:,1"), 1)
        self.assertEqual(report["autopilot_activity"].get("scene:architecture-re-review"), 1)
        self.assertIsNone(report["autopilot_activity"].get("scene:closeout-and-sync"))

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
        cfg_path = self.gemini / "config/autopilot/config.json"
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
        self.assertEqual(with_shortcuts["autopilot_activity"].get("entry:ti"), 2)
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

        autopilot_dir = self.gemini / "config/autopilot"
        scenes_dir = autopilot_dir / "scenes"
        scenes_dir.mkdir(parents=True)
        profile = copy.deepcopy(self.base)
        profile["scenes"]["enabled_scenes"] = ["plan-and-orchestrate", "deliver-and-verify"]
        profile["scenes"]["deliver_strategy"] = "merge_dev_only"
        (autopilot_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
        (scenes_dir / "01-plan-and-orchestrate.md").write_text(
            "---\n"
            "description: Plan the task\n"
            f"required_guardrail: {DEFAULT_SPECS['scene_specs']['plan-and-orchestrate']['required_guardrail']}\n"
            "fallback_prompt: Draft the plan\n"
            "---\n"
            "Plan body.\n",
            encoding="utf-8",
        )
        (scenes_dir / "custom-extra.md").write_text(
            "---\n"
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
        self.assertEqual(assessment["stage_contracts"], PIPELINE_STAGE_CONTRACTS)
        self.assertEqual(list(assessment["stage_contracts"]), list(SCENE_ORDER))
        for stage, contract in assessment["stage_contracts"].items():
            self.assertTrue(contract["deliverable"], stage)
            self.assertTrue(contract["quality_bar"], stage)
        installed = assessment["installed_pipeline"]
        self.assertEqual(installed["profile_status"], "ok")
        self.assertEqual(installed["deliver_strategy"], "merge_dev_only")
        self.assertEqual(installed["missing_stages"], ["architecture-re-review", "closeout-and-sync"])
        self.assertEqual(installed["custom_scene_files"], ["custom-extra.md"])
        self.assertEqual(installed["incomplete_scene_contracts"], ["03-deliver-and-verify.md"])
        findings = {finding["code"]: finding for finding in assessment["findings"]}
        self.assertEqual(
            set(findings),
            {
                "missing_pipeline_stages",
                "over_engineered_pipeline",
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

        (autopilot_dir / "project.json").write_text('{"schema": "wrong"}', encoding="utf-8")
        bad_project = distill_chat_history(
            home, self.gemini, days=7, max_sessions_per_agent=10, now_ts=now
        )
        self.assertEqual(
            bad_project["pipeline_assessment"]["installed_pipeline"]["profile_status"], "error"
        )
        (autopilot_dir / "project.json").unlink()
        (autopilot_dir / "profile.json").write_text("{corrupt", encoding="utf-8")
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


