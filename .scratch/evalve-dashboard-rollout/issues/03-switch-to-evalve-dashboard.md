# 03: Switch to the Evalve dashboard at / (step 2)

**What to build:** The Operator runs `up --dashboard evalve --ui-image <build for />` and Teammates get the Evalve dashboard at `/`. Links to `/app` and `/app/...` redirect permanently to the same path and query without the `/app` prefix. The API paths still reach Kitaru. The Bundled dashboard is no longer reachable, and Evalve does not set Kitaru's `EXTERNAL_UI` or `DASHBOARD_URL`. `--dashboard evalve` with no `ui_image` fails before any AWS write. Evalve saves the step. The pilot runbook explains step 1 testing, the full-parity condition for step 2 (`evalve-ui` ADR 0003) and the switch command. Spec: `.scratch/evalve-dashboard-rollout/spec.md`.

**Blocked by:** 02 (Run the Evalve dashboard at /app/).

**Status:** ready-for-agent

- [ ] `up` accepts `--dashboard {bundled,evalve}`.
- [ ] `up` test: `--dashboard evalve` with no saved or given `ui_image` fails with a clear message and makes no AWS writes.
- [ ] Terraform plan test: `evalve` with an empty `ui_image` is rejected.
- [ ] Terraform plan test: with `evalve`, Caddy sends the API paths to Kitaru, redirects `/app` and `/app/*` to the path without the prefix (query kept), and sends all other paths to the dashboard container.
- [ ] Terraform plan test: the Kitaru environment has no `EXTERNAL_UI` or `DASHBOARD_URL`.
- [ ] `up` tests: the saved step is reused, a change back to `bundled` works, and the printed URL is the Evalve dashboard at `/`.
- [ ] The pilot runbook describes both steps, the full-parity list and the switch command, using the glossary terms.
- [ ] All local checks from the README pass.
