#!/usr/bin/env python3
"""Scripted stand-in for Claude Code / Codex: a line-oriented TUI driven by a JSON scenario.

Usage: fake_agent.py --type=<claude|codex> --scenario <file.json>
(installed for tests as executables named ``claude`` and ``codex``).

Scenario: {"trust": bool, "log": path, "steps": [step, ...]}. Every received prompt (any line that
is not a new-conversation command) consumes the next step:
  {"do": [[argv...], ...], "shell": "bash text", "screen": "working|limit|known|unknown|ready",
   "sleep": seconds, "limit_text": "...", "spawn": "plain|setsid", "exit": true, "text": "..."}
Commands run with the inherited environment (KANKAN_SEAT, KANKAN_SESSION...). In ``do`` argv items and in
``shell``, ``{turn}`` is replaced by the Q&A turn token found in the received prompt (``turn `<token>```) and
``{file}`` by a temporary file holding the step's ``text`` (Q&A answers).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

TURN = re.compile(r"turn `([0-9a-f]{16})`")

SCREENS = {
    "claude": {
        "ready": "────────────────\n❯ ",
        "working": "✶ Working… ({n}s · ↓ 10 tokens · esc to interrupt)",
        "trust": (" Accessing workspace:\n\n Quick safety check: Is this a project you created or one you trust?\n\n"
                  " ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel"),
        "limit": "  ⎿  You've hit your session limit · resets 11pm",
        "unknown": "Press any key to continue...",
    },
    "codex": {
        "ready": "  ? for shortcuts\n› ",
        "working": "• Working ({n}s • esc to interrupt)",
        "trust": ("  Folder access\n\n  Trust this folder? Codex can read, edit, and run files here.\n\n"
                  "› 1. Trust and continue\n  2. Back to Agent Command Center\n\n  enter continue · esc back"),
        "limit": "■ You've hit your usage limit. Try again at 11:05 PM.",
        "unknown": "Press any key to continue...",
        "known": ("  Switch to gpt-mini for lower credit usage?\n\n› 1. Switch\n  2. Keep current model\n"
                  "  3. Keep current model (never show again)\n\n  enter select · esc back"),
        "new": ("  Where should the new conversation run?\n› 1. Use current Git worktree\n"
                "  2. Create new Git worktree\n  enter select · esc back"),
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", default=os.path.basename(sys.argv[0]))
    parser.add_argument("--scenario", default=os.environ.get("FAKE_AGENT_SCENARIO"))
    args, _ = parser.parse_known_args()
    agent_type = args.type if args.type in SCREENS else "claude"
    # Per seat: ``<FAKE_AGENT_SCENARIO_DIR>/<KANKAN_SEAT>.json`` when it exists (seats kankan starts itself, such as
    # the Q&A pool's members), otherwise the scenario of FAKE_AGENT_SCENARIO / --scenario.
    seat_dir, seat_name = os.environ.get("FAKE_AGENT_SCENARIO_DIR"), os.environ.get("KANKAN_SEAT")
    if seat_dir and seat_name and os.path.exists(os.path.join(seat_dir, f"{seat_name}.json")):
        args.scenario = os.path.join(seat_dir, f"{seat_name}.json")
    scenario = {}
    if args.scenario and os.path.exists(args.scenario):
        with open(args.scenario) as handle:
            scenario = json.load(handle)
    if agent_type in scenario and isinstance(scenario[agent_type], dict):
        scenario = dict(scenario, **scenario[agent_type])
    if '--version' in sys.argv:
        print(scenario.get('version', 'fake-v1'))
        return 0
    if '-p' in sys.argv or 'exec' in sys.argv:
        time.sleep(float(scenario.get('probe_sleep', 0)))
        print(scenario.get('probe_message', 'OK'))
        return int(scenario.get('probe_exit', 0))
    if 'debug' in sys.argv and 'models' in sys.argv:
        print(json.dumps(scenario.get('catalog', {'models': []})))
        return 0
    screens = SCREENS[agent_type]
    log_path = scenario.get("log") or os.environ.get("FAKE_AGENT_LOG")
    steps = list(scenario.get("steps", []))

    def log(event, **fields):
        if log_path:
            with open(log_path, "a") as handle:
                handle.write(json.dumps({"t": time.time(), "event": event, **fields}) + "\n")

    def show(text):
        sys.stdout.write("\x1b[2J\x1b[H" + text)
        if not text.endswith(" "):
            sys.stdout.write("\n")
        sys.stdout.flush()

    import signal

    def on_sigint(signum, frame):
        log("sigint")
    signal.signal(signal.SIGINT, on_sigint)
    log("start", type=agent_type, pid=os.getpid(), cwd=os.getcwd(), seat=os.environ.get("KANKAN_SEAT"), argv=sys.argv[1:])
    if scenario.get("trust"):
        show(screens["trust"])
        line = sys.stdin.readline()
        log("trust_answer", line=line)
    if scenario.get('startup_exit'):
        return int(scenario.get('startup_exit'))
    if scenario.get('ready_delay'):
        time.sleep(float(scenario['ready_delay']))
    show(screens['unknown'] if scenario.get('no_ready') else screens["ready"])
    while True:
        line = sys.stdin.readline()
        if not line:
            log("eof")
            return 0
        text = line.rstrip("\n")
        log("input", line=text)
        if text == '/exit':
            log('exit_command')
            if not scenario.get('ignore_exit'):
                return 0
            show(screens['ready'])
            continue
        if text in ("/clear", "/new"):
            if agent_type == "codex" and text == "/new":
                show(screens["new"])
                answer = sys.stdin.readline()
                log("new_dialog_answer", line=answer.rstrip("\n"))
            log("new_conversation")
            show(screens["ready"])
            continue
        if not text.strip():
            show(screens["ready"])
            continue
        step = steps.pop(0) if steps else {}
        status_line(scenario, log, step)  # Claude Code runs it after each request, also during a turn
        run_step(step, screens, show, log, text)
        status_line(scenario, log, step)
        if step.get("exit"):
            log("exit")
            return int(step.get("exit_code", 0))
        show(screens["ready"])


def status_line(scenario, log, step=None):
    """With ``--settings`` naming a status line command (kankan's cache reporter), run it as Claude Code does, with
    ``prompt_cache.expires_at`` = now + the step's or else the scenario's ``cache_seconds`` (default 3600; ``null``
    reports no cache)."""
    if "--settings" not in sys.argv:
        return
    try:
        settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
        command = settings["statusLine"]["command"]
    except (IndexError, ValueError, KeyError, TypeError):
        return
    seconds = (step or {}).get("cache_seconds", scenario.get("cache_seconds", 3600))
    cache = None if seconds is None else {"expires_at": int(time.time() + seconds), "ttl": "1h", "warm": True}
    proc = subprocess.run(["sh", "-c", command], input=json.dumps({"prompt_cache": cache}), capture_output=True,
                          text=True)
    log("status_line", code=proc.returncode, expires_at=cache and cache["expires_at"])


def substitute(step, prompt):
    """``{turn}``: the token named in ``prompt`` (empty when none); ``{file}``: a file holding ``text``."""
    found = TURN.search(prompt or "")
    values = {"{turn}": found.group(1) if found else ""}
    if "text" in step:
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        handle.write(step["text"])
        handle.close()
        values["{file}"] = handle.name

    def fill(item):
        for key, value in values.items():
            item = item.replace(key, value)
        return item
    return fill


def run_step(step, screens, show, log, prompt=""):
    """Show the step's screen, run its commands, wait, optionally read input."""
    fill = substitute(step, prompt)
    screen = step.get("screen", "working")
    seconds = float(step.get("sleep", 0.5))
    if screen == "limit" and step.get("limit_text"):
        show(step["limit_text"])
    elif screen in screens:
        show(screens[screen].format(n=1))
    if step.get("spawn"):
        argv = list(step.get("spawn_argv") or ["sleep", "1000"])
        child = subprocess.Popen(argv, start_new_session=step["spawn"] == "setsid")
        log("spawned", pid=child.pid, mode=step["spawn"])
    for argv in step.get("do", []):
        argv = [fill(item) for item in argv]
        proc = subprocess.run(argv, capture_output=True, text=True)
        log("command", argv=argv, code=proc.returncode, out=proc.stdout[-2000:], err=proc.stderr[-2000:])
    if step.get("shell"):
        proc = subprocess.run(["bash", "-c", fill(step["shell"])], capture_output=True, text=True)
        log("shell", code=proc.returncode, out=proc.stdout[-2000:], err=proc.stderr[-2000:])
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.1)
    if step.get("wait_input"):
        line = sys.stdin.readline()
        log("input", line=line.rstrip("\n"), during=screen)
    if step.get("after_screen"):
        show(screens[step["after_screen"]].format(n=2))
        time.sleep(float(step.get("after_sleep", 0)))


if __name__ == "__main__":
    sys.exit(main())
