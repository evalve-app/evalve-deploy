# Kitaru worker credentials, and what agent code can read

Research for Shortcut story sc-384. Date: 2026-11-20.

## Scope and sources

- **Question.** In Kitaru 0.27.6, which credentials can a worker use, and what can the agent code that it starts read?
- **Primary source.** The Kitaru source at tag `python/kitaru/v0.27.6`, commit `96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91`. Each `path:line` below is a path in that repository at that commit. Base URL: `https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/`.
- **Earlier research that this note checks.** `kitaru-worker-on-aws.md` and `kitaru-workers-for-loops-on-aws.md` (untracked files in `docs/research/` of the main checkout of this repository), `evalve-backend/docs/research/kitaru-runner-assumptions.md`, and story sc-257.
- **Deployment.** The AWS deployment uses the `local` auth scheme. All statements are for `local` unless the text says otherwise.
- **Labels.** **[V]** verified in the source (cited). **[T]** tested on a local Linux machine. **[I]** inferred from verified facts; not tested. **[U]** not verified.
- No live server was used. No secret value was read.

## Short answers

1. **Credential types.** Only a *worker token* can claim tasks. Only an *account credential* (a `KITKEY_` API key or a session token) can register a worker and renew its token. An API key has no expiry. A worker token and a session token live 3600 s by default. A task token lives for the task timeout plus 300 s.
2. **Limits on a credential.** An API key cannot be limited. It can do all that its account can do. The limit is on the *worker*, not on the key: the worker scope (task kinds, one agent version, label selectors, one job). There is no scope for a Loop, an Experiment or a Run.
3. **Is the key removed?** Both statements are true. The worker removes `KITARU_API_KEY` from the environment of the agent subprocess. But the subprocess runs as the same OS user as the worker, and it can read the key from `/proc/<worker pid>/environ`. So agent code can read the worker's credential unless the worker and the agent are isolated.
4. **Service account.** A service account has the same rights as a Teammate's account that is not an admin. Its rights cannot be reduced. **At 0.27.6 no API route issues an API key for a service account.** Story sc-257 and both older notes assume such a route.
5. **Second party.** Yes. The Evalve backend can register a worker with its own key and give the Fargate task only the worker ID and the worker token. This token is short-lived (3600 s by default), the worker cannot renew it, and it cannot do what an account can do. The Modal backend of Kitaru uses the same method.

## 1. Credential types

There are four credentials. The server tells them apart by the prefix and by the token subject.

| Credential | What it is | Lifetime | Renewal | Revocation |
| --- | --- | --- | --- | --- |
| API key (`KITKEY_…`) | A key that an account owns. | No expiry. The key record has no expiry field. | `rotate` gives a new value. The old value can stay valid for `retain_period_minutes`. | Set `active=false`, delete the key, or deactivate the account. |
| Session token | A JWT with an account subject. `POST /api/v1/login` gives one for a password or an API key. | `JWT_LIFETIME_SECONDS`, default 3600. | Log in again. | Not possible before expiry, except by deactivation of the account. |
| Worker token | A JWT with a worker subject (worker ID and account ID). | `WORKER_TOKEN_LIFETIME_SECONDS`, default 3600. | `POST /api/v1/workers/{id}/token`, with an account credential of the account that registered the worker. | Delete the worker: claims and heartbeats then fail, but the token still passes the other worker routes until it expires. Deactivate the account: all routes fail. |
| Task token | A JWT with a task subject (task, attempt, worker, account, job, grants). | Task timeout plus `TASK_TOKEN_EXPIRY_LEEWAY_SECONDS` (300). An agent version has a default timeout of 3600 s. | None. | None. A new attempt of the task makes the old token fail on task-checked routes. |

Sources **[V]**:

