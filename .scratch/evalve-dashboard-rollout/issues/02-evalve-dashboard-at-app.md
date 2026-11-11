# 02: Run the Evalve dashboard at /app/ (step 1)

**What to build:** The Operator runs `up --ui-image <digest>` and gets the Evalve dashboard at `/app/` on the deployment's hostname, while Teammates keep the Bundled dashboard at `/`. The API paths (`/api/*`, `/health*`, `/docs*`, `/redoc*`, `/openapi.json`) still reach Kitaru. The dashboard runs as a third container on loopback only, with restart and log rotation like the Kitaru container. Evalve saves `ui_image` and the step `bundled` in state, and later `up`, `stop`, `start`, `down` and restore keep both. With no `ui_image`, the deployment is the same as today. The `up` summary shows the image and the step and says that a change replaces EC2. After `up`, it prints the Bundled dashboard URL and the Evalve dashboard URL. Spec: `.scratch/evalve-dashboard-rollout/spec.md`. ADR: `docs/adr/0001-evalve-dashboard-as-pinned-ui-image.md`.

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] `up` accepts `--ui-image`. Terraform rejects a reference that is not an immutable digest of the `evalve-ui` image in GHCR under `evalve-app`.
- [x] A `dashboard` setting exists with the values `bundled` and `evalve` and the default `bundled`. This ticket supports only `bundled`.
- [x] Terraform plan test: an empty `ui_image` gives the same Caddy configuration and units as today.
- [x] Terraform plan test: with `ui_image` and `bundled`, Caddy sends the API paths to Kitaru, `/app` and `/app/*` to the dashboard container on `127.0.0.1:3000`, and all other paths to Kitaru. The dashboard unit exists and publishes on loopback only.
- [x] Terraform plan test: the server role still reads only the two Evalve secrets, and the deployment output includes `ui_image` and `dashboard`.
- [x] `up` tests: a new deployment uses `bundled`. A later `up` without the options reuses the saved values. The summary shows both values. The printed URLs match the step.
- [x] Lifecycle tests: `down`, restore, `stop` and `start` carry `ui_image` and `dashboard` forward.
- [x] The README describes `--ui-image`, step 1 and the EC2 replacement, and uses "Bundled dashboard" and "Evalve dashboard" as `GLOSSARY.md` defines them.
- [x] All local checks from the README pass.

## Comments

### Done (2026-11-11)

- `up --ui-image` saves `ui_image` and `dashboard` (`bundled`) in state. `down`, restore, `stop` and `start` keep both.
- Terraform rejects a `ui_image` that is not `ghcr.io/evalve-app/evalve-ui@sha256:<64 hex>`. It also rejects `dashboard = "evalve"` until ticket 03 removes that rule.
- With an empty `ui_image`, the rendered `user_data` has the same SHA-256 as before this ticket, so existing deployments do not replace EC2.
- `caddy adapt --validate` with the pinned Caddy image accepts the step 1 Caddyfile.
- Container contract (port 3000, `/app` prefix kept) is in ADR 0001, section "Container contract". `evalve-ui` must match it.
- `evalve start` starts `evalve-ui.service` only when the unit exists.
