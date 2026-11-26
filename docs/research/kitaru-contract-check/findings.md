# Kitaru contract check for a Loop task: findings

Ticket: [sc-465](https://app.shortcut.com/team-4-1/story/465). Map: [sc-382](https://app.shortcut.com/team-4-1/story/382). Run date: 2026-11-25 (local time).

**Result: 23 PASS, 0 FAIL, 13 INFO.** No fact contradicts the claim decision or the credential decision. One fact contradicts the isolation decision (section 3). One fact changes a rule of the agent image (section 1).

## How it ran

- `uv run check.py`, all checks, 15 minutes. The full output of each check is in `results.json`.
- Server: `zenmldocker/kitaru-server:0.27.6` with Postgres 16, auth scheme `local`, default settings. A second server on the same database has `KITARU_SERVER_WORKER_TOKEN_LIFETIME_SECONDS=40` for the expiry check.
- Workers: a test agent image that starts from `zenmldocker/kitaru-worker:0.27.6` (`image/Dockerfile`). Root owns all of `/app`. The user is uid 1000. The containers run with an init process, as `initProcessEnabled` gives.
- `image/launcher.py` stands in for the Supervisor of [sc-498](https://app.shortcut.com/team-4-1/story/498): one worker process for each job, a new session, a private directory that is `HOME`, `TMPDIR` and the working directory, and the environment of that story.
- Each worker is registered as [sc-473](https://app.shortcut.com/team-4-1/story/473) says: the job ID, one `agent` claim with no agent version, one `evaluator` claim, no selectors. The worker gets `KITARU_API_URL`, `KITARU_WORKER_ID` and `KITARU_API_TOKEN`.
- A Run is an experiment run with `baseline_evaluation_mode: none`, as the Evalve backend sends. Each job has 1 agent task and 2 evaluator tasks.
- Engine: podman 5.8.9 (rootless) on Linux 7.3.4, x86_64. Not Fargate. `kernel.yama.ptrace_scope` is 0.

Labels: **PASS** = the fact is as the decisions assume. **INFO** = the check records what Kitaru does; there was no expected result.

## 1. The worker and its token

| Fact | Result | What ran and the numbers |
|---|---|---|
| `/app/.venv/bin/kitaru worker start --timeout N` runs with `KITARU_API_URL`, `KITARU_WORKER_ID` and `KITARU_API_TOKEN` and no key | PASS, with a condition | The job completed and the worker exited with code 0. The container had no key and no stored login. **Condition:** the bare worker image has no `kitaru` CLI. There, the command prints "Install the Kitaru CLI with `pip install 'kitaru[cli]'`" and exits with code 2. The test image installs `kitaru[cli,worker]==0.27.6` in `/app/.venv`. |
| `python -m kitaru.worker` with `KITARU_WORKER_TIMEOUT` | INFO | It also completes a job in token mode, exit code 0, in the bare worker image. It installs no SIGTERM handler (source), so the CLI stays the command of the Supervisor. |
| The job-scoped worker claims the agent task and each evaluator task of its job | PASS | 3 tasks `completed`, each with the `worker_id` of the worker. |
| A job-scoped worker exits by itself with code 0 when its job ends | PASS | Exit code 0. The exit came 2.3 s after the end of the last task (one poll interval of 2 s). One Replay with a cold uv cache took 18 s from start to exit. |
| The lifetime of a worker token | INFO | `token_expires_at` is 3600 s after the registration. |
| What Kitaru does with held tasks when a worker token expires | INFO | Token of 40 s, agent task of 100 s. From the expiry, each claim and each heartbeat gives 401. The agent process continues. 60 s after the expiry (at 100 s) the server puts the agent task back to `pending` with no worker. The job stays `running`. No result session is linked to the replay and no evaluation is recorded. The worker does not exit: it tries the claim again with a delay of up to 60 s until its `--timeout`. The check killed it after 330 s. |
| What Kitaru does with a task that a killed worker held | PASS | SIGKILL to the container. The task went back to `pending` after 64 s (heartbeat timeout of 60 s, sweep each 15 s). The job stayed `running`. With no worker, the task stayed `pending` (watched for 30 s). |
| A second start of a worker with the same worker ID and token | PASS | The new process loaded the worker, claimed the pending task as attempt 2, ran the evaluator tasks, and exited with code 0. The job completed. |
| Two workers on one worker ID at the same time | PASS | The second process started while the first held the agent task. The agent task ran one time (attempt 1). The job completed. The two processes exited with code 0. |
| A worker in token mode gets the Kitaru secrets of the agent version | PASS | The agent process had the value of the secret (compared by SHA-256) and the `env` of the run spec. |
| The environment of the agent process | PASS | It has a task token in `KITARU_API_TOKEN`, not the worker token. It has no `KITARU_API_KEY` and no `EVALVE_LOOP_TASK_SECRET`. It has `KITARU_WORKER_ID`, `UV_CACHE_DIR` and the two cache roots, because the worker passes its environment. |
| A judge with the provider `openai` and a connection schema gets `OPENAI_API_KEY` from a default connection, in a job-scoped worker that holds no key | PASS | The judge task had the key of the connection (compared by SHA-256). Its labels were only `kitaru/provider: openai`, with no `kitaru/requires-credentials`. The evaluator with no provider did not get the key. |

**What this means for the stories.**

- **The agent lockfile must have `kitaru[cli]`, not only `kitaru`.** If not, the Supervisor command does not start. This changes check 2 of [sc-244](https://app.shortcut.com/team-4-1/story/244) (the check must also run `/app/.venv/bin/kitaru worker start --help`) and the lockfile of the pilot agent in [sc-243](https://app.shortcut.com/team-4-1/story/243).
- **A Replay must complete each task before the worker token expires.** A task that is in progress at the expiry is lost 60 s later, and the worker cannot claim again. This agrees with the Run deadline of sc-473. The 30 s of the Supervisor after `run_deadline` are inside the 60 s.
- **A worker with an expired token does not exit.** The `--timeout` and the SIGKILL of the Supervisor after the deadline are both necessary.
- **A second start on one worker ID is safe**, in sequence and at the same time. The rule "at most one worker process for each job ID" of sc-498 is not necessary for Kitaru. A replacement Loop task that gets the same Assignments can start the workers again.

## 2. Signals and the image

| Fact | Result | What ran and the numbers |
|---|---|---|
| `find -writable` and `/usr/bin/timeout` are in the worker image | PASS | GNU coreutils 9.1 and GNU findutils 4.9.0. `/usr/local/bin/python3` is Python 3.13.18. In the bare image, uid 1000 owns `/app`. |
| `find /app -writable` prints nothing when root owns `/app` | PASS | In the test agent image, as uid 1000. |
| `timeout` signals only its child, not the worker processes in their own sessions | PASS | `timeout --kill-after=30 30` with a child that ignores SIGTERM. The child got SIGTERM one time. The worker got no signal and continued to poll. At 60 s the container exited with code 137. |
| A worker that gets SIGTERM one time drains | PASS | The agent task (20 s) completed. The agent process got no signal. The worker exited with code 143 when the task ended. The worker claimed nothing more: the 2 evaluator tasks stayed `pending` and the job stayed `running`. |
| A worker that gets SIGTERM two times cancels its tasks | PASS | The worker exited 0.6 s after the second signal, with code 143. The agent task was `canceled` and **the job was `canceled`**. The agent process got no signal that it can catch (its handlers for SIGTERM, SIGINT and SIGHUP logged nothing). |
| The 30 s of `timeout --kill-after=30`, agent task longer than 30 s | INFO | The Supervisor stand-in sent SIGTERM one time. The worker drained and sent heartbeats. At 30 s the container exited with code 137. The agent task stayed `running` in Kitaru until the heartbeat timeout. |
| The 30 s of `timeout --kill-after=30`, agent task ends in the 30 s | INFO | The agent task completed and the worker exited with code 143. The container exited with code 124 after 52 s. The evaluator tasks stayed `pending`. |

**What this means for the stories.**

- The entrypoint and the SIGTERM rule of sc-498 work as written. A worker exit code of 143 is a normal drain.
- **A drain does not finish a Replay.** After a drain, the evaluator tasks have no worker. Only a new worker process for the same job (a replacement Loop task) or the Run deadline ends the Run.
- The exit code of the container at the hard lifetime limit is 124 (drain in time) or 137 (SIGKILL after 30 s). [sc-271](https://app.shortcut.com/team-4-1/story/271) and the smoke run [sc-530](https://app.shortcut.com/team-4-1/story/530) can use these.

## 3. Isolation

| Fact | Result | What ran and the numbers |
|---|---|---|
| The agent and the evaluators start when root owns all of `/app`, `.venv` included, with a source tree that cannot be written and a new `HOME` for each worker | PASS | uid 1000. Each write to `/app`, `/app/agent`, `/app/.venv`, `site-packages` and `/opt/kcc` gave `EACCES`. `HOME` and `TMPDIR` of the agent and of the evaluators were the private directory. |
| The working directory | INFO | The agent process runs in the `working_dir` of the run spec (`/app`), not in the private directory. An evaluator runs in the private directory. |
| An evaluator with an inline dependency block (`openai==3.49.0`) installs and runs when root owns `/app` | PASS | `openai` 3.49.0 was imported from `/tmp/evalve/cache/uv/archive-v0/…`. Nothing was written to `/app`. |
| 10 `uv run` commands at the same time are safe on one uv cache | PASS | 10 commands in the form that Kitaru uses, on a cold cache: each exit code 0, 22.5 s. Then one Run with 10 jobs and 10 job-scoped workers in one container on one cold cache: 10 jobs `completed`, each worker exit code 0, 47 s. |
| Memory of 10 workers | INFO | The cgroup `memory.peak` of the container was 2153 MB. This is a local number with the page cache. The smoke run measures Fargate. |
| No file stays in a private directory | PASS | After each worker process exited and the stand-in deleted its directory, `/tmp/evalve/replays/` was empty. |
| **The files that stay between Runs** | INFO, **contradicts the isolation decision** | Two Runs of 2 jobs in one container. Agent code (uid 1000) can write, outside its private directory: `/tmp`, `/var/tmp`, `/dev/shm`, `/home/kitaru`, `/tmp/evalve/cache/uv`, `/tmp/evalve/cache/kitaru-blobs` and `/tmp/evalve/cache/kitaru-payloads`. A marker file that a Replay of the first Run wrote to each of these 7 places was found by each Replay of the second Run. What Kitaru leaves by itself: 9806 files in the uv cache, 6 in the blob cache, 4 in the payload cache. |
| A Replay can see the private directories of the Replays that run at the same time | INFO | The directories have mode `0700`, but each Replay has the same uid. A Replay listed 2 directories. |
| Agent code can read the worker token from `/proc` of the worker | PASS | The agent process read `KITARU_API_TOKEN` from `/proc/<pid>/environ` of the worker (compared by SHA-256). It also read the channel secret from `/proc` of the process that started the worker. Both are documented limits. |

**The contradiction.** The isolation decision ([sc-390](https://app.shortcut.com/team-4-1/story/390)) says: "A Replay sees nothing that a different Replay wrote", and "Replays share only state that is keyed by its content and that agent code does not write (the agent image, the uv cache, the Kitaru caches)". The check shows two gaps:

1. Agent code can write the uv cache and the two Kitaru caches. They are directories of uid 1000, and the agent has the same uid.
2. Agent code can write `/tmp`, `/var/tmp`, `/dev/shm` and `/home/kitaru`. The sweep of sc-498 deletes only `/tmp/evalve/replays/`. An agent that writes a fixed path (for example `/tmp/cache.json`) reads its file again in the next Replay and in the next Run.

An agent that writes only below `HOME`, `TMPDIR` or a relative path is isolated as the decision says. This ticket does not change the decision. The new ticket [Decide: what a Loop task does with the places outside the private directory that agent code can write](https://app.shortcut.com/team-4-1/story/534) holds the question.

## 4. Not checked

- All behaviour on Fargate. The smoke run [sc-530](https://app.shortcut.com/team-4-1/story/530) has those facts.
- An agent task that ends in the 60 s between the expiry of the worker token and the requeue.
- The retry limit of 3 for a stale task (source: the third stale attempt makes the task `abandoned` and the job `failed`).
- Which signal the agent process gets on a cancel. It is not SIGTERM, SIGINT or SIGHUP.
- `kernel.yama.ptrace_scope = 1`, and docker as the engine.
- A real model call. The judge stand-in records the key and calls no model.

## 5. Run it again

```bash
cd docs/research/kitaru-contract-check
uv run check.py            # needs uv, and docker or podman
uv run check.py --only sigterm
```

Before a Kitaru upgrade, change the version in the `dependencies` line at the top of `check.py`. The server image, the worker image and the client follow that line. A FAIL, or an INFO result that differs from this file, is a fact that changed.