- Key or token by prefix: `src/kitaru/server/adapters/auth/auth_service.py:134-139`.
- API key fields, no expiry: `src/kitaru/server/domain/api_key.py:62-75`, `src/kitaru/server/adapters/db/orm/api_key.py:58-65`. Rotation: `domain/api_key.py:93-121`. An inactive key fails: `auth_service.py:430-431`.
- Lifetimes: `src/kitaru/server/api/config.py:75-77`. Environment variables use the prefix `KITARU_SERVER_` (`src/kitaru/server/config.py:51`).
- Token issue: `auth_service.py:263-349`. Token subjects: `src/kitaru/server/adapters/auth/jwt.py:85-214`.
- A session token from an API key stays valid after the key is deleted: `auth_service.py:210-213`.
- Each token check loads the account and fails if the account is not active: `auth_service.py:455-497`.
- Agent version timeout: `src/kitaru/api_models/v1/agent_version.py:54`.

### Which credential does what

| Action | Route | Accepted credential | Source **[V]** |
| --- | --- | --- | --- |
| Register a worker | `POST /api/v1/workers` | Account only (API key or session token) | `src/kitaru/server/adapters/rest/routers/workers.py:92-135` |
| Renew a worker token | `POST /api/v1/workers/{id}/token` | Account only, and the same account that registered the worker | `workers.py:138-164`, `src/kitaru/server/application/services/worker_service.py:200-214` |
| Claim tasks | `POST /api/v1/tasks/claim` | Worker token only | `src/kitaru/server/adapters/rest/routers/tasks.py:86-124` |
| Heartbeat | `POST /api/v1/workers/{id}/heartbeat` | Worker token only | `workers.py:228-252` |
| Report a task result | `PATCH /api/v1/tasks/{id}` | Task token only | `tasks.py:174-199` |

The rules for each route are in `src/kitaru/server/adapters/rest/dependencies/auth.py:323-459`. A route that uses `authorize` gives 403 to a worker token and to a task token (`auth.py:340-349`).

So an API key alone cannot claim a task. The worker changes it into a worker token first.

### How the worker uses them

A worker has two start modes **[V]**:

- **Key mode.** The environment has `KITARU_API_KEY`. The worker registers itself, gets a worker token, and renews it with the key when the server answers 401 (`src/kitaru/worker/worker.py:215-240`, `src/kitaru/worker/auth.py:21-59`, `src/kitaru/client/auth.py:129-156`).
- **Token mode.** The environment has `KITARU_WORKER_ID` and `KITARU_API_TOKEN` and no key. The worker loads the registered worker and uses the token as a fixed bearer. It cannot renew, because the renew route does not accept a worker token (`worker.py:242-263`, `src/kitaru/client/api_client.py:160-163`). `KITARU_WORKER_SCOPE` must not be set in this mode; the scope comes from the server (`src/kitaru/worker/config.py:54-66`, `worker.py:257`).

A third mode exists: `KITARU_WORKER_ID` with an API key. The worker then renews through the key, and the key must be of the account that registered the worker (`worker.py:263`, `worker_service.py:212-213`).

**Revocation of the API key does not stop a worker that runs.** The worker token has no reference to the key (`jwt.py:124-156`) **[V]**. **[I]** The worker continues until the token expires, at most 3600 s by default. Then the renewal fails. Story sc-257 says that the team can revoke the worker's key alone; this is true with that delay. Deactivation of the account stops the worker at once.

### Does a worker token survive a server restart?

**[I]** Yes, if `KITARU_SERVER_JWT_SIGNING_KEY` does not change. Tokens are signed JWTs and the server keeps no token record (`jwt.py:27`, `api/config.py:72`, `162-163`). The older note left this open. Not tested on a live server.

## 2. Can a credential be limited?

