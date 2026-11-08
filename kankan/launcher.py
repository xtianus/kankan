"""``kankan launcher``: holds the seat, starts the agent and drives its cycle (spec §8.3–§8.6).

The launcher shares the pane's terminal with the agent, so it never prints while the
agent runs: events go to the audit log. It is single-threaded (``preexec_fn`` is used
to prepare the agent process).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

from kankan import cards, clock, labels, messages, procutil, seats, workflow
from kankan.adapters import Adapter, Classification, classify, load_adapter
from kankan.config import check_lists, check_registry
from kankan.errors import KankanError, Status
from kankan.project import Project
from kankan.seats import Lease
from kankan.seatstate import load_state, update_state
from kankan.tmux import Tmux, session_name

STATES = ("starting", "idle", "working", "waiting_human", "limited", "suspending", "closing", "stopping")
SCREEN_POLL = 1.0
FATAL_EXIT = 3  # configuration problems: the supervisor does not restart the launcher


def _wall() -> float:
    return time.time()


class Launcher:
    def __init__(self, project: Project, adapter: Adapter, tmux: Tmux, agent_type: str, seat: Optional[str] = None):
        self.project = project
        self.adapter = adapter
        self.tmux = tmux
        self.type = agent_type
        self.wanted_seat = seat
        self.lease: Optional[Lease] = None
        self.agent: Optional[subprocess.Popen] = None
        self.pane: Optional[str] = os.environ.get("TMUX_PANE")
        self.session: Optional[str] = None
        self.state = "starting"
        self.prev_state: Optional[str] = None
        self.task: Optional[dict] = None
        self.pending_writes: list = []  # texts to paste, in order, for the current assignment
        self.next_tick = 0.0
        self.next_board_check = 0.0
        self.ready_since: Optional[float] = None
        self.unknown_since: Optional[float] = None
        self.inactivity_reported = False
        self.known_answered: Optional[str] = None
        self.trust_warned = False
        self.stop_after_task = False
        self.idle_since = clock.monotonic()
        self.resume_label: Optional[str] = None
        self.last_screen = ""

    # ------------------------------------------------------------------ helpers
    @property
    def cfg(self):
        return self.project.cfg

    def log(self, event: str, /, **fields) -> None:
        self.project.log(event, seat=self.lease.seat if self.lease else None, **fields)

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.log("launcher_state", old=self.state, new=state)
        self.state = state
        if self.lease:
            update_state(self.project.paths, self.lease.seat, lambda s: setattr(s, "launcher_state", state))

    def screen(self) -> Classification:
        text = self.tmux.capture(self.pane) if self.pane else ""
        self.last_screen = text
        return classify(self.adapter, text, self.cfg.tail_lines, clock.now())

    def quiet(self) -> bool:
        """No attached client typed during the last ``write_quiet_seconds``."""
        if not self.session:
            return True
        activity = self.tmux.client_activity(self.session)
        return activity is None or _wall() - activity >= self.cfg.write_quiet_seconds

    def can_write(self, cls: Classification, queue: bool = False) -> bool:
        ready = cls.state == "ready" or (queue and self.adapter.queue_while_working and cls.state == "working")
        return ready and self.quiet()

    def paste(self, text: str) -> None:
        self.tmux.paste(self.pane, text, self.adapter.submit_key)
        self.log("pane_write", text=text[:200])

    # ------------------------------------------------------------------ startup
    def start(self) -> None:
        """§8.3: checks, seat occupation, session rename, agent start."""
        snap = self.project.trello.snapshot(self.cfg.board_id)
        check_registry(self.cfg, snap.desc)
        check_lists(self.cfg, snap.lists)
        from kankan import worktrees
        main_wts = worktrees.main_checked_out(self.project.paths.repo, self.cfg.main_branch)
        pids = {"launcher": os.getpid(), "supervisor": os.getppid(), "sid": seats.pane_sid()}
        self.lease = seats.acquire(self.project, self.type, self.wanted_seat, pids=pids)
        self.project.reload_config()
        if main_wts:
            self.log("main_checked_out", paths=[str(p) for p in main_wts])
        self._resume_block_journal()
        if self.pane:
            current = self.tmux.run(["display-message", "-p", "-t", self.pane, "#{session_name}"]).stdout.strip()
            wanted = session_name("ai-", self.project.paths.project, self.lease.seat)
            if current != wanted:
                if self.tmux.has_session(wanted):
                    owner = self.tmux.get_option(wanted, "@kankan_repo")
                    raise KankanError(Status.CONFIG_ERROR, f"tmux session {wanted} already exists"
                                      + (f" for {owner}; set project_name in config.json" if owner else ""))
                self.tmux.rename_session(current, wanted)
            self.session = wanted
            self.tmux.set_option(wanted, "@kankan_repo", str(self.project.paths.repo))
        self._drop_interrupted_forward()
        update_state(self.project.paths, self.lease.seat, lambda s: setattr(s, "stop_requested", False))
        self.spawn_agent()

    def _resume_block_journal(self) -> None:
        state = load_state(self.project.paths, self.lease.seat)
        if state.block_journal:
            try:
                from kankan import integrate
                integrate.resume_block(self.project, self.lease)
            except KankanError as err:
                self.log("block_resume_failed", error=err.message)

    def _drop_interrupted_forward(self) -> None:
        """A forward recorded as ``sending`` may have reached the agent: never send it twice (§14.3)."""
        def drop(state):
            if state.pending_forward and state.pending_forward.get("status") == "sending":
                state.pending_forward = None
                return True
            return False
        if update_state(self.project.paths, self.lease.seat, drop):
            self.log("forward_dropped")

    def spawn_agent(self) -> None:
        worktree = self.project.paths.seat_worktree(self.lease.seat)
        env = dict(os.environ)
        env.update(KANKAN_SEAT=self.lease.seat, KANKAN_SESSION=self.lease.session,
                   KANKAN_PROJECT=self.project.paths.project)
        env.pop("KANKAN_SUPERVISOR_PID", None)
        launcher_pid = os.getpid()

        def child_setup():
            procutil.set_pdeathsig(signal.SIGKILL)
            if os.getppid() != launcher_pid:
                os._exit(1)
            for sig in (signal.SIGINT, signal.SIGTSTP, signal.SIGQUIT):
                signal.signal(sig, signal.SIG_DFL)

        self.agent = subprocess.Popen(self.adapter.command, cwd=str(worktree), env=env, preexec_fn=child_setup)
        seats.write_proc(self.project, self.lease.seat, agent=self.agent.pid)
        self.log("agent_started", pid=self.agent.pid, command=self.adapter.command)

    def trust_allowed(self) -> bool:
        worktree = os.path.realpath(self.project.paths.seat_worktree(self.lease.seat))
        root = os.path.realpath(self.project.paths.worktree_root)
        return worktree.startswith(root + os.sep)

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        try:
            self.start()
            while True:
                if self.agent.poll() is not None:
                    self.log("agent_exited", code=self.agent.returncode)
                    return 0
                if self.step():
                    return 0
                clock.sleep(SCREEN_POLL)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self.agent is not None and self.agent.poll() is None:
            self.agent.terminate()
            try:
                self.agent.wait(5)
            except subprocess.TimeoutExpired:
                self.agent.kill()
        sid = os.getsid(0)
        if self.lease is not None:
            if sid == seats.pane_sid():
                survivors = procutil.terminate_session(sid, {os.getpid(), os.getppid()}, grace=seats.KILL_GRACE)
                if survivors:
                    self.log("agent_leftovers", pids=survivors)
            try:
                self.set_state("stopped")
            except Exception:  # noqa: BLE001 - shutdown must release the seat
                pass
            seats.release(self.project, self.lease)
            self.lease = None

    def step(self) -> bool:
        """One pass: returns True when the launcher must exit."""
        cls = self.screen()
        if self.answer_dialogs(cls):
            return False
        if self.state == "starting":
            if cls.state == "ready":
                self.set_state("idle")
                self.next_tick = 0.0
            return False
        self.track_limit(cls)
        if self.state == "limited":
            return self.stop_requested() and not self.task
        if self.state == "idle":
            return self.idle(cls)
        self.periodic_board_check()
        if self.state in ("working", "suspending"):
            self.working(cls)
        elif self.state == "waiting_human":
            self.waiting_human(cls)
        elif self.state == "closing":
            self.closing(cls)
        return False

    def answer_dialogs(self, cls: Classification) -> bool:
        if cls.state == "trust":
            if self.state != "starting" and not self.trust_allowed():
                return True
            if not self.trust_allowed():
                if not self.trust_warned:
                    self.log("trust_not_answered", worktree=str(self.project.paths.seat_worktree(self.lease.seat)))
                    self.trust_warned = True
                return True
            if self.quiet() and self.known_answered != "trust":
                self.tmux.send_keys(self.pane, cls.answer or [])
                self.known_answered = "trust"
                self.log("trust_answered")
            return True
        if cls.state == "known":
            if self.quiet() and self.known_answered != cls.pattern:
                self.tmux.send_keys(self.pane, cls.answer or [])
                self.known_answered = cls.pattern
                self.log("dialog_answered", dialog=cls.pattern)
            return True
        self.known_answered = None
        return False

    def stop_requested(self) -> bool:
        return self.stop_after_task or load_state(self.project.paths, self.lease.seat).stop_requested

    # ------------------------------------------------------------------ idle
    def idle(self, cls: Classification) -> bool:
        if self.pending_writes:
            self.flush_assignment(cls)
            return False
        if self.stop_requested():
            self.log("stop_honoured")
            return True
        now = clock.monotonic()
        if self.cfg.idle_exit_minutes and now - self.idle_since >= self.cfg.idle_exit_minutes * 60:
            self.log("idle_exit")
            return True
        if now < self.next_tick:
            return False
        self.next_tick = now + self.cfg.poll_seconds
        try:
            task = workflow.tick(self.project, self.lease)
        except KankanError as err:
            self.log("tick_failed", status=err.status.value, error=err.message)
            if err.status == Status.CONFIG_ERROR:
                self.stop_after_task = True
                return True
            if err.status == Status.NOT_OWNER:
                return True
            return False
        if self.gc_due():
            self.gc_resume_labels()
        if task is None:
            return False
        self.task = task
        self.ready_since = None
        self.unknown_since = None
        self.inactivity_reported = False
        self.pending_writes = [self.adapter.new_conversation, messages.PROMPT]
        self.flush_assignment(cls)
        return False

    def flush_assignment(self, cls: Classification) -> None:
        """Send the new-conversation command, then the prompt, each only when the pane is ready."""
        if not self.can_write(cls):
            return
        text = self.pending_writes.pop(0)
        self.paste(text)
        if not self.pending_writes:
            self.set_state("working")
            self.working_since = clock.monotonic()
        else:
            clock.sleep(1.0)

    # ------------------------------------------------------------------ working
    def take_signal(self) -> Optional[dict]:
        def take(state):
            sig = state.signal
            if sig is None:
                return None
            state.signal = None
            return sig
        sig = update_state(self.project.paths, self.lease.seat, take)
        if sig is None:
            return None
        if not self.task or sig.get("task_id") != self.task["id"]:
            self.log("stale_signal_ignored", signal=sig, task=self.task["id"] if self.task else None)
            return None
        return sig

    def working(self, cls: Classification) -> None:
        sig = self.take_signal()
        if sig is not None:
            self.log("signal_received", kind=sig["kind"])
            if sig["kind"] in ("ask", "fail"):
                self.set_state("waiting_human")
                self.waiting_since = clock.monotonic()
            else:
                self.set_state("closing")
                self.closing_since = clock.monotonic()
            return
        if self.state == "working" and self.suspend_pending():
            if self.can_write(cls, queue=True):
                self.paste(messages.SUSPEND_REQUEST)
                self.clear_suspend_request()
                self.set_state("suspending")
                self.ready_since = None
            return
        self.check_inactivity(cls)

    def check_inactivity(self, cls: Classification) -> None:
        now = clock.monotonic()
        limit = self.cfg.idle_ready_minutes * 60 * (3 if self.state == "suspending" else 1)
        if cls.state == "ready":
            self.ready_since = self.ready_since or now
        else:
            self.ready_since = None
        if cls.state == "unknown":
            self.unknown_since = self.unknown_since or now
        else:
            self.unknown_since = None
        if self.inactivity_reported or not self.task:
            return
        message = None
        if self.ready_since and now - self.ready_since >= limit:
            message = messages.NO_SIGNAL
        elif self.unknown_since and now - self.unknown_since >= limit:
            message = messages.UNKNOWN_SCREEN.format(seat=self.lease.seat)
        if message:
            self.inactivity_reported = True
            try:
                labels.set_label(self.project, self.task["card"], self.cfg.label_error, "inactivity", self.lease.seat,
                                 comment=message, op_id=f"inactive-{self.task['id']}")
            except KankanError as err:
                self.log("label_failed", error=err.message)

    # suspension requests are recorded in seat state by HOLD/user-move detection
    def suspend_pending(self) -> bool:
        return bool(load_state(self.project.paths, self.lease.seat).suspend_requested)

    def clear_suspend_request(self) -> None:
        update_state(self.project.paths, self.lease.seat, lambda s: setattr(s, "suspend_requested", None))

    def request_suspend(self, reason: str) -> None:
        if self.state != "working" and self.state != "limited":
            return
        task_id = self.task["id"] if self.task else None

        def mark(state):
            if not state.suspend_requested:
                state.suspend_requested = {"task_id": task_id, "reason": reason}
        update_state(self.project.paths, self.lease.seat, mark)
        self.log("suspend_requested", reason=reason)

    # ------------------------------------------------------------------ board checks while busy
    def periodic_board_check(self) -> None:
        now = clock.monotonic()
        if now < self.next_board_check or not self.task:
            return
        self.next_board_check = now + self.cfg.poll_seconds
        try:
            snap = self.project.trello.snapshot(self.cfg.board_id)
            check_registry(self.cfg, snap.desc)
        except KankanError as err:
            if err.status == Status.CONFIG_ERROR:
                self.registry_lost()
            else:
                self.log("board_check_failed", error=err.message)
            return
        card = snap.cards.get(self.task["card"])
        if card is not None and self.cfg.label_hold in card.label_ids:
            self.request_suspend("hold")
        if self.state in ("working", "suspending", "limited"):
            self.check_user_move(snap)

    def check_user_move(self, snap) -> None:
        """§14.6: an in-progress card moved out of the seat's lists, archived or deleted by the user."""
        from kankan.refstore import card_ref
        value = self.project.refs.read(card_ref(self.task["card"]))
        if value is None or value.meta.get("owner") != self.lease.seat:
            return
        card = snap.cards.get(self.task["card"])
        meta = value.meta
        if card is not None and card.list_id in (meta.get("target_list"), meta.get("from_list")):
            return
        if card is None:
            card = self.project.trello.card(self.task["card"])
            if card is not None and not card.closed and card.list_id in (meta.get("target_list"), meta.get("from_list")):
                return
        self.request_suspend("user_moved")

    def registry_lost(self) -> None:
        if self.stop_after_task:
            return
        self.stop_after_task = True
        self.log("registry_lost")
        if self.task:
            try:
                self.project.trello.add_comment(self.task["card"], messages.REGISTRY_LOST.format(seat=self.lease.seat),
                                                f"registry-lost-{self.task['id']}")
            except KankanError as err:
                self.log("comment_failed", error=err.message)

    # ------------------------------------------------------------------ waiting for a human
    def waiting_human(self, cls: Classification) -> None:
        from kankan import forward
        forward.step(self, cls)

    # ------------------------------------------------------------------ limits
    def track_limit(self, cls: Classification) -> None:
        if cls.state == "limit" and self.state not in ("limited", "starting"):
            self.prev_state = self.state
            self.set_state("limited")
            if self.task and cls.resume_at is not None:
                try:
                    self.resume_label = labels.resume_label(self.project, cls.resume_at)
                    labels.set_label(self.project, self.task["card"], self.resume_label, "limit", self.lease.seat)
                except KankanError as err:
                    self.log("label_failed", error=err.message)
        elif cls.state == "working" and self.state == "limited":
            self.set_state(self.prev_state or "working")
            if self.task and self.resume_label:
                try:
                    labels.clear_label(self.project, self.task["card"], self.resume_label, "limit", self.lease.seat)
                except KankanError as err:
                    self.log("label_failed", error=err.message)
            self.resume_label = None
        if self.state == "limited":
            # HOLD while limited: queue the request, read on resumption (§9.4)
            self.periodic_board_check()

    def gc_due(self) -> bool:
        now = clock.monotonic()
        due = now >= getattr(self, "next_gc", 0.0)
        if due:
            self.next_gc = now + 600
        return due

    def gc_resume_labels(self) -> None:
        try:
            labels.gc_resume_labels(self.project)
        except KankanError as err:
            self.log("resume_gc_failed", error=err.message)

    # ------------------------------------------------------------------ closing
    def closing(self, cls: Classification) -> None:
        if self.task is not None:
            update_state(self.project.paths, self.lease.seat, lambda s: setattr(s, "task", None))
            self.task = None
            try:
                cards.converge(self.project, self.lease)
            except KankanError as err:
                self.log("converge_failed", error=err.message)
        if cls.state in ("ready", "unknown") or clock.monotonic() - self.closing_since > 60:
            self.idle_since = clock.monotonic()
            self.next_tick = 0.0
            self.set_state("idle")


def main(agent_type: str, seat: Optional[str]) -> int:
    for sig in (signal.SIGINT, signal.SIGTSTP, signal.SIGQUIT):
        signal.signal(sig, signal.SIG_IGN)
    project = Project.open(os.getcwd())
    adapter = load_adapter(project.paths, agent_type)
    launcher = Launcher(project, adapter, Tmux(), agent_type, seat)
    try:
        return launcher.run()
    except KankanError as err:
        project.log("launcher_error", status=err.status.value, error=err.message)
        print(f"kankan launcher: {err.status.value}: {err.message}", flush=True)
        return FATAL_EXIT if err.status in (Status.CONFIG_ERROR, Status.USAGE_ERROR) else 2
