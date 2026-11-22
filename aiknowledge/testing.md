# Testing (local suite)

## Running

- Run the full suite in parallel and in its own session: `setsid -w .venv/bin/python -m pytest -q -n 16 --dist worksteal` (about 1.5–2 min on 8 cores; serially it takes far longer, mostly in the process tests). `setsid` is needed because recovery tests kill process sessions. The process tests mostly wait for launchers running as real processes, hence more workers than cores and `worksteal` to spread the long tests; a single file runs quickly without `-n`.
- Markers (`pyproject.toml`): `tmux` (needs a real tmux server), `slow` (`-m "not slow"` skips the tmux process-chain tests), `trello_live` (part of the default run when a test board is configured, skipped otherwise; `-m 'not trello_live'` leaves them out). Live tests: [live-tests.md](live-tests.md).
- Keep the source files unchanged while process tests run: launchers record the code hash at start and agent commands refuse to run with different code.

## Fakes and fixtures

- Tests use a fake Trello server (`tests/fakes/fake_trello.py`, with fault injection), a scripted fake agent (`tests/fakes/fake_agent.py`, installed as `claude`/`codex` on a temporary PATH) and private tmux servers (`KANKAN_TMUX_SOCKET`). Each test has its own temp directory, `HOME` and tmux socket, which is what makes the parallel run safe.
- The fake Trello server serves attachment uploads (`upload`, `remove_attachment`, `redirect_download`, the `truncate` fault, downloads counted in `downloads` (racy when the client aborts a too-large download: assert on `auth_log`/`requests` instead), a `404` fault echoing the credential, `auth_log` records whether each request carried the credential); the `external_host` fixture (`tests/fakes/external_host.py`) is an HTTP server on `localhost`, another origin than the fake's `127.0.0.1`, for redirect tests.
- The fake Trello server's `delay` fault (`fake.inject(method, path_regex, "delay", times)`) serves matching requests normally after `fake.response_delay` seconds (default 1), without holding the fake's lock.
- Crash points: `faults.point(name)` raises `faults.SimulatedCrash` (a `BaseException`, so ordinary handlers do not swallow it) when the comma-separated `KANKAN_FAULT` names it. Points exist in `cards`, `config`, `forward`, `integrate`, `properties`, `qa`, `qa_answer` and `seats`; the Q&A ones are `qa_after_chunk`, `qa_before_covered`, `qa_after_covered`, `qa_after_signal`, `qa_after_handoff_summary`, `qa_after_handoff_tx`, `qa_after_handoff_move`, `qa_after_turn_saved`, `qa_replace_registered`, `qa_replace_ref`, `qa_replace_seatref`.

## Speed and cleanup

- Test speed: the full suite runs as `setsid -w .venv/bin/python -m pytest -q -n 16 --dist worksteal` (about 1.5 min); serially it takes far longer, mostly in the process tests. `tests/conftest.py` makes `os.fsync` a no-op in the pytest process, copies a session-wide template Git repository for `git_repo`, and sets `KANKAN_SCREEN_POLL=0.5` (read once by `kankan/launcher.py` at import; 0.05–1 accepted, otherwise 1 s) so launcher processes step faster; `test_card_goes_from_backlog_to_done` keeps the real 1 s. The fake servers use `serve_forever(poll_interval=0.01)` so teardown does not wait 0.5 s. The `tmux_server` fixture kills its server (retrying while it still answers) and then every process whose working directory is under the test's `tmp_path`; each pytest process (xdist worker) does the same at its end for its `kankan-test-<pid>-*` servers and its base temp directory. Only a run killed from outside (SIGKILL, timeout) can still leave servers behind: kill them with `tmux -L <name> kill-server` and by pid, never with a `pkill -f` pattern that matches the calling shell.
- Tests skip `kankan init`'s settle wait (conftest).

## Writing tests

- Tests set `KANKAN_SKIP_PREFLIGHT=1` (autouse); `tests/test_doctor.py` unsets it where it matters. `tests/helpers.bind_project` binds with `integration_branch="main"`; new tests pass `integration_branch="kankan-agents"`.
- Never call `monkeypatch.undo()` in a test: it also undoes the autouse isolated HOME.
- `procutil.terminate_session`/`terminate_pids` reap the processes they kill: when a test's own `Popen` child is killed that way, `Popen.wait()` may return 0 (ECHILD); assert that the process has exited (`poll() is not None`), never on its exit code.
