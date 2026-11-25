# The instance role reads three named secrets

Decided 2026-11-25. Story [sc-233](https://app.shortcut.com/team-4-1/story/233). This ADR replaces the "only two secrets" policy that ADR 0001 names.

Until now, IAM and one Terraform assertion let the instance role read two secrets only: the Application secret and the Database secret. The Evalve backend (ADR 0004) uses the master user of the database from the Database secret, so it can read and write the tables of Kitaru. We decided to give the backend its own PostgreSQL role, and to keep the password of that role in a third secret, `evalve/backend`. We chose this because it closes the open point of ADR 0004 with one secret and one setup step, and because the same secret is the place for later backend keys that must not go back to an old value on a restore.

The terms Application secret, Database secret, Backend secret and Instance role are in `GLOSSARY.md`.

## Decision

1. **The rule.** The instance role reads three secrets, each by its name. It reads the Backend secret only when a backend image is set. No other role reads Secrets Manager. A new secret needs a change to this ADR.

   | Secret | Name | Keys | Reader |
   |---|---|---|---|
   | Application secret | `evalve/application` | `jwt_signing_key`, `encryption_key`, `admin_password` | Instance role |
   | Database secret | Made and named by RDS | `username`, `password` (the master user) | Instance role |
   | Backend secret | `evalve/backend` | `db_user`, `db_password` | Instance role |

   One Terraform assertion checks the exact list: three secrets with a backend image, two with none.
2. **The database user of the backend.** A PostgreSQL role `evalve` owns the database `evalve` and each object in it. The role cannot create a database or a role, and it cannot connect to the database of Kitaru. The running backend holds only the credentials of this role.
3. **What the separate role protects.** It protects the tables of Kitaru against a mistake in the backend, such as a wrong migration. It does not protect against a backend that an attacker controls, because the backend container can read the credentials of the instance role, and thus the Database secret (point 8).
4. **The Backend secret.** `evalve up` makes it when a backend image is set, with a random password, as it makes the Application secret. No version of it is pinned: the server always reads the newest value.
5. **Why these keys are not in the Application secret.** There is one reason for each type of key.
   * The database password: a new key in the Application secret makes a new version of that secret. The version is a part of the EC2 user data, so each existing Deployment would replace its EC2 instance, and each rotation of the password would do the same.
   * A key that comes from outside the Deployment, such as the key of the GitHub App: `down` and a restore pin the version of the Application secret to the snapshot, so a restore would bring back an old key.
6. **The database setup step.** On each refresh, before the backend starts, the server runs the backend image once with the credentials of the master user. This step makes the database `evalve` and the role `evalve` when they are missing, makes the role the owner of the database and of each object in it, removes the rights of point 2, and sets the password of the role to the value in the Backend secret. A second run changes nothing. The step changes the owner of each object by its name. It does not use `REASSIGN OWNED`, which would also move the database of Kitaru. If the step fails, the backend stays stopped, and Kitaru and the dashboard run as before. The running backend no longer makes its database.
7. **One path for each case.** The step of point 6 is the only path for a first install, for a Deployment whose database belongs to the master user, for a rotation and for a restore.
   * Rotation: write a new value to the Backend secret. The next refresh sets it in the database and restarts the backend. The command for this belongs to the key rotation story ([sc-267](https://app.shortcut.com/team-4-1/story/267)).
   * Restore: the snapshot brings back the password that the role had at the time of the snapshot. The next refresh sets it to the value in the secret. A restore has no special step.
   * `down`, and the removal of the backend image: the Backend secret, the role and the database stay.
8. **The instance role.** The backend container uses the host network, so it can read the credentials of the instance role. ADR 0004 accepts this because the backend runs no agent code. That is still true, and it is not the full reason now: from the epic "03 Experiments on Fargate" the backend needs the role for its own work, so the role cannot be hidden from the container. We accept the wider role for the pilot, with three limits that Terraform assertions check:
   * `iam:PassRole` permits only the one execution role, and only for ECS tasks.
   * The execution role reads no secret, and no task role exists.
   * `sts:AssumeRole` permits only the push-only role.
9. **A secret in a `RunTask` override.** No secret goes into a `RunTask` override, because `DescribeTasks` returns the overrides. There is one exception: the channel secret of the Loop task (ADR 0006 point 13). It is good only for the Assignments of one Loop task, and it ends with the task.
10. **Later keys.** The Git flow ([sc-255](https://app.shortcut.com/team-4-1/story/255)) is not a part of the current epics. When it arrives, the App ID and the private key of the GitHub App go into the Backend secret. That story sets the key names.

## Decided in ADR 0006

[ADR 0006](0006-an-experiment-of-a-loop-is-a-commit-and-an-agent-version.md) point 13 and its Consequences hold the rest of the secrets policy. This ADR records these points and does not decide them.

* **No secret `evalve/worker`.** The Loop task reads nothing from Secrets Manager. It has no task role, and the execution role reads no secret.
* **No Kitaru key for the backend.** For a Loop, the backend acts only with the credential of the caller. A backend key comes back only with a background runner ([sc-287](https://app.shortcut.com/team-4-1/story/287)).
* **The secrets of an agent are Kitaru secrets on the agent version**: the model key, and `DATABASE_URL` for the pilot. The builder of the agent image attaches them by name, and the builder can be a Teammate. Judge keys come only from a provider connection. At Kitaru 0.27.6 a secret name is unique for the server, and each account can read the value of each secret.
* **The channel secret** is the one secret in a `RunTask` override (point 9). There is no presigned source URL.
* **Worker tokens.** The backend stores the worker tokens of the open Run in plain form. A token lives 3600 s.
* **The ECR token.** The backend gives each active Kitaru account a token from the push-only role for the one repository of agent images. The session is 900 s, but the token can live 12 hours (not verified). It can push and cannot delete.
* **Accepted for the pilot.** Agent code can read the worker token of its Replay and the channel secret, because it runs as the same OS user as the worker and the Supervisor.

## Considered options

* **Keep the master user.** Rejected: no new secret, but a mistake in the backend can damage the tables of Kitaru, and the user must be allowed to create a database.
* **A role with IAM login and no password.** Rejected: it saves the secret, but the backend must get a new login token every 15 minutes, and a later backend key still needs a secret.
* **The password in the Application secret.** Rejected: see point 5.
* **`psql` on the server for the setup step.** Rejected: the backend image already has the PostgreSQL driver and the code that makes the database.
* **The master credentials in the running backend, which lowers its own rights.** Rejected: the master password would stay in the environment of the backend.
* **One secret for each role, with one reader for each secret** (the draft of 2026-11-16). Rejected: with no worker secret, each secret has the same reader, so the rule says nothing.

## Consequences

* A backend that an attacker controls gets the master password of the database, the password of the Admin account and the signing key of Kitaru, and it can run each image as a task in the account. The backend must not run agent code, or code that a Teammate supplies.
* The backend image gets one command for the database setup step. `infra/refresh.py` runs it and reads the Backend secret. `evalve up` makes a secret by its name. The story [sc-253](https://app.shortcut.com/team-4-1/story/253) builds these parts.
* The assertion "The server role must only read the two Evalve secrets" changes to the list of point 1.
* The first `evalve up` after this change moves the database of a live Deployment to the role `evalve`. The backend restarts, and the EC2 instance is replaced only if the user data changes.
* Each secret costs about $0.40 for each month.
* Revisit when the backend runs code that a Teammate supplies, when a background runner arrives (sc-287), or when the Git flow arrives (sc-255).
