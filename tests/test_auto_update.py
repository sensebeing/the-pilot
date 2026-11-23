"""Focused contracts for the zero-touch update paths."""
import base64
import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import subprocess
import sys
import time
import zipfile

from the_pilot import installer, packaging
from the_pilot.assets import runtime


def tree_paths(root: Path) -> set[str]:
    """Snapshot a checkout tree without Git internals or byte-code caches."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if "__pycache__" not in path.parts and ".git" not in path.parts
    }


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
        for name in ("pyproject.toml", "README.md", "LICENSE", ".gitignore"):
            shutil.copyfile(source / name, checkout / name)
        for folder in ("src", "bin", "skills"):
            shutil.copytree(source / folder, checkout / folder, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
        for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
            subprocess.run(["git", "-C", str(checkout), "config", key, value], check=True)
        self.commit_checkout(checkout, "baseline")
        return checkout

    def make_package_checkout(self, root: Path, *, pyproject_text: str | None = None) -> Path:
        """Create a clean main checkout holding every packaging input."""
        source = Path(__file__).parents[1]
        checkout = root / "the-pilot"
        checkout.mkdir(parents=True)
        for name in ("pyproject.toml", "README.md", "LICENSE", ".gitignore"):
            if pyproject_text is not None and name == "pyproject.toml":
                (checkout / name).write_text(pyproject_text, encoding="utf-8")
            else:
                shutil.copyfile(source / name, checkout / name)
        shutil.copytree(
            source / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
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
            uv.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$@" > "$HOME/uv-args"\n'
                'mkdir -p "$HOME/.local/bin"\n'
                'touch "$HOME/.local/bin/pilot"\n'
            )
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
            arguments = (home / "uv-args").read_text().splitlines()
            self.assertEqual(arguments[:3], ["tool", "install", "--force"])
            self.assertTrue(arguments[-1].endswith(".whl"), arguments[-1])

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
            source = Path(__file__).parents[1]
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copyfile(source / name, checkout / name)
            shutil.copytree(source / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(source / "bin", checkout / "bin")
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
                install_arguments = uv_log.read_text().splitlines()[-1].split()
                self.assertEqual(install_arguments[:3], ["tool", "install", "--force"])
                self.assertTrue(install_arguments[-1].endswith(".whl"), install_arguments[-1])
                uv_log.write_text("")
                installer.self_update(args)
                self.assertEqual(uv_log.read_text(), "")
                (state / "auto_update.json").write_text(json.dumps({"last_checked_at": 123456.0, "installed_sha": "old", "installed_tree_digest": "old"}))
                local_args = installer.build_parser().parse_args(["self-update", "--local-only", "--gemini-dir", str(gemini), "--home-dir", str(home), "--no-project", "--json"])
                installer.self_update(local_args)
                local_report = json.loads((state / "auto_update.json").read_text())
                self.assertEqual(local_report["last_checked_at"], 123456.0)
                self.assertEqual(local_report["installed_sha"], sha)

    def test_self_update_installs_a_verified_wheel_that_ignores_build_residue(self):
        """A deleted module left in ignored build staging can neither reach the wheel nor reinstall forever.

        The updater must build only the controlled package files, verify the
        wheel tree digest against the checkout digest, extract that wheel, and
        then stop re-calling ``uv`` for the same commit. Failure means a
        gitignored ``build/lib`` copy of a deleted module is installed again,
        so the installed package digest can never equal its source and every
        update check force-reinstalls the same commit.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, gemini = root / "home", root / "gemini"
            checkout = self.make_package_checkout(home)
            stale = checkout / "build/lib/the_pilot/assets/autopilot.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("# deleted module residue\n", encoding="utf-8")
            egg_info = checkout / "src/the_pilot.egg-info/stale.txt"
            egg_info.parent.mkdir(parents=True)
            egg_info.write_text("stale metadata\n", encoding="utf-8")
            site_packages = home / ".local/share/uv/tools/the-pilot/lib/python3.12/site-packages"
            polluted = site_packages / "the_pilot/assets"
            polluted.mkdir(parents=True)
            (polluted / "autopilot.py").write_text("# polluted install\n", encoding="utf-8")
            launcher = home / ".local/bin/pilot"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\n# live launcher\n", encoding="utf-8")
            launcher.chmod(0o755)
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text(json.dumps({
                "installed_sha": "old", "installed_tree_digest": "old",
            }))
            uv_log = root / "uv.log"
            uv_wheel = root / "installed.whl"
            uv = root / "uv"
            uv.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, shutil, sys, zipfile\n"
                "from pathlib import Path\n"
                "arguments = sys.argv[1:]\n"
                "with open(os.environ['UV_LOG'], 'a', encoding='utf-8') as log:\n"
                "    log.write(json.dumps(arguments) + '\\n')\n"
                "wheel = Path(arguments[-1])\n"
                "shutil.copyfile(wheel, os.environ['UV_WHEEL_COPY'])\n"
                "site_packages = Path(os.environ['SITE_PACKAGES'])\n"
                "shutil.rmtree(site_packages / 'the_pilot', ignore_errors=True)\n"
                "with zipfile.ZipFile(wheel) as archive:\n"
                "    for name in archive.namelist():\n"
                "        if name.startswith('the_pilot/'):\n"
                "            archive.extract(name, site_packages)\n"
                "launcher = Path(os.environ['LAUNCHER'])\n"
                "launcher.parent.mkdir(parents=True, exist_ok=True)\n"
                "launcher.write_text('#!/bin/sh\\n')\n"
                "launcher.chmod(0o755)\n",
                encoding="utf-8",
            )
            uv.chmod(0o755)
            args = installer.build_parser().parse_args([
                "self-update", "--local-only", "--gemini-dir", str(gemini),
                "--home-dir", str(home), "--json",
            ])
            env = {
                "HOME": str(root / "process-home"), "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                "UV_LOG": str(uv_log), "UV_WHEEL_COPY": str(uv_wheel),
                "SITE_PACKAGES": str(site_packages), "LAUNCHER": str(launcher),
            }
            before = tree_paths(checkout)
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(installer.shutil, "which", return_value=str(uv)):
                installer.self_update(args)
                digest = packaging.compute_source_tree_digest(checkout / "src/the_pilot")
                calls = [json.loads(line) for line in uv_log.read_text().splitlines()]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:3], ["tool", "install", "--force"])
                self.assertTrue(calls[0][-1].endswith(".whl"), calls[0][-1])
                wheel_names = set(zipfile.ZipFile(uv_wheel).namelist())
                self.assertNotIn("the_pilot/assets/autopilot.py", wheel_names)
                self.assertIn("the_pilot/assets/runtime.py", wheel_names)
                self.assertEqual(
                    packaging.compute_wheel_tree_digest(uv_wheel), digest,
                )
                installed = site_packages / "the_pilot"
                self.assertFalse((installed / "assets/autopilot.py").exists())
                self.assertEqual(packaging.compute_source_tree_digest(installed), digest)
                self.assertEqual(tree_paths(checkout), before)
                self.assertFalse((checkout / "dist").exists())
                metadata = json.loads((state / "auto_update.json").read_text())
                self.assertEqual(metadata["installed_tree_digest"], digest)
                uv_log.write_text("")
                installer.self_update(args)
                self.assertEqual(uv_log.read_text(), "")

    @unittest.skipUnless(shutil.which("uv"), "uv is required to build a wheel")
    def test_offline_uv_build_matches_the_source_digest_without_build_staging(self):
        """A cold-cache offline build of the checkout ignores ignored build residue.

        Failure means a fresh machine cannot build the wheel without network
        access, or the wheel carries a deleted module and/or disagrees with
        the source digest, which would keep the installed tool mismatched and
        reinstall it on every update check.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            source = Path(__file__).parents[1]
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copyfile(source / name, checkout / name)
            shutil.copytree(
                source / "src", checkout / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            stale = checkout / "build/lib/the_pilot/assets/autopilot.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("# deleted module residue\n", encoding="utf-8")
            egg_info = checkout / "src/the_pilot.egg-info/stale.txt"
            egg_info.parent.mkdir(parents=True)
            egg_info.write_text("stale metadata\n", encoding="utf-8")
            before = tree_paths(checkout)
            cache = root / "cache"
            cache.mkdir()
            result = subprocess.run(
                ["uv", "build", "--wheel", "--offline", "--out-dir", str(root / "dist")],
                cwd=checkout, capture_output=True, text=True, timeout=180,
                env={
                    **os.environ,
                    "UV_CACHE_DIR": str(cache),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wheel = next((root / "dist").glob("*.whl"))
            names = set(zipfile.ZipFile(wheel).namelist())
            self.assertNotIn("the_pilot/assets/autopilot.py", names)
            self.assertIn("the_pilot/assets/runtime.py", names)
            self.assertEqual(
                packaging.compute_wheel_tree_digest(wheel),
                packaging.compute_source_tree_digest(checkout / "src/the_pilot"),
            )
            self.assertEqual(tree_paths(checkout), before)

    def test_editable_wheel_matches_the_pep660_contract(self):
        """Editable installs must expose the checkout through a valid PEP 660 wheel.

        Failure means the documented `python3 -m pip install -e .` cannot
        import the checkout, lacks the `pilot` console entry point, or ships a
        RECORD whose hashes and sizes do not match the archive.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self.make_package_checkout(root)
            dist = root / "dist"
            with contextlib.chdir(checkout):
                filename = packaging.build_editable(str(dist))
                metadata_name = packaging.prepare_metadata_for_build_editable(
                    str(root / "metadata")
                )
            self.assertEqual(filename, "the_pilot-0.1.0-0.editable-py3-none-any.whl")
            self.assertEqual(metadata_name, "the_pilot-0.1.0.dist-info")
            self.assertTrue((root / "metadata/the_pilot-0.1.0.dist-info/METADATA").is_file())
            record_path = "the_pilot-0.1.0.dist-info/RECORD"
            with zipfile.ZipFile(dist / filename) as archive:
                names = archive.namelist()
                self.assertFalse([name for name in names if name.startswith("the_pilot/")])
                self.assertEqual(
                    archive.read("the_pilot.pth").decode("utf-8"),
                    f"{(checkout / 'src').resolve()}\n",
                )
                self.assertEqual(
                    archive.read("the_pilot-0.1.0.dist-info/entry_points.txt").decode("utf-8"),
                    "[console_scripts]\npilot = the_pilot.cli:main\n",
                )
                record_rows = [
                    row
                    for row in archive.read(record_path).decode("utf-8").splitlines()
                    if row
                ]
                self.assertIn(f"{record_path},,", record_rows)
                for row in record_rows:
                    if row == f"{record_path},,":
                        continue
                    name, digest, size = row.rsplit(",", 2)
                    content = archive.read(name)
                    expected = base64.urlsafe_b64encode(
                        hashlib.sha256(content).digest()
                    ).rstrip(b"=").decode("ascii")
                    self.assertEqual(digest.removeprefix("sha256="), expected)
                    self.assertEqual(int(size), len(content))

    def test_wheel_record_stays_valid_csv_with_a_comma_in_a_package_path(self):
        """Wheel RECORD is a CSV file, so a comma in an asset name must be quoted.

        Failure means a legitimate bundled asset such as
        `assets/report,2026.json` produces an ambiguous RECORD row that
        installers and audit tools cannot parse as three fields.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self.make_package_checkout(root)
            asset = checkout / "src/the_pilot/assets/report,2026.json"
            asset.write_text("{}\n", encoding="utf-8")
            wheel = packaging.build_wheel_from_checkout(checkout, root / "dist")
            with zipfile.ZipFile(wheel) as archive:
                record = archive.read("the_pilot-0.1.0.dist-info/RECORD").decode("utf-8")
            rows = list(csv.reader(io.StringIO(record)))
            self.assertNotIn("\r", record)
            self.assertTrue(all(len(row) == 3 for row in rows), rows)
            self.assertIn("the_pilot/assets/report,2026.json", {row[0] for row in rows})

    def test_sdist_carries_the_sources_without_build_residue(self):
        """The PEP 517 sdist must rebuild the package from sources alone.

        Failure means a source release ships a deleted module from ignored
        `build/` staging, omits the packaging backend or package sources, or
        declares third-party build requirements that a cold offline
        environment cannot satisfy.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self.make_package_checkout(root)
            stale = checkout / "build/lib/the_pilot/assets/autopilot.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("# deleted module residue\n", encoding="utf-8")
            out = root / "sdist"
            with contextlib.chdir(checkout):
                filename = packaging.build_sdist(str(out))
                requirements = (
                    packaging.get_requires_for_build_wheel(),
                    packaging.get_requires_for_build_editable(),
                    packaging.get_requires_for_build_sdist(),
                )
            self.assertEqual(filename, "the_pilot-0.1.0.tar.gz")
            self.assertEqual(requirements, ([], [], []))
            with tarfile.open(out / filename) as archive:
                names = set(archive.getnames())
                pkg_info = archive.extractfile("the_pilot-0.1.0/PKG-INFO").read().decode("utf-8")
            self.assertIn("the_pilot-0.1.0/pyproject.toml", names)
            self.assertIn("the_pilot-0.1.0/PKG-INFO", names)
            self.assertIn("the_pilot-0.1.0/src/the_pilot/packaging.py", names)
            self.assertIn("the_pilot-0.1.0/src/the_pilot/assets/runtime.py", names)
            self.assertIn("Metadata-Version: 2.1", pkg_info)
            self.assertIn("Name: the-pilot", pkg_info)
            self.assertFalse(any("build/" in name for name in names))
            self.assertFalse(any("egg-info" in name for name in names))

    def test_wheel_build_or_digest_failure_never_touches_the_live_launcher(self):
        """Stage 1 failures must leave the current pilot launcher and uv untouched.

        Failure means a broken `pyproject.toml` or a wheel/checkout digest
        mismatch reaches `uv tool install --force`, which deletes the live
        `~/.local/bin/pilot` before building and reproduces
        `exec: pilot: not found` when that build fails.
        """
        for case in ("invalid-pyproject", "digest-mismatch"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home, gemini = root / "home", root / "gemini"
                broken = "[project\n" if case == "invalid-pyproject" else None
                checkout = self.make_package_checkout(home, pyproject_text=broken)
                launcher = home / ".local/bin/pilot"
                launcher.parent.mkdir(parents=True)
                launcher.write_text("#!/bin/sh\n# live launcher\n", encoding="utf-8")
                uv_log = root / "uv.log"
                uv = root / "uv"
                uv.write_text(
                    "#!/bin/sh\n"
                    "printf '%s\\n' \"$@\" >> \"$UV_LOG\"\n"
                )
                uv.chmod(0o755)
                state = gemini / "config/the-pilot/state"
                state.mkdir(parents=True)
                (state / "auto_update.json").write_text(json.dumps({
                    "installed_sha": "old", "installed_tree_digest": "old",
                }))
                args = installer.build_parser().parse_args([
                    "self-update", "--local-only", "--gemini-dir", str(gemini),
                    "--home-dir", str(home), "--json",
                ])
                env = {
                    "HOME": str(root / "process-home"),
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "UV_LOG": str(uv_log),
                }
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(installer.shutil, "which", return_value=str(uv)):
                    if case == "digest-mismatch":
                        with self.assertRaises(installer.SetupError):
                            installer._install_uv_tool_from_checkout(checkout, home, "0" * 64)
                    else:
                        with self.assertRaises(installer.SetupError) as caught:
                            installer.self_update(args)
                        self.assertIn("pyproject.toml", str(caught.exception))
                        metadata = json.loads((state / "auto_update.json").read_text())
                        self.assertIn("pyproject.toml", metadata["error"])
                self.assertEqual(
                    launcher.read_text(encoding="utf-8"), "#!/bin/sh\n# live launcher\n"
                )
                self.assertFalse(uv_log.exists())

    def test_uv_exit_status_and_stderr_reach_the_update_record_and_log(self):
        """A failed `uv tool install` must preserve its exit code and stderr cause.

        Failure means `auto_update.json` and `auto_update.log` again carry only
        `returned non-zero exit status 2` with no diagnostic, so the operator
        cannot tell why the launcher update failed or whether it was retried.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, gemini = root / "home", root / "gemini"
            checkout = self.make_package_checkout(home)
            launcher = home / ".local/bin/pilot"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\n# live launcher\n", encoding="utf-8")
            uv_log = root / "uv.log"
            uv = root / "uv"
            uv.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" >> \"$UV_LOG\"\n"
                "printf '%s\\n' 'error: failed to acquire uv tool lock' >&2\n"
                "exit 2\n"
            )
            uv.chmod(0o755)
            state = gemini / "config/the-pilot/state"
            state.mkdir(parents=True)
            (state / "auto_update.json").write_text(json.dumps({
                "installed_sha": "old", "installed_tree_digest": "old",
            }))
            args = installer.build_parser().parse_args([
                "self-update", "--local-only", "--gemini-dir", str(gemini),
                "--home-dir", str(home), "--json",
            ])
            env = {
                "HOME": str(root / "process-home"),
                "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                "UV_LOG": str(uv_log),
            }
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(installer.shutil, "which", return_value=str(uv)):
                with self.assertRaises(installer.SetupError) as caught:
                    installer.self_update(args)
            failure = str(caught.exception)
            self.assertIn("returned non-zero exit status 2", failure)
            self.assertIn("error: failed to acquire uv tool lock", failure)
            metadata = json.loads((state / "auto_update.json").read_text())
            self.assertIn("returned non-zero exit status 2", metadata["error"])
            self.assertIn("error: failed to acquire uv tool lock", metadata["error"])
            log = (state / "auto_update.log").read_text(encoding="utf-8")
            self.assertIn("returned non-zero exit status 2", log)
            self.assertIn("error: failed to acquire uv tool lock", log)
            self.assertEqual(
                launcher.read_text(encoding="utf-8"), "#!/bin/sh\n# live launcher\n"
            )

    def test_subprocess_timeout_decodes_byte_stderr_into_the_error(self):
        """A timed-out command's raw byte stderr must become readable diagnostics.

        `subprocess.TimeoutExpired.stderr` is bytes even when `text=True` was
        requested, so failure here means a timeout raises AttributeError or
        renders as `b'...'`, hiding the cause from `auto_update.json` and the
        log.
        """
        script = (
            "import sys, time\n"
            "sys.stderr.write('timed out acquiring uv tool lock')\n"
            "sys.stderr.flush()\n"
            "time.sleep(5)\n"
        )
        with self.assertRaises(installer.SetupError) as caught:
            installer.subprocess_run([sys.executable, "-c", script], 1)
        failure = str(caught.exception)
        self.assertIn("timed out after 1s", failure)
        self.assertIn("timed out acquiring uv tool lock", failure)
        self.assertNotIn("b'", failure)

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

