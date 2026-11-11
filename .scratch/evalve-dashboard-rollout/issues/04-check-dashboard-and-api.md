# 04: Check the dashboard and the API after up

**What to build:** After the current readiness check (health and the unauthenticated 401), `up` confirms on the public URL that the dashboard path for the step (`/app/` for `bundled`, `/` for `evalve`) returns a successful HTML response from the Evalve dashboard, and that `/api/v1/info` returns Kitaru JSON. A wrong Caddy route is therefore found at once. The checks are skipped when `ui_image` is empty. Each failure tells the Operator one next step. Spec: `.scratch/evalve-dashboard-rollout/spec.md`.

**Blocked by:** 03 (Switch to the Evalve dashboard at /).

**Status:** ready-for-agent

- [ ] Readiness tests with a mocked opener: success for each step, and failure when the dashboard path returns Kitaru or the Bundled dashboard, an error, or no response.
- [ ] Readiness test: failure when `/api/v1/info` does not return Kitaru JSON.
- [ ] `up` test: the checks receive the step and are not called when `ui_image` is empty.
- [ ] Each failure message names the failed path and one next step.
- [ ] All local checks from the README pass.
