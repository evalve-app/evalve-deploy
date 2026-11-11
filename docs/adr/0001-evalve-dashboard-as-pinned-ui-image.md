# Evalve deploys the Evalve dashboard as a pinned UI image

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
- The UI image carries a label with the Kitaru version it targets. `evalve up` stops before Terraform runs when that label does not match the Kitaru version of `server_image`.
