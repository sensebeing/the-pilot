"""Focused contracts for the zero-touch update paths."""
import io
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
import zipfile

from the_pilot import installer
from the_pilot.assets import runtime


class AutoUpdateTest(unittest.TestCase):
    def test_shared_runtime_helpers_load_without_reading_the_script(self):
        """Package callers can use shared helpers without a readable runtime script."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script_path = root / "runtime.py"
            gemini = root / "gemini"
            loaded_code = compile(Path(runtime.__file__).read_bytes(), str(script_path), "exec")
            module = {"__name__": "shared_pilot_runtime", "__file__": str(script_path)}
            with mock.patch.dict(os.environ, {
                "HOME": str(root / "home"), "THE_PILOT_GEMINI_DIR": str(gemini),
            }):
                exec(loaded_code, module)
            self.assertTrue(module["is_entry_key"]("deploy-check"))
            self.assertFalse(gemini.exists())

    def test_hook_and_auto_update_logs_rotate_at_512_kib(self):
        """A writer replaces the previous archive and starts a fresh log at 512 KiB."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gemini = root / "gemini"
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            limit = 512 * 1024
            hook_log = state / "pilot.log"
            for path in (hook_log, state / "auto_update.log"):
                path.write_bytes(b"a" * limit)
                path.with_suffix(".log.1").write_bytes(b"older")
            with mock.patch.dict(os.environ, {"THE_PILOT_GEMINI_DIR": str(gemini)}), \
                    mock.patch.object(runtime, "STATE_DIR", state), \
                    mock.patch.object(runtime, "LOG_PATH", hook_log):
                runtime.log_event("hook_after_rotation")
                installer._log_auto_update(state, "update_after_rotation")
            for path, event in ((hook_log, "hook_after_rotation"),
                                (state / "auto_update.log", "update_after_rotation")):
                self.assertEqual(path.with_suffix(".log.1").read_bytes(), b"a" * limit)
                self.assertEqual(json.loads(path.read_text())["event"], event)

    def make_checkout(self, home: Path) -> Path:
        checkout = home / "the-pilot"
        checkout.mkdir(parents=True)
        source = Path(__file__).parents[1]
        for folder in ("src", "bin", "skills"):
            shutil.copytree(source / folder, checkout / folder, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(source / ".gitignore", checkout / ".gitignore")
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
        for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
            subprocess.run(["git", "-C", str(checkout), "config", key, value], check=True)
        self.commit_checkout(checkout, "baseline")
        return checkout

    def commit_checkout(self, checkout: Path, message: str) -> str:
        git = ["git", "-C", str(checkout)]
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-m", message], check=True, capture_output=True)
        return subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()

    def test_installed_package_uses_newer_main_for_imports_and_cli(self):
        """A proven clean main checkout supplies newer code to imports and CLI calls."""
        for vcs_metadata in (True, False):
            with self.subTest(vcs_metadata=vcs_metadata), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                home, gemini = root / "home", root / "gemini"
                checkout = self.make_checkout(home)
                package = checkout / "src/the_pilot"
                site_packages = root / "venv/lib/python3.12/site-packages"
                boot_package = site_packages / "the_pilot"
                shutil.copytree(package, boot_package)
                base = subprocess.check_output(
                    ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
                ).strip()
                (package / "cli.py").write_text(
                    "import json, os, sys\n"
                    "print(json.dumps([__file__, os.environ['THE_PILOT_DELEGATED'], sys.argv[1:]]))\n"
                )
                head = self.commit_checkout(checkout, "new CLI")
                state = gemini / "config/the-pilot/state"
                state.mkdir(parents=True)
                metadata = {"last_local_sync_at": time.time(), "last_checked_at": time.time()}
                if vcs_metadata:
                    direct_url = site_packages / "the_pilot-0.1.0.dist-info/direct_url.json"
                    direct_url.parent.mkdir()
                    direct_url.write_text(json.dumps({"vcs_info": {"commit_id": base}}))
                else:
                    metadata["installed_sha"] = head
                (state / "auto_update.json").write_text(json.dumps(metadata))
                env = {
                    "PATH": os.environ["PATH"], "HOME": str(home), "PYTHONPATH": str(site_packages),
                    "THE_PILOT_ENABLE_AUTO_UPDATE": "1", "THE_PILOT_GEMINI_DIR": str(gemini),
                }
                paths = subprocess.check_output(
                    [sys.executable, "-c",
                     "from the_pilot import defaults, installer; "
                     "print(defaults.__file__); print(installer.SCRIPTS_DIR); print(installer.PACKAGE_DIR)"],
                    cwd=root, env=env, text=True,
                ).splitlines()
                self.assertEqual(paths, [str(package / "defaults.py"), str(package), str(boot_package)])
                args = ["status", f"--gemini-dir={gemini}", "--json"]
                cli = subprocess.run(
                    [sys.executable, "-c",
                     "from the_pilot import cli; import os, sys; "
                     "os.environ['HOME'] = sys.argv[1]; sys.exit(cli.main(sys.argv[2:]))",
                     str(home), *args],
                    cwd=root, env={
                        **env, "HOME": str(root / "other-home"),
                        "THE_PILOT_GEMINI_DIR": str(root / "other-gemini"),
                    },
                    text=True, capture_output=True, check=True,
                )
                self.assertEqual(json.loads(cli.stdout), [str(package / "cli.py"), "1", args])

    def test_project_only_repairs_artifact_drift_at_matching_source_digest(self):
        """A modified managed GEMINI.md block is repaired even when the engine is current."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home, gemini = root / "home", root / "gemini"
            checkout = self.make_checkout(home)
            installer.apply(installer.build_parser().parse_args([
                "apply", "--gemini-dir", str(gemini), "--no-project", "--json",
            ]))
            gemini_md = gemini / "config/GEMINI.md"
            original = gemini_md.read_text()
            gemini_md.write_text("User-owned notes\n")
            state = gemini / "config/the-pilot/state"
            state.mkdir()
            digest = installer._compute_source_tree_digest(checkout / "src/the_pilot")
            (state / "auto_update.json").write_text(json.dumps({"runtime_tree_digest": digest}))
            args = installer.build_parser().parse_args([
                "self-update", "--project-only", "--home-dir", str(home),
                "--gemini-dir", str(gemini), "--json",
            ])
            with mock.patch.dict(os.environ, {"HOME": str(root / "process-home"), "THE_PILOT_GEMINI_DIR": str(gemini)}):
                report = installer.self_update(args)
            self.assertIsNone(report["error"])
            self.assertEqual(report["local_source_status"], "updated")
            self.assertIn(original, gemini_md.read_text())
            self.assertIn("User-owned notes", gemini_md.read_text())

    def test_read_only_cli_commands_do_not_schedule_updates(self):
        """Inspection leaves update metadata absent even when automatic maintenance is enabled."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            gemini = home / ".gemini"
            checkout = self.make_checkout(home)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                installer.apply(installer.build_parser().parse_args([
                    "apply", "--gemini-dir", str(gemini), "--no-project", "--json",
                ]))
            env = {
                "HOME": str(home), "PATH": os.environ["PATH"],
                "THE_PILOT_GEMINI_DIR": str(gemini), "THE_PILOT_ENABLE_AUTO_UPDATE": "1",
                "THE_PILOT_BIN": str(checkout / "bin/pilot"),
            }
            state_dir = gemini / "config/the-pilot/state"
            for args in (("probe",), ("status",), ("distill",), ("worker", "status"), ("mode",), ("log",)):
                with self.subTest(args=args):
                    subprocess.run(
                        [sys.executable, str(checkout / "bin/pilot"), *args],
                        cwd=root, env=env, check=True, capture_output=True, timeout=10,
                    )
                    self.assertFalse((state_dir / "auto_update.json").exists())
                    self.assertFalse((state_dir / "auto_update.lock").exists())

    def test_redirected_self_update_keeps_global_tool_and_skills_outside_scope(self):
        """A redirected installation repairs its own artifacts until a home override is explicit."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home, gemini = root / "home", root / "gemini"
            checkout = self.make_checkout(home)
            global_skills = home / ".claude/skills"
            global_skills.mkdir(parents=True)
            local_skills = gemini / "config/skills"
            local_skills.mkdir(parents=True)
            uv = root / "uv"
            uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/uv-args"\n')
            uv.chmod(0o755)
            env = {"HOME": str(home), "PATH": f"{root}:{os.environ['PATH']}"}
            subprocess.run(
                [sys.executable, str(checkout / "bin/pilot"), "apply", "--gemini-dir", str(gemini), "--no-project"],
                cwd=root, env=env, check=True, capture_output=True, timeout=10,
            )
            self.assertFalse((gemini / "config/the-pilot/state/auto_update.lock").exists())
            command = [sys.executable, str(checkout / "bin/pilot"), "self-update",
                       "--local-only", "--gemini-dir", str(gemini), "--json"]
            subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, timeout=10)
            self.assertTrue((local_skills / "the-pilot").is_symlink())
            self.assertFalse((global_skills / "the-pilot").exists())
            self.assertFalse((home / "uv-args").exists())

            subprocess.run([*command, "--home-dir", str(home)], cwd=root, env=env, check=True, capture_output=True, timeout=10)
            self.assertTrue((global_skills / "the-pilot").is_symlink())
            self.assertIn("install\n--force", (home / "uv-args").read_text())

    def test_cli_background_checks_keep_local_and_remote_cooldowns_separate(self):
        """Stale local code can repair immediately without postponing its remote check."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            gemini = home / ".gemini"
            checkout = self.make_checkout(home)
            launcher = home / ".local/bin/pilot"
            launcher.parent.mkdir(parents=True)
            launcher.touch()
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            metadata_path = state / "auto_update.json"
            last_checked_at = time.time() - 60
            metadata_path.write_text(json.dumps({"last_checked_at": last_checked_at}))
            env = {
                "HOME": str(home), "PATH": os.environ["PATH"],
                "THE_PILOT_ENABLE_AUTO_UPDATE": "1",
            }
            original_popen = subprocess.Popen

            def spawn(command, **kwargs):
                if command[0] == sys.executable:
                    return mock.Mock()
                return original_popen(command, **kwargs)

            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(installer, "_in_test_harness", return_value=False), \
                    mock.patch.object(installer.shutil, "which", return_value="/fake/uv"), \
                    mock.patch.object(installer.subprocess, "Popen", side_effect=spawn) as popen:
                self.assertEqual(installer.maybe_trigger_background_auto_update(gemini), "spawned_local_sync")
                self.assertEqual(
                    popen.call_args.args[0],
                    [sys.executable, str(checkout / "bin/pilot"), "self-update", "--local-only",
                     "--gemini-dir", str(gemini), "--json"],
                )
                metadata = json.loads(metadata_path.read_text())
                self.assertEqual(metadata["last_checked_at"], last_checked_at)
                self.assertEqual(installer.maybe_trigger_background_auto_update(gemini), "cooldown")

                sha = subprocess.check_output(
                    ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], text=True
                ).strip()
                metadata.update(
                    installed_sha=sha,
                    installed_tree_digest=installer._compute_source_tree_digest(checkout / "src/the_pilot"),
                    last_checked_at=time.time() - installer.AUTO_UPDATE_INTERVAL_SECONDS - 1,
                )
                metadata_path.write_text(json.dumps(metadata))
                self.assertEqual(installer.maybe_trigger_background_auto_update(gemini), "spawned_remote_check")
                self.assertNotIn("--local-only", popen.call_args.args[0])
                self.assertGreater(json.loads(metadata_path.read_text())["last_checked_at"], last_checked_at)

    def test_self_update_installs_missing_uv_launcher_at_default_home(self):
        """Current install metadata cannot mask a missing launcher at the default home."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            gemini = home / ".gemini"
            checkout = self.make_checkout(home)
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text(json.dumps({
                "installed_sha": subprocess.check_output(
                    ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], text=True
                ).strip(),
                "installed_tree_digest": installer._compute_source_tree_digest(checkout / "src/the_pilot"),
            }))
            uv = root / "uv"
            uv.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$@" > "$HOME/uv-args"\n'
                'mkdir -p "$HOME/.local/bin"\n'
                'touch "$HOME/.local/bin/pilot"\n'
            )
            uv.chmod(0o755)
            result = subprocess.run(
                [sys.executable, str(checkout / "bin/pilot"), "self-update",
                 "--local-only", "--gemini-dir", str(gemini), "--json"],
                cwd=root, env={"HOME": str(home), "PATH": f"{root}:{os.environ['PATH']}"},
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(json.loads(result.stdout)["status"], "up_to_date")
            self.assertTrue((home / ".local/bin/pilot").is_file())
            self.assertEqual((home / "uv-args").read_text().splitlines(), ["tool", "install", "--force", str(checkout)])

    def test_session_start_announces_without_loading_or_syncing_the_profile(self):
        """SessionStart exposes the initial pane state before project maintenance.

        Failure means an idle or prompted startup is unannounced or stalls
        behind a project sync before reaching the composer.
        resident-fact: status: SessionStart 只按初始输入宣告，不读取配置或同步项目。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "osc.log"
            env = {"THE_PILOT_GEMINI_DIR": str(root / "gemini"),
                   "THE_PILOT_OSC_TARGET": str(target)}
            cases = (
                ({}, runtime.OSC_7501_DONE),
                ({"lastUserInput": "<USER_REQUEST>   </USER_REQUEST>"}, runtime.OSC_7501_DONE),
                ({"lastUserInput": "<USER_REQUEST>Start</USER_REQUEST>"}, runtime.OSC_7501_WORKING),
            )
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                runtime, "_sync_and_maybe_reexec", side_effect=AssertionError("startup sync")
            ), mock.patch.object(runtime, "load_profile", side_effect=AssertionError("startup load")):
                for initial, frame in cases:
                    with self.subTest(initial=initial), mock.patch(
                        "sys.stdin", io.StringIO(json.dumps({**initial, "workspacePaths": [str(root)]}))
                    ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        self.assertEqual(runtime.main(["hook", "--event", "SessionStart"]), 0)
                        self.assertEqual(json.loads(stdout.getvalue()), {})
            self.assertEqual(target.read_text(), "".join(frame for _, frame in cases))

    def test_stop_reexecs_updated_engine_on_the_same_turn(self):
        """An eligible Stop syncs once and preserves the updated engine's hook and pane decisions."""
        question = {"name": "ask_question", "args": {"questions": '[{"question":"Which?"}]'}}
        cases = (
            ("continue", {"content": "请确认以下 2 个问题："}, "continue", ""),
            ("waiting", {"content": "Choose", "tool_calls": [question]}, "allow", runtime.OSC_7501_WAITING_QUESTION),
            ("stale", {"content": "Done"}, "allow", ""),
            ("settled", {"content": "Done"}, "allow", runtime.OSC_7501_DONE),
        )
        for case, planner_step, decision, osc in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                gemini = root / "gemini"
                installer.apply(installer.build_parser().parse_args([
                    "apply", "--gemini-dir", str(gemini), "--no-project", "--json",
                ]))
                engine = gemini / "config/the-pilot/runtime.py"
                child = root / "child.py"
                source = engine.read_text()
                child.write_text(source.replace(
                    "    raise SystemExit(main())",
                    "    assert os.environ['THE_PILOT_REEXEC'] == '1'\n"
                    "    assert sys.argv[1:] == ['hook', '--event', 'Stop']\n"
                    "    with (Path.home().parent / 'reexecs').open('a') as count: count.write('reexec\\n')\n"
                    "    raise SystemExit(main())",
                ))
                shim = root / "pilot-shim.py"
                shim.write_text(
                    "#!/usr/bin/env python3\n"
                    "import os, pathlib, shutil, sys\n"
                    "assert sys.argv[1:3] == ['self-update', '--project-only']\n"
                    "root = pathlib.Path(os.environ['HOME']).parent\n"
                    "with (root / 'syncs').open('a') as syncs: syncs.write('sync\\n')\n"
                    "shutil.copyfile(root / 'child.py', pathlib.Path(os.environ['THE_PILOT_GEMINI_DIR']) / 'config/the-pilot/runtime.py')\n"
                    "print('{\"local_source_status\":\"updated\"}')\n"
                )
                shim.chmod(0o755)
                transcript = root / "transcript.jsonl"
                steps = [{"type": "USER_INPUT", "content": "start"}, {"type": "PLANNER_RESPONSE", **planner_step}]
                transcript.write_text("".join(json.dumps(step) + "\n" for step in steps))
                payload = {
                    "conversationId": "stop-reexec", "transcriptPath": str(transcript),
                    "terminationReason": "NO_TOOL_CALL", "fullyIdle": True,
                }
                osc_target = root / "osc"
                state = gemini / "config/the-pilot/state"
                if case == "stale":
                    with mock.patch.object(runtime, "STATE_DIR", state):
                        self.assertTrue(runtime.save_state(
                            "stop-reexec", {"current_user_turn_id": "newer-turn", "delivery_status": "none"}
                        ))
                result = subprocess.run(
                    [sys.executable, str(engine), "hook", "--event", "Stop"],
                    input=json.dumps(payload), text=True, capture_output=True, check=True,
                    env={
                        "PATH": os.environ["PATH"], "HOME": str(root / "home"),
                        "THE_PILOT_GEMINI_DIR": str(gemini), "THE_PILOT_BIN": str(shim),
                        "THE_PILOT_DISABLE_AUTO_UPDATE": "1", "THE_PILOT_OSC_TARGET": str(osc_target),
                    },
                )
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], decision)
                if decision == "allow":
                    self.assertEqual(output, {"decision": "allow"})
                self.assertEqual((root / "syncs").read_text(), "sync\n")
                self.assertEqual((root / "reexecs").read_text(), "reexec\n")
                self.assertEqual(osc_target.read_text() if osc_target.exists() else "", osc)

    def test_cooldown_suppresses_background_spawn(self):
        """The six hour TTL prevents duplicate background self-update processes."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            gemini = root / ".gemini"
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text(json.dumps({"last_checked_at": time.time()}))
            env = {"HOME": str(root), "THE_PILOT_ENABLE_AUTO_UPDATE": "1"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(installer, "_discover_checkout", return_value=(None, False)), \
                    mock.patch.object(installer.subprocess, "Popen") as popen:
                self.assertEqual(installer.maybe_trigger_background_auto_update(gemini), "none")
                popen.assert_not_called()

    def test_direct_runtime_does_not_wait_for_an_active_updater(self):
        """Background maintenance never holds a runtime command behind an active local repair."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            gemini = home / ".gemini"
            checkout = self.make_checkout(home)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                installer.apply(installer.build_parser().parse_args([
                    "apply", "--gemini-dir", str(gemini), "--no-project", "--json",
                ]))
            pilot_dir = gemini / "config/the-pilot"
            state = pilot_dir / "state"
            request = root / "request"
            launcher = root / "pilot"
            launcher.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "with Path(os.environ['REQUEST']).open('a') as out:\n"
                "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "os.execv(sys.executable, [sys.executable, os.environ['CHECKOUT_PILOT'], *sys.argv[1:]])\n"
            )
            launcher.chmod(0o755)
            env = {
                "PATH": os.environ["PATH"], "HOME": str(home),
                "THE_PILOT_GEMINI_DIR": str(gemini), "THE_PILOT_BIN": str(launcher),
                "THE_PILOT_ENABLE_AUTO_UPDATE": "1",
                "REQUEST": str(request), "CHECKOUT_PILOT": str(checkout / "bin/pilot"),
            }
            with installer._file_lock(state / "auto_update.lock"):
                result = subprocess.run(
                    [sys.executable, str(pilot_dir / "runtime.py"), "mode", "confirm"],
                    capture_output=True, text=True, check=True, timeout=3, env=env,
                )
                self.assertEqual(result.returncode, 0)
                self.assertFalse((state / "auto_update.json").exists())
            self.assertEqual(
                [json.loads(line) for line in request.read_text().splitlines()],
                [["self-update", "--background", "--gemini-dir", str(gemini), "--json"]],
            )

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
            log_record = (gemini / "config/the-pilot/state/auto_update.log").read_text().splitlines()[-1]
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
            gemini = root / ".gemini"
            state = gemini / "config/the-pilot/state"
            old = time.time() - installer.AUTO_UPDATE_INTERVAL_SECONDS - 1
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text(json.dumps({"last_checked_at": old}))
            env = {"HOME": str(root), "THE_PILOT_ENABLE_AUTO_UPDATE": "1"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(installer, "_discover_checkout", return_value=(None, False)), \
                    mock.patch.object(installer.subprocess, "Popen", side_effect=OSError("spawn")):
                with self.assertRaisesRegex(installer.SetupError, "Could not start Pilot auto-update"):
                    installer.maybe_trigger_background_auto_update(gemini)
                record = json.loads((state / "auto_update.json").read_text())
                self.assertEqual(record["last_checked_at"], old)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"], "spawn")
            self.assertIn("auto_update_spawn_failed", (state / "auto_update.log").read_text())

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

    def test_stale_build_staging_is_discarded_before_uv_reinstall_and_digest_converges(self):
        """Deleted-module build residue cannot keep the same commit reinstalling."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, gemini = root / "home", root / "gemini"
            checkout = home / "the-pilot"
            checkout.mkdir(parents=True)
            source = Path(__file__).parents[1]
            for name in ("pyproject.toml", "setup.py", "README.md", ".gitignore"):
                shutil.copyfile(source / name, checkout / name)
            shutil.copytree(
                source / "src", checkout / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
                subprocess.run(["git", "-C", str(checkout), "config", key, value], check=True)
            self.commit_checkout(checkout, "source")
            stale = checkout / "build/lib/the_pilot/assets/autopilot.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("# deleted module residue\n", encoding="utf-8")
            entry = home / ".local/bin/pilot"
            entry.parent.mkdir(parents=True)
            entry.write_text("#!/bin/sh\n")
            entry.chmod(0o755)
            package = home / ".local/share/uv/tools/the-pilot/lib/python3.12/site-packages/the_pilot"
            shutil.copytree(checkout / "src/the_pilot", package)
            (package / "assets/autopilot.py").write_text("# polluted install\n", encoding="utf-8")
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text(json.dumps({
                "installed_sha": "old", "installed_tree_digest": "old",
            }))
            installs: list[Path] = []
            real_run = installer.subprocess_run

            def fake_run(command, timeout, *, env=None):
                if command[:3] == ["uv", "tool", "install"]:
                    checkout_arg = Path(command[-1])
                    self.assertFalse((checkout_arg / "build").exists())
                    shutil.rmtree(package)
                    shutil.copytree(checkout_arg / "src/the_pilot", package)
                    installs.append(checkout_arg)
                    return ""
                return real_run(command, timeout, env=env)

            args = installer.build_parser().parse_args([
                "self-update", "--local-only", "--gemini-dir", str(gemini),
                "--home-dir", str(home), "--no-project", "--json",
            ])
            with mock.patch.object(installer.shutil, "which", return_value="/fake/uv"), \
                    mock.patch.object(installer, "subprocess_run", side_effect=fake_run):
                installer.self_update(args)
                digest = installer._compute_source_tree_digest(checkout / "src/the_pilot")
                self.assertEqual(len(installs), 1)
                self.assertFalse((checkout / "build").exists())
                self.assertFalse((package / "assets/autopilot.py").exists())
                self.assertEqual(installer._compute_source_tree_digest(package), digest)
                metadata = json.loads((state / "auto_update.json").read_text())
                self.assertEqual(metadata["installed_tree_digest"], digest)
                installer.self_update(args)
                self.assertEqual(len(installs), 1)

    @unittest.skipUnless(shutil.which("uv"), "uv is required to build a wheel")
    def test_wheel_build_excludes_deleted_modules_left_in_build_staging(self):
        """Stale gitignored build staging never reaches the distributable wheel."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            source = Path(__file__).parents[1]
            for name in ("pyproject.toml", "setup.py", "README.md", "LICENSE"):
                shutil.copyfile(source / name, checkout / name)
            shutil.copytree(
                source / "src", checkout / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            stale = checkout / "build/lib/the_pilot/assets/autopilot.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("# deleted module residue\n", encoding="utf-8")
            result = subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(root / "dist")],
                cwd=checkout, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wheel = next((root / "dist").glob("*.whl"))
            names = set(zipfile.ZipFile(wheel).namelist())
            self.assertNotIn("the_pilot/assets/autopilot.py", names)
            self.assertIn("the_pilot/assets/runtime.py", names)

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
            update_env = {"THE_PILOT_ENABLE_AUTO_UPDATE": "1", "THE_PILOT_DISABLE_AUTO_UPDATE": "0"}
            with mock.patch.dict(os.environ, update_env), mock.patch.object(installer.shutil, "which", return_value=None):
                unmanaged_report = installer.self_update(args)
            self.assertIsNone(unmanaged_report["error"])
            self.assertEqual(unmanaged_report["cli_status"], "skipped_unmanaged")
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/pilot").write_text("#!/bin/sh\n")
            (home / ".local/bin/pilot").chmod(0o755)
            real_popen = subprocess.Popen
            spawned: list[list[str]] = []
            def fake_popen(command, **kwargs):
                if isinstance(command, list) and "--local-only" in command:
                    spawned.append(list(command))
                    return mock.Mock()
                return real_popen(command, **kwargs)
            with mock.patch.dict(os.environ, update_env), mock.patch.object(installer.shutil, "which", return_value="/fake/uv"), \
                    mock.patch.object(installer.subprocess, "Popen", side_effect=fake_popen):
                report = installer.self_update(args)
            self.assertEqual(report["cli_status"], "spawned_local_sync")
            self.assertEqual(len(spawned), 1)
            self.assertEqual(spawned[0][0], sys.executable)
            self.assertIn("--local-only", spawned[0])
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
        """A clean local checkout repairs the installed engine and runs it on the same turn."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, gemini = root / "home", root / "gemini"
            checkout = self.make_checkout(home)
            installer.apply(installer.build_parser().parse_args([
                "apply", "--gemini-dir", str(gemini), "--json",
            ]))
            pilot_dir = gemini / "config/the-pilot"
            engine = pilot_dir / "runtime.py"
            source = checkout / "src/the_pilot/assets/runtime.py"
            source.write_text(source.read_text().replace("【Pilot 启动口令", "【Upgraded engine"), encoding="utf-8")
            self.commit_checkout(checkout, "change hook output")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "python3").symlink_to(sys.executable)
            payload = {"conversationId": "local-sync", "lastUserInput": "r auth",
                       "workspacePaths": [], "cwd": str(root)}
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(home),
                "THE_PILOT_GEMINI_DIR": str(gemini),
                "THE_PILOT_BIN": str(checkout / "bin/pilot"),
                "THE_PILOT_DISABLE_AUTO_UPDATE": "1",
            }
            result = subprocess.run(
                [sys.executable, str(engine), "hook", "--event", "PreInvocation"],
                input=json.dumps(payload), text=True, capture_output=True, env=env, check=True,
            )
            self.assertIn("Upgraded engine", result.stdout)
            self.assertEqual(engine.read_bytes(), source.read_bytes())
            log = (pilot_dir / "state/pilot.log").read_text()
            self.assertEqual(log.count('"event": "local_auto_synced"'), 1)
            self.assertFalse((pilot_dir / "project.json").exists())

    def test_pilot_reexec_failure_is_reported_as_hook_failure(self):
        """A failing or malformed upgraded engine fails the hook instead of faking success."""
        cases = (
            (
                "import sys\nsys.stderr.write('upgraded engine refused\\n')\nsys.exit(7)\n",
                "engine_reexec_failed",
                "upgraded engine refused",
            ),
            ("print('[]')\n", "engine_reexec_invalid_output", "JSON object"),
        )
        for child_source, expected_error, expected_detail in cases:
            with self.subTest(error=expected_error), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                gemini = root / "gemini"
                installer.apply(installer.build_parser().parse_args([
                    "apply", "--gemini-dir", str(gemini), "--no-project", "--json",
                ]))
                engine = gemini / "config/the-pilot/runtime.py"
                child = root / "child_engine.py"
                child.write_text("#!/usr/bin/env python3\n" + child_source)
                shim = root / "pilot-shim.py"
                shim.write_text(
                    "#!/usr/bin/env python3\n"
                    "import os, pathlib, shutil\n"
                    "engine = pathlib.Path(os.environ['THE_PILOT_GEMINI_DIR']) / 'config/the-pilot/runtime.py'\n"
                    "shutil.copyfile(os.environ['CHILD_ENGINE'], engine)\n"
                    "print('{\"local_source_status\": \"updated\"}')\n"
                )
                shim.chmod(0o755)
                payload = {"conversationId": "c", "lastUserInput": "hello", "workspacePaths": []}
                env = {
                    **os.environ,
                    "HOME": str(root / "home"),
                    "THE_PILOT_GEMINI_DIR": str(gemini),
                    "THE_PILOT_BIN": str(shim),
                    "THE_PILOT_DISABLE_AUTO_UPDATE": "1",
                    "CHILD_ENGINE": str(child),
                }
                result = subprocess.run(
                    [sys.executable, str(engine), "hook", "--event", "PreInvocation"],
                    input=json.dumps(payload), text=True, capture_output=True, env=env,
                )
                self.assertEqual(result.returncode, 2)
                failure = json.loads(result.stderr)
                self.assertEqual(failure["error"], expected_error)
                self.assertIn(expected_detail, failure["detail"])

    def test_pilot_local_sync_timeout_kills_the_updater_process_group(self):
        """An overrunning updater is unconfirmed and leaves no installation writer behind."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            gemini = root / "gemini"
            state_dir = gemini / "config/the-pilot/state"
            pid_file = root / "child.pid"
            shim = root / "pilot-shim.sh"
            shim.write_text(
                "#!/bin/sh\n"
                "sleep 30 &\n"
                "echo $! > \"$CHILD_PID_FILE\"\n"
                "sleep 30\n"
            )
            shim.chmod(0o755)
            env = {
                "HOME": str(root / "home"),
                "THE_PILOT_BIN": str(shim),
                "THE_PILOT_GEMINI_DIR": str(gemini),
                "CHILD_PID_FILE": str(pid_file),
            }
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(runtime, "GEMINI_DIR", gemini), \
                    mock.patch.object(runtime, "PILOT_DIR", gemini / "config/the-pilot"), \
                    mock.patch.object(runtime, "STATE_DIR", state_dir), \
                    mock.patch.object(runtime, "LOG_PATH", state_dir / "pilot.log"), \
                    mock.patch.object(runtime, "LOCAL_SYNC_TIMEOUT_SECONDS", 0.5):
                started = time.time()
                confirmed = runtime.maybe_sync_local_engine()
                elapsed = time.time() - started
            self.assertFalse(confirmed)
            self.assertLess(elapsed, 10)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text().strip())
            deadline = time.time() + 2
            while True:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                if time.time() >= deadline:
                    self.fail("local sync timeout left its child writer running")
                time.sleep(0.05)

