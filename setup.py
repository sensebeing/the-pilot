"""Build wheels from the current source tree, never ignored staging residue.

Setuptools copies every file under ``build/lib`` into the wheel. A module that
was deleted from ``src`` but survives in the gitignored ``build/`` tree would
therefore be installed again, so the installed package tree never matches its
source checkout and the auto-updater reinstalls the same commit repeatedly.
"""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class _CleanBuildPy(_build_py):
    """Discard stale staging before copying the current package sources."""

    def run(self) -> None:
        shutil.rmtree(Path(self.build_lib), ignore_errors=True)
        super().run()


setup(cmdclass={"build_py": _CleanBuildPy})
