Status: resolved

# Spec: Deploy the Evalve dashboard beside, then in place of, the Bundled dashboard

## Problem Statement

The Operator deploys one shared Kitaru server with Evalve, and Teammates use the Bundled dashboard to sign in, create API keys, and view sessions. The team builds its own Evalve dashboard in the separate `evalve-ui` repo, with its own layout and later its own features (Loops). Evalve has no way to deploy that dashboard. Caddy sends every path to Kitaru, so the only UI a Teammate can reach is the Bundled dashboard. The Operator also has no safe way to test the Evalve dashboard on the real server while Teammates continue to use the Bundled dashboard.

## Solution

Evalve deploys a pinned Evalve dashboard image on the same EC2 host and the same hostname as Kitaru, and the Operator moves the deployment through two steps with one `up` option:

- **Step 1, `--dashboard bundled`:** the Bundled dashboard stays at `/` for Teammates, and the Evalve dashboard runs at `/app/` for the Operator to test.
- **Step 2, `--dashboard evalve`:** the Evalve dashboard serves `/`, old `/app/...` links redirect to the same path at `/`, and the Bundled dashboard is no longer reachable.

The Kitaru API stays at the same paths in both steps, so agents, API keys, cookies and CORS settings do not change. If no Evalve dashboard image is set, Evalve behaves exactly as it does today. Evalve refuses to deploy an Evalve dashboard image that targets a different Kitaru version than the server image. After each `up`, Evalve checks that the dashboard and the API both answer on the public URL.

The decisions are recorded in this repo's ADR 0001 and in `evalve-ui` ADRs 0001 and 0003.

## User Stories

1. As an Operator, I want to set an Evalve dashboard image for my deployment, so that Evalve runs the dashboard that the `evalve-ui` repo publishes.
2. As an Operator, I want the Evalve dashboard image to be pinned by digest, so that every `up`, `start` and restore runs the exact same dashboard build.
3. As an Operator, I want Evalve to reject an Evalve dashboard image reference that is not an immutable digest from the expected registry, so that a mutable tag cannot change the dashboard without my knowledge.
4. As an Operator, I want a deployment with no Evalve dashboard image to behave exactly as it does today, so that I can upgrade Evalve before the Evalve dashboard is ready.
5. As an Operator, I want to choose the rollout step with one `--dashboard` option on `up`, so that I do not edit Terraform or Caddy by hand.
6. As an Operator, I want `--dashboard bundled` to be the default for a new deployment, so that Teammates keep the Bundled dashboard until I choose to switch.
7. As an Operator, I want Evalve to remember the dashboard step and the Evalve dashboard image between runs, so that a later `up` without the options does not undo my choice.
8. As an Operator, I want `stop`, `start`, `down` and `up --restore` to keep the dashboard step and the Evalve dashboard image, so that a restored deployment comes back with the same dashboard.
9. As an Operator, I want `up` to show the dashboard step and the Evalve dashboard image in the summary before I confirm, so that I know what will change.
10. As an Operator, I want `up` to tell me that a change of dashboard step or image replaces the EC2 instance, so that I can plan the short outage.
11. As an Operator, I want `--dashboard evalve` to fail before any AWS change when no Evalve dashboard image is set, so that I cannot remove the only working dashboard by mistake.
12. As an Operator, I want `up` to stop before Terraform runs when the Evalve dashboard image targets a different Kitaru version than the server image, so that a version mismatch never reaches Teammates.
13. As an Operator, I want the version mismatch message to name both versions, so that I know which image to change.
14. As an Operator, I want `up` to fail with a clear message when it cannot read the version label of an image, so that the check never passes silently.
15. As an Operator, in step 1, I want the Evalve dashboard at `/app/` on the same hostname, so that it signs in with the same session cookie as the Bundled dashboard.
16. As an Operator, in step 1, I want the Bundled dashboard to keep working at `/`, so that Teammates are not affected while I test.
17. As an Operator, in step 1, I want `up` to print both dashboard URLs, so that I know where to test the Evalve dashboard.
18. As an Operator, in step 2, I want the Evalve dashboard at `/`, so that Teammates use it at the address they already bookmarked.
19. As a Teammate, in step 2, I want links to `/app/...` to redirect to the same path and query at `/`, so that links shared during step 1 still work.
20. As a Teammate, after step 2, I want an invitation link created in step 1 to still work, so that I can join even if the Operator invited me before the switch.
21. As a Teammate, after step 2, I want `kitaru login` to open a device verification page that works, so that I can connect my CLI.
22. As a Teammate, in both steps, I want my agent's `KITARU_API_URL` and API key to keep working, so that my agent never needs a change because of the dashboard.
23. As an Operator, in both steps, I want `/api/*`, `/health`, `/docs`, `/redoc` and `/openapi.json` to reach Kitaru, so that the API, health checks and API docs work as before.
24. As an Operator, I want `up` to check after deployment that the dashboard path answers with the Evalve dashboard, so that a broken Caddy route is found at once.
25. As an Operator, I want `up` to check after deployment that `/api/v1/info` answers with Kitaru, so that a route that sends API traffic to the dashboard is found at once.
26. As an Operator, I want the readiness and dashboard checks to give me one clear next step when they fail, so that I can fix the deployment.
27. As an Operator, I want the dashboard container to restart on failure and start on boot, like the Kitaru and Caddy containers, so that a host restart does not leave Teammates without a dashboard.
28. As an Operator, I want the dashboard container to listen only on loopback, so that the only public entry point stays Caddy over HTTPS.
29. As an Operator, I want the dashboard container's logs to rotate like the other containers' logs, so that the disk does not fill.
30. As an Operator, I want the server role to keep access to only the two Evalve secrets, so that a public dashboard image adds no new secret access.
31. As an Operator switching to a custom domain, I want the dashboard routes to follow the new hostname, so that both dashboards work after `--domain`.
32. As an Operator restoring a snapshot, I want the dashboard to come up at the new sslip.io hostname with the same step and image, so that restore needs no extra dashboard work.
33. As an Operator, I want the README and the pilot runbook to describe both steps and when to move to step 2, so that the rollout does not depend on my memory.
34. As a Teammate, I want the quickstart to name the dashboard I actually use, so that the instructions match the screen.
35. As an Operator, I want `credentials` and the other messages to say "dashboard" in a way that is correct in both steps, so that the CLI does not point Teammates to the wrong UI.
36. As a maintainer, I want the Terraform test to fail if a Caddy route changes, so that a route change is a deliberate decision.
37. As a maintainer, I want unit tests for the `--dashboard` and image rules that need no AWS and no registry, so that the local checks stay fast and offline.

