import subprocess
import sys
import venv
from pathlib import Path

import pytest

from tests.conftest import ROOT

pytestmark = pytest.mark.slow


def test_wheel_installs_and_runs_outside_source_tree(tmp_path):
    pytest.importorskip("setuptools")
    dist = tmp_path / "dist"
    build = subprocess.run([sys.executable, "-m", "pip", "wheel", str(ROOT), "-w", str(dist), "--no-deps",
                            "--no-build-isolation", "-q"], capture_output=True, text=True, cwd=tmp_path)
    assert build.returncode == 0, build.stderr
    wheel = next(dist.glob("kankan-*.whl"))
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    python = env_dir / "bin" / "python"
    install = subprocess.run([str(python), "-m", "pip", "install", "-q", "--no-index", "--no-deps", str(wheel)],
                             capture_output=True, text=True, cwd=tmp_path)
    assert install.returncode == 0, install.stderr
    kankan = env_dir / "bin" / "kankan"
    help_out = subprocess.run([str(kankan), "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert help_out.returncode == 0 and "init" in help_out.stdout and "supervisor" not in help_out.stdout
    screen = ROOT / "tests" / "fixtures" / "screens" / "ready__codex_ready.txt"
    check = subprocess.run([str(kankan), "adapter", "check", str(screen), "--json"], capture_output=True, text=True,
                           cwd=tmp_path)
    assert check.returncode == 0 and '"state": "ready"' in check.stdout
    files = subprocess.run([str(python), "-c", "import kankan, pathlib; p = pathlib.Path(kankan.__file__).parent; "
                            "print(all((p / f).exists() for f in ['adapters_default.json', 'skill/SKILL.md', "
                            "'hooks/pre-push', 'systemd/kankan-up@.service']))"], capture_output=True, text=True,
                           cwd=tmp_path)
    assert files.stdout.strip() == "True"
