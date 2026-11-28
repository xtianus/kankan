"""``QaLauncher``: the launcher of the Q&A seat ``<type>-qa`` (questions T§4–T§5, F§3).

A task is a conversation on the active card of ``Q&A``; it is made of turns, each started by a prompt written to the
agent (the first prompt, a delivery of follow-ups, or the user typing in the pane) with its own turn token. ``answer``
and ``fail`` end a turn (state ``qa_waiting``); ``handoff`` and ``yield`` end the conversation, and so does the user
moving, archiving, deleting or holding the active card. The launcher never pastes comment texts: the agent reads them
with ``kankan task --json``.
"""

from __future__ import annotations

import json
import shlex
import time
from typing import Optional

from kankan.boardreader import LIVE, max_age
from kankan import boardconfig, boardsettings, clock, faults, labels, messages, procutil, qa, qa_answer, seats, workflow
from kankan import qapool
from kankan.qapool import clear_offer
from kankan.adapters import Classification, ModelEffort
from kankan.config import check_registry, qa_list_open
from kankan.errors import KankanError, Status
from kankan.launcher import Launcher, poll_done
from kankan.refstore import RefValue, card_ref
from kankan.seatstate import load_state, update_state

QA_STATES = ("starting", "idle", "working", "qa_waiting", "limited", "closing")
CLOSING_TIMEOUT = 60  # seconds closing waits for a "ready" pane before going idle without the clear
ERROR_CAUSES = ("fail", "inactivity")  # the Error label causes a delivery removes (questions F§3.4)


class QaListUnavailable(KankanError):
    """The ``Q&A`` list is missing or archived: the launcher exits with ``FATAL_EXIT`` (questions F§8.14)."""

    def __init__(self):
        super().__init__(Status.CONFIG_ERROR, messages.QA_LIST_UNAVAILABLE)