- **API key: no.** A key has a name and an owner only (`src/kitaru/api_models/v1/api_key.py:25-28`, `domain/api_key.py:62-75`) **[V]**. It resolves to its account (`auth_service.py:432-434`), and the account has all rights (section 4).
- **Worker: yes, by its scope.** The scope is stored on the worker at registration. The claim route uses the stored scope, not a value from the request (`src/kitaru/server/application/services/task_service.py:116-124`) **[V]**. A holder of a worker token cannot change the scope: no route updates a worker (`workers.py`, all routes) **[V]**.
- **What a scope can name** **[V]** (`src/kitaru/api_models/v1/worker.py:45-83`):
  - task kinds, in 1 to 16 claims;
  - one agent version on an `agent` claim;
  - label selectors;
  - one job.
- **No scope for a Loop, an Experiment or a Run.** An agent task has only the agent version labels (`src/kitaru/server/application/services/job_service.py:53-68`) **[V]**. One job is one replay of one session; `kitaru-workers-for-loops-on-aws.md` shows this, and this note did not check it again.
- **The scope does not limit who registers.** Each account can register a worker with each scope, and can delete each worker (`worker_service.py:100-135`, `234-245`) **[V]**.

So: "one agent version" is possible, "one job" is possible, "one Loop" is possible only when the Loop has its own agent version. The limit applies to the worker token. It does not apply to the API key that made the worker.

## 3. Does the worker remove its key from the agent subprocess?

### What the worker does **[V]**

`build_process_env` makes the environment of each task process (`src/kitaru/worker/process.py:326-363`):

1. It copies the full environment of the worker.
2. It adds the `env` of the run spec, the task extras, and the secret env.
3. It removes these names: `KITARU_API_URL`, `KITARU_API_KEY`, `KITARU_API_TOKEN`, `KITARU_TASK_ID`, `KITARU_TASK_INPUTS`, `KITARU_REPLAY_ID`, `KITARU_TASK_PLUGIN_PATH`, `KITARU_TASK_PAYLOAD_PATH`, `KITARU_TASK_RESULT_PATH`, `KITARU_CREDENTIALS_PATH` (`process.py:51-64`).
4. It sets `KITARU_API_URL`, `KITARU_TASK_ID`, and `KITARU_API_TOKEN` (the task token).

The agent handler uses this function (`src/kitaru/worker/handlers/agent.py:49`). The `setup_command` and `teardown_command` hooks run with the same environment (`src/kitaru/worker/hooks.py:171-187`). Step 3 comes after step 2, so an agent version cannot put `KITARU_API_KEY` back through its `env`.

So the docs are correct: "your broad API key is stripped from the child environment" (`docs/book/deploy/authentication.md:70`). In token mode, the worker token is removed in the same way, because it is in `KITARU_API_TOKEN`.

### What the worker does not do

