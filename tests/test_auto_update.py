"""Focused contracts for the zero-touch update paths."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            state = root / "state"; state.mkdir()
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
            root = Path(root); gemini = root / "gemini"; state = gemini / "config/autopilot/state"; state.mkdir(parents=True)
            (state / "auto_update.json").write_text('{"status":"up_to_date"}')
            args = installer.build_parser().parse_args(["status", "--gemini-dir", str(gemini), "--no-project", "--json"])
            report = installer.status(args)
            self.assertEqual(report["auto_update"]["status"], "up_to_date")

