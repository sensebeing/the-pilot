"""Project compilation and installed overlay lifecycle contracts.

resident-fact: settlement: installed overlays survive cwd changes and upgrades;
explicit universal selection and uninstall remove only the owned project link.
resident-fact: admission: invalid overlays cannot escape scene paths or write an
installation; project compilation is byte-identical to the pre-extraction owner.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from the_pilot.compiler import compile_bundle, merge_hooks_json
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

    def test_legacy_bytes_and_parallel_project_isolation(self):
        baseline = json.loads((FIXTURES / "legacy-compilation.json").read_text())
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
