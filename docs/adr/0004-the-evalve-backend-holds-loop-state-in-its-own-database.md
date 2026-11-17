# The Evalve backend holds Loop state in its own database

Decided 2026-11-16. Story [sc-232](https://app.shortcut.com/team-4-1/story/232). This ADR replaces ADR 0002.

ADR 0002 said that Evalve deploys no Loop state API, because Kitaru stores all that an MVP Loop needs. The epic [01 Agent-driven Loops on AWS](https://app.shortcut.com/team-4-1/epic/26) changes that: a Teammate's coding agent runs a Loop through the Evalve MCP, and one service must own the Loop rules and keep the state between calls. Kitaru cannot hold that state. We decided to run the Evalve backend as a container on the existing EC2 host, with its own database on the PostgreSQL server of Kitaru, and with no credential of its own for Kitaru. We chose this because it adds one container and one database to the deployment, and no host, no secret and no account.

The terms Loop, Experiment, Run and Evalve backend are in `GLOSSARY.md`.

## Decision

1. **Where the backend runs.** A container on the existing EC2 host, with `--network host`, bound to `127.0.0.1:8100`. Caddy sends `/evalve/*` to it. The IMDS hop limit stays 1. The host stays a t3.small. The memory spike [sc-236](https://app.shortcut.com/team-4-1/story/236) can change the host size.
2. **Where Loop state lives.** A separate database named `evalve` on the existing RDS instance of Kitaru. The backend creates the database at startup if it is missing. The migrations of Kitaru never touch it, and the backend never touches the database of Kitaru.
3. **Database credentials (interim).** The backend uses the PostgreSQL user of Kitaru, from the existing application secret. The secrets ADR ([sc-233](https://app.shortcut.com/team-4-1/story/233)) decides the final credentials.
4. **Restore and `down`.** One RDS snapshot holds the database of Kitaru and the `evalve` database at the same point in time. The backend leaves a restored `evalve` database as it is. Restore and `down` have no special step for Loops: a Loop that was `running` in the snapshot continues on the next call.
5. **Which repo holds the backend code.** [`evalve-app/evalve-backend`](https://github.com/evalve-app/evalve-backend). A `v*` tag publishes the public image `ghcr.io/evalve-app/evalve-backend`. `evalve-deploy` pins the image by digest, as it pins the server image. The same repo publishes the local Evalve MCP as a second package. The MCP calls the backend over HTTP and never runs on AWS.
6. **How the backend checks a caller.** On each request, the backend sends the caller's Bearer credential (a `KITKEY_` key or a Kitaru token) to Kitaru `GET /api/v1/accounts/me`. It never stores or logs the credential. A missing, invalid or expired credential, or a deactivated account, gets 401. If Kitaru cannot answer, the caller gets 503. The story [sc-313](https://app.shortcut.com/team-4-1/story/313) decides whether a short cache is acceptable, and what a caller is allowed to do. Cookie sign-in for the dashboard comes in [sc-284](https://app.shortcut.com/team-4-1/story/284).
7. **Which identity the backend uses for Kitaru.** The caller's key, on each request. The backend has no Kitaru key of its own, so each Kitaru experiment belongs to the Teammate. The backend does no work with no caller attached. A service account ([sc-287](https://app.shortcut.com/team-4-1/story/287)) comes with the epic "Experiments on Fargate", when the backend must work in the background.
8. **What the backend holds in this epic.** Loop state only: the Loop, its Experiments and their Runs. It holds no AWS credential of its own, no GitHub credential and no approval record. Those come with the later epics and with the secrets ADR.

## Considered options

* **A Loop state API module in `evalve-deploy`** (the fallback in ADR 0002). Rejected: the backend has its own release cycle, and a deploy tool must not hold application code.
* **A schema in the database of Kitaru.** Rejected: the migrations of Kitaru own that database, and a Kitaru upgrade could change or remove tables that it does not know.
* **A second RDS instance.** Rejected: it adds cost and a second backup that must stay in step with the first.
* **A file on the host (SQLite).** Rejected: each change of the dashboard step replaces the EC2 instance, and the file would go with it.
* **A separate host or a Fargate service for the backend.** Rejected for now: it adds cost and a network path for a service that only answers calls.
* **A Kitaru key or service account for the backend.** Rejected for this epic: it is a new secret, and each experiment would belong to the backend and not to the Teammate. It becomes necessary when the backend works in the background (sc-287).

## Consequences

* **The backend container can read the credentials of the instance role.** With the host network, IMDS is one hop from the container. The Kitaru and UI containers cannot reach IMDS. This is acceptable only because the backend runs Evalve code and never agent code. Do not run agent code in this container.
* **The backend can read and write the tables of Kitaru** while it uses the PostgreSQL user of Kitaru (point 3). The code does not do so. The secrets ADR must close this.
* That PostgreSQL user must be allowed to create a database.
* A Loop moves only while a caller is connected. A run in flight finishes in Kitaru, and the next call collects it.
* Each request costs one call to Kitaru `/accounts/me`, until sc-313 decides on a cache. If Kitaru is down, the backend refuses each call with 503.
* Caddy gets one route, `/evalve/*`, before the dashboard route. The deployment gets one optional systemd unit and one image variable (`backend_image`). The story [sc-246](https://app.shortcut.com/team-4-1/story/246) builds them.
* `evalve up` can check the running build, because `GET /evalve/health` returns the image reference.
* Revisit when the backend must work in the background (sc-287), when it gets AWS or GitHub credentials, when the memory spike sc-236 has results, or when the secrets ADR sets the database credentials.
