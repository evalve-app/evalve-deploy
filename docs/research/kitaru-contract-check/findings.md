# Kitaru contract check for a Loop task: findings

Ticket: [sc-465](https://app.shortcut.com/team-4-1/story/465). Map: [sc-382](https://app.shortcut.com/team-4-1/story/382). Run date: 2026-11-25 (local time).

**Result: 23 PASS, 0 FAIL, 13 INFO.** No fact contradicts the claim decision or the credential decision. One fact contradicts the isolation decision (section 3). One fact changes a rule of the agent image (section 1).

**Added on 2026-11-27:** the decision on that contradiction ([sc-534](https://app.shortcut.com/team-4-1/story/534)) and the run of the check with its block are in section 6. The script has the option `--lock` for it.

**Added on 2026-11-28:** the run with the block on Linux x86_64 with podman ([sc-548](https://app.shortcut.com/team-4-1/story/548)) is in section 6: 26 PASS, 0 FAIL, 14 INFO, the same statuses as on arm64.

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

An agent that writes only below `HOME`, `TMPDIR` or a relative path is isolated as the decision says. This ticket does not change the decision. The new ticket [Decide: what a Loop task does with the places outside the private directory that agent code can write](https://app.shortcut.com/team-4-1/story/534) holds the question. Section 6 has its answer.

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
uv run check.py --lock     # with the block of sc-534 (section 6), writes results-lock.json
```

Before a Kitaru upgrade, change the version in the `dependencies` line at the top of `check.py`. The server image, the worker image and the client follow that line. A FAIL, or an INFO result that differs from this file, is a fact that changed.

Run the check with `--lock` too. An agent image has the block, so the result with `--lock` is the one that counts for a Loop task. On an Apple Silicon Mac, set `DOCKER_DEFAULT_PLATFORM=linux/arm64`: with the emulated x86_64 images, each Replay fails with `--lock` (6.4).

## 6. The block: uid 1000 can write only below `/tmp/evalve` (sc-534)

Tickets: [sc-534](https://app.shortcut.com/team-4-1/story/534) and [sc-548](https://app.shortcut.com/team-4-1/story/548). Machines:

- 2026-11-27: an Apple Silicon Mac with Docker Desktop 29.7.2.
- 2026-11-28: the machine of the run of 2026-11-25, with Linux 7.3.6 (7.3.4 on 2026-11-25). Fedora 44, x86_64, rootless podman 5.8.9 with crun 1.30, SELinux enforcing.

Not Fargate.

**The decision.** Code of a Replay (the agent, each process that it starts, and the evaluators) can write only below `/tmp/evalve`. The final layer of `evalve build` takes the write access of uid 1000 away from `/tmp`, `/var/tmp`, `/run/lock` and `/home/kitaru`, and from what is in them, and makes `/tmp/evalve` for uid 1000. `/dev/shm` and `/dev/mqueue` stay open, and the Supervisor empties them before each Run. The shared caches stay as they are.

**The option `--lock`.** `uv run check.py --lock` builds the test agent image with the commands of that final layer (`ARG LOCK` in `image/Dockerfile`). The checks are the same, plus three facts that hold only with the block. A full run with `--lock` writes `results-lock.json`.

**Result with `--lock`, all checks, natively on arm64: 26 PASS, 0 FAIL, 14 INFO.** The run of 2026-11-25 (Linux x86_64, podman, no block) gave 23 PASS, 0 FAIL, 13 INFO. Each fact of that run has the same status with the block. The 3 new PASS and the 1 new INFO are new facts of the script (6.6). One INFO fact has a different detail, and the block is not the cause: in `timeout_entrypoint`, with an agent task that ends in the 30 s, the three reads "20 s after the limit" are empty. The reads start 50 s after the start of the container, and `container_seconds` is 50.2 s. The same check with no block on the Mac gives the same empty reads (`lock-2026-11-27/baseline-arm64/timeout_entrypoint.log`). The end is the same as in the first run: exit code 124, the agent task `completed`, the evaluator tasks `pending`.

**Result with `--lock`, all checks, natively on Linux x86_64 with podman (2026-11-28): 26 PASS, 0 FAIL, 14 INFO.** Each of the 40 facts has the same status as on arm64. Thus with no emulator, the Kitaru worker, uv, Python and the evaluators work with the block on x86_64. The probe agent reads `/proc/<pid>/cmdline` of each process above it, and each read works. The output is `lock-2026-11-28/lock-amd64-podman/all-checks.log` and `results-lock.json`. Four INFO facts show something different from arm64. The other INFO facts differ only in IDs and times.

- **Three facts: `/dev/mqueue` cannot be written.** In `image`, the directories below `/dev` that uid 1000 can write are only `/dev/shm`. In `files_between_runs`, no file of uid 1000 stays in `/dev/mqueue`, and a marker of the first Run is found only in `/dev/shm` and the three shared caches (3 markers in each place, 2 on arm64). The block is not the cause. On this machine, `/dev/mqueue` has mode 1777, but SELinux denies the write of a container (an AVC `denied { write }` for `container_t` on `dev="mqueue"` in the journal). The bare worker image, with no block, gives the same `EACCES`. With `--security-opt label=disable`, uid 1000 can write `/dev/mqueue`. The commands and their output are in `lock-2026-11-28/lock-amd64-podman/dev-mqueue.log`. Thus what uid 1000 can write below `/dev` depends on the engine and the host.
- **`timeout_entrypoint`, an agent task that ends in the 30 s:** the three reads "20 s after the limit" are not empty. They show a worker that drains, as in the run of 2026-11-25. `container_seconds` is 51.8 s (52.4 s on 2026-11-25, 50.2 s on the Mac). The check takes it when `wait` returns, after the reads, so the container ended at that time or before. This is inferred: on the Mac, the container ended before the reads, and on this machine after them.

Other numbers, with the numbers of 2026-11-25 on this machine with no block: the Run of 10 jobs took 46.2 s (47.3 s), and the 10 `uv run` commands 21.5 s (22.5 s). The cgroup `memory.peak` was 1810 MB (2153 MB on 2026-11-25, 3864 MB on the Mac). It is a local number with the page cache (section 3).

### 6.1 The places that uid 1000 can write

Read in `zenmldocker/kitaru-worker:0.27.6` and in the test agent image, with `find / -xdev … -writable` and `find /dev -writable`.

| Place | Where it comes from | With no block | With `--lock` |
|---|---|---|---|
| `/tmp`, `/var/tmp` | The image, mode 1777 | Can be written | `EACCES` |
| `/run/lock` (`/var/lock` is a link to it) | The image, mode 1777. **Not in the first run of this check.** | Can be written | `EACCES` |
| `/home/kitaru` | The image, owner `kitaru`. It holds three shell files (24 KB). | Can be written | `EACCES` |
| `/tmp/uv-6c2078bb75587c01.lock` | A file of the worker image, owner root, mode 0666 | Can be written | Cannot be written |
| `/dev/shm` | A mount of the engine (tmpfs, 64 MB with Docker) | Can be written | Can be written |
| `/dev/mqueue` | A mount of the engine. **Not in the first run of this check.** | Can be written with Docker Desktop. `EACCES` with podman on the machine of 2026-11-28 (SELinux). | Can be written with Docker Desktop. `EACCES` with podman on the machine of 2026-11-28 (SELinux). |
| `/tmp/evalve` and the three shared caches below it | With `--lock`, the image makes `/tmp/evalve` (uid 1000, mode 0700) | Can be written | Can be written |

With `--lock`, the command of check 6 of `evalve build` prints nothing as uid 1000:

```bash
find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /tmp/evalve \) -prune -o -writable -print
```

`find` exits with code 1, because uid 1000 cannot read each directory (for example `/root`). Only the standard output counts.

On arm64, between two Runs with `--lock`, a marker of the first Run is found only in `/dev/shm`, `/dev/mqueue` and the three shared caches. With no block, it is found in 9 places (the 7 of section 3, `/run/lock` and `/dev/mqueue`). With podman on the machine of 2026-11-28, `/dev/mqueue` is not one of these places.

### 6.2 What ran

The first runs of the day used a first form of the block: `/tmp` and `/var/tmp` with mode 0755, and `/home/kitaru` given to root. `/run/lock` and the lock file of uv were still open. The form of `--lock` is the final one: the four places and what is in them. The output of each run of that day is in `lock-2026-11-27/`. The full run with `--lock` on arm64 is `lock-2026-11-27/lock-arm64/` (`all-checks.log` and `results-lock.json`). The full run with `--lock` on Linux x86_64 with podman is `lock-2026-11-28/lock-amd64-podman/all-checks.log` and the `results-lock.json` next to `check.py`. The table has the first runs.

| Configuration | `token_mode` | `files_between_runs` | `uv_concurrency` |
|---|---|---|---|
| No block, x86_64 emulated | 9 PASS, 0 FAIL | 1 PASS, 0 FAIL | 2 PASS, 0 FAIL |
| No block, arm64 | 9 PASS, 0 FAIL | 1 PASS, 0 FAIL | 2 PASS, 0 FAIL |
| First block, arm64 | 9 PASS, 0 FAIL | 1 PASS, 0 FAIL | 2 PASS, 0 FAIL |
| First block, with `/run/lock` closed and the lock file removed, arm64 | 9 PASS, 0 FAIL | 1 PASS, 0 FAIL | 2 PASS, 0 FAIL |
| First block, x86_64 emulated | 1 PASS, **3 FAIL** | 1 PASS, 0 FAIL, but each of the 4 jobs failed | 1 PASS, **1 FAIL** |
| First block, x86_64 emulated, probe agent with no read of `/proc/<pid>/cmdline` | 9 PASS, 0 FAIL | 1 PASS, 0 FAIL | 2 PASS, 0 FAIL |

- With the block on arm64, each job completed, each worker exited with code 0, and the judge evaluator imported `openai` 3.49.0 from `/tmp/evalve/cache/uv/`. The Run of 10 jobs took 12.1 s (10.5 s with no block).
- On emulated x86_64 with the block, each agent task exited with code 133, so no evaluator ran. The cause is the emulator, not Kitaru (6.4).
- `files_between_runs` gave no FAIL when its jobs failed, because it had no fact for the status of a job. It has one now ("Each Replay of the two Runs completed and gave its report").

### 6.3 Which program writes where

One run of `token_mode` with the first block on arm64, with each worker process under `strace -f -e trace=%file` (2 Replays, cold uv cache). Summary: `lock-2026-11-27/first-lock-arm64-strace/strace-summary.txt`.

- 49,971 file system calls. 22 gave `EACCES`, and none gave `EPERM` or `EROFS`.
- All 22 are on purpose: 18 are the marker files of the probe agent, and 4 are `os.access("/app", os.W_OK)` of the evaluators.
- The Kitaru worker wrote 14 times, each below `/tmp/evalve/cache/kitaru-*` or its private directory. Its `kitaru-task-*` directories are in `TMPDIR`.
- uv wrote 5,653 times, each below `/tmp/evalve/cache/uv`. It only looks for `/tmp/pyproject.toml` and `/tmp/uv.toml`, which do not exist.
- No program opened `/run/lock`, `/var/lock`, `/dev/mqueue` or the lock file of uv in `/tmp`.

Thus the Kitaru worker, uv, Python and the evaluators need no place that the block closes. Kitaru drops the output of a task that completes, so the worker logs cannot show a warning of a task. The trace covers that for these 2 Replays.

### 6.4 The x86 emulator of an Apple Silicon Mac

With the block, a program in an x86_64 container on an Apple Silicon Mac can stop with exit code 133 when it reads `/proc/<pid>/cmdline`. The message is `assertion failed [!result.is_error]: Failed to create temporary file (ThreadContextFcntl.cpp:85 create_tempfile)`.

- The emulator of Docker Desktop (Rosetta) makes a file `/tmp/rosetta.<n>.<m>` for that read. It does not use `TMPDIR`.
- Run in the image with the block, as uid 1000: `cat /proc/self/cmdline` and `python -c 'open("/proc/self/cmdline","rb").read()'` exit with code 133. `/proc/self/comm`, `environ`, `stat` and `maps`, `/proc/1/cmdline`, `tempfile` and `multiprocessing.Lock()` work.
- With only `/tmp` open again, the read works. With only `/var/tmp` open again, it fails.
- The probe agent reads the `cmdline` of the processes above it. Kitaru, uv and Python do not read a `cmdline` file in a Replay: with no such read in the probe agent, each check passes on emulated x86_64 with the block.

A Loop task on Fargate runs x86_64 with no emulator, so this is a limit of a local run on a Mac only. On Linux x86_64 with no emulator, the same probe agent reads `/proc/<pid>/cmdline` with the block, and each check passes (2026-11-28, the start of section 6). For Fargate, this is still inferred: the smoke run [sc-530](https://app.shortcut.com/team-4-1/story/530) runs the block there. The Supervisor must read `/proc/<pid>/stat` and no `cmdline` file ([sc-498](https://app.shortcut.com/team-4-1/story/498)).

### 6.5 Not verified

- Fargate. The smoke run [sc-530](https://app.shortcut.com/team-4-1/story/530) has the facts: the block in a Loop task, the directories that the engine makes there, and the size of `/dev/shm`.
- A real agent or evaluator. Code that writes `/tmp` or the home directory of the passwd file gets `EACCES`.
- The trace on more than 2 Replays, and on x86_64.

### 6.6 What changed in the script

- `--lock`, and `results-lock.json` for a full run with it. `results.json` is still the run of 2026-11-25 on Linux x86_64 with podman, from the script before these changes.
- The logs of the Supervisor stand-in and of the probe agent are in `/tmp/evalve/kcc`, not in `/tmp/kcc`, so that they work with the block.
- The probe agent also tries `/run/lock` and `/dev/mqueue`. It only makes its marker file and writes nothing to it, because a file in `/dev/mqueue` is a message queue.
- `image` has two new facts: the places of the test agent image that uid 1000 can write (with `--lock`: none), and the directories below `/dev` that uid 1000 can write.
- `files_between_runs` has two new facts: each Replay completed, and with `--lock` the four places give `EACCES` and hold no marker.
- For docker: `rm -f -t 0` and `image exists` are commands of podman only, so the script left its containers and pulled each image again. Docker now gets `rm -f` and `image inspect`. podman keeps `rm -f -t 0` and `image exists`. The run of 2026-11-28 with podman pulled no image (each image was present) and left no container and no network.
