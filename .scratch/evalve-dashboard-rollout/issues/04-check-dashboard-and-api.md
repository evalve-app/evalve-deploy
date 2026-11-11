# 04: Check the dashboard and the API after up

**What to build:** After the current readiness check (health and the unauthenticated 401), `up` confirms on the public URL that the dashboard path for the step (`/app/` for `bundled`, `/` for `evalve`) returns a successful HTML response from the Evalve dashboard, and that `/api/v1/info` returns Kitaru JSON. A wrong Caddy route is therefore found at once. The checks are skipped when `ui_image` is empty. Each failure tells the Operator one next step. Spec: `.scratch/evalve-dashboard-rollout/spec.md`.

**Blocked by:** 03 (Switch to the Evalve dashboard at /).

Status: resolved

- [x] Readiness tests with a mocked opener: success for each step, and failure when the dashboard path returns Kitaru or the Bundled dashboard, an error, or no response.
- [x] Readiness test: failure when `/api/v1/info` does not return Kitaru JSON.
- [x] `up` test: the checks receive the step and are not called when `ui_image` is empty.
- [x] Each failure message names the failed path and one next step.
- [x] All local checks from the README pass.

## Comments

- The check identifies the Evalve dashboard by `<title>Evalve</title>` in a `text/html` response. The Bundled dashboard in the Kitaru 0.27.6 wheel has `<title>Kitaru</title>` and `data-app="kitaru"`. ADR 0001, "Container contract", now records the title as a contract with `evalve-ui`.
- `/api/v1/info` is unauthenticated in Kitaru 0.27.6 (`routers/info.py`). "Kitaru JSON" means a JSON object with the required string fields `version` and `auth_scheme`. The check does not compare the version. Ticket 05 does that.
- The check retries HTTP 5xx and network errors for up to 180 seconds, because bootstrap starts `evalve-ui.service` with `--no-block`. Any other status, a redirect, or a wrong body fails at once.
- `up` runs the check after it removes the restore record. Kitaru is restored at that point, so the "retry up" step in each message works without `--restore`.
- `start` does not run the check, because the ticket names only `up`.
- Gap: a build for `/` served at `/app/` still has the Evalve title, so the check passes. The ticket does not ask for an asset path check.
