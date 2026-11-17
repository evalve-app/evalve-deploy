# Evalve deploys the Evalve dashboard as a pinned UI image

Status: superseded by ADR 0003. This repo never shipped this design. The code is on the tag `archive/zach-ui-test`.

Evalve deploys the Evalve dashboard on the same EC2 host and the same hostname as the Kitaru server. The `evalve-ui` repo publishes a public OCI image to GitHub Container Registry that serves its static build. Evalve pins that image by digest in a `ui_image` variable, as it pins `server_image`. The bootstrap script starts the UI container on `127.0.0.1:3000`, and Caddy sends the UI paths to it. We chose this so that one `evalve up` deploys the server and the UI together, and `up --restore` brings back both at the new hostname. A UI that deploys itself would lose its API address after each restore.

## Considered options

- **Separate static hosting deployed by `evalve-ui`.** Rejected: the restore changes the API hostname, and a different origin needs CORS changes and bearer tokens that expire after one hour with no refresh.
- **Private image.** Rejected: EC2 would need a pull token in Secrets Manager, which breaks the policy that allows only two secrets. The image holds only static files and no secrets.
- **Kitaru `EXTERNAL_UI` and `DASHBOARD_URL`.** Rejected: the settings are not in the Kitaru user docs, and Caddy routing alone does the same job. See `evalve-ui` ADR 0001.

## Consequences

- `ui_image` is optional. When it is empty, Caddy sends all paths to Kitaru as before, and `--dashboard evalve` fails before Terraform runs. This repo can ship its part before `evalve-ui` publishes an image.
- `evalve up --dashboard bundled|evalve` selects the rollout step from `evalve-ui` ADR 0003. `bundled` keeps the bundled dashboard at `/` and serves the Evalve dashboard at `/app/`. `evalve` serves the Evalve dashboard at `/` and redirects `/app/*` to `/*`. Evalve saves the value in S3 state. The default for a new deployment is `bundled` until step 2 is done. Each change replaces EC2.
- Caddy sends `/api/*`, `/health*`, `/docs*`, `/redoc*` and `/openapi.json` to Kitaru in both steps. `infra/tests/deployment.tftest.hcl` asserts these routes.
- After the readiness check, `evalve up` checks that the UI path returns the Evalve dashboard and that `/api/v1/info` returns Kitaru.
- The UI image carries a label with the Kitaru version it targets. `evalve up` stops before Terraform runs when that label does not match the Kitaru version of `server_image`. The next section fixes how `up` reads both versions.

## Kitaru version contract

Added 2026-11-11 by ticket 01 of `.scratch/evalve-dashboard-rollout`.

**The server image has no version label.** On 2026-11-11, we read the pinned index `zenmldocker/kitaru-server@sha256:05ea0048…1d47` from Docker Hub with an anonymous token. The `linux/amd64` config (`sha256:5cdb892f…d857`) and the `linux/arm64` config (`sha256:93c9dab9…534a`) have `Labels: null`. The index, the platform manifests and the platform descriptors have no annotations. The two attestation descriptors carry only `vnd.docker.reference.*` keys. The environment has only `PATH` and `PYTHON_VERSION`. So `up` cannot learn the Kitaru version from the server image.

**Server side: a `kitaru_version` setting next to `server_image`.**

- The value is a plain Kitaru version, for example `0.27.6`, with no `v` prefix.
- The default is `0.27.6`. It sits beside the default digest in `infra/variables.tf` and beside `PHASE_TWO_IMAGE` in `src/evalve/lifecycle.py`. A change of one default changes the other in the same commit.
- `kitaru_version` is saved and carried forward like `server_image`, so `stop`, `start`, `down` and restore keep it.
- An Operator who overrides `server_image` must also give `kitaru_version`. If the saved state has no `kitaru_version`, `up` uses `0.27.6` only when `server_image` is the default digest. Otherwise `up` stops and asks for the version.
- Added by ticket 05: `up --kitaru-version <version>` gives the version. `up` has no option for `server_image`, so this option is the only way to answer that stop.
- Added by ticket 05: this stop applies only when `ui_image` is set, like the two version checks below. Without `ui_image`, `up` behaves as before (spec user story 4), and Terraform records its default `0.27.6`. If that pair is wrong, the tag check stops the next `up` that sets `ui_image`.
- `up` checks the pair: it resolves the Docker Hub tag `zenmldocker/kitaru-server:<kitaru_version>` and requires its digest to equal the `server_image` digest. On 2026-11-11, tag `0.27.6` resolved to `sha256:05ea0048…1d47`. If the tag moves, does not exist, or Docker Hub does not answer, `up` stops with a message that names the tag and the digest. The check never passes silently.

**UI side: the `io.github.evalve-app.kitaru.version` label.**

- Every `evalve-ui` image sets the image config label `io.github.evalve-app.kitaru.version`. Its value is the Kitaru version whose `openapi.json` types the build uses, in the same format as `kitaru_version`, for example `0.27.6`.
- We do not use `org.opencontainers.image.version`, because that key holds the image's own version.
- `up` reads the label from the config blob `Labels` of the `linux/amd64` image, because the server AMI is x86_64. If `ui_image` is an index, `up` takes its `linux/amd64` manifest.
- The label must equal `kitaru_version` as an exact string. A missing label, an unreadable label, or a different value stops `up` before Terraform runs, and the message names both versions.
- Both version checks run only when `ui_image` is set. With an empty `ui_image`, `up` calls no registry, as today.
- Added by ticket 05: `up` runs both checks before the summary and the confirmation prompt, so a mismatch stops `up` before any AWS write.
- `evalve-ui` ADR 0004 records the same contract for the image build.

## Container contract

Added 2026-11-11 by ticket 02 of `.scratch/evalve-dashboard-rollout`.

- The `evalve-ui` image listens on container port 3000. Evalve publishes it as `127.0.0.1:3000:3000`.
- Caddy keeps the `/app` prefix when it proxies `/app` and `/app/*` to the container. The step 1 image therefore serves its build under `/app/`.
- Added by ticket 04: the `index.html` of every `evalve-ui` build has the title `<title>Evalve</title>` and is served as `text/html`. After `up`, Evalve reads the dashboard path (`/app/` in step 1, `/` in step 2) and requires that title. The Bundled dashboard has `<title>Kitaru</title>` and `data-app="kitaru"`, so a route that sends the dashboard path to Kitaru fails the check.
- The `evalve-ui` repo has not yet recorded these points. It must match them, or this section changes.