class QaLauncher(Launcher):
    PLANNED_RESTART_STATES = ("idle", "qa_waiting")  # a changed pair restarts the agent only between turns

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleared = False  # the agent's conversation was cleared since the last prompt
        self.idle_saw_ready = False
        self.pair_wait_logged = None
        self.held_logged = False
        self.next_list_check = 0.0
        self.router: Optional[qapool.Router] = None  # the routing round, for the router ``<type>-qa`` (qapool T§5)
        self.next_route = 0.0
        self.reset_conversation()

    def reset_conversation(self) -> None:
        """Forget the per-conversation state (a new claim, or after closing)."""
        self.turn: Optional[str] = None  # the token of the turn this launcher started last
        self.delivered = qa.Inputs()  # what this launcher run delivered in this conversation (T§5)
        self.thread = {}  # comment id -> Comment, read from the conversation's start
        self.cursor: Optional[str] = None
        self.card = None  # the active card at the last poll
        self.seen_attachments = frozenset()
        self.recompute = True  # recompute the candidates at the next poll
        self.followups: Optional[qa.Inputs] = None  # what a delivery would deliver now (qa.to_deliver)
        self.current = qa.Inputs()  # the uncovered candidates at the last poll (a turn typed in the pane)
        self.ended: Optional[str] = None  # why the conversation ended, once it has
        self.suspend_text_pending = False
        self.waiting_saw_ready = False
        self.next_qa_poll = 0.0
        self.end_pending: Optional[str] = None  # a handoff/yield signal whose end failed: retried
        self.next_end_retry = 0.0
        self.restart_wording = False  # taken up without a prompt: the next delivery uses QA_RESTART_FOLLOWUP
        self.clear_before_followup = False  # seat F§4: a silent start could not clear the agent's conversation

    # ------------------------------------------------------------------ hooks
    def acquire(self, pids: dict):
        """Occupy this launcher's own seat, the router or a member (qapool T§2): a closed member and any Q&A seat
        while the pool is stopping are refused by ``acquire_qa`` (``FATAL_EXIT``). The router then runs its start
        (qapool T§9): its own conversations completed and ended, the members whose launcher is gone removed, the
        title prefixes removed."""
        lease = seats.acquire_qa(self.project, self.type, pids, create=not seats.existing_only(),
                                 seat=self.wanted_seat)
        if not qapool.is_member(lease.seat):
            try:
                qapool.router_start(self.project, lease)
            except BaseException:
                seats.release(self.project, lease)
                raise
        return lease

    def own_roles(self):
        return ('questions',)

    def agent_command(self, pair: ModelEffort) -> list:
        """qapool T§3: with the ``claude_status_line`` reporter, Claude Code's status line is ``kankan statusline``,
        which records the cache expiry and shows the user's own status line."""
        command = super().agent_command(pair)
        if (self.adapter.prompt_cache or {}).get("reporter") == "claude_status_line":
            wrapper = self.project.paths.kankan_dir / "bin" / "kankan"
            command += ["--settings", json.dumps({"statusLine": {"type": "command",
                                                                 "command": f"{shlex.quote(str(wrapper))} statusline"}})]
        return command

    def spawn_agent(self, pair: ModelEffort) -> None:
        """A new agent process starts cold (qapool T§3): its last turn's end is forgotten."""
        update_state(self.project.paths, self.lease.seat, lambda state: setattr(state, "turn_ended_at", None))
        super().spawn_agent(pair)

    def record_turn_end(self) -> None:
        """The end of a turn (a signal accepted, or the agent ready again without one): ``turn_ended_at`` for the
        estimated cache expiry (qapool T§3), and the answer record gets the turn's inputs as delivered (T§5 step 3;
        inputs that arrived during the turn stay listed). A turn cut short by a killed agent never gets here."""
        now = time.time()
        update_state(self.project.paths, self.lease.seat, lambda state: setattr(state, "turn_ended_at", now))
        task = self.task
        if not task or not self.turn or not qa.valid_conversation(task.get("conversation")):
            return
        try:
            turn = qa.read_turn(self.project.paths, task["conversation"]) or {}
            if turn.get("token") == self.turn:
                qapool.turn_ended(self.project, task["card"], qa.Inputs.from_dict(turn.get("inputs")))
        except KankanError as err:
            self.log("qa_record_failed", card=task["card"], error=err.message)

    def startup_roles(self):
        return ['questions']  # no fallback to another role's pair (questions T§11)

    def release_on_stop(self, prepared: Optional[dict]) -> None:
        self.prepared = None  # the conversation is kept: it continues when the seat starts again

    def refresh_assignment(self, task: dict) -> Optional[dict]:
        """The conversation is still this seat's (no Trello read: the active-card poll sees the card)."""
        current = self.seat_task()
        value = self.project.refs.read(card_ref(task["card"]))
        if current is None or current.get("id") != task["id"] or current.get("ended") \
                or not qa.ours(value, self.lease.seat, task.get("conversation")):
            return None
        return current

    def seat_task(self) -> Optional[dict]:
        return load_state(self.project.paths, self.lease.seat).task

    # ------------------------------------------------------------------ main loop
    def step(self) -> bool:
        """One pass: returns True when the launcher must exit."""
        if self.exit_now:
            return True
        self.refresh_settings()
        if self.lease_revoked():
            return True
        procutil.reap_adopted(self.own_children())
        self.route_step()
        if self.state == 'starting' and self.stop_requested():
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
            return self.exit_now
        self.track_limit(cls)
        if self.state == "limited":
            return self.exit_now or ((self.stop_requested() or self.removal_drained()) and not self.task)
        if self.removal_drained():
            self.log("seat_drained")
            return True
        if self.state == "idle":
            return self.idle(cls)
        self.periodic_board_check()
        if self.state == "working":
            self.working(cls)
        elif self.state == "qa_waiting":
            self.qa_waiting(cls)
        if self.state == "closing":
            self.closing(cls)
        return self.exit_now

    def stop_requested(self) -> bool:
        """A stop request of the seat, or the pool's stopping mark (qapool T§9): ``Launcher.start`` clears a stale
        request, the mark stays until a start command."""
        return super().stop_requested() or qapool.stopping(self.project.paths)

    def start(self) -> None:
        """A stopped pool refuses the launcher on its local mark before any Trello read (qapool T§9); the occupation
        checks it again under the project lock."""
        if qapool.stopping(self.project.paths):
            router = seats.qa_seat_name(self.type)
            raise KankanError(Status.CONFIG_ERROR, messages.QA_POOL_STOPPING.format(router=router),
                              seats.POOL_REFUSED)
        super().start()

    def shutdown(self) -> None:
        """A graceful stop of the router writes the cold prefix on every card it recorded warm (qapool T§8); an
        immediate stop does not, and the next start removes the prefixes."""
        if self.is_router() and self.stop_requested():
            try:
                qapool.cold_all(self.project)
            except Exception as err:  # noqa: BLE001 - shutdown must release the seat
                self.log("qa_prefix_failed", error=str(err))
        super().shutdown()

    def is_router(self) -> bool:
        return self.lease is not None and not qapool.is_member(self.lease.seat)

    def own_children(self):
        return super().own_children() | (self.router.children() if self.router else set())

    def route_step(self) -> None:
        """The router runs the routing round every ``qa_poll_seconds`` in every state, also while its own agent
        answers or is limited (qapool T§5); a failure is logged and the round runs again at the next interval."""
        if not self.is_router() or self.running_pair is None:
            return  # the first round waits for the router's own agent: a cold router takes the first card
        now = clock.monotonic()
        if now < self.next_route:
            return
        self.next_route = now + self.cfg.qa_poll_seconds
        if self.router is None:
            self.router = qapool.Router(self.project, self.lease.seat, self.log)
        try:
            self.router.round()
        except KankanError as err:
            self.log("qa_route_failed", status=err.status.value, error=err.message)
        finally:
            poll_done(self, "route")

    def removal_drained(self) -> bool:
        """seat T§5: a Q&A seat being removed gracefully exits once no turn runs and no answer is pending; a pending
        answer is completed first (the take-up of ``idle`` does it), also after a launcher restart."""
        from kankan import seatremove
        if self.state not in ("idle", "qa_waiting", "limited") or self.removing_mode() != "graceful":
            return False
        return not seatremove.has_work(self.project, self.lease.seat)

    # ------------------------------------------------------------------ idle: claim the top card
    def idle(self, cls: Classification) -> bool:
        self.track_idle_pane(cls)
        if self.pending_writes:
            self.flush_assignment(cls)
            return self.exit_now
        if self.stop_requested():
            self.log("stop_honoured")
            return True
        now = clock.monotonic()
        if self.cfg.idle_exit_minutes and now - self.idle_since >= self.cfg.idle_exit_minutes * 60:
            self.log("idle_exit")
            return True
        if now < self.next_tick:
            return False
        self.next_tick = now + self.cfg.qa_poll_seconds
        if self.task is None:
            if self.type in self.versions or self.type in self.catalogs:
                self.next_tick = now  # check again after the nonblocking preparation completes
                return False
            pair = self.questions_pair()
            if pair is not None and pair != self.running_pair and self.agent is not None:
                self.plan_restart(pair)  # a held conversation is taken up once the new agent is ready
                return False
            held = self.held_conversation()
            if held is not None:
                self.resume(held, cls)
                return self.exit_now
            if pair != self.running_pair:
                if self.pair_wait_logged != pair:  # no claim before the questions pair is verified
                    self.log("qa_claim_waits_for_pair", model=pair.model if pair else None,
                             effort=pair.effort if pair else None)
                    self.pair_wait_logged = pair
                return False
            if self.take_offer(cls):  # qapool T§4
                return self.exit_now
            if not self.is_router():
                return False  # a member never scans Q&A: it takes the cards offered to it
            try:
                try:
                    task = self.tick()
                finally:
                    poll_done(self, "board")
            except QaListUnavailable:
                raise
            except KankanError as err:
                self.log("tick_failed", status=err.status.value, error=err.message)
                if err.status == Status.CONFIG_ERROR:
                    self.stop_after_task = True
                    return True
                return err.status == Status.NOT_OWNER
            if task is None:
                return False
            self.reset_conversation()
            self.task = task
        self.open_conversation(cls)
        return self.exit_now

    def tick(self) -> Optional[dict]:
        """One board read (B3) of the idle router: checks, settings and the unfinished handoff moves; the cards are
        given out by the routing round (qapool T§5), never claimed here."""
        cfg = self.cfg
        snap = self.project.board.snapshot(max_age(cfg.qa_poll_seconds))  # the claim re-reads live (board reader T§8)
        check_registry(cfg, snap.desc)
        self.check_qa_list(qa_list_open(cfg, snap.lists))
        boardsettings.apply_read(self.project, boardsettings.from_snapshot(self.project, snap),
                                 self.adapters or boardsettings.adapters_for(self.project.paths))
        boardsettings.drain_actions(self.project)
        refs = workflow.card_refs(self.project)
        self.converge_handoffs(refs)
        return None  # qapool T§5: the routing round gives the cards out, through offers

    def track_idle_pane(self, cls: Classification) -> None:
        """The clear sent by ``closing`` holds only while the idle pane stays "ready": once it leaves it (the user
        typed into it), the next claim sends the new-conversation command again."""
        if not self.cleared:
            self.idle_saw_ready = False
        elif cls.state == "ready":
            self.idle_saw_ready = True
        elif self.idle_saw_ready:
            self.cleared = False
            self.idle_saw_ready = False

    # ------------------------------------------------------------------ offers (qapool T§4)
    def take_offer(self, cls: Classification) -> bool:
        """Take the card the router offered this seat, between turns and once no answer is pending: an offer of the
        card it holds is dropped (its polling delivers the input); otherwise its conversation ends (the card stays in
        ``Q&A``) and it claims the offered card, whose first turn reads the whole thread; an offer that cannot be
        taken (the card left, got ``HOLD`` or another holder) is dropped. True when the offer was handled."""
        offer = load_state(self.project.paths, self.lease.seat).qa_offer
        if offer is None or self.state not in ("idle", "qa_waiting") or self.pending_writes:
            return False
        card_id = offer.get("card")
        if self.task is not None and self.task.get("card") == card_id:
            self.drop_offer(offer, "held")
            return True
        if self.task is not None and not self.ended and self.settle_pending() is not None:
            return False  # completed, still being posted or failed: the follow-ups change, taken at a later step
        try:
            card = self.project.board.card(card_id, LIVE)
            value = self.project.refs.read(card_ref(card_id))
        except KankanError as err:
            self.log("qa_offer_failed", card=card_id, status=err.status.value, error=err.message)
            return False
        if qa.active_ended(self.cfg, card) or not qa.claimable(value):
            self.drop_offer(offer, "unclaimable")
            return True
        if self.task is not None:
            task = self.task
            try:
                qa.end(self.project, self.lease, task, handed_off=self.handoff_done())
            except KankanError as err:
                self.log("qa_end_failed", status=err.status.value, error=err.message)
                return False
            self.log("qa_conversation_ended", card=task["card"], reason="switched", turn=self.turn)
            self.forget_task(task)
            self.cleared = False
        faults.point("after_old_conversation_ended")
        try:
            task = qa.claim(self.project, self.lease, card, self.running_pair)
        except KankanError as err:
            self.log("qa_offer_failed", card=card_id, status=err.status.value, error=err.message)
            return False
        if task is None:
            self.drop_offer(offer, "claim refused")
            return True
        clear_offer(self.project, self.lease.seat, offer.get("offer_id"))
        self.log("qa_offer_taken", card=card_id)
        self.reset_conversation()
        self.task = task
        self.set_state("idle")
        self.open_conversation(cls)
        return True

    def drop_offer(self, offer: dict, reason: str) -> None:
        clear_offer(self.project, self.lease.seat, offer.get("offer_id"))
        self.log("qa_offer_dropped", card=offer.get("card"), reason=reason)

    def forget_task(self, task: dict) -> None:
        """Clear the seat task of an ended conversation and the launcher's conversation state."""
        task_id = task["id"]

        def clear(state):
            if state.task and state.task.get("id") == task_id:
                state.task = None
            state.suspend_requested = None
        update_state(self.project.paths, self.lease.seat, clear)
        self.task = None
        self.pending_writes = []
        self.reset_conversation()

    # ------------------------------------------------------------------ restarts (T§4 Restart, board settings T§11)
    def questions_pair(self) -> Optional[ModelEffort]:
        """The verified ``questions`` pair of this seat's type; None while it is missing or unverified (there is no
        fallback to another role's pair)."""
        try:
            pair = boardconfig.effective(self.project.paths, "questions").get(self.type)
            if pair is None or boardsettings.role_block(self.project, self.type, "questions", pair):
                return None
        except KankanError as err:
            self.log("questions_pair_failed", status=err.status.value, error=err.message)
            return None
        return pair

    def plan_restart(self, pair: ModelEffort) -> None:
        """Restart the agent with ``pair`` (only from ``idle`` or ``qa_waiting``: never during a turn). With a
        conversation, ``prepared`` names its card and ref, so a replacement that cannot start sets ``Error`` on it
        as an agent dying at startup; the conversation is kept."""
        prepared = {"model": pair.model, "effort": pair.effort}
        if self.task:
            value = self.project.refs.read(card_ref(self.task["card"]))
            if value is not None:
                prepared.update(card=self.task["card"], ref_oid=value.oid)
        self.log("qa_restart_planned", model=pair.model, effort=pair.effort,
                 card=self.task["card"] if self.task else None)
        self.begin_restart(prepared)

    def after_restart_ready(self, cls: Classification) -> None:
        """The replacement agent is ready: take up the held conversation, if any, with the new pair."""
        self.prepared = None
        held = self.held_conversation()
        if held is not None:
            self.resume(held, cls)
        elif self.task is not None:
            self.to_closing()  # the conversation is no longer this seat's: forget it

    def held_conversation(self) -> Optional[RefValue]:
        """The ``qa`` ref this seat owns (its active conversation), if any."""
        for value in workflow.card_refs(self.project).values():
            if value.meta.get("phase") == "qa" and value.meta.get("owner") == self.lease.seat:
                return value
        return None

    def resume(self, value: RefValue, cls: Classification) -> None:
        """Take up the seat's conversation after a launcher start or an agent restart: a new task ID (``take_over``,
        which keeps this seat's base and reflog length), the pending answer or handoff completed first, then either
        a turn with the ``QA_RESTART`` prompt or, when the latest answer covers everything, ``qa_waiting`` without a
        prompt (questions T§4 Restart)."""
        try:
            task = qa.take_over(self.project, self.lease, value, self.running_pair)
        except KankanError as err:
            self.log("qa_take_over_failed", status=err.status.value, error=err.message)
            self.task = None  # taken up again at the next tick (NOT_OWNER: the ref changed, the next tick sees it)
            return
        self.reset_conversation()
        self.task = task
        self.log("qa_resumed", card=task["card"], conversation=task["conversation"], task=task["id"])
        try:
            kind = qa_answer.complete_pending(self.project, self.lease, task)
        except KankanError as err:
            self.log("qa_pending_failed", status=err.status.value, error=err.message)
            if err.status == Status.NOT_OWNER:
                self.end_conversation("ended", turn_running=False)
            else:
                self.task = None  # retried at the next tick
            return
        if kind is not None:
            self.drop_signal(task)  # the completed operation's signal belongs to no turn of this run
            if kind == "handoff":
                self.end_conversation("handoff", turn_running=False)
                return
        self.open_conversation(cls, restart=True)

    def drop_signal(self, task: dict) -> None:
        def drop(state):
            if state.signal and state.signal.get("task_id") == task["id"]:
                state.signal = None
        update_state(self.project.paths, self.lease.seat, drop)

    def check_qa_list(self, open_: bool) -> None:
        if not open_:
            self.log("qa_list_unavailable", list=self.cfg.qa_list)
            raise QaListUnavailable()

    def converge_handoffs(self, refs) -> None:
        """Complete the moves of handed-off cards (``parked`` refs still holding ``from_list``), one ref per
        project lock hold: one card read, the move and its verification read (T§3)."""
        from kankan import cards
        from kankan.config import project_lock
        for card_id, value in refs.items():
            meta = value.meta
            if meta.get("phase") != "parked" or meta.get("cause") != "handoff" or not meta.get("from_list"):
                continue
            ref = card_ref(card_id)
            try:
                with project_lock(self.project.paths):
                    current = self.project.refs.read(ref)
                    if current is None or current.oid != value.oid:
                        continue
                    event = cards.converge_ref(self.project, self.lease, ref, current,
                                               self.project.board.card(card_id, LIVE))
                if event:
                    self.log("qa_handoff_converged", card=card_id, result=event)
            except KankanError as err:
                self.log("converge_failed", card=card_id, error=err.message)

    def open_conversation(self, cls: Classification, restart: bool = False) -> None:
        """The first turn of a claimed conversation: read the card and its whole thread, save the turn, then queue
        the new-conversation command (unless just cleared) and the prompt. With ``restart`` (a taken-up
        conversation, in a fresh agent), the turn's inputs are the uncovered candidates (those of a first turn when
        neither ``turn.json`` nor ``covered`` exists) and the prompt is ``QA_RESTART``; when an answer completed and
        nothing is uncovered, the launcher waits for follow-ups without a prompt (questions T§4 Restart)."""
        task = self.task
        listed = self.listed_inputs(task["card"])  # before the live reads (qapool T§5 step 3, reconciling)
        try:
            card = qa.card_state(self.project, task["card"])
            if qa.active_ended(self.cfg, card):
                self.end_conversation("card_left", turn_running=False)
                return
            comments = self.project.board.thread(card.id, LIVE)
            cands = qa.candidates(self.project, card, comments) if restart else None
        except KankanError as err:
            self.log("qa_open_failed", status=err.status.value, error=err.message)
            if restart:
                self.task = None  # taken up again at the next tick
            return  # retried at the next tick
        self.thread = {c.id: c for c in comments}
        self.cursor = max((c.date for c in comments), default="")  # a comment point: a date (board reader T§3)
        self.card = card
        self.seen_attachments = frozenset(a.id for a in card.attachment_items)
        if restart:
            value = self.project.refs.read(card_ref(task["card"]))
            ref_covered = value.meta.get("covered") if value else None
            turn = qa.read_turn(self.project.paths, task["conversation"])
            context = None
            if turn is None and not ref_covered:  # a crash between the claim and the first turn
                inputs, context, answered = qa.first_turn(self.project, card, comments)
                self.reconcile_record(card, comments, listed, cands, inputs)
                if answered and not inputs.comments and not inputs.attachments:
                    self.start_silently(cls, task, context)
                    return
            else:
                inputs = qa.uncovered(cands, qa.effective_covered(ref_covered, turn))
            self.reconcile_record(card, comments, listed, cands, inputs)
            nothing_new = not inputs.comments and not inputs.attachments
            if nothing_new and (ref_covered or self.turn_ended_before(task["card"])):
                self.wait_after_restart(task)  # a failed turn counts as delivered: never asked again
                return
            token = self.begin_turn(inputs, context)
            if token is None:
                return
            self.pending_writes = [messages.QA_RESTART.format(turn=token)]
        else:
            inputs, context, answered = qa.first_turn(self.project, card, comments)
            self.reconcile_record(card, comments, listed, qa.candidates(self.project, card, comments), inputs)
            if answered and not inputs.comments and not inputs.attachments:
                self.start_silently(cls, task, context)
                return
            token = self.begin_turn(inputs, context)
            if token is None:
                return
            self.pending_writes = ([] if self.cleared else [self.adapter.new_conversation]) \
                + [messages.QA_PROMPT.format(turn=token)]
        self.flush_assignment(cls)

    def turn_ended_before(self, card_id: str) -> bool:
        """The answer record has a turn on the card that ended (its description delivered), as opposed to a turn cut
        short by a killed agent, which updated nothing (qapool T§5 step 3)."""
        entry = qapool.load_record(self.project.paths).get(card_id) or {}
        return bool((entry.get("delivered") or {}).get("description"))

    def listed_inputs(self, card_id: str) -> set:
        """The IDs the answer record lists as inputs of the card."""
        return {item["id"] for item in (qapool.load_record(self.project.paths).get(card_id) or {}).get("inputs") or []}

    def reconcile_record(self, card, comments, listed: set, cands: qa.Inputs, inputs: qa.Inputs) -> None:
        """qapool T§5 step 3: the entries listed before the live reads that the reads no longer have (deleted by
        the user) leave the record, and so do those the first turn counts as context, never delivered by a turn."""
        context = [c for c in cands.comments if c not in inputs.comments] \
            + [a for a in cands.attachments if a not in inputs.attachments]
        try:
            dropped = qapool.reconcile(self.project, card.id, listed, [c.id for c in comments],
                                       [a.id for a in card.attachment_items], context_ids=context)
        except KankanError as err:
            self.log("qa_record_failed", card=card.id, error=err.message)
            return
        if dropped:
            self.log("qa_record_reconciled", card=card.id, dropped=dropped)

    def start_silently(self, cls: Classification, task: dict, context: qa.Inputs) -> None:
        """seat F§4: a card taken again whose latest answer is complete and has nothing new waits without a
        prompt, as after a restart; its old thread is recorded as covered, and the agent's previous conversation is
        cleared now, or before the next follow-up when the pane cannot be written yet."""
        try:
            qa.record_covered(self.project, self.lease, task, context)
        except KankanError as err:
            self.log("qa_record_covered_failed", status=err.status.value, error=err.message)
            if err.status == Status.NOT_OWNER:
                self.end_conversation("ended", turn_running=False)
            else:
                self.task = None  # taken up again at the next tick
            return
        self.wait_after_restart(task)
        if not self.cleared:
            if self.can_write(cls):
                self.paste(self.adapter.new_conversation)
                self.cleared = True
            else:
                self.clear_before_followup = True

    def wait_after_restart(self, task: dict) -> None:
        """A taken-up conversation whose latest answer covers everything: no prompt and no comment; the next
        delivery tells the fresh agent to read the whole thread (``QA_RESTART_FOLLOWUP``)."""
        self.turn = task.get("turn")
        self.restart_wording = True
        self.pending_writes = []
        self.log("qa_resumed_waiting", card=task["card"], conversation=task["conversation"])
        self.turn_over()

    def flush_assignment(self, cls: Classification) -> None:
        if self.task is not None and self.stop_requested():
            # A stop before the prompt keeps the conversation and its seat task (base, reflog): the next start
            # takes it up, and its restart turn delivers what this turn would have.
            self.log("qa_stop_before_prompt", card=self.task["card"])
            self.pending_writes = []
            self.task = None
            self.reset_conversation()
            self.set_state("idle")
            self.next_tick = 0.0
            return
        super().flush_assignment(cls)
        if self.state == "working":
            self.cleared = False

    # ------------------------------------------------------------------ turns
    def begin_turn(self, inputs: qa.Inputs, context: Optional[qa.Inputs] = None) -> Optional[str]:
        """Start a turn (``turn.json``, then the seat task's ``turn``) before its prompt is written; None when it
        cannot start (the conversation ended: it is ended here too)."""
        try:
            token = qa.start_turn(self.project, self.lease, self.task, inputs, context)
        except KankanError as err:
            self.log("qa_turn_failed", status=err.status.value, error=err.message)
            if err.status == Status.NOT_OWNER:
                self.end_conversation("ended", turn_running=False)
            return None
        self.turn = token
        self.task = dict(self.task, turn=token)
        old = self.delivered
        self.delivered = qa.Inputs(old.comments + [c for c in inputs.comments if c not in old.comments],
                                   old.attachments + [a for a in inputs.attachments if a not in old.attachments])
        self.followups = None
        self.inactivity_reported = False
        self.ready_since = self.unknown_since = None
        return token

    def take_signal(self) -> Optional[dict]:
        """The signal of the current task and turn; a signal of an older turn is dropped (T§4)."""
        sig = super().take_signal()
        if sig is None:
            return None
        turn = (sig.get("payload") or {}).get("turn")
        if turn != self.turn:
            self.log("stale_signal_ignored", signal=sig, turn=self.turn)
            return None
        return sig

    def on_signal(self, sig: dict) -> None:
        kind = sig["kind"]
        self.log("signal_received", kind=kind, turn=self.turn)
        self.record_turn_end()
        if kind in ("answer", "fail") and not self.ended:
            self.turn_over()
        elif self.ended:
            self.to_closing()
        else:
            self.end_pending = kind
            self.end_by_signal()

    def end_by_signal(self) -> None:
        """End the conversation after ``handoff`` or ``yield``; ``closing`` only once the end succeeded (a transient
        failure is retried every ``qa_poll_seconds``)."""
        self.next_end_retry = clock.monotonic() + self.cfg.qa_poll_seconds
        self.end_conversation(self.end_pending, turn_running=False)
        if self.ended:
            self.end_pending = None
            self.to_closing()

    def retry_end(self) -> bool:
        """True while an end requested by a signal is still pending."""
        if not self.end_pending or self.ended:
            return False
        if clock.monotonic() >= self.next_end_retry:
            self.end_by_signal()
        return True

    def turn_over(self) -> None:
        """``answer``, ``fail`` or a no-signal ``Error``: wait for follow-ups, polled at once."""
        self.set_state("qa_waiting")
        self.waiting_saw_ready = False
        self.followups = None
        self.recompute = True
        self.next_qa_poll = 0.0

    # ------------------------------------------------------------------ working
    def working(self, cls: Classification) -> None:
        sig = self.take_signal()
        if sig is not None:
            self.on_signal(sig)
            return
        if self.retry_end():
            return
        if self.ended:
            self.finish_ended_turn(cls)
            return
        self.check_inactivity(cls)

    def finish_ended_turn(self, cls: Classification) -> None:
        """The conversation ended during the turn: ask the agent to ``yield``, then clear once it is done (its
        ``yield`` signal, or a "ready" pane for the inactivity delay)."""
        if self.suspend_text_pending:
            if self.can_write(cls, queue=True):
                self.paste(messages.QA_SUSPEND)
                self.suspend_text_pending = False
                self.ready_since = None
            return
        now = clock.monotonic()
        self.ready_since = (self.ready_since or now) if cls.state == "ready" else None
        if self.ready_since and now - self.ready_since >= self.cfg.idle_ready_minutes * 60:
            self.to_closing()

    def request_suspend(self, reason: str) -> None:
        """The conversation ended during a turn: ask the agent to ``yield`` (``QA_SUSPEND``, without the "commit
        WIP" text of the work seats)."""
        self.suspend_text_pending = True
        self.log("suspend_requested", reason=reason)

    def report_inactivity(self, message: str) -> None:
        """``Error`` once per turn (the op-id carries the turn token); a stop without a signal ends the turn. A turn
        whose ``handoff`` completed is no stop without a signal: the conversation is over."""
        if self.handoff_done():
            self.end_conversation("handoff", turn_running=False)
            return
        no_signal = message == messages.NO_SIGNAL
        if no_signal and self.settle_pending() not in (None, "failed"):
            return  # completed (an answer ends the turn, a handoff the conversation), still being posted, or ended
        self.inactivity_reported = True
        try:
            labels.set_label(self.project, self.task["card"], self.cfg.label_error, "inactivity", self.lease.seat,
                             comment=messages.QA_NO_SIGNAL if no_signal else message,
                             op_id=f"inactive-{self.task['id']}-{self.turn}")
        except KankanError as err:
            self.log("label_failed", error=err.message)
        if no_signal:
            self.record_turn_end()
            self.turn_over()

    def settle_pending(self) -> Optional[str]:
        """Complete the current turn's pending answer or handoff that its command left unfinished (questions T§4):
        ``"answer"`` (the turn is answered: ``qa_waiting``), ``"handoff"`` (the conversation is over), ``"busy"`` (a
        command still holds the conversation lock and is posting: check again later), ``"ended"`` (the conversation
        ended meanwhile), ``"failed"`` (retried later), or None when nothing was pending."""
        task = self.task
        try:
            kind = qa_answer.complete_pending(self.project, self.lease, task, lock_timeout=0)
        except qa.LockBusy:
            return "busy"
        except KankanError as err:
            self.log("qa_pending_failed", status=err.status.value, error=err.message)
            if err.status == Status.NOT_OWNER:
                self.end_conversation("ended", turn_running=False)
                return "ended"
            return "failed"
        if kind is None:
            return None
        self.drop_signal(task)  # handled here: the signal of the completed operation is not taken again
        self.log("qa_pending_settled", card=task["card"], kind=kind, turn=self.turn)
        self.record_turn_end()
        if kind == "handoff":
            self.end_conversation("handoff", turn_running=False)
        else:
            self.turn_over()
        return kind

    # ------------------------------------------------------------------ waiting for follow-ups
    def qa_waiting(self, cls: Classification) -> None:
        sig = self.take_signal()
        if sig is not None:
            self.on_signal(sig)
            return
        if self.retry_end():
            return
        if self.ended:
            self.to_closing()
            return
        if self.stop_requested():  # honoured between turns; the conversation is kept
            self.log("stop_honoured", card=self.task["card"] if self.task else None)
            self.exit_now = True
            return
        self.check_list_while_waiting()
        if self.exit_now:
            return
        if self.type not in self.versions and self.type not in self.catalogs:
            pair = self.questions_pair()
            if pair is not None and pair != self.running_pair and self.agent is not None:
                self.plan_restart(pair)
                return
            # qapool T§4: a known follow-up of the held card is delivered before any offer is taken
            if pair == self.running_pair and self.followups is None and self.take_offer(cls):
                return
        if cls.state == "ready":
            self.waiting_saw_ready = True
        elif cls.state == "working" and self.waiting_saw_ready:
            self.typed_turn()
            return
        if self.followups is not None:
            self.deliver(cls)

    def check_list_while_waiting(self) -> None:
        """An archived ``Q&A`` list stops the seat between turns (``FATAL_EXIT``); the conversation is kept."""
        now = clock.monotonic()
        if now < self.next_list_check:
            return
        self.next_list_check = now + self.cfg.qa_poll_seconds
        try:
            info = self.project.board.list_info(self.cfg.qa_list, max_age(self.cfg.qa_poll_seconds)) \
                if self.cfg.qa_list else None
        except KankanError as err:
            self.log("qa_list_check_failed", error=err.message)
            return
        self.check_qa_list(info is not None and not info.closed)

    def deliver(self, cls: Classification) -> None:
        """Deliver the follow-ups: the base pane write rules, the turn saved before the prompt, then ``FORWARDED``
        and the removal of the ``Error`` of the conversation's ``fail`` or inactivity (T§5)."""
        inputs = self.followups
        if inputs is None or not self.can_write(cls):
            return
        settled = self.settle_pending()
        if settled == "failed":
            self.followups = None  # retried with the next poll
            self.recompute = True
        if settled is not None:
            return  # what was completed changes the follow-ups: recomputed at the next poll, or still posting
        if self.clear_before_followup:  # seat F§4: the previous card's conversation goes first
            self.paste(self.adapter.new_conversation)
            self.clear_before_followup = False
            self.waiting_saw_ready = False  # the pane working on the clear is not the user typing a turn
            return  # the follow-up is delivered at the next poll
        inputs = self.live_followups(inputs)
        if inputs is None:
            return
        token = self.begin_turn(inputs)
        if token is None:
            return
        template = messages.QA_RESTART_FOLLOWUP if self.restart_wording else messages.QA_FOLLOWUP
        self.paste(template.format(counts=self.counts(inputs), turn=token))
        self.restart_wording = False
        self.set_state("working")
        self.cleared = False
        try:
            self.project.trello.add_comment(self.task["card"], messages.FORWARDED, f"forwarded-{token}")
        except KankanError as err:
            self.log("comment_failed", error=err.message)
        self.clear_errors()
        self.log("qa_delivered", card=self.task["card"], turn=token, comments=len(inputs.comments),
                 attachments=len(inputs.attachments))

    def live_followups(self, inputs: qa.Inputs) -> Optional[qa.Inputs]:
        """qapool T§5 step 3, reconciling before a follow-up: the card and its thread read live; the comments and
        attachments deleted since they were found (the comment log keeps them) leave the follow-ups, this run's
        thread and the answer record. None when nothing remains (no follow-up is sent) or the reads failed (the
        follow-ups are recomputed at the next poll)."""
        card_id = self.task["card"]
        listed = self.listed_inputs(card_id)
        try:
            card = qa.card_state(self.project, card_id, LIVE)
            comments = self.project.board.thread(card_id, LIVE)
        except KankanError as err:
            self.log("qa_poll_failed", status=err.status.value, error=err.message)
            self.followups = None
            self.recompute = True
            return None
        self.card = card
        self.thread = {c.id: c for c in comments}
        self.seen_attachments = frozenset(a.id for a in card.attachment_items)
        kept = qa.Inputs([c for c in inputs.comments if c in self.thread],
                         [a for a in inputs.attachments if a in self.seen_attachments], inputs.dates)
        self.reconcile_record(card, comments, listed, kept, kept)
        if kept.comments or kept.attachments:
            return kept
        self.log("qa_followups_deleted", card=card_id)
        self.followups = None
        return None

    def typed_turn(self) -> None:
        """The user typed in the pane (§8.5 detection): a turn whose inputs are the current candidates."""
        if self.settle_pending() is not None:
            return
        token = self.begin_turn(self.current)
        if token is None:
            return
        self.set_state("working")
        self.cleared = False
        self.clear_errors()
        self.log("qa_typed_turn", card=self.task["card"], turn=token)

    def counts(self, inputs: qa.Inputs) -> str:
        parts = []
        if inputs.comments:
            n = len(inputs.comments)
            parts.append(messages.QA_COUNT_COMMENTS[n != 1].format(n=n))
        if inputs.attachments:
            n = len(inputs.attachments)
            by_id = {a.id: a for a in (self.card.attachment_items if self.card else [])}
            names = ", ".join(f"`{_safe_name(by_id[a].name or by_id[a].file_name)}`" if a in by_id else f"`{a}`"
                              for a in inputs.attachments)
            parts.append(messages.QA_COUNT_ATTACHMENTS[n != 1].format(n=n, names=names))
        return ", ".join(parts)

    def clear_errors(self) -> None:
        for cause in ERROR_CAUSES:
            try:
                labels.clear_label(self.project, self.task["card"], self.cfg.label_error, cause, self.lease.seat)
            except KankanError as err:
                self.log("label_failed", error=err.message)

    # ------------------------------------------------------------------ active-card poll
    def periodic_board_check(self) -> None:
        """Replaced by the active-card poll: no board read (B3) during a conversation."""
        due = self.next_qa_poll
        self.poll_active()
        if self.next_qa_poll != due:
            poll_done(self, "card")

    def poll_active(self) -> None:
        """Every ``qa_poll_seconds``, without the project lock: one card read (C1) and the comments since the
        cursor (M2). A card gone from ``Q&A``, archived, deleted or on ``HOLD`` ends the conversation; otherwise
        the follow-ups to deliver are recomputed when something changed."""
        if not self.task or self.ended:
            return
        now = clock.monotonic()
        if now < self.next_qa_poll:
            return
        self.next_qa_poll = now + self.cfg.qa_poll_seconds
        task = self.task
        try:
            card = qa.card_state(self.project, task["card"], max_age(self.cfg.qa_poll_seconds))
            if qa.active_ended(self.cfg, card):  # ending deletes the conversation: confirm it live first
                card = qa.card_state(self.project, task["card"], LIVE)
            if qa.active_ended(self.cfg, card):
                self.end_conversation(_end_reason(self.cfg, card), turn_running=self.state != "qa_waiting")
                return
            new, point = self.project.board.new_comments(card.id, self.cursor or "", max_age(self.cfg.qa_poll_seconds))
        except KankanError as err:
            self.log("qa_poll_failed", status=err.status.value, error=err.message)
            return
        self.card = card
        for comment in new:
            self.thread[comment.id] = comment
        self.cursor = point  # also with no comment of this card: served by the comment log (board reader T§7)
        attachments = frozenset(a.id for a in card.attachment_items)
        recorded = self.recorded_followups(task["card"])
        if not (new or attachments != self.seen_attachments or self.recompute or recorded):
            return
        self.seen_attachments = attachments
        value = self.project.refs.read(card_ref(task["card"]))
        if not qa.ours(value, self.lease.seat, task.get("conversation")):
            self.end_conversation("ref_changed", turn_running=self.state != "qa_waiting")
            return
        try:
            cands = qa.candidates(self.project, card, list(self.thread.values()), max_age(self.cfg.qa_poll_seconds))
        except KankanError as err:
            self.log("qa_poll_failed", status=err.status.value, error=err.message)
            self.recompute = True  # the new items are already in the thread: recomputed at the next poll
            return
        self.recompute = False
        covered = qa.effective_covered(value.meta.get("covered"), qa.read_turn(self.project.paths,
                                                                               task["conversation"]))
        self.current = qa.uncovered(cands, covered)
        self.followups = qa.to_deliver(cands, covered, self.delivered)

    def recorded_followups(self, card_id: str) -> bool:
        """qapool T§5 step 3, holder delivery: inputs of the card the answer record lists and this run has not
        delivered, whatever the cursor's position (found by a round while this seat restarted or answered); a listed
        comment missing from the thread read so far brings the thread in."""
        listed = self.listed_inputs(card_id)
        pending = {i for i in listed if i not in self.delivered.comments and i not in self.delivered.attachments}
        if not pending:
            return False
        if any(i not in self.thread for i in pending if i not in self.seen_attachments):
            try:
                for comment in self.project.board.thread(card_id, max_age(self.cfg.qa_poll_seconds)):
                    self.thread.setdefault(comment.id, comment)
            except KankanError as err:
                self.log("qa_poll_failed", status=err.status.value, error=err.message)
        return True

    # ------------------------------------------------------------------ ends
    def handoff_done(self) -> bool:
        """This conversation's ``handoff`` transition happened (ref ``parked`` with its handoff, or since ``impl``)."""
        if not self.task:
            return False
        return qa.handed_off(self.project.refs.read(card_ref(self.task["card"])), self.task.get("conversation"))

    def end_conversation(self, reason: str, turn_running: bool = True) -> None:
        """End the conversation: the ref is deleted at once (a later command of the turn gets ``not_owner``); with
        a turn running, the agent is asked to ``yield`` and the clear waits for it, otherwise ``closing`` now."""
        task = self.task
        if task is None or self.ended:
            return
        handed_off = self.handoff_done()
        try:
            qa.end(self.project, self.lease, task, handed_off=handed_off)
        except KankanError as err:
            self.log("qa_end_failed", status=err.status.value, error=err.message)
            if err.status == Status.NOT_OWNER:
                self.exit_now = True
            return  # retried at the next poll
        self.ended = "handoff" if handed_off else reason
        self.log("qa_conversation_ended", card=task["card"], reason=self.ended, turn=self.turn)
        if turn_running and self.state != "idle":
            if not handed_off:
                self.request_suspend(reason)
        else:
            self.to_closing()

    def to_closing(self) -> None:
        if self.state != "closing":
            self.set_state("closing")
            self.closing_since = clock.monotonic()

    def closing(self, cls: Classification) -> None:
        """Forget the conversation, wait for a "ready" pane, send the new-conversation command and go idle."""
        if self.task is not None:
            task_id = self.task["id"]

            def clear(state):
                if state.task and state.task.get("id") == task_id:
                    state.task = None
                state.suspend_requested = None
            update_state(self.project.paths, self.lease.seat, clear)
            self.task = None
            self.pending_writes = []
            self.reset_conversation()
        if self.can_write(cls):
            self.paste(self.adapter.new_conversation)
            self.cleared = True
        elif clock.monotonic() - self.closing_since <= CLOSING_TIMEOUT:
            return
        self.idle_since = clock.monotonic()
        self.next_tick = 0.0
        self.set_state("idle")


def _safe_name(name: str) -> str:
    """An attachment name for a pane prompt: control characters and runs of whitespace become one space, and
    backticks become quotes, so a name can neither split nor garble the prompt."""
    printable = "".join(ch if ch.isprintable() else " " for ch in name or "")
    return " ".join(printable.split()).replace("`", "'")


def _end_reason(cfg, card) -> str:
    if card is None:
        return "deleted"
    if card.closed:
        return "archived"
    if card.list_id != cfg.qa_list:
        return "moved"
    return "hold"