- **It does not change the OS user.** The task process starts with `create_subprocess_shell` in a new session, as the same user (`src/kitaru/worker/platforms/posix.py:42-58`) **[V]**.
- **[T]** A child that starts in this way can read `/proc/<parent pid>/environ`. Test: a Python parent with a dummy variable, a child from `asyncio.create_subprocess_shell(..., env=<without the variable>, start_new_session=True)`. The child did not have the variable in its environment, and it read the variable from the parent's `/proc` entry. Machine: Linux 7.3.4, `kernel.yama.ptrace_scope = 0`. Not tested on Fargate, and not tested with `ptrace_scope = 1`.
- **[I]** So agent code can read `KITARU_API_KEY` (key mode) or the worker token (token mode) of the worker that started it. Story sc-257 is correct in effect.
- **All other variables pass to the agent** (`process.py:354`) **[V]**. This includes model keys in the worker environment. **[I]** On Fargate it also includes `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, so agent code can use the task role.
- **Stored logins.** The Kitaru CLI does not use `KITARU_API_TOKEN`, and can use a login that is stored on disk. The docs say: "Run workers under a dedicated OS or container identity with no broader stored Kitaru credentials" (`authentication.md:72`) **[V]**. A Fargate task that gets its credential only from the environment has no stored login **[I]**.

### What agent code can do with each credential

| Credential that agent code holds | What it permits |
| --- | --- |
| Task token (always) | Read and update its own task (`task_service.py:215-218`, `256-259`, `289-292`). Create sessions. Read and write the sessions of its own task. Read sessions and blobs that are in its grants (`src/kitaru/server/application/services/resource_access.py:116-180`). A replay task gets blob grants for the recorded files only (`resource_access.py:98-105`). Routes: `routers/sessions.py`, `routers/replays.py:112`, `routers/blobs.py:61-111`, `routers/tasks.py:131-179`. All other routes give 403. **[V]** |
| Worker token (read from the parent, token mode) | Claim each task in the worker's scope. Each claim returns the task spec with its secret env and a task token (`tasks.py:108-124`, `agent.py:49`). Read the worker, read a job by ID (`routers/jobs.py:73`; the job service has no check for a worker), download a blob by ID (`blobs.py:111`; the blob check applies only to a task token, `resource_access.py:177-178`). All other routes give 403. **[V]** for the routes, **[I]** for the attack. |
| API key (read from the parent, key mode) | All that the account can do (section 4). **[I]** |

## 4. Service account in comparison with a Teammate's account

**Rights** **[V]**:

- Under `local`, the permission provider is `AdminFlagPermissionProvider` (`src/kitaru/server/adapters/rest/dependencies/services.py:159-161`).
- It refuses four actions to an account that is not an admin: create an account, update an account, deactivate an account, set the admin flag. It permits all other actions on all resources (`src/kitaru/server/adapters/permissions/admin_flag.py:26-77`).
- The docs say the same: "everyone authenticated can read and write everything" (`authentication.md:8`). This agrees with the map for epic 01.
- A service account cannot be an admin. The update that sets the admin flag loads only user accounts (`src/kitaru/server/application/services/account_service.py:270-283`); the docs say it at `authentication.md:78`.
- API keys are the exception to "read everything": an account sees and changes only its own keys (`src/kitaru/server/application/services/api_key_service.py:62-155`).

So a service account has the same rights as a Teammate's account that is not an admin. **Its rights cannot be reduced.** No role, permission or scope field exists on an account or on a key. The only switch is `active` (`account_service.py:316-346`, admin only).

**A service account cannot get an API key through the API at 0.27.6.** This is new, and it contradicts sc-257 and both older notes.

- A service account is created "active without credentials" (`account_service.py:146-169`; request fields are `name` and `email`, `src/kitaru/api_models/v1/account.py:59-63`) **[V]**.
- `POST /api/v1/api-keys` takes only `name`, and the key always belongs to the caller (`api_key.py:25-28`, `src/kitaru/server/adapters/rest/routers/api_keys.py:47-71`, `api_key_service.py:43-60`) **[V]**. Request models refuse unknown fields (`src/kitaru/api_models/v1/base.py:91-94`), so an owner field gives 422.
- `api_key_service.py:58` is the only place in `src/` that makes a key record **[V]** (search for `ApiKey(owner_id`, `encode_api_key(` and `create_api_key(`).
- A service account has no password and no activation token, so it cannot log in to make its own key (`auth_service.py:183-189`) **[V]**.
- The docs say "Create one with `client.service_accounts.create(...)`, then issue it an API key" (`authentication.md:97`) and do not say how. The Python client has no such method (`src/kitaru/client/resources/service_accounts.py`, `src/kitaru/client/resources/api_keys.py`) **[V]**.
- **[U]** Not tested on a live server. The UI source and later Kitaru versions were not read. Under the `control_plane` scheme, service accounts come from the control plane (`src/kitaru/server/adapters/auth/control_plane.py:279-298`); that scheme is not used here.

**What works instead** **[I]**: an admin creates a *user* account that is not an admin and has a password (`POST /api/v1/users`), logs in as that account, and creates a key with `POST /api/v1/api-keys`. The rights are the same as those of a service account. The account has a password that must be stored or thrown away. The team must change steps 2 and 3 of sc-257, or check on the live server how the tracer key of sc-264 was made.

## 5. Can the Evalve backend register a worker and give the task a short-lived credential?

**Yes** **[V]** in the source, **[U]** on a live server.

1. The backend calls `POST /api/v1/workers` with its own key and a scope, for example one `agent` claim for the agent version of the Loop and one `evaluator` claim. The answer has the worker ID, a worker token, and `token_expires_at` (`workers.py:92-135`).
2. The backend starts the Fargate task with `KITARU_API_URL`, `KITARU_WORKER_ID`, `KITARU_API_TOKEN` and, as an upper limit, `KITARU_WORKER_TIMEOUT`. It does not set `KITARU_API_KEY` or `KITARU_WORKER_SCOPE`.
3. The worker starts in token mode (`worker.py:188-192`, `242-263`).
4. The backend deletes the worker when the Run or the Loop ends (`DELETE /api/v1/workers/{id}`).

Kitaru does the same for its Modal workers: the server registers a worker, issues a worker token, and starts the sandbox with these four variables and the command `python -m kitaru.worker` (`src/kitaru/server/adapters/rest/ephemeral_workers.py:118-139`, `src/kitaru/server/adapters/ephemeral_workers/modal.py:77-83`, `src/kitaru/server/ephemeral_worker_settings.py:24`).

Limits:

- **Lifetime.** The public route always uses `WORKER_TOKEN_LIFETIME_SECONDS`, default 3600 s. The request has no lifetime field (`workers.py:126-128`, request model at `src/kitaru/api_models/v1/worker.py:150-158`) **[V]**. Only the Modal path of the server can ask for a different lifetime (`ephemeral_workers.py:124-128`). The setting is for the whole server.
- **No renewal in the task.** The token is fixed for the life of the process (`worker.py:259-262`) **[V]**. The backend can get a new token from the renew route, but a worker that runs cannot take it. When the token expires, claims fail and the worker retries with a delay of up to 60 s until its timeout (`worker.py:326-329`). **[I]** For one Fargate task for each Loop, the Loop must end within the token lifetime, or the team raises `KITARU_SERVER_WORKER_TOKEN_LIFETIME_SECONDS`, or the backend starts a new task with a new worker.
- **Held tasks at expiry** **[U]**. The task process has its own task token, so it can still report its result **[I]**. The heartbeat fails, and the server can then treat the worker as not live (`WORKER_LIVENESS_TIMEOUT_SECONDS = 60`, `src/kitaru/server/config.py:61`). The code that requeues the tasks of a dead worker was not read.
- **Revocation.** After the worker is deleted, a claim gives 404 (`task_service.py:119`, `tasks.py:95-96`) and so does a heartbeat (`workers.py:237-238`) **[V]**. The deletion is not a full revocation **[I]**: the worker-token check loads only the account, not the worker (`auth_service.py:455-461`), so the token still passes the job read and the blob download until it expires. Task tokens that were already issued also stay valid until they expire; the token check does not load the worker (`auth_service.py:463-476`).
- **Start command.** `python -m kitaru.worker` reads only the environment (`src/kitaru/worker/__main__.py:22-27`) **[V]**. The older note says that it installs no SIGTERM handler. **[U]** It was not checked that `kitaru worker start` works with a token and no key or stored login.
- **The token is still readable by agent code** (section 3). But it permits much less than a key: only the claims of that worker's scope, for at most the token lifetime, and the backend can end the claims by deletion of the worker.
- **The `evaluator` claim is wide.** Only an `agent` claim can name an ID (`api_models/v1/worker.py:54-66`) **[V]**. `kitaru-workers-for-loops-on-aws.md` shows that an `evaluator` claim matches the evaluator tasks of each job on the server. **[I]** So agent code that reads a worker token with an `evaluator` claim can claim evaluator tasks of other Loops and of Teammates, with their secret env and task tokens. A worker with only the `agent` claim for one agent version does not have this problem, but then a second worker must run the evaluators.
- **Job owner.** The account in a task token is the owner of the job, not the account of the worker (`tasks.py:116`) **[V]**.
- `kitaru-workers-for-loops-on-aws.md` says of this mode: "Treat it as internal. Do not build on it." It is not in `docs/book`. That is a judgement about support, not a fact about function. It is the only path at 0.27.6 that keeps a long-lived key out of the task.

## 6. Checks of the earlier research

Confirmed **[V]**:

- The worker changes its key into a worker token and renews the token by itself (`kitaru-worker-on-aws.md`, runtime table).
- The task process gets a task token for one task and attempt, and `KITARU_API_KEY` is removed from its environment (both notes).
- Model keys in the worker environment pass to each task (`process.py:354-357`).
- A worker can start with `KITARU_WORKER_ID` and `KITARU_API_TOKEN` and no key; it cannot renew; the token lives 3600 s (`kitaru-workers-for-loops-on-aws.md`, section 5).
- Under `local`, each account can read and write everything, except account administration and other accounts' API keys (`kitaru-runner-assumptions.md`, section 5).
- `kitaru-workers-for-loops-on-aws.md` infers that the agent process "can read what the worker can read". The local test in section 3 supports this.

Contradicted or corrected:

1. **"A `KITKEY_` key of a service account"** (sc-257 steps 2 and 3, both older notes, section 5 of `kitaru-runner-assumptions.md`, and the Kitaru docs at `authentication.md:97`). No API route at 0.27.6 issues a key for a service account. See section 4.
2. **sc-257: "Agent code in the task can read the worker's environment."** The environment that the agent gets does not have the key. The agent can read the key by another way, the worker's `/proc` entry. The result is the same; the reason is different, and the fix is isolation or token mode, not an environment change.
3. **Kitaru docs: "your broad API key is stripped from the child environment."** True, but not sufficient. The docs add the rule on a dedicated identity themselves (`authentication.md:72`).
4. **"Not verified: whether a worker token survives a Kitaru restart"** (`kitaru-worker-on-aws.md`). The source shows stateless signed tokens. See section 1.

## 7. Not verified

- All behaviour on a live server, and all behaviour on Fargate.
- That no path issues a key for a service account in the UI or in a version after 0.27.6. How the worker key of the tracer sc-264 was made.
- The `/proc` read on Fargate, or with `kernel.yama.ptrace_scope = 1`, or with a worker and an agent under different users.
- That `kitaru worker start` accepts token mode. Only `python -m kitaru.worker` was read.
- What the server does with held tasks after a token-mode worker loses its token (the task sweeper was not read).
- That a task token stays valid after its worker is deleted (inferred from the token check).
- The secret routes (who can read a Kitaru secret). `kitaru-runner-assumptions.md` says that secrets are limited to their owner; this note did not check it.
- The value of the lifetime settings on the AWS server. The defaults are from the source.

## 8. What this means for "one Fargate task per Loop"

This section is the author's judgement.

- The smallest credential for the Fargate task is the worker token of a worker that the backend registered. Then the task holds no account credential, and agent code can at most claim tasks of the same scope for the rest of the token lifetime.
- The cost is the 3600 s limit. A Loop that is longer needs a server setting or a new task. Decide this before the design is locked.
- If the task gets a `KITKEY_` key, agent code can get all rights of that account. A separate account for the worker does not reduce this, because all accounts have the same rights. It only makes revocation of that one key possible.
- Kitaru starts the agent as a subprocess of the worker, as the same user. It has no setting for a different user or a separate container. So isolation between the worker and the agent needs a change outside Kitaru, and none was tested.
- Do not give the Fargate task a task role with rights that agent code must not have. The agent inherits the variable that gives access to the role.