## Implementation Decisions

- **New deployment settings.** Two values join the non-secret deployment settings that Evalve saves in S3 state and passes to every Terraform apply, including the configuration used by `down`, `stop`/`start` and restore:
  - `ui_image`: optional. Empty means no Evalve dashboard. When set, it must be an immutable digest reference to the `evalve-ui` image in GitHub Container Registry under `evalve-app`. Terraform validates the format in the same way as `server_image`.
  - `dashboard`: `bundled` or `evalve`, default `bundled`. Terraform rejects `evalve` when `ui_image` is empty.
- **CLI.** `up` takes `--dashboard {bundled,evalve}` and `--ui-image <digest reference>`. Missing options fall back to the saved values, then to the defaults. The pre-confirmation summary shows both values and notes that a change replaces EC2. `--dashboard evalve` with no `ui_image` fails before any AWS write.
- **Version label check.** A new boundary function reads OCI image labels from a public registry without Docker (anonymous registry API). `up` reads the Kitaru version label of `ui_image` and the Kitaru version of `server_image` and stops before Terraform when they differ. It runs only when `ui_image` is set. The `evalve-ui` image must carry a label that names the Kitaru version it targets. The label key is part of the contract with `evalve-ui` and the first ticket fixes it.
- **Host layout.** The bootstrap script adds a third systemd unit for the dashboard container, built like the Kitaru unit: pinned image, published on `127.0.0.1:3000` only, local log driver with rotation, restart always. The unit exists only when `ui_image` is set.
- **Caddy routes.** One site block for the hostname, in this order:
  - Always: `/api/*`, `/health*`, `/docs*`, `/redoc*` and `/openapi.json` go to Kitaru on `127.0.0.1:8000`.
  - No `ui_image`: everything else goes to Kitaru (today's behavior).
  - `bundled`: `/app` and `/app/*` go to the dashboard container. Everything else goes to Kitaru.
  - `evalve`: `/app` and `/app/*` redirect permanently to the same path without the `/app` prefix, query kept. Everything else goes to the dashboard container.
- **No Kitaru UI settings.** Evalve does not set `EXTERNAL_UI` or `DASHBOARD_URL`. CORS, cookie and CSRF settings do not change, because the Evalve dashboard is on the same origin.
- **Base path contract.** The dashboard container serves the build for the base path of the step. For step 1, the `evalve-ui` image serves a build for `/app/`. For step 2, it serves a build for `/`. The image reference therefore changes at the switch, and `up --dashboard evalve --ui-image <root build>` performs the switch in one apply. How `evalve-ui` publishes the two builds (two images or one image with both) is decided in `evalve-ui`. Evalve only requires that the pinned image serves the base path of the chosen step.
- **Readiness.** After the current health and 401 checks, `up` checks two more things on the public URL:
  - The dashboard path (`/app/` in step 1, `/` in step 2) returns a successful HTML response from the Evalve dashboard.
  - `/api/v1/info` returns Kitaru JSON.

  These checks are skipped when `ui_image` is empty.
- **Output text.** `up` prints "Evalve dashboard: <url>" for the Evalve dashboard and "Bundled dashboard: <url>" for the Bundled dashboard, depending on the step. Docs use the glossary terms: Bundled dashboard, Evalve dashboard, Operator, Teammate, Admin account.
- **Docs.** README: describe `--dashboard`, `--ui-image`, both steps, and the EC2 replacement. Pilot runbook: step 1 testing, the full-parity condition for step 2 (all Bundled dashboard routes plus login, `/activate-user`, `/devices/verify`, Settings → API keys, Settings → Members), and the switch command. Teammate quickstart: no new steps, because the address and the screen names stay the same.

## Testing Decisions

- Good tests here check behavior an Operator can see at a seam: what `up` passes to Terraform, what it prints, when it stops, and what the generated host configuration contains. They do not check helper function calls or private formatting.
- **Seam 1: the `up` command**, through `cli.main` with mocked `Aws`, `terraform.apply`, `verify_dns`, `wait_for_kitaru` and the new label-reading function. Prior art: the `UpTest` class. Cases:
  - default step for a new deployment
  - saved values reused on a later `up`
  - `--dashboard evalve` without an image fails before AWS writes
  - label mismatch stops before `terraform.apply` and names both versions
  - unreadable label fails
  - empty `ui_image` skips the label check
  - summary and URL output for each step
- **Seam 2: the Terraform plan with the mocked AWS provider.** Prior art: the Caddy and IAM assertions in the deployment Terraform test. Cases:
  - empty `ui_image`: Caddy and units match today's output
  - `bundled`: API routes to Kitaru, `/app` to the dashboard container, the rest to Kitaru, dashboard unit present on loopback
  - `evalve`: `/app` redirect, the rest to the dashboard container
  - invalid `ui_image` reference rejected
  - `evalve` with empty `ui_image` rejected
  - IAM still allows only the two secrets
  - the deployment output includes both new settings
- **Seam 3: the readiness check.** Prior art: `ReadinessTest`, which uses a mocked HTTP opener. Cases: dashboard path success, dashboard path returning Kitaru or an error, `/api/v1/info` not returning Kitaru, checks skipped without `ui_image`.
- **Lifecycle.** Extend the existing lifecycle tests so that `down`, restore and `start`/`stop` carry `ui_image` and `dashboard` forward.
- Tests need no AWS, no Docker and no network. They run in the existing "Local checks" commands.

## Out of Scope

- The `evalve-ui` application: screens, sign-in, base path build setting, image build and publishing, and the version label. That work lives in the `evalve-ui` repo.
- The Loop state API from `evalve-ui` ADR 0002, its RDS schema and its `/loops-api/` route.
- Kitaru `EXTERNAL_UI` and `DASHBOARD_URL`, and any CORS or cookie change.
- A separate host, subdomain or static hosting for the dashboard.
- Private registry images and pull credentials.
- Any `evalve invite` CLI command. Invitations stay in Settings → Members.
- Removing the Bundled dashboard from the Kitaru image. In step 2, Caddy only stops sending traffic to it.

## Further Notes

- **Open item for the first ticket:** confirm that the pinned `zenmldocker/kitaru-server` image carries a readable Kitaru version label. If it does not, add a Kitaru version setting next to `server_image` and validate both together.
- **Open item for the first ticket:** fix the label key that `evalve-ui` must set, and record it in this repo's ADR 0001 and in `evalve-ui`.
- Every change of `dashboard` or `ui_image` replaces the EC2 instance because `user_data` changes. RDS and the Elastic IP stay.
- Step 2 happens only at full parity with the Bundled dashboard (`evalve-ui` ADR 0003). Until then, `bundled` stays the default.
- Invitation links from step 1 use the Bundled format `/activate-user?user=<id>&token=<token>`. The Evalve dashboard keeps that format, so the links survive step 2.
- References: `docs/research/kitaru-external-ui.md`, `docs/adr/0001-evalve-dashboard-as-pinned-ui-image.md`, `GLOSSARY.md`.

## Comments

- 2026-11-11: Tickets 01–05 are resolved, so the spec is resolved. No ticket covers this user story:
  - Story 35 ("dashboard" wording correct in both steps): the `credentials` help text at `src/evalve/cli.py:89` says "bootstrap Kitaru dashboard login". `GLOSSARY.md` lists "Kitaru dashboard" under _Avoid_.
