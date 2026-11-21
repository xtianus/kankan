"""kankan starts its own processes with the installed kankan, never with a kankan/ package of the working directory."""

import shlex
import subprocess
import sys

import pytest

from kankan import __version__, agents_cmd, build, stopall, supervisor


@pytest.fixture
def decoy(tmp_path):
    """A directory holding a kankan/ package, like kankan's own repository where its agents run."""
    root = tmp_path / "repo"
    (root / "kankan").mkdir(parents=True)
    (root / "kankan" / "__init__.py").write_text("")
    (root / "kankan" / "__main__.py").write_text("print('decoy kankan')\n")
    (root / "kankan" / "cli.py").write_text("def main():\n    print('decoy kankan')\n    return 0\n")
    return root


def output(argv, cwd) -> str:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60).stdout.strip()


def test_python_m_imports_the_working_directory(decoy):
    """Why the bootstrap exists: `-m kankan` runs the decoy."""
    assert output([sys.executable, "-m", "kankan", "--version"], decoy) == "decoy kankan"


def test_command_ignores_the_working_directory(decoy):
    assert output(build.command("--version"), decoy) == f"kankan {__version__}"


def test_supervisor_and_launcher_use_the_bootstrap():
    assert agents_cmd.supervisor_argv("claude", "claude-1")[:3] == build.command()
    assert supervisor.launcher_argv("codex", None) == build.command("launcher", "--type=codex")


def test_agent_wrapper_ignores_the_working_directory(decoy, kproject):
    bin_dir = agents_cmd.agent_path("/usr/bin:/bin", kproject.paths).split(":")[0]
    assert output([f"{bin_dir}/kankan", "--version"], decoy) == f"kankan {__version__}"


def test_autostart_unit_ignores_the_working_directory(decoy, kproject):
    text = agents_cmd.unit_content(kproject, {"claude": 2}, qa="codex")
    line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    argv = shlex.split(line[len("ExecStart="):])
    assert argv[-3:] == ["up", "claude=2", "qa=codex"]
    assert output(argv[:-3] + ["--version"], decoy) == f"kankan {__version__}"
    assert not agents_cmd.unit_legacy(text)


def test_unit_pattern_reads_both_forms(kproject):
    unit = agents_cmd.unit_path(kproject)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(agents_cmd.unit_content(kproject, {"claude": 2}, qa="codex"))
    assert stopall._unit_args(kproject) == "claude=2 qa=codex"
    legacy = f"[Service]\nExecStart={sys.executable} -m kankan up codex=1\n"
    unit.write_text(legacy)
    assert stopall._unit_args(kproject) == "codex=1" and agents_cmd.unit_legacy(legacy)
