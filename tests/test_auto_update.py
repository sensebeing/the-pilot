"""Focused contracts for the zero-touch update paths."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import subprocess
import time

from the_pilot import installer
from the_pilot.assets import autopilot


class AutoUpdateTest(unittest.TestCase):
    def test_workspace_overlay_sync_is_invoked_before_runtime_load(self):
        """Matching project IDs sync the workspace overlay on the same turn."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            overlay = root / ".pilot/project.json"
            overlay.parent.mkdir()
            overlay.write_text(json.dumps({"projectId": "p", "shortcuts": {"x": "new"}}))
            old = root / "project.json"
            old.write_text(json.dumps({"projectId": "p", "shortcuts": {"x": "old"}}))
            with mock.patch.object(autopilot, "AUTOPILOT_DIR", root), mock.patch.object(autopilot, "GEMINI_DIR", root), \
                 mock.patch.object(autopilot, "maybe_trigger_auto_update"), mock.patch.object(autopilot, "subprocess") as proc:
                proc.run.return_value = mock.Mock(returncode=0)
                autopilot.maybe_sync_project_overlay({"workspacePaths": [str(root)]})
                proc.run.assert_called_once()

    def test_corrupt_overlay_is_logged_without_writing(self):
        """Corrupt workspace JSON logs failure and leaves installed state untouched."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / ".pilot").mkdir()
            (root / ".pilot/project.json").write_text("{")
            (root / "project.json").write_text('{"projectId":"p"}')
            with mock.patch.object(autopilot, "AUTOPILOT_DIR", root), mock.patch.object(autopilot, "STATE_DIR", root / "state"), mock.patch.object(autopilot, "LOG_PATH", root / "state/log"):
                autopilot.maybe_sync_project_overlay({"workspacePaths": [str(root)]})
                self.assertIn("project_auto_sync_failed", (root / "state/log").read_text())

    def test_cooldown_suppresses_background_spawn(self):
        """The six hour TTL prevents duplicate background self-update processes."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"
            state.mkdir()
            (state / "auto_update.json").write_text(json.dumps({"last_checked_at": autopilot.time.time()}))
            with mock.patch.object(autopilot, "STATE_DIR", state), mock.patch.dict(os.environ, {"ORCA_AUTOPILOT_PILOT_BIN": "/bin/true", "ORCA_AUTOPILOT_GEMINI_DIR": "", "ORCA_AUTOPILOT_DISABLE_AUTO_UPDATE": ""}), mock.patch.object(autopilot.subprocess, "Popen") as popen:
                autopilot.maybe_trigger_auto_update()
                popen.assert_not_called()

    def test_parser_exposes_self_update_contract(self):
        """The CLI exposes checkout and home overrides for isolated update tests."""
        args = installer.build_parser().parse_args(["self-update", "--checkout-dir", "/tmp/x", "--home-dir", "/tmp/h", "--json"])
        self.assertEqual(args.checkout_dir, "/tmp/x")
        self.assertEqual(args.home_dir, "/tmp/h")

    def test_status_surfaces_persisted_update(self):
        """Status reports persisted auto-update metadata without mutating it."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            gemini = root / "gemini"
            state = gemini / "config/autopilot/state"
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text('{"status":"up_to_date"}')
            args = installer.build_parser().parse_args(["status", "--gemini-dir", str(gemini), "--no-project", "--json"])
            report = installer.status(args)
            self.assertEqual(report["auto_update"]["status"], "up_to_date")

    def test_self_update_status_distinguishes_same_head(self):
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
            log_record = (gemini / "config/autopilot/state/autopilot.log").read_text().splitlines()[-1]
            self.assertIsInstance(json.loads(log_record)["ts"], str)

    def test_self_update_skips_dirty_and_non_main_checkouts(self):
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
        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            (checkout / "README").write_text("x")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-m", "init"], check=True, capture_output=True)
            result = installer.main([
                "self-update", "--checkout-dir", str(checkout), "--gemini-dir", str(Path(root) / "gemini"),
                "--no-project",
            ])
            self.assertEqual(result, 0)

    def test_overlay_sync_uses_real_installer_and_v2_template(self):
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
            state_dir = gemini / "config/autopilot/state"
            with mock.patch.dict(os.environ, {
                "ORCA_AUTOPILOT_PILOT_BIN": str(Path(__file__).parents[1] / "bin/pilot"),
                "ORCA_AUTOPILOT_GEMINI_DIR": str(gemini),
                "ORCA_AUTOPILOT_STATE_DIR": str(state_dir),
                "ORCA_AUTOPILOT_DISABLE_AUTO_UPDATE": "1",
            }), mock.patch.object(autopilot, "GEMINI_DIR", gemini), mock.patch.object(autopilot, "AUTOPILOT_DIR", gemini / "config/autopilot"), mock.patch.object(autopilot, "PROFILE_PATH", gemini / "config/autopilot/profile.json"), mock.patch.object(autopilot, "CONFIG_PATH", gemini / "config/autopilot/config.json"), mock.patch.object(autopilot, "STATE_DIR", state_dir), mock.patch.object(autopilot, "LOG_PATH", state_dir / "autopilot.log"):
                result = autopilot.handle_pre_invocation({
                    "conversationId": "c1", "lastUserInput": "cq hello", "workspacePaths": [str(workspace)],
                    "transcriptPath": str(transcript),
                })
            self.assertIn("V2 hello", json.dumps(result, ensure_ascii=False))
            self.assertEqual(json.loads((gemini / "config/autopilot/profile.json").read_text())["autopilot"]["max_consecutive_auto_turns"], 9)
            self.assertEqual(json.loads((gemini / "config/autopilot/project.json").read_text())["entry_specs"]["cq"]["template"], "V2 {arg}")

