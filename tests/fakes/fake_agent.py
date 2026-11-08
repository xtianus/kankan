#!/usr/bin/env python3
"""Scripted stand-in for Claude Code / Codex: a line-oriented TUI driven by a JSON scenario.

Usage: fake_agent.py --type=<claude|codex> --scenario <file.json>
(installed for tests as executables named ``claude`` and ``codex``).

Scenario: {"trust": bool, "log": path, "steps": [step, ...]}. Every received prompt (any line that
is not a new-conversation command) consumes the next step:
  {"do": [[argv...], ...], "shell": "bash text", "screen": "working|limit|known|unknown|ready",
   "sleep": seconds, "limit_text": "...", "spawn": "plain|setsid", "exit": true}
Commands run with the inherited environment (KANKAN_SEAT, KANKAN_SESSION...).
"""

import argparse
import json
import os
import subprocess
import sys
import time

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
    scenario = {}
    if args.scenario and os.path.exists(args.scenario):
        with open(args.scenario) as handle:
            scenario = json.load(handle)
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
    log("start", type=agent_type, pid=os.getpid(), cwd=os.getcwd(), seat=os.environ.get("KANKAN_SEAT"))
    if scenario.get("trust"):
        show(screens["trust"])
        line = sys.stdin.readline()
        log("trust_answer", line=line)
    show(screens["ready"])
    while True:
        line = sys.stdin.readline()
        if not line:
            log("eof")
            return 0
        text = line.rstrip("\n")
        log("input", line=text)
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
        run_step(step, screens, show, log)
        if step.get("exit"):
            log("exit")
            return int(step.get("exit_code", 0))
        show(screens["ready"])


def run_step(step, screens, show, log):
    """Show the step's screen, run its commands, wait, optionally read input."""
    screen = step.get("screen", "working")
    seconds = float(step.get("sleep", 0.5))
    if screen == "limit" and step.get("limit_text"):
        show(step["limit_text"])
    elif screen in screens:
        show(screens[screen].format(n=1))
    if step.get("spawn"):
        argv = ["sleep", "1000"]
        child = subprocess.Popen(argv, start_new_session=step["spawn"] == "setsid")
        log("spawned", pid=child.pid, mode=step["spawn"])
    for argv in step.get("do", []):
        proc = subprocess.run(argv, capture_output=True, text=True)
        log("command", argv=argv, code=proc.returncode, out=proc.stdout[-2000:], err=proc.stderr[-2000:])
    if step.get("shell"):
        proc = subprocess.run(["bash", "-c", step["shell"]], capture_output=True, text=True)
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
