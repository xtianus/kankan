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

from kankan import boardconfig, boardsettings, cards, clock, labels, messages, model_catalog, modelcheck, procutil, seats, workflow
from kankan.adapters import Adapter, Classification, ModelEffort, classify, load_adapter, start_command
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


def _supervisor_pid() -> Optional[int]:
    """The parent, only if it is the kankan supervisor (never record e.g. the user's shell)."""
    parent = os.getppid()
    return parent if os.environ.get("KANKAN_SUPERVISOR_PID") == str(parent) else None


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
        self.next_lease_check = 0.0
        self.adapters = {}
        self.versions = {}
        self.version_due = {}
        self.version_known = set()
        self.catalogs = {}
        self.probes = {}
        self.restart = None
        self.running_pair = None
        self.spawning_pair = None
        self.start_deadline = None
        self.unsafe = False
        self.assignment_version_pair = None
        self.prepared = None  # claimed task whose activation has not succeeded yet (keeps its pinned pair)
        self.next_activation = 0.0
        self.next_flush = 0.0
        self.exit_now = False
        self.refresh_error = None
        self.recorded_checks = {}

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
        procutil.set_child_subreaper()
        snap = self.project.trello.snapshot(self.cfg.board_id)
        check_registry(self.cfg, snap.desc)
        check_lists(self.cfg, snap.lists)
        from kankan import worktrees
        main_wts = worktrees.main_checked_out(self.project.paths.repo, self.cfg.integration_branch)
        pids = {"launcher": os.getpid(), "supervisor": _supervisor_pid(), "sid": seats.pane_sid()}
        self.lease = seats.acquire(self.project, self.type, self.wanted_seat, pids=pids)
        self.project.reload_config()
        self.set_state('starting')
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
        update_state(self.project.paths, self.lease.seat, lambda state: (
            setattr(state, 'planned_restart', None), setattr(state, 'running_selection', None)))
        self.check_start_deadline = clock.monotonic() + self.adapter.model_check_timeout_seconds
        boardsettings.apply_read(self.project, boardsettings.from_snapshot(self.project, snap), boardsettings.adapters_for(self.project.paths))
        self.settings_step()

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

    def spawn_agent(self, pair: ModelEffort) -> None:
        worktree = self.project.paths.seat_worktree(self.lease.seat)
        seats.write_agent_file(self.project, self.lease.seat, self.lease.session, worktree)
        env = dict(os.environ)
        env.update(KANKAN_SEAT=self.lease.seat, KANKAN_SESSION=self.lease.session,
                   KANKAN_PROJECT=self.project.paths.project)
        from kankan.agents_cmd import agent_path
        env["PATH"] = agent_path(env.get("PATH", "/usr/bin:/bin"), self.project.paths)
        env.pop("KANKAN_SUPERVISOR_PID", None)
        launcher_pid = os.getpid()

        def child_setup():
            procutil.set_pdeathsig(signal.SIGKILL)
            if os.getppid() != launcher_pid:
                os._exit(1)
            for sig in (signal.SIGINT, signal.SIGTSTP, signal.SIGQUIT):
                signal.signal(sig, signal.SIG_DFL)

        command = start_command(self.adapter, pair)
        if self.pane and os.isatty(1):
            # The old agent has exited; discard its visible ready prompt before watching the replacement.
            os.write(1, b"\x1b[2J\x1b[H")
        self.agent = subprocess.Popen(command, cwd=str(worktree), env=env, preexec_fn=child_setup)
        self.spawning_pair = pair
        self.start_deadline = clock.monotonic() + self.adapter.start_timeout_seconds
        self.ready_since = self.unknown_since = None
        self.known_answered = None
        self.inactivity_reported = False
        seats.write_proc(self.project, self.lease.seat, agent=self.agent.pid)
        self.log("agent_started", pid=self.agent.pid, command=command)

    def own_children(self):
        objects = [self.agent, *(p.process for p in self.versions.values()), *(p.process for p in self.catalogs.values()),
                   *(p.process for _, p in self.probes.values())]
        return {p.pid for p in objects if p is not None} | modelcheck.poll_retired()

    def settings_step(self):
        self.adapters = boardsettings.adapters_for(self.project.paths)
        self.adapter = self.adapters[self.type]
        requests = boardsettings.poll(self.project, self.adapters)
        state = boardconfig.load(self.project.paths)
        values = boardconfig._values(state, boardconfig.schema(self.adapters))
        if not requests:
            requests = [boardconfig.CheckRequest(role, name, pair, state.settings.get(role, {}).get('revision', 0))
                        for role, value in values.items() for name, pair in value.items()]
        now = clock.monotonic()
        for name, pending in list(self.versions.items()):
            version = pending.poll()
            if version is not None:
                self.versions.pop(name)
                self.version_due[name] = now + self.adapters[name].model_check_retry_seconds
                if version:
                    self.version_known.add(name)
                    catalog = model_catalog.start_refresh(self.project.paths, name, version)
                    if catalog:
                        self.catalogs[name] = catalog
                else:
                    self.version_known.discard(name)
        for name, pending in list(self.catalogs.items()):
            if pending.poll():
                self.catalogs.pop(name)
                self.adapters[name] = load_adapter(self.project.paths, name, read_only=True)
        for name, adapter in self.adapters.items():
            if name not in self.versions and now >= self.version_due.get(name, 0):
                self.versions[name] = modelcheck.start_version(adapter)
        pair = values.get('implementation', {}).get(self.type)
        # A claim that needs a restart gets a fresh polled CLI version before it is eligible.
        if self.running_pair and self.state == 'idle':
            changed = tuple(sorted((r, p.model, p.effort) for r, value in values.items()
                                   if (p := value.get(self.type)) is not None and p != self.running_pair))
            if changed and changed != self.assignment_version_pair and self.type not in self.versions:
                self.assignment_version_pair = changed
                self.versions[self.type] = modelcheck.start_version(self.adapter)
        for key, (request, pending) in list(self.probes.items()):
            result = pending.poll()
            if result is not None:
                boardsettings.record_check(self.project, request, result)
                self.probes.pop(key)
        for request in requests:
            name = request.agent_type
            if name not in self.version_known or name in self.versions or name in self.catalogs:
                continue
            adapter = self.adapters[name]
            result = modelcheck.cached(adapter, request.pair)
            if result is None:
                self.record_cached(request, modelcheck.CheckResult('unchecked', modelcheck._version(adapter)), now)
            if result and result.status in ('passed', 'failed'):
                self.record_cached(request, result, now)
                continue
            key = (name, request.pair.model, request.pair.effort, modelcheck._version(adapter))
            if key not in self.probes:
                pending = modelcheck.start(adapter, request.pair)
                if pending:
                    self.probes[key] = (request, pending)
        if state.pending:
            boardsettings.drain_actions(self.project)

    def record_cached(self, request, result, now):
        """Record an unchanged cached result at most once per settings poll interval (each record takes the project lock)."""
        key = (request.role, request.agent_type, request.pair, request.revision)
        value = (result.status, result.version, result.message)
        previous = self.recorded_checks.get(key)
        if previous and previous[0] == value and now - previous[1] < self.cfg.settings_poll_seconds:
            return
        boardsettings.record_check(self.project, request, result)
        self.recorded_checks[key] = (value, now)

    def refresh_settings(self) -> None:
        """Reload local configuration and poll the board settings; an error never ends a running task."""
        try:
            self.project.reload_config()
            self.settings_step()
        except KankanError as err:
            if (err.status, err.message) != self.refresh_error:
                self.log("settings_refresh_failed", status=err.status.value, error=err.message)
            self.refresh_error = (err.status, err.message)
            if err.status == Status.CONFIG_ERROR:
                self.stop_after_task = True  # as a configuration error in a tick: stop once the seat is idle
            return
        self.refresh_error = None

    def startup_step(self, cls):
        if self.agent is None:
            roles = ['implementation', 'review']
            refs = workflow.card_refs(self.project)
            held = [v.meta.get('phase') for v in refs.values() if v.meta.get('owner') == self.lease.seat and v.meta.get('phase') in ('impl', 'review')]
            if held:
                roles = ['review' if 'review' in held else 'implementation']
            if self.type in self.version_known and self.type not in self.versions and self.type not in self.catalogs:
                for role in roles:
                    pair = boardconfig.effective(self.project.paths, role).get(self.type)
                    if pair and not boardsettings.role_block(self.project, self.type, role, pair):
                        self.spawn_agent(pair)
                        return
            pending = self.type in self.versions or self.type in self.catalogs or any(key[0] == self.type for key in self.probes)
            if not pending or clock.monotonic() >= self.check_start_deadline:
                self.failed_start('no verified model/effort combination is available')
            return
        if self.agent.poll() is not None:
            self.failed_start('agent exited before readiness')
        if self.start_deadline is not None and clock.monotonic() >= self.start_deadline:
            self.failed_start('agent did not become ready before its startup deadline')
        if cls.state == 'ready':
            self.agent_ready()

    def agent_ready(self):
        self.running_pair = self.spawning_pair
        update_state(self.project.paths, self.lease.seat, lambda state: (
            setattr(state, 'running_selection', self.running_pair.to_dict()), setattr(state, 'planned_restart', None)))
        state = load_state(self.project.paths, self.lease.seat)
        for card_id, causes in state.label_causes.items():
            if causes.get(self.cfg.label_error, {}).get('cause') == 'startup':
                try:
                    labels.clear_label(self.project, card_id, self.cfg.label_error, 'startup', self.lease.seat)
                except KankanError as err:
                    self.log("label_failed", error=err.message)
        self.set_state('idle')
        self.next_tick = 0.0

    def failed_start(self, reason, survivors=None):
        if survivors:
            self.mark_unavailable(survivors)
        prepared = self.prepared
        pair = ModelEffort(prepared['model'], prepared['effort']) if prepared else self.spawning_pair
        if prepared and pair:
            try:
                card = self.project.trello.card(prepared['card'])
                if card:
                    text = messages.MODEL_START_FAILED.format(type=self.type, model=pair.model, effort=pair.effort, reason=reason)
                    if self.cfg.label_error not in card.label_ids:
                        labels.set_label(self.project, card.id, self.cfg.label_error, 'startup', self.lease.seat,
                                         comment=text, op_id='startup-' + prepared['ref_oid'])
                    else:
                        self.project.trello.comment_once(card.id, text, 'startup-' + prepared['ref_oid'])
            except KankanError as err:
                self.log("label_failed", error=err.message)
        raise KankanError(Status.NEEDS_ACTION, messages.MODEL_START_UNAVAILABLE.format(reason=reason))

    def begin_restart(self, prepared):
        self.prepared = prepared
        self.restart = {'phase': 'exit', 'pid': self.agent.pid, 'start': procutil.start_time(self.agent.pid),
                        'targets': procutil.agent_tree(self.agent.pid, procutil.start_time(self.agent.pid),
                            keep=self.adapter.restart_keep, own=self.own_children())}
        update_state(self.project.paths, self.lease.seat, lambda state: setattr(state, 'planned_restart', dict(prepared)))
        self.set_state('starting')

    def restart_step(self, cls):
        restart = self.restart
        now = clock.monotonic()
        if restart['phase'] == 'ready':
            if self.agent.poll() is not None:
                self.failed_start('replacement agent exited before readiness')
            if now >= self.start_deadline:
                self.failed_start('replacement agent did not become ready before its startup deadline')
            if cls.state == 'ready':
                self.agent_ready()
                self.restart = None
                self.activate(cls)  # a transient failure keeps self.prepared; idle() retries it
                return True
            return False
        if self.agent.poll() is None:
            if restart['phase'] == 'exit':
                if self.can_write(cls):
                    self.paste(self.adapter.exit_command)
                    restart.update(phase='wait_exit', deadline=now+self.adapter.exit_timeout_seconds)
                return False
            if now >= restart.get('deadline', now+1):
                sig = signal.SIGKILL if restart['phase'] == 'term' else signal.SIGTERM
                if procutil.alive(restart['pid'], restart['start']):
                    os.kill(restart['pid'], sig)
                if restart['phase'] == 'kill':
                    self.failed_start('old agent survived termination', [restart['pid']])
                restart.update(phase='kill' if sig == signal.SIGKILL else 'term', deadline=now+(2 if sig == signal.SIGKILL else seats.KILL_GRACE))
            return False
        targets = dict(restart['targets'])
        targets.update(procutil.agent_tree(restart['pid'], restart['start'], keep=self.adapter.restart_keep, own=self.own_children()))
        live = [(pid, start) for pid, start in targets.items() if pid != restart['pid'] and procutil.alive(pid, start)]
        restart['targets'] = list(targets.items())
        if live:
            if restart['phase'] not in ('cleanup_term', 'cleanup_kill'):
                procutil._signal_all([pid for pid, _ in live], signal.SIGTERM)
                restart.update(phase='cleanup_term', deadline=now+seats.KILL_GRACE)
            elif now >= restart['deadline']:
                if restart['phase'] == 'cleanup_kill':
                    self.failed_start('agent descendants survived termination', [pid for pid, _ in live])
                procutil._signal_all([pid for pid, _ in live], signal.SIGKILL)
                restart.update(phase='cleanup_kill', deadline=now+2)
            return False
        pair = ModelEffort(self.prepared['model'], self.prepared['effort'])
        self.spawn_agent(pair)
        restart['phase'] = 'ready'
        return False

    def trust_allowed(self) -> bool:
        worktree = os.path.realpath(self.project.paths.seat_worktree(self.lease.seat))
        root = os.path.realpath(self.project.paths.worktree_root)
        return worktree.startswith(root + os.sep)

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        try:
            self.start()
            while True:
                if self.agent is not None and self.agent.poll() is not None and self.state != "starting":
                    self.log("agent_exited", code=self.agent.returncode)
                    return 0
                if self.step():
                    return 0
                clock.sleep(SCREEN_POLL)
        finally:
            self.shutdown()

    def mark_unavailable(self, survivors):
        from kankan.config import atomic_write_json
        data = seats.read_proc(self.project, self.lease.seat) or {}
        # A survivor without a start time has exited: recording it would let pid reuse pass for it.
        entries = [{'pid': pid, 'start': start} for pid in set(survivors) if (start := procutil.start_time(pid)) is not None]
        if not entries:
            return
        data['unavailable'] = entries
        atomic_write_json(self.project.paths.seats_dir / f'{self.lease.seat}.proc.json', data)
        self.unsafe = True

    def shutdown(self) -> None:
        for pending in [*self.versions.values(), *self.catalogs.values(), *(pending for _, pending in self.probes.values())]:
            pending.cancel()
        if self.lease is not None:
            # Stop and failed-start paths obey the same descendant invariant as a completed restart.
            pid = self.agent.pid if self.agent is not None else 0
            started = procutil.start_time(pid) if pid else 0
            survivors = procutil.terminate_agent_tree(pid, started, grace=seats.KILL_GRACE,
                keep=self.adapter.restart_keep, own=self.own_children())
            if self.agent is not None:
                self.agent.poll()  # Popen alone owns this exit status.
            sid = os.getsid(0)
            if sid == seats.pane_sid():
                survivors += procutil.terminate_session(sid, {os.getpid(), os.getppid()} | self.own_children(), grace=seats.KILL_GRACE)
            if survivors:
                self.log('agent_leftovers', pids=survivors)
                self.mark_unavailable(survivors)
            try:
                self.set_state("stopped")
                update_state(self.project.paths, self.lease.seat, lambda state: setattr(state, 'running_selection', None))
            except Exception:  # noqa: BLE001 - shutdown must release the seat flock
                pass
            seats.release(self.project, self.lease, retain=self.unsafe)
            self.lease = None


    def step(self) -> bool:
        """One pass: returns True when the launcher must exit."""
        if self.exit_now:
            return True
        self.refresh_settings()
        if self.lease_revoked():
            return True
        procutil.reap_adopted(self.own_children())
        if self.state == 'starting' and self.stop_requested():
            self.release_unsent(self.prepared)
            return True
        cls = self.screen() if self.agent is not None or self.state != 'starting' else Classification('unknown')
        dialog = self.answer_dialogs(cls)
        if self.state == "starting":
            if self.restart:
                self.restart_step(cls)
            else:
                self.startup_step(cls)
            return self.exit_now
        if dialog:
            self.periodic_board_check()
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

    def lease_revoked(self) -> bool:
        """Every poll interval: the seat ref must still name this session (``seat recover``, §15.4)."""
        now = clock.monotonic()
        if now < self.next_lease_check:
            return False
        self.next_lease_check = now + self.cfg.poll_seconds
        try:
            seats.verify_owner(self.project, self.lease.seat, self.lease.session)
        except KankanError:
            self.log("lease_revoked")
            return True
        return False

    def answer_dialogs(self, cls: Classification) -> bool:
        if cls.state == "trust":
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
            return self.exit_now
        if self.stop_requested():
            self.release_unsent(self.prepared)
            self.log("stop_honoured")
            return True
        if self.prepared is not None:
            # A claimed task never gets a new tick: it would be chosen again as a resume with the current pair.
            if clock.monotonic() >= self.next_activation:
                self.activate(cls)
            return self.exit_now
        now = clock.monotonic()
        if self.cfg.idle_exit_minutes and now - self.idle_since >= self.cfg.idle_exit_minutes * 60:
            self.log("idle_exit")
            return True
        if now < self.next_tick:
            return False
        self.next_tick = now + self.cfg.poll_seconds
        try:
            if self.type in self.versions or self.type in self.catalogs:
                self.next_tick = now  # Check again after the nonblocking preparation completes.
                return False
            task = workflow.tick(self.project, self.lease, defer_assignment=True)
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
        pair = ModelEffort(task['model'], task['effort'])
        self.prepared = task
        if pair != self.running_pair:
            self.begin_restart(task)
            return False
        self.activate(cls)
        return self.exit_now

    def activate(self, cls: Classification) -> None:
        """Activate ``self.prepared``, with the error handling of a tick (retry, exit on NOT_OWNER/CONFIG_ERROR)."""
        prepared = self.prepared
        try:
            task = workflow.activate_task(self.project, self.lease, prepared)
        except KankanError as err:
            self.log("activation_failed", status=err.status.value, error=err.message)
            if err.status in (Status.CONFIG_ERROR, Status.NOT_OWNER):
                self.stop_after_task = self.stop_after_task or err.status == Status.CONFIG_ERROR
                self.prepared = None
                self.exit_now = True
            else:
                self.next_activation = clock.monotonic() + self.cfg.poll_seconds
            return
        self.prepared = None
        if task is not None:
            self.assign(task, cls)
        elif self.stop_requested():
            self.release_unsent(prepared)

    def release_unsent(self, prepared: Optional[dict]) -> None:
        """A stop arrived before the prompt: give back a fresh, untouched claim (others stay held for a resume)."""
        self.prepared = None
        if not prepared:
            return
        try:
            if workflow.release_unsent(self.project, self.lease, prepared):
                self.log("stop_yield", card=prepared.get('card'))
        except KankanError as err:
            self.log("stop_yield_failed", status=err.status.value, error=err.message)

    def assign(self, task, cls):
        self.task = task
        self.ready_since = None
        self.unknown_since = None
        self.inactivity_reported = False
        self.pending_writes = [self.adapter.new_conversation, messages.PROMPT]
        self.flush_assignment(cls)

    def flush_assignment(self, cls: Classification) -> None:
        """Send the new-conversation command, then the prompt, each only when the pane is ready."""
        if not self.can_write(cls):
            return
        now = clock.monotonic()
        if now < self.next_flush:
            return
        stopping = self.stop_requested()
        try:
            active = None if stopping else workflow.refresh_task(self.project, self.lease, self.task)
        except KankanError as err:
            self.log('assignment_recheck_failed', error=err.message)
            if err.status not in (Status.CONFIG_ERROR, Status.NOT_OWNER):
                self.next_flush = now + self.cfg.poll_seconds  # retried at the tick's pace
                return
            self.stop_after_task = True
            active = None
        if active is None:
            if stopping:
                self.release_unsent(self.task)
            task_id = self.task.get('id') if self.task else None
            def cancel(state):
                if state.task and state.task.get('id') == task_id:
                    state.task = None
                    state.signal = None
            update_state(self.project.paths, self.lease.seat, cancel)
            self.pending_writes = []
            self.task = None
            self.state = 'idle'
            self.next_tick = 0.0
            return
        self.task = active
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
                self.waiting_saw_ready = False  # the agent usually keeps working a moment after asking
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
        """The request was pasted: remember its cause on the task (yield reads it) and clear it."""
        def clear(state):
            reason = (state.suspend_requested or {}).get("reason")
            if state.task is not None and reason:
                state.task["suspend_cause"] = reason
            state.suspend_requested = None
        update_state(self.project.paths, self.lease.seat, clear)

    def request_suspend(self, reason: str) -> None:
        if self.state not in ("working", "limited", "waiting_human"):
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
            boardsettings.apply_read(self.project, boardsettings.from_snapshot(self.project, snap), self.adapters or boardsettings.adapters_for(self.project.paths))
            boardsettings.drain_actions(self.project)
        except KankanError as err:
            if err.status == Status.CONFIG_ERROR:
                self.registry_lost()
            else:
                self.log("board_check_failed", error=err.message)
            return
        card = snap.cards.get(self.task["card"])
        if card is not None and self.cfg.label_hold in card.label_ids:
            self.request_suspend("hold")
        if self.state in ("working", "suspending", "limited", "waiting_human"):
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
        elif cls.state in ("working", "ready") and self.state == "limited":
            # ready too: the agent may resume and finish between two polls; unknown keeps it limited
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
            def clear(state):
                state.task = None
                state.pending_forward = None
                state.suspend_requested = None
            update_state(self.project.paths, self.lease.seat, clear)
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
