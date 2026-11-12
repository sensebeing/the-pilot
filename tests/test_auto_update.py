"""Focused contracts for the zero-touch update paths."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import subprocess
import sys
import time

from the_pilot import installer
from the_pilot.assets import runtime


class AutoUpdateTest(unittest.TestCase):
    def test_project_only_skips_older_clean_worktree_but_updates_dirty_or_newer(self):
        """Overlay sync compares normalized committed content and protects newer installs."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = root / "repo"
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
                subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
            fixture = json.loads((Path(__file__).parent / "fixtures/alter-service.json").read_text())
            fixture["projectId"] = "overlay-test"
            pilot_dir = repo / ".pilot"
            pilot_dir.mkdir()
            (pilot_dir / "project.json").write_text("{invalid json\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "ancient-invalid"], check=True, capture_output=True)
            (pilot_dir / "project.json").write_text(json.dumps(fixture))
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "v1"], check=True, capture_output=True)
            older = root / "older-wt"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "old-topic", str(older), "HEAD"], check=True, capture_output=True)
            v2 = dict(fixture, description="v2")
            (pilot_dir / "project.json").write_text(json.dumps(v2, indent=2) + "\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "v2"], check=True, capture_output=True)
            gemini = root / "gemini"
            installer.apply(installer.build_parser().parse_args([
                "apply", "--gemini-dir", str(gemini), "--project", str(repo), "--json",
            ]))
            args = installer.build_parser().parse_args([
                "self-update", "--project-only", "--workspace", str(older),
                "--gemini-dir", str(gemini), "--json",
            ])
            self.assertEqual(installer.self_update(args)["project_status"], "skipped_older_worktree")
            installed = json.loads((gemini / "config/the-pilot/project.json").read_text())
            self.assertEqual(installed, installer.normalize_project(v2) if hasattr(installer, "normalize_project") else v2)

            dirty = dict(fixture, description="dirty-v3")
            (older / ".pilot/project.json").write_text(json.dumps(dirty))
            self.assertEqual(installer.self_update(args)["project_status"], "updated")
            self.assertEqual(json.loads((gemini / "config/the-pilot/project.json").read_text())["description"], "dirty-v3")

            newer = root / "newer-wt"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", str(newer), "-b", "new-topic"], check=True, capture_output=True)
            newest = dict(v2, description="newer-v3")
            (newer / ".pilot/project.json").write_text(json.dumps(newest, indent=2) + "\n")
            subprocess.run(["git", "-C", str(newer), "add", "."], check=True)
            subprocess.run(["git", "-C", str(newer), "commit", "-m", "newer"], check=True, capture_output=True)
            newer_args = installer.build_parser().parse_args([
                "self-update", "--project-only", "--workspace", str(newer),
                "--gemini-dir", str(gemini), "--json",
            ])
            self.assertEqual(installer.self_update(newer_args)["project_status"], "updated")

    def test_single_overlay_commit_does_not_block_clean_sync(self):
        """A clean one-commit overlay remains synchronizable when installed data differs."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = root / "repo"
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
                subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
            project = json.loads((Path(__file__).parent / "fixtures/alter-service.json").read_text())
            project["projectId"] = "single-commit"
            (repo / ".pilot").mkdir()
            (repo / ".pilot/project.json").write_text(json.dumps(project, separators=(",", ":")))
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "overlay"], check=True, capture_output=True)
            gemini = root / "gemini"
            installer.apply(installer.build_parser().parse_args([
                "apply", "--gemini-dir", str(gemini), "--project", str(repo), "--json",
            ]))
            installed = gemini / "config/the-pilot/project.json"
            altered = dict(project, description="manually installed")
            installed.write_text(json.dumps(altered, indent=2) + "\n")
            args = installer.build_parser().parse_args([
                "self-update", "--project-only", "--workspace", str(repo),
                "--gemini-dir", str(gemini), "--json",
            ])
            self.assertEqual(installer.self_update(args)["project_status"], "updated")
            self.assertEqual(json.loads(installed.read_text())["description"], project["description"])
    def test_workspace_overlay_sync_is_invoked_before_runtime_load(self):
        """Matching project IDs sync the workspace overlay on the same turn."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            overlay = root / ".pilot/project.json"
            overlay.parent.mkdir()
            overlay.write_text(json.dumps({"projectId": "p", "shortcuts": {"x": "new"}}))
            old = root / "project.json"
            old.write_text(json.dumps({"projectId": "p", "shortcuts": {"x": "old"}}))
            with mock.patch.object(runtime, "PILOT_DIR", root), mock.patch.object(runtime, "GEMINI_DIR", root), \
                 mock.patch.object(runtime, "STATE_DIR", root / "state"), mock.patch.object(runtime, "LOG_PATH", root / "state/log"), \
                 mock.patch.object(runtime, "maybe_trigger_auto_update"), mock.patch.object(runtime, "subprocess") as proc:
                proc.run.return_value = mock.Mock(returncode=0, stdout='{"project_status":"updated"}')
                runtime.maybe_sync_project_overlay({"workspacePaths": [str(root)]})
                proc.run.assert_called_once()
                self.assertIn("project_auto_synced", (root / "state/log").read_text())

    def test_corrupt_overlay_is_logged_without_writing(self):
        """Corrupt workspace JSON fails closed in the installer and logs failure in the runtime."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            gemini = root / "gemini"
            pilot_dir = gemini / "config/the-pilot"
            pilot_dir.mkdir(parents=True)
            source = json.loads((Path(__file__).parent / "fixtures/alter-service.json").read_text())
            (pilot_dir / "project.json").write_text(json.dumps(source))
            (root / ".pilot").mkdir()
            (root / ".pilot/project.json").write_text("{")
            with mock.patch.object(runtime, "GEMINI_DIR", gemini), mock.patch.object(runtime, "PILOT_DIR", pilot_dir), mock.patch.object(runtime, "STATE_DIR", root / "state"), mock.patch.object(runtime, "LOG_PATH", root / "state/log"), mock.patch.dict(os.environ, {"THE_PILOT_BIN": str(Path(__file__).parents[1] / "bin/pilot")}):
                runtime.maybe_sync_project_overlay({"workspacePaths": [str(root)]})
                self.assertEqual(json.loads((pilot_dir / "project.json").read_text())["projectId"], source["projectId"])
                self.assertIn("project_auto_sync_failed", (root / "state/log").read_text())

    def test_cooldown_suppresses_background_spawn(self):
        """The six hour TTL prevents duplicate background self-update processes."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"
            state.mkdir()
            (state / "auto_update.json").write_text(json.dumps({"last_checked_at": runtime.time.time()}))
            with mock.patch.object(runtime, "STATE_DIR", state), mock.patch.dict(os.environ, {"THE_PILOT_BIN": "/bin/true", "THE_PILOT_GEMINI_DIR": "", "THE_PILOT_DISABLE_AUTO_UPDATE": ""}), mock.patch.object(runtime.subprocess, "Popen") as popen:
                runtime.maybe_trigger_auto_update()
                popen.assert_not_called()

    def test_parser_exposes_self_update_contract(self):
        """The CLI exposes checkout and home overrides for isolated update tests."""
        args = installer.build_parser().parse_args(["self-update", "--checkout-dir", "/tmp/x", "--home-dir", "/tmp/h", "--json"])
        self.assertEqual(args.checkout_dir, "/tmp/x")
        self.assertEqual(args.home_dir, "/tmp/h")
        self.assertFalse(args.project_only)

    def test_status_surfaces_persisted_update(self):
        """Status reports persisted auto-update metadata without mutating it."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            gemini = root / "gemini"
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text('{"status":"up_to_date"}')
            args = installer.build_parser().parse_args(["status", "--gemini-dir", str(gemini), "--no-project", "--json"])
            report = installer.status(args)
            self.assertEqual(report["auto_update"]["status"], "up_to_date")

    def test_self_update_status_distinguishes_same_head(self):
        """A clean main checkout with no incoming commit is up to date."""
        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            (checkout / "README").write_text("x")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "init"], check=True, capture_output=True)
            upstream = Path(root) / "upstream.git"
            subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(upstream)], check=True)
            subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)
            gemini = Path(root) / "gemini"
            args = installer.build_parser().parse_args([
                "self-update", "--checkout-dir", str(checkout), "--gemini-dir", str(gemini),
                "--no-project", "--json",
            ])
            report = installer.self_update(args)
            self.assertEqual(report["source_status"], "up_to_date")
            log_record = (gemini / "config/the-pilot/state/pilot.log").read_text().splitlines()[-1]
            self.assertIsInstance(json.loads(log_record)["ts"], str)

    def test_self_update_skips_dirty_and_non_main_checkouts(self):
        """Dirty and non-main checkouts are reported as skipped without pulling."""
        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            (checkout / "README").write_text("x")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "init"], check=True, capture_output=True)
            (checkout / "README").write_text("dirty")
            args = installer.build_parser().parse_args([
                "self-update", "--checkout-dir", str(checkout), "--gemini-dir", str(Path(root) / "g1"),
                "--no-project", "--json",
            ])
            self.assertEqual(installer.self_update(args)["source_status"], "skipped_dirty_checkout")
            subprocess.run(["git", "-C", str(checkout), "restore", "README"], check=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "topic"], check=True, capture_output=True)
            args.gemini_dir = str(Path(root) / "g2")
            self.assertEqual(installer.self_update(args)["source_status"], "skipped_non_main_branch")

    def test_self_update_human_output(self):
        """Human self-update output succeeds when the checkout has a bare origin."""
        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            (checkout / "README").write_text("x")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "init"], check=True, capture_output=True)
            upstream = Path(root) / "upstream.git"
            subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(upstream)], check=True)
            subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)
            result = installer.main([
                "self-update", "--checkout-dir", str(checkout), "--gemini-dir", str(Path(root) / "gemini"),
                "--no-project",
            ])
            self.assertEqual(result, 0)

    def test_invalid_explicit_checkout_fails_closed(self):
        """An explicit non-Git checkout is failed and persisted as failed metadata."""
        with tempfile.TemporaryDirectory() as root:
            gemini = Path(root) / "gemini"
            args = installer.build_parser().parse_args(["self-update", "--checkout-dir", str(Path(root) / "missing"), "--gemini-dir", str(gemini), "--no-project", "--json"])
            with self.assertRaises(installer.SetupError):
                installer.self_update(args)
            self.assertEqual(json.loads((gemini / "config/the-pilot/state/auto_update.json").read_text())["status"], "failed")

    def test_no_source_is_reported_explicitly(self):
        """Without checkout or uv, source and overall status remain skipped_no_source."""
        with tempfile.TemporaryDirectory() as root, mock.patch.object(installer, "_discover_checkout", return_value=(None, False)), mock.patch.object(installer.shutil, "which", return_value=None):
            args = installer.build_parser().parse_args(["self-update", "--gemini-dir", str(Path(root) / "gemini"), "--no-project", "--json"])
            report = installer.self_update(args)
            self.assertEqual(report["source_status"], "skipped_no_source")
            self.assertEqual(report["status"], "skipped_no_source")

    def test_spawn_failure_does_not_advance_cooldown(self):
        """A failed Popen records failure and leaves last_checked_at available for retry."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"
            old = time.time() - runtime.AUTO_UPDATE_INTERVAL_SECONDS - 1
            state.mkdir()
            (state / "auto_update.json").write_text(json.dumps({"last_checked_at": old}))
            with mock.patch.object(runtime, "STATE_DIR", state), mock.patch.object(runtime, "LOG_PATH", state / "pilot.log"), mock.patch.dict(os.environ, {"THE_PILOT_BIN": sys.executable, "THE_PILOT_GEMINI_DIR": "", "THE_PILOT_ENABLE_AUTO_UPDATE": "1", "THE_PILOT_DISABLE_AUTO_UPDATE": ""}), mock.patch.object(runtime.subprocess, "Popen", side_effect=OSError("spawn")):
                runtime.maybe_trigger_auto_update()
                record = json.loads((state / "auto_update.json").read_text())
                self.assertEqual(record["last_checked_at"], old)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"], "spawn")
                runtime.maybe_trigger_auto_update()
            self.assertIn("auto_update_spawn_failed", (state / "pilot.log").read_text())

    def test_overlay_sync_uses_real_installer_and_v2_template(self):
        """Workspace overlay changes are compiled through the installer runtime."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            gemini = root_path / "gemini"
            workspace = root_path / "workspace"
            source = json.loads((Path(__file__).parent / "fixtures/alter-service.json").read_text())
            source["projectId"] = "p"
            overlay = workspace / ".pilot/project.json"
            overlay.parent.mkdir(parents=True)
            overlay.write_text(json.dumps(source))
            args = installer.build_parser().parse_args([
                "apply", "--gemini-dir", str(gemini), "--project", str(overlay), "--max-turns", "9", "--json",
            ])
            installer.apply(args)
            source["entry_specs"]["cq"]["template"] = "V2 {arg}"
            overlay.write_text(json.dumps(source))
            transcript = root_path / "transcript.jsonl"
            transcript.write_text(json.dumps({"type": "USER_INPUT", "content": "cq hello"}) + "\n")
            state_dir = gemini / "config/the-pilot/state"
            with mock.patch.dict(os.environ, {
                "THE_PILOT_BIN": str(Path(__file__).parents[1] / "bin/pilot"),
                "THE_PILOT_GEMINI_DIR": str(gemini),
                "THE_PILOT_STATE_DIR": str(state_dir),
                "THE_PILOT_DISABLE_AUTO_UPDATE": "1",
            }), mock.patch.object(runtime, "GEMINI_DIR", gemini), mock.patch.object(runtime, "PILOT_DIR", gemini / "config/the-pilot"), mock.patch.object(runtime, "PROFILE_PATH", gemini / "config/the-pilot/profile.json"), mock.patch.object(runtime, "CONFIG_PATH", gemini / "config/the-pilot/config.json"), mock.patch.object(runtime, "STATE_DIR", state_dir), mock.patch.object(runtime, "LOG_PATH", state_dir / "pilot.log"):
                result = runtime.handle_pre_invocation({
                    "conversationId": "c1", "lastUserInput": "cq hello", "workspacePaths": [str(workspace)],
                    "transcriptPath": str(transcript),
                })
            self.assertIn("V2 hello", json.dumps(result, ensure_ascii=False))
            self.assertEqual(json.loads((gemini / "config/the-pilot/profile.json").read_text())["pilot"]["max_consecutive_auto_turns"], 9)
            self.assertEqual(json.loads((gemini / "config/the-pilot/project.json").read_text())["entry_specs"]["cq"]["template"], "V2 {arg}")
            log_text = (state_dir / "pilot.log").read_text()
            self.assertIn("project_auto_synced", log_text)
            self.assertNotIn("project_auto_sync_failed", log_text)

    def test_self_update_pulls_new_commit_syncs_skills_and_preserves_profile(self):
        """A clean main checkout pulls upstream commits, syncs skill symlinks, and preserves profile.json."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            upstream = root_path / "upstream.git"
            subprocess.run(["git", "init", "--bare", "-b", "main", str(upstream)], check=True, capture_output=True)
            producer = root_path / "producer"
            subprocess.run(["git", "init", "-b", "main", str(producer)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(producer), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(producer), "config", "user.name", "Test"], check=True)
            for name in ("the-pilot", "the-pilot-setup"):
                skill_dir = producer / "skills" / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(f"# {name}\n")
            subprocess.run(["git", "-C", str(producer), "add", "."], check=True)
            subprocess.run(["git", "-C", str(producer), "commit", "-m", "v1"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(producer), "remote", "add", "origin", str(upstream)], check=True)
            subprocess.run(["git", "-C", str(producer), "push", "-u", "origin", "main"], check=True, capture_output=True)

            checkout = root_path / "checkout"
            subprocess.run(["git", "clone", str(upstream), str(checkout)], check=True, capture_output=True)
            (producer / "skills/the-pilot/SKILL.md").write_text("# the-pilot v2\n")
            subprocess.run(["git", "-C", str(producer), "add", "."], check=True)
            subprocess.run(["git", "-C", str(producer), "commit", "-m", "v2"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(producer), "push", "origin", "main"], check=True, capture_output=True)
            expected_sha = subprocess.run(
                ["git", "-C", str(producer), "rev-parse", "--short", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()

            gemini = root_path / "gemini"
            home = root_path / "home"
            (home / ".agents/skills").mkdir(parents=True)
            unmanaged = home / ".claude/skills/the-pilot"
            unmanaged.mkdir(parents=True)
            (unmanaged / "custom.txt").write_text("keep")

            apply_args = installer.build_parser().parse_args([
                "apply", "--gemini-dir", str(gemini), "--no-project", "--max-turns", "11", "--json",
            ])
            installer.apply(apply_args)

            update_args = installer.build_parser().parse_args([
                "self-update", "--checkout-dir", str(checkout), "--home-dir", str(home),
                "--gemini-dir", str(gemini), "--no-project", "--json",
            ])
            report = installer.self_update(update_args)
            self.assertEqual(report["status"], "updated")
            self.assertEqual(report["source_status"], "updated")
            self.assertEqual(report["head_sha"], expected_sha)
            self.assertTrue((home / ".agents/skills/the-pilot").is_symlink())
            self.assertFalse(unmanaged.is_symlink())
            self.assertEqual((unmanaged / "custom.txt").read_text(), "keep")
            self.assertEqual(
                json.loads((gemini / "config/the-pilot/profile.json").read_text())["pilot"]["max_consecutive_auto_turns"],
                11,
            )

    def test_self_update_reinstalls_uv_tool_when_head_already_pulled(self):
        """A pulled-ahead checkout refreshes the uv bundle even when pull changes no commit."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            home = root / "home"
            checkout = home / "the-pilot"
            checkout.mkdir(parents=True)
            shutil.copytree(Path(__file__).parents[1] / "src", checkout / "src")
            shutil.copytree(Path(__file__).parents[1] / "bin", checkout / "bin")
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "source"], check=True, capture_output=True)
            bare = root / "origin.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(bare)], check=True)
            subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/pilot").write_text("#!/bin/sh\n")
            (home / ".local/bin/pilot").chmod(0o755)
            uv_log = root / "uv.log"
            uv = root / "uv"
            uv.write_text("#!/bin/sh\necho \"$@\" >> \"$UV_LOG\"\nexit 0\n")
            uv.chmod(0o755)
            gemini = root / "gemini"
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            sha = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            (state / "auto_update.json").write_text(json.dumps({"installed_sha": "old", "installed_tree_digest": "old"}))
            env = {"HOME": str(root / "process-home"), "PATH": f"{root}:{os.environ['PATH']}", "UV_LOG": str(uv_log)}
            with mock.patch.dict(os.environ, env, clear=False):
                args = installer.build_parser().parse_args(["self-update", "--gemini-dir", str(gemini), "--home-dir", str(home), "--no-project", "--json"])
                installer.self_update(args)
                report = json.loads((state / "auto_update.json").read_text())
                self.assertEqual(report["installed_sha"], sha)
                self.assertIn("tool install --force", uv_log.read_text())
                uv_log.write_text("")
                installer.self_update(args)
                self.assertEqual(uv_log.read_text(), "")
                (state / "auto_update.json").write_text(json.dumps({"last_checked_at": 123456.0, "installed_sha": "old", "installed_tree_digest": "old"}))
                local_args = installer.build_parser().parse_args(["self-update", "--local-only", "--gemini-dir", str(gemini), "--home-dir", str(home), "--no-project", "--json"])
                installer.self_update(local_args)
                local_report = json.loads((state / "auto_update.json").read_text())
                self.assertEqual(local_report["last_checked_at"], 123456.0)
                self.assertEqual(local_report["installed_sha"], sha)

    def test_project_only_zero_cooldown_syncs_clean_main_checkout_and_skips_dirty_or_branch(self):
        """Project-only sync updates clean main immediately and ignores dirty or feature branches."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            home, gemini = root / "home", root / "gemini"
            checkout = home / "the-pilot"
            shutil.copytree(Path(__file__).parents[1], checkout, ignore=shutil.ignore_patterns(".git", ".venv"))
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "source"], check=True, capture_output=True)
            installer.apply(installer.build_parser().parse_args(["apply", "--gemini-dir", str(gemini), "--no-project", "--json"]))
            engine = checkout / "src/the_pilot/assets/runtime.py"
            engine.write_text(engine.read_text() + "\n# local upgrade marker\n")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "upgrade"], check=True, capture_output=True)
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            metadata = {"last_checked_at": time.time()}
            (state / "auto_update.json").write_text(json.dumps(metadata))
            args = installer.build_parser().parse_args(["self-update", "--project-only", "--gemini-dir", str(gemini), "--home-dir", str(home), "--json"])
            with mock.patch.object(installer.shutil, "which", return_value=None):
                unmanaged_report = installer.self_update(args)
            self.assertIsNone(unmanaged_report["error"])
            self.assertEqual(unmanaged_report["cli_status"], "skipped_unmanaged")
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/pilot").write_text("#!/bin/sh\n")
            (home / ".local/bin/pilot").chmod(0o755)
            with mock.patch.object(installer.shutil, "which", return_value="/fake/uv"):
                report = installer.self_update(args)
            self.assertEqual(report["local_source_status"], "none")
            self.assertIn("local upgrade marker", (gemini / "config/the-pilot/runtime.py").read_text())
            persisted = json.loads((state / "auto_update.json").read_text())
            self.assertIn("runtime_tree_digest", persisted)
            self.assertNotIn("installed_tree_digest", persisted)
            engine.write_text(engine.read_text() + "\n# dirty\n")
            before = (gemini / "config/the-pilot/runtime.py").read_bytes()
            self.assertEqual(installer.self_update(args)["local_source_status"], "none")
            self.assertEqual(before, (gemini / "config/the-pilot/runtime.py").read_bytes())
            subprocess.run(["git", "-C", str(checkout), "restore", str(engine.relative_to(checkout))], check=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "feature"], check=True, capture_output=True)
            self.assertEqual(installer.self_update(args)["local_source_status"], "none")

    def test_pilot_pre_invocation_reexecs_updated_engine_on_same_turn(self):
        """A changed on-disk engine is delegated once so the current turn uses new behavior."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            gemini = root / "gemini"
            pilot_dir = gemini / "config/the-pilot"
            pilot_dir.mkdir(parents=True)
            source = Path(__file__).parents[1] / "src/the_pilot/assets/runtime.py"
            engine = pilot_dir / "runtime.py"
            engine.write_bytes(source.read_bytes())
            installer.apply(installer.build_parser().parse_args(["apply", "--gemini-dir", str(gemini), "--no-project", "--json"]))
            shim = root / "pilot-shim.py"
            shim.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "if sys.argv[1:3] == ['self-update', '--project-only']:\n"
                "    d = pathlib.Path(os.environ['THE_PILOT_GEMINI_DIR']) / 'config/the-pilot'\n"
                "    (d / 'runtime.py').write_text((d / 'runtime.py').read_text() + '\\n# upgraded\\n')\n"
                "    config = json.loads((d / 'config.json').read_text())\n"
                "    config['entry_shortcuts']['ping'] = {'template': 'upgraded shortcut body', 'requires_arg': False}\n"
                "    (d / 'config.json').write_text(json.dumps(config))\n"
                "    print(json.dumps({'local_source_status': 'updated'}))\n"
                "else:\n"
                "    print(json.dumps({}))\n"
            )
            shim.chmod(0o755)
            payload = {"conversationId": "c", "lastUserInput": "ping", "workspacePaths": []}
            env = {
                **os.environ,
                "HOME": str(root / "home"),
                "THE_PILOT_GEMINI_DIR": str(gemini),
                "THE_PILOT_BIN": str(shim),
                "THE_PILOT_DISABLE_AUTO_UPDATE": "1",
            }
            result = subprocess.run(
                [sys.executable, str(engine), "hook", "--event", "PreInvocation"],
                input=json.dumps(payload), text=True, capture_output=True, env=env, check=True,
            )
            receipt = json.loads(result.stdout)
            self.assertIn("upgraded shortcut body", json.dumps(receipt, ensure_ascii=False))

