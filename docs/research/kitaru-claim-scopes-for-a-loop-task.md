# Kitaru claim scopes for one Fargate task per Loop

Research date: 2026-11-20. Ticket: [sc-383](https://app.shortcut.com/team-4-1/story/383). Map: [sc-382](https://app.shortcut.com/team-4-1/story/382).

**Question.** In Kitaru 0.27.6, which worker claim scopes let one Fargate task that lives for a full Loop claim only the work of that Loop, across each Run and each Experiment?

**Sources.** The Kitaru source at tag `python/kitaru/v0.27.6`, commit [`96dfb82`][k], read with `git show` from a local clone. Each fact cites a file and its lines at that commit. One fact about Evalve comes from `evalve-backend` at commit `5b79bfa`.

**Earlier research.** Two local notes exist: `docs/research/kitaru-worker-on-aws.md` and `docs/research/kitaru-workers-for-loops-on-aws.md`. They are untracked files in the main checkout and are not on this branch. This note checks section 13 of the second note and the facts that it uses.

**Labels.** **[V]** = read in the source at the commit. **[I]** = the author's inference from verified facts. **[U]** = not verified.

**Limits.** Nothing was run. No worker was started and no server was called. All behaviour in this note is read from source.

## Summary

- **No scope selects a Loop, an Experiment or a Run.** A scope has three fields: `claims`, `selectors` and `job_id`. The model refuses all other fields **[V]**.
- **Only a job scope limits a worker to the work of one Loop.** One scope holds one job ID. One Run makes one job for each session. So a job scope needs one worker process for each job of each Run **[V]** **[I]**.
- **A worker with `agent=<version>` and `evaluator` claims is not limited to one Loop.** It claims the agent tasks of each job on that agent version. It claims evaluator tasks of all jobs on the server **[V]**.
- **An evaluator task cannot be limited to one Loop without a job scope.** Evaluator tasks carry no label for the agent version, the Experiment, the Run or the Loop **[V]**.
- **No scope reserves work.** A scope filters what its worker claims. A worker with a wider scope can claim the same tasks first, from any account **[V]**.
- **A wide worker on a Teammate's machine can make a Run fail.** It claims the oldest pending tasks of all jobs. If it cannot run an agent task, the task fails, and Kitaru does not try a failed task again **[V]**.

## 1. Claim selectors and refused combinations

### What a worker can set

| Field | Type | Meaning | Source |
| --- | --- | --- | --- |
| `claims` | 1 to 16 items | Each item has a `kind` and an optional `agent_version_id`. The server combines the items with OR. | [worker.py:45-52, 72-76][wmodel] **[V]** |
| `selectors` | optional list | Each item has a label `key`, a list of `values` and a `required` flag. The server combines the items with AND. | [worker.py:35-42, 77-80][wmodel] **[V]** |
| `job_id` | optional, one UUID | The worker claims tasks of this job only. | [worker.py:81-83][wmodel] **[V]** |

- The task kinds are `agent`, `evaluator`, `importer` and `analyzer` ([task.py:38-44][tmodel]) **[V]**.
- The default scope has one claim for each kind and no agent version ([config.py:41-43][wcfg]) **[V]**.
- The CLI flags are `--claim`, `--selector` and `--job-id` ([worker_commands.py:38-53][wcmd]). The environment variables are `KITARU_WORKER_SCOPE__CLAIMS`, `KITARU_WORKER_SCOPE__SELECTORS` and `KITARU_WORKER_SCOPE__JOB_ID` ([config.py:35-37][wcfg], [deploy/workers.md:29-31][dw]) **[V]**.
- The scope is fixed at registration. The worker routes are register, renew token, list, get, heartbeat and delete. There is no update route ([routers/workers.py:92, 138, 167, 201, 228, 255][wrouter]) **[V]**.

### How the server applies a scope

- A claim with an agent version matches tasks with that `agent_version_id`. A claim without one matches all tasks of its kind ([task_repository.py:60-80][trepo]) **[V]**.
- Only agent tasks have an `agent_version_id` ([orm/task.py:190-204][torm]) **[V]**.
- The job ID and each selector are added with AND to each claim term ([task_repository.py:83-103, 298-316][trepo]) **[V]**. A selector cannot apply to one claim only.
- A selector with `required: true` refuses a task that does not have the label. A selector with `required: false` accepts a task that does not have the label ([task_repository.py:96-102][trepo], [domain/worker.py:139-145][wdom]) **[V]**.
- The server adds one default selector when the scope has none for `kitaru/requires-credentials`: an empty list of values, not required. So a worker without that selector claims no task that carries that label ([domain/worker.py:106-121][wdom]) **[V]**.
- The server gives pending tasks oldest first, with `FOR UPDATE SKIP LOCKED` ([task_repository.py:300-335][trepo]) **[V]**.
- The claim query has no filter on the owner of the job or of the worker ([task_repository.py:298-335][trepo], [task_service.py:116-124][tsvc]) **[V]**.

### What is refused

The scope model does these checks. The server uses the same model for the registration request ([worker.py:150-158][wmodel]).

| Combination | Result | Source |
| --- | --- | --- |
| A field that is not `claims`, `selectors` or `job_id` (for example a Run ID) | Refused. The model forbids extra fields. | [base.py:19-22][base] **[V]** |
| No claims, or more than 16 claims | Refused | [worker.py:72-76][wmodel] **[V]** |
| `agent_version_id` on a claim that is not `agent` | Refused | [worker.py:54-66][wmodel] **[V]** |
| Two equal claims | Refused | [worker.py:108-113][wmodel] **[V]** |
| An `agent` claim with no version together with an `agent` claim with a version | Refused | [worker.py:114-119][wmodel] **[V]** |
| `selectors` set to an empty list | Refused | [worker.py:120-122][wmodel] **[V]** |
| Two selectors with the same key | Refused | [worker.py:123-125][wmodel] **[V]** |
| More than one job ID | Not possible. The field holds one UUID. | [worker.py:81-83][wmodel] **[V]** |
| A scope together with `KITARU_WORKER_ID` | Refused by the worker, before the request | [config.py:54-66][wcfg], [cli/workers.py:118-124][wcli] **[V]** |

What is accepted:

- Up to 16 `agent` claims with different agent versions, together with other kinds **[V]**.
- A job ID together with claims and selectors **[V]**.
- A job ID of a job that does not exist, or of a settled job. The registration does not read the job ([worker_service.py:100-135][wsvc]) **[V]**.

## 2. Jobs and tasks of one Run

- One call to start a Run makes all replays of the Run with `create_replay_pipelines` ([experiment_service.py:497-509][es]) **[V]**.
- That function makes one job for each session that is ready for replay ([replay_pipeline.py:241-252][rp]) **[V]**. A session that is not ready gets a failed replay with no job ([replay_pipeline.py:231-240][rp]) **[V]**.
- So a Run on a cohort version of N ready sessions has N jobs.

Each job holds these tasks:

| Task | When the server makes it | Number | Labels | Source |
| --- | --- | --- | --- | --- |
| Agent task | At the start of the Run | 1 | `kitaru/agent_version` and `agent_version`, both the agent version ID | [replay_pipeline.py:265-273][rp], [job_service.py:53-68][js] **[V]** |
| Baseline evaluator task | At the start of the Run, only when the baseline evaluation mode is not `none` | Up to 1 for each evaluator | The plugin labels (section 3) | [replay_pipeline.py:274-303][rp] **[V]** |
| Result evaluator task | When the agent task completes | 1 for each evaluator of the Experiment | The plugin labels (section 3) | [replay_pipeline.py:340-363][rp] **[V]** |

- The Evalve backend sends `"baseline_evaluation_mode": "none"` (`evalve-backend/src/evalve_backend/executor.py:193-195` at `5b79bfa`) **[V]**. So a job of a Loop today holds 1 agent task, then 1 evaluator task for each evaluator.
- The server makes result evaluator tasks only when the agent task has the status `completed` ([replay_pipeline.py:341-342][rp]) **[V]**.
- All these tasks have `on_failure: abort` ([replay_pipeline.py:271, 301, 358][rp]) **[V]**. One failed task makes the job fail ([task_transitions.py:41-56][tt]) **[V]**.
- No task has a label for the Experiment, the Run or the Loop. All task labels come from two server functions ([job_service.py:58-68][js], [plugin_resolution.py:174-196][pr]) **[V]**. No request field lets the caller add a label **[V]** (text search for `labels=` in `src/kitaru/server` at the commit).
- The jobs of a Run can be listed: `GET /api/v1/experiment-runs/{id}/jobs` ([routers/experiment_runs.py:159-185][errouter]) **[V]**. Each replay also shows its `job_id` ([replay.py:118-120][rmodel]) **[V]**.

## 3. Can an evaluator task be limited to one Loop without a job scope?

**No.**

- The `evaluator` claim cannot have an ID ([worker.py:54-66][wmodel]) **[V]**. It matches the evaluator tasks of all jobs ([task_repository.py:78-79][trepo]) **[V]**.
- An evaluator task has at most three labels ([plugin_resolution.py:170-196][pr]) **[V]**:
  - `kitaru/plugin_namespace`: the namespace of the evaluator name. The only namespace that the server accepts is `kitaru` ([names.py:43, 156-160][names]) **[V]**. So this label separates built-in evaluators from the evaluators of the team. It does not separate Loops.
  - `kitaru/provider`: the provider of the evaluator.
  - `kitaru/requires-credentials`: the provider, when the worker must hold its credentials.
- None of these labels names the agent version, the Experiment, the Run or the Loop **[V]**.
- A required selector on `kitaru/agent_version` does not help. It applies to each claim, and evaluator tasks do not have that label. The worker then claims no evaluator task ([task_repository.py:96-98][trepo]) **[V]**.
- The job ID is the only field that limits an evaluator task to the work of one Loop **[V]**.

## 4. A job-scoped worker: finished job, and work that is put back in the queue

### The job is already finished

1. The worker registers. The server does not read the job ([worker_service.py:100-135][wsvc]) **[V]**.
2. The first claim gives no tasks, because the claim query reads pending tasks only ([task_repository.py:304, 316][trepo]) **[V]**.
3. After a claim that is not full, the worker reads the job. If the status is `completed`, `failed` or `canceled`, the claim loop ends ([worker.py:61-63, 338-342, 393-404][worker]) **[V]**.
4. The process stops with exit code 0 ([cli/workers.py:55-59, 255-266][wcli]) **[V]**.

So the worker stops after one claim call and one read of the job **[I]**.

### The job does not exist

- The read of the job fails. The worker logs a warning and continues to poll ([worker.py:395-399][worker]) **[V]**.
- The client maps HTTP 404 to `NotFoundError`, a subclass of `APIError` ([client/exceptions.py:56, 101][cexc]) **[V]**.
- **[I]** A worker with a wrong or deleted job ID polls until its lifetime timeout or a signal. Without `--timeout` it does not stop.

### A worker dies while it holds a task of the job

Kitaru puts tasks back in the queue. It does not put jobs back in the queue.

1. A task that is claimed or running and has no heartbeat for 60 s is stale ([server/config.py:69][scfg], [task_repository.py:360-368][trepo]) **[V]**. The sweep runs each 15 s ([server/config.py:72][scfg]) **[V]**.
2. If the attempt number is less than the retry limit of 3, the task goes back to `pending` with no worker ([task_service.py:345-353][tsvc], [domain/task.py:402-416][tdom], [server/config.py:70][scfg]) **[V]**.
3. Each claim adds 1 to the attempt number ([domain/task.py:383-387][tdom]) **[V]**. At the third stale attempt the task becomes `abandoned` ([task_service.py:354-358][tsvc]) **[V]**. An abandoned task is a hard failure, so the job fails ([domain/task.py:82-84][tdom], [task_transitions.py:50-52][tt]) **[V]**.
4. The job keeps the status `running` during this time. Only a pending job can start, and no code moves a job back to `pending` ([task_transitions.py:155-159][tt], [domain/job.py:117-147][jdom]) **[V]**.

Effects on a job-scoped worker:

- A job-scoped worker that is alive continues to poll, because the job is not settled. It claims the task when the task is pending again **[I]** from [worker.py:393-404][worker] and [task_repository.py:93-94][trepo].
- A new job-scoped worker for the same job ID claims the task in the same way. The scope does not record which worker had the task **[I]**.
- If the job-scoped worker is the worker that died, no other worker with that job ID exists. A wider worker can claim the task. If none can, the task stays pending.

### A pending task in a running job has no timeout

- The timeout of 3600 s cancels a job only while the job is `pending`, which means that no worker claimed a task of it ([job_repository.py:146-151][jrepo], [task_transitions.py:360-361][tt], [server/config.py:73][scfg]) **[V]**.
- The sweep has four parts: cancel propagation, pending jobs, stale tasks and idempotency keys ([task_sweeper.py:209-242][sweep]) **[V]**. No part reads a pending task of a running job.
- **[I]** A task that goes back to the queue, or a result evaluator task that the server adds after the agent task, waits with no limit when no live worker covers it. The Run stays open until a worker claims the task or a caller cancels the job. This matters when the Fargate task stops between the agent task and its evaluator tasks.

### A job-scoped worker that never stops

- A job-scoped worker without a `kitaru/requires-credentials` selector does not claim an evaluator task that carries that label (section 1) **[V]**.
- **[I]** If the job has such a task and no other worker claims it, the job does not settle and the job-scoped worker does not stop.

## 5. Several worker processes in one container

**Yes, several can run at the same time.** Each is an independent client of the server.

- Each start registers a new worker record. The default name is the hostname and the process ID. Names are not unique ([worker.py:89-99, 227-234][worker], [routers/workers.py:103-104][wrouter]) **[V]**.
- Each worker process gets its own worker token and renews it with the API key ([worker.py:72-86, 237-239][worker]) **[V]**.
- One worker process already runs up to 10 tasks at the same time, each as a subprocess ([config.py:29, 44][wcfg], [worker.py:312-336][worker]) **[V]**.

What the processes share:

| Item | Shared? | Detail | Source |
| --- | --- | --- | --- |
| Blob cache and payload cache | Yes. The default paths are `~/.cache/kitaru/blobs` and `~/.cache/kitaru/payloads`. | The cache is keyed by SHA-256. A write uses a temporary file and an atomic rename. The eviction code accepts that "another process sharing the cache" removed an entry. Each process applies the 1 GiB limit of the payload cache by itself. | [worker.py:55-58, 178-181][worker], [blob_cache.py:113-133, 141-161][bcache] **[V]** |
| uv environments of evaluators | Yes, through the uv cache of the user | An evaluator runs with `uv run --no-project --python <worker python> --with <dependency>`. Kitaru sets no uv cache directory. | [process.py:429-466][proc], [handlers/evaluation.py:59-67][evalh] **[V]** |
| Scratch directory of a task | No | Each task gets a new temporary directory `kitaru-task-*`. | [task_runner.py:113-123][runner] **[V]** |
| Working directory of the agent | Yes | Each agent task runs the command of the agent version in its `working_dir`. The `copy_workdir` hook copies it into the scratch directory of the task. | [handlers/agent.py:55-59][agenth], [hooks.py:82-97][hooks] **[V]** |
| Environment variables | Yes | Each task gets the full environment of its worker process. The worker removes `KITARU_API_KEY` and gives a token for one task and one attempt. | [process.py:354-362][proc] **[V]** |
| The API key | Yes | Each worker process reads `KITARU_API_KEY` to register and to renew. | [deploy/workers.md:37][dw] **[V]** |

- **[I]** Model keys in the container environment are visible to each task of each worker process. One process or several makes no difference here.
- **[U]** The safety of the uv cache under parallel use is a property of uv. It is not in the Kitaru source and was not checked. Kitaru already runs up to 10 `uv run` commands in parallel inside one worker.
- **[U]** Running two `Worker` objects in one Python process was not checked. The CLI starts one worker for each process ([cli/workers.py:230-266][wcli]).
- **[U]** The memory and CPU of several worker processes in one Fargate task were not measured.

## 6. What a worker with a wide scope can claim

A worker that a Teammate starts with `kitaru worker start` and no scope flags has the default scope.

- The default scope claims all kinds with no agent version. The server reads this as "the whole pending queue" ([worker.py:85-94][wmodel], [task_repository.py:72-73][trepo]) **[V]**.
- The only limit is the default credentials selector: it does not claim a task with the label `kitaru/requires-credentials` ([domain/worker.py:118-120][wdom]) **[V]**.
- It claims tasks of jobs that other accounts own. The claim has no owner filter (section 1) **[V]**.
- It claims oldest first, up to its free slots, at most 100 for each call ([worker.py:60, 312-325][worker]) **[V]**.
- A job scope of another worker does not stop this. The job ID is a filter on the worker that has it. It is not a lock on the job ([task_repository.py:93-94][trepo]) **[V]**.

What happens when the wide worker claims an agent task of a Loop:

1. It runs the command of the agent version in the `working_dir` of the agent version, on the Teammate's machine ([handlers/agent.py:55-59][agenth]) **[V]**.
2. If the command exits with an error, the worker sets the task to `failed` ([task_runner.py:141-150, 249-260][runner]) **[V]**.
3. If the working directory does not exist, the spawn raises an error, and the worker sets the task to `failed` with "Task runner failed" ([platforms/posix.py:42-53][posix], [worker.py:422-426, 440-445][worker]) **[V]** for the code path, **[I]** that a missing directory raises there.
4. A failed task is terminal. Only a stale task goes back to the queue ([task_service.py:345-358][tsvc]) **[V]**.
5. The job fails, and the replay fails ([task_transitions.py:50-52][tt], [replay_pipeline.py:380-381][rp]) **[V]**.

So one wide worker can make replays of a Run fail, and no setting of the Fargate worker prevents it **[I]**.

A wide worker can also claim the evaluator tasks of a Loop. It can run them if it has Python, `uv` and the keys that the evaluator needs ([deploy/workers.md:10][dw]) **[V]**.

The reverse is also true. A Fargate worker with an `evaluator` claim and no job ID claims the evaluator tasks of other Teammates and of other Loops (section 3) **[V]**.

## 7. Scopes that one Fargate task for a Loop can use

This section is the author's reasoning from sections 1 to 6. All **[I]**.

| Shape | Claims only the work of its Loop? | Cost |
| --- | --- | --- |
| A. One worker for the Loop: `agent=<version>` and `evaluator`, no job ID | No. It also claims agent tasks of other Loops and replays on the same agent version, and evaluator tasks of all jobs. | None in the container. This is the scope that the earlier note recommends. |
| B. One job-scoped worker process for each job of each Run, with `agent=<version>` and `evaluator` | Yes | A supervisor process in the container. For each Run it lists the jobs of the Run and starts one worker process for each job. It needs the Run IDs of the Loop and a key that can read them. |
| C. One worker for the Loop with `agent=<version>` only, and one job-scoped worker with `evaluator` only for each job | Evaluator tasks: yes. Agent tasks: only by agent version. | The same supervisor as B. Fewer processes during the agent tasks. |

- Shape B is the only shape that claims only the work of the Loop. It follows the one-shot pattern that the Kitaru docs give for CI: `kitaru worker start --job-id "$JOB_ID" --timeout 1800` ([deploy/workers.md:57-61][dw]) **[V]**.
- In shape B each worker process stops when its job settles (section 4). Give each a `--timeout`, because a wrong job ID does not stop the process.
- In stage 2 each Experiment has a new agent version. A job-scoped worker can use an `agent` claim with no version, because the job ID already limits it. So shape B does not need to know the agent versions before they exist.
- No shape reserves the work. A wide worker of a Teammate can claim the tasks first in each shape (section 6). To prevent that, each other worker on the server must have a narrow scope. That is a rule for the team, not a Kitaru setting.

## 8. Check of the earlier research

Confirmed against the source:

- A scope has `claims`, `selectors` and `job_id` only, and no scope for a Run or an Experiment (section 1).
- One Run makes one job and one agent task for each ready session (section 2).
- An agent task has two labels, both the agent version ID (section 2).
- Only the `agent` claim can have an ID (section 1).
- A job ID filters claims and does not reserve the job (section 6).
- A job-scoped worker stops when its job is settled (section 4).
- The claim loop stops only on a signal, at the lifetime timeout, or when the job of a job-scoped worker is settled ([worker.py:300-343, 387-405][worker]).
- The timeouts and limits: heartbeat timeout 60 s, retry limit 3, pending job timeout 3600 s, evaluator task 300 s, concurrency 10 (section 4, [server/config.py:69-75][scfg], [config.py:29][wcfg]).
- The `--drain-timeout` option and `KITARU_WORKER_DRAIN_TIMEOUT` exist ([worker_commands.py:73-79][wcmd], [config.py:36, 49][wcfg]). Section 15 of the earlier note lists the variable name as not checked again. It is now checked.

Corrections and additions:

1. **Baseline evaluator tasks.** Section 2 of the earlier note says that a job gets its evaluator tasks when the agent task completes. That is true for result evaluator tasks. The server also makes baseline evaluator tasks at the start of the Run when the baseline evaluation mode is not `none` (section 2). The Evalve backend sends `none`, so the Loops of today are as the earlier note says.
2. **"A job that no worker claims is canceled after 3600 s."** This is true only for a job that is still `pending`. A pending task in a running job has no timeout (section 4). The earlier note does not say this.
3. **"Two workers at the same time cause no problem" (section 13).** This is true for two workers that can run the agent. It is not true for a wide worker on a machine that cannot run the agent: the task fails and is not tried again (section 6).
4. **"The `evaluator` claim takes other work on the server" (section 13).** Confirmed. A job scope removes this cost, at the price of one worker process for each job (section 7). The earlier note does not consider several job-scoped workers in one task.
5. **Stage 2 (section 13).** The earlier note says that a worker for a Loop needs an `agent` claim with no ID, which takes the tasks of other agents. With a job scope that claim takes no other work (section 7).
6. **The evaluator claim and "only label selectors and the provider-credentials rule limit it" (section 11).** Confirmed, with one detail: no evaluator label names a Loop, a Run, an Experiment or an agent version, and the namespace label has one possible value (section 3).

## 9. Not verified

- All behaviour above is read from source. No worker, server or Fargate task was run.
- That a missing `working_dir` raises an error in the spawn call, and not a non-zero exit. The result is a failed task in the two cases.
- The safety of the uv cache when several processes use it at the same time.
- Two `Worker` objects in one Python process.
- The memory, CPU and start time of several worker processes in one Fargate task.
- The Kitaru source after 0.27.6. The earlier note says that no file under `src/kitaru/worker/` changed in 0.27.7. This note did not check that again, and did not read the server changes of 0.27.7.
- Open issues or plans in Kitaru for a scope by Run, an `evaluator` claim with an ID, or an idle exit.
- How the supervisor of shape B gets the Run IDs of a Loop. That is a design question for the map, not a Kitaru fact.

[k]: https://github.com/zenml-io/kitaru/tree/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91
[wmodel]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/api_models/v1/worker.py
[tmodel]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/api_models/v1/task.py
[rmodel]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/api_models/v1/replay.py
[base]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/base.py
[wcfg]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/config.py
[worker]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/worker.py
[bcache]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/blob_cache.py
[proc]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/process.py
[runner]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/task_runner.py
[hooks]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/hooks.py
[agenth]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/handlers/agent.py
[evalh]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/handlers/evaluation.py
[posix]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/worker/platforms/posix.py
[wcmd]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/cli/worker_commands.py
[wcli]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/cli/workers.py
[cexc]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/client/exceptions.py
[wrouter]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/workers.py
[errouter]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/experiment_runs.py
[trepo]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/db/repositories/task_repository.py
[jrepo]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/db/repositories/job_repository.py
[torm]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/db/orm/task.py
[wdom]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/domain/worker.py
[tdom]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/domain/task.py
[jdom]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/domain/job.py
[names]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/domain/names.py
[wsvc]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/application/services/worker_service.py
[tsvc]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/application/services/task_service.py
[tt]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/application/services/task_transitions.py
[js]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/application/services/job_service.py
[pr]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/application/services/plugin_resolution.py
[rp]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/application/services/replay_pipeline.py
[es]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/application/services/experiment_service.py
[sweep]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/api/task_sweeper.py
[scfg]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/config.py
[dw]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/deploy/workers.md
