# External UI for Evalve's Kitaru server

Research date: 2026-11-10. Scope: Kitaru server image `zenmldocker/kitaru-server@sha256:05ea0048…1d47`, which is Kitaru **0.27.6** ([infra/variables.tf:69-77](../../infra/variables.tf), [src/evalve/lifecycle.py:18](../../src/evalve/lifecycle.py)). Docker Hub resolves tag `0.27.6` to that exact digest (registry `HEAD /v2/zenmldocker/kitaru-server/manifests/0.27.6`, checked 2026-11-10). Source citations point at the `python/kitaru/v0.27.6` tag, commit [`96dfb82`][k]. Docs URLs come from the published site; the docs search tool reports its own source revision, so each docs claim is also paired with the matching `docs/book` file at the tag.

## Summary

- **Yes, an external UI can replace the bundled dashboard.** Kitaru 0.27.6 has a built-in switch: `KITARU_SERVER_EXTERNAL_UI=true` plus `KITARU_SERVER_DASHBOARD_URL=<url>`. The server then stops serving the bundled UI and redirects every non-API path to that URL ([ui.py:98-121, 163-177][ui], [config.py:82-83, 183-184][cfg]). The two settings appear in the source and the Helm chart, not in the user docs.
- **The dashboard is not a separate component.** It is a prebuilt SPA bundled into the Python wheel and served by the same FastAPI process on port 8000 ([ui.py:47-56, 124-160][ui], [pyproject.toml:110-111][pyp]). Without `EXTERNAL_UI`, the bundled UI and an external UI can run side by side.
- **The API is a plain REST API under `/api/v1`, with a committed OpenAPI 3 spec per release** ([openapi/openapi.json][oas], `info.version` = `0.27.6`). The running server also serves `/openapi.json`, `/docs` and `/redoc`. The npm package `@zenml-io/kitaru` has a hand-written client whose types are generated from that spec. The client has no wrappers for users, API keys, devices or browser login. No formal API stability policy was found.
- **Browser auth has two options.** `POST /api/v1/login` (password grant) returns a bearer JWT and a CSRF token in the body. It also sets an HttpOnly cookie when `AUTH_COOKIE_NAME` is set. The cookie is hard-coded `SameSite=Lax` ([auth.py:310-322][auth]), so cookie auth works only from a same-site origin. A cross-site UI must send `Authorization: Bearer`. CORS is controlled by `KITARU_SERVER_CORS_ALLOW_ORIGINS`.
- **Sessions last one hour.** `JWT_LIFETIME_SECONDS` defaults to 3600, and 0.27.6 has no refresh grant for browser sessions ([config.py:75][cfg], [api_models/v1/auth.py:27-33][grant]).
- **Invitations, password setup, sign-in and API key creation are all REST endpoints.** An external UI can replace every dashboard step this repo's docs use. The invitation link itself (`/activate-user?user=…&token=…`) is built by the bundled UI in the browser, not by the server. An external UI must define its own activation page.
- **No public source was found for the dashboard version that 0.27.6 ships.** The bundle (`kitaru-ui-v0.5.2`) comes from release assets of `zenml-io/zenml-frontend-monorepo` ([download-ui.sh:4, 29][dl]), and that repository returns 404 to anonymous GitHub API requests. An older dashboard is public under Apache-2.0 at [`zenml-io/kitaru-ui`][kui] (tags up to v0.1.6, May 2026). It targets the earlier ZenML-based Kitaru API, so it is useful only as a stack and pattern reference. Build against the OpenAPI spec.
- **The simplest shape for this repo is to put the external UI on the same host behind Caddy.** Caddy sends `/api/*`, `/health`, `/docs`, `/redoc` and `/openapi.json` to Kitaru and everything else to the UI's static files. Because UI and API share an origin, it needs no CORS or cookie changes. A subdomain or separate static hosting also works but needs extra env settings, and a separate site needs bearer-token auth.

## 1. Who serves the dashboard, and can it be turned off?

**The Kitaru server serves it.** The release image runs one process: `uvicorn kitaru.server.api.main:app` on port 8000 ([release-server.Dockerfile:123][dock]). This repo runs that image on `127.0.0.1:8000`, and Caddy terminates HTTPS and proxies everything to it ([infra/bootstrap.sh.tftpl:36-44, 60-65](../../infra/bootstrap.sh.tftpl)).

The UI is a static SPA build shipped inside the Python wheel at `kitaru/_ui/dist`, with a `bundle_manifest.json` ([pyproject.toml:110-111][pyp]). The 0.27.6 wheel on PyPI contains `kitaru-ui-v0.5.2`, as its manifest and the release pin show ([releases/python/kitaru/0.27.6.toml:3][rel]). At startup, `register_ui()` mounts `/assets` and a catch-all `GET /{path}`. The catch-all returns static files or `index.html`. It returns 404 for the reserved prefixes `api`, `health`, `docs`, `redoc` and `openapi.json` ([ui.py:30, 124-160][ui]). The catch-all is registered last, after all API routers ([app.py:599][app]).

**Modes** ([ui.py:163-177][ui]):

| Setting | Behaviour |
| --- | --- |
| default (`EXTERNAL_UI=false`) | Serves the bundled UI if the wheel contains it. |
| `EXTERNAL_UI=true` + `DASHBOARD_URL` | No bundled UI. Every non-reserved path returns a redirect to `DASHBOARD_URL` itself; the request path and query are dropped ([ui.py:106-121][ui]). Startup fails if `DASHBOARD_URL` is empty ([config.py:183-184][cfg]). |
| no bundle in the package | UI serving is disabled, with a log line. |

All settings are `KITARU_SERVER_`-prefixed environment variables ([server/config.py:51][scfg], [config.py:49-97][cfg]).

Other effects of `DASHBOARD_URL`, which apply even without `EXTERNAL_UI`:

- It is added to the CORS allow-list as-is ([config.py:123-150][cfg]).
- `GET /api/v1/info` reports it ([info.py:36-69][info]).
- The CLI device login flow sends people to `{DASHBOARD_URL}/devices/verify?device_id=…&user_code=…` ([mapping/devices.py:76-83][devmap], [auth.py:238][auth]). If `DASHBOARD_URL` is unset, it uses the server's base URL.

**The vendor uses this pattern too.** The Helm chart's ZenML Pro mode sets `external_ui: "True"` and points `dashboard_url` at the hosted ZenML Pro dashboard ([helm/templates/_environment.tpl:117-124][helmenv]). The non-Pro chart exposes `dashboardURL` and `corsAllowOrigins` but not `external_ui` ([helm/values.yaml:52-55, 134-140][helmval]).

**Documentation status.** `EXTERNAL_UI`, `DASHBOARD_URL`, `CORS_ALLOW_ORIGINS`, `AUTH_COOKIE_*` and `ROOT_URL_PATH` are not in the published Docker configuration list ([Docker docs][d-docker], [docs/book/deploy/docker.md:35-63][docker-md]). A grep of the whole `docs/` tree at the tag found none of them. They are confirmed from source and the Helm chart only.

**Is Kitaru built on ZenML?** Kitaru 0.27.6 is not; versions up to 0.21 were.

- **0.27.6.** ZenML GmbH is the author and copyright holder ([pyproject.toml:9][pyp]), and the docs, Docker org and Pro control plane are ZenML-branded. However, 0.27.6 neither imports nor depends on the `zenml` package: there is no `zenml` entry in its [uv.lock][lock] and no `import zenml` under `src/`.
- **0.21.0 and earlier.** `v0.21.0` depended on `zenml[local]>=0.96.1` and `zenml[server]>=0.96.1` ([pyproject.toml at v0.21.0][pyp21]). The rewrite began in `0.22.0rc0`, which moved all routes to `/api/v1` ([CHANGELOG.md:445-454][chg]).
- **Inherited from ZenML today:** nothing in the server code path. Only branding, the image namespace, and the optional ZenML Pro control plane remain.
- **The public `kitaru-ui` repo predates the rewrite.** It describes itself as "built on ZenML" and talks to a ZenML server ([kitaru-ui README][kui-readme]). The server is its own FastAPI + SQLAlchemy + Postgres app ([Under the hood][d-uth]). Only the optional `control_plane` auth scheme ties it to ZenML Pro ([config.py:63-70, 164-168][cfg]). This repo uses `local` ([infra/refresh.py:42](../../infra/refresh.py)).

## 2. Which API, and is it usable from another repo?

- **REST/JSON under `/api/v1/...`**, which the docs describe as "a plain versioned REST API" ([Under the hood][d-uth], [docs/book/concepts/under-the-hood.md:14][uth-md]). The bundled UI calls the same `/api/v1` endpoints through an `openapi-fetch`-style client. For example, it calls `POST /api/v1/users` and `POST /api/v1/users/{account_id}/activate` (observed in the minified `kitaru-ui-v0.5.2` bundle). There is no GraphQL.
- **The OpenAPI spec** is committed at [openapi/openapi.json][oas], has 99 paths, and carries `info.version` = `0.27.6`. CI checks that it matches the app (`just openapi-check`, [Justfile:97-99][just], run at [ci.yml:58][ci]). The live server also exposes `/openapi.json`, `/docs` and `/redoc`, because the FastAPI app does not disable them and the UI catch-all reserves those paths ([app.py:380-385][app], [ui.py:30][ui]).
- **Some endpoints exist only for the dashboard.** These are under `/api/v1/ui/*`: sessions with evaluations, evaluation aggregates and sample data ([routers/ui.py][uirouter]). They are in the spec, but their names suggest they serve the dashboard's needs.
- **TypeScript client.** `@zenml-io/kitaru` 0.8.0 at this tag is Apache-2.0 ([packages/core/package.json:1-5][tspkg]). The client is hand-written. Its types come from `src/generated/openapi.ts`, which is generated from the spec with `openapi-typescript` ([generate-openapi.mjs][tsgen]). The docs say the root and `/client` entries are runtime-neutral and usable in browsers ([docs/book/deploy/sdks.md:98][sdks-md]).
  - Its resource wrappers cover accounts, agents, sessions, cohorts, evaluators, experiments, replays, jobs and similar resources. They do not cover users, API keys or devices ([packages/core/src/resources][tsres]). The only `/api/v1/login` call is in the Node-only stored-login reader (`src/node/stored-login.ts`).
  - The generated file is not exported as its own package entry. The exports are `.`, `./client`, `./environment`, `./errors`, `./node`, `./evaluator` and `./adapter`.
  - No extra headers are required for raw `fetch`. `Idempotency-Key` is optional; requests without it are processed normally ([idempotency.py:168-169][idem]). `X-Kitaru-Client` only feeds analytics context ([app.py:284][app]).
  - An external UI can either use the SDK for data views and raw `fetch` for account flows, or generate its own types from `openapi.json` (for example `openapi-typescript` + `openapi-fetch`, the same pairing the SDK uses).
- **Stability.** The path is versioned (`v1`), and the project says it follows SemVer ([CHANGELOG.md:5-6][chg]). It is still 0.x, and minor releases have changed API behaviour without a `v2` path, for example `merge_evaluations` → `create_evaluations` and new 409 responses in 0.25.0 ([CHANGELOG.md:244][chg]). [DEPRECATIONS.md][dep] keeps some old fields "until the dashboard and generated clients" move. No written stability guarantee for `/api/v1` was found. Pin the UI to a Kitaru version, and regenerate types whenever `server_image` changes.

## 3. How a browser client authenticates

**Login.** Use `POST /api/v1/login` with form fields. The `password` grant (username + password) is accepted under `AUTH_SCHEME=local` ([auth.py:92-178, 243-328][auth]). The response body is `{access_token, token_type: "bearer", expires_in, csrf_token}`. When `AUTH_COOKIE_NAME` is set, the server also sets a cookie ([auth.py:310-322][auth]) with these attributes:

- `HttpOnly`
- `SameSite=Lax` (hard-coded, no setting)
- `Secure` from `AUTH_COOKIE_SECURE`, or from the request scheme when that is unset ([auth.py:399-413][auth])
- `Domain` from `AUTH_COOKIE_DOMAIN`, host-only when that is empty
- `Max-Age` equal to the token lifetime

API-key and device grants never set the cookie. Logout is `POST /api/v1/logout`, which deletes the cookie ([auth.py:331-347][auth]).

**Per-request credentials** ([dependencies/auth.py:65, 182-204][deps], [auth_service.py:116-149][svc]):

- `Authorization: Bearer <token>` is checked first. The token can be a session JWT or a `KITKEY_` API key. A header credential needs no CSRF token.
- Otherwise the server reads the auth cookie. A cookie session then requires `X-CSRF-Token` to match the `csrf_token` returned at login. CSRF tokens are only issued when `AUTH_COOKIE_NAME` is set ([auth_service.py:351-355][svc]).
- Session lifetime is `JWT_LIFETIME_SECONDS`, 3600 by default ([config.py:75][cfg], [auth_service.py:282-286][svc]). There is no refresh grant ([auth_models GrantType][grant]), so the UI must sign in again after expiry.
- OAuth/OIDC sign-in for a self-hosted server: none in 0.27.6. The only schemes are `none`, `local` and `control_plane` (ZenML Pro) ([Authentication docs][d-auth], [docs/book/deploy/authentication.md:10-13][auth-md]). The device flow (`/api/v1/device_authorization`) is for CLIs, not browsers.

**How the bundled UI (v0.5.2) does it** (observed in the minified bundle):

- It calls the API with `credentials: "include"` and a base URL taken from build-time `VITE_API_BASE_URL`, or the page origin.
- It stores the CSRF token in a JS-readable cookie named `kitaru.cookie.csrf-token` and sends it as `X-CSRF-Token`.
- It also has a `localStorage` key `kitaru.local-storage.access-token`.

**CORS** ([app.py:379-392][app], [config.py:123-150][cfg]):

- `CORSMiddleware` uses `allow_methods=["*"]` and `allow_headers=["*"]`.
- Origins come from `CORS_ALLOW_ORIGINS` (comma-separated, default `*`), plus `DASHBOARD_URL`, `CONTROL_PLANE_API_URL` and `SERVER_URL` when those are set.
- `allow_credentials` is true only when `*` is not in the list.
- Kitaru passes the list to Starlette's `CORSMiddleware` (Starlette 1.3.1 in the lockfile). That middleware checks the request `Origin` for membership in the list ([Starlette CORS docs][starlette-cors]). A `DASHBOARD_URL` with a path or trailing slash therefore never equals a browser `Origin`, so set `CORS_ALLOW_ORIGINS` to the bare UI origin.
- This repo sets `CORS_ALLOW_ORIGINS` to the server URL, `AUTH_COOKIE_NAME=evalve_session` and `AUTH_COOKIE_SECURE=true` ([infra/refresh.py:34-51](../../infra/refresh.py)).

**What this means for a different origin.** The rules below come from browser standards, not Kitaru:

- Cookies treat two hosts as same-site when they share a registrable domain ([RFC 6265bis, same-site requests][rfc-samesite]). The registrable domain is derived from the Public Suffix List ([URL Standard, registrable domain][url-regdom]).
- A `SameSite=Lax` cookie is not sent on cross-site subresource requests such as `fetch` ([RFC 6265bis, SameSite][rfc-samesite]).

- **Same site (same registrable domain), for example `ui.example.com` → `kitaru.example.com`:**
  - Cookie auth works with `fetch(..., {credentials: "include"})` and `X-CSRF-Token`. The Lax cookie is sent on same-site requests.
  - You must add the UI origin to `CORS_ALLOW_ORIGINS`.
  - A host-only cookie is enough. `AUTH_COOKIE_DOMAIN` is only needed if the UI must see the cookie on its own host, and it cannot see it anyway because the cookie is HttpOnly.
- **sslip.io hostnames:** `sslip.io` is not on the Public Suffix List (checked the list downloaded 2026-11-10). So all `*.sslip.io` hosts count as one site, and `ui.<ip>.sslip.io` is same-site with `<ip>.sslip.io`. Do **not** set `AUTH_COOKIE_DOMAIN=sslip.io`, because the session cookie would then go to every sslip.io host on the internet.
- **Cross site, for example a static host on another domain:** the Lax cookie is not sent on cross-site `fetch`. The UI must keep `access_token` from the login body and send `Authorization: Bearer`. It still needs a CORS entry. Keeping the token in JS exposes it to XSS, which the HttpOnly cookie avoids.

## 4. Can the external UI do invitations, sign-in and API keys?

Yes. Every dashboard step in [docs/aws-pilot.md](../aws-pilot.md) and [docs/team-quickstart.md](../team-quickstart.md) maps to a REST call:

| Dashboard step | API | Notes |
| --- | --- | --- |
| Settings → Members → Create User → Invite | `POST /api/v1/users` with no password | Admin only ([admin_flag.py:26-33][perm]). Returns `UserActivationTokenResponse` with a one-time `activation_token` ([users.py:47-84][users], [Authentication docs][d-auth]). |
| Invitation link | none; built client-side | The bundled UI builds `${origin}/activate-user?user=<id>&token=<token>` (observed in the bundle). The external UI must own its link format and activation page. |
| Teammate sets a password | `POST /api/v1/users/{account_id}/activate` with `{activation_token, password}` | Unauthenticated by design ([users.py:156-184][users]). |
| Sign in | `POST /api/v1/login` | See §3. Current account: `GET /api/v1/accounts/me`. |
| Settings → API keys → Create | `POST /api/v1/api-keys` with `{name}` | Plaintext key is returned once ([api_keys.py:47-71][keys]). List, update (deactivate), rotate and delete are also available ([Authentication docs][d-auth]). |
| Reset or lock out | `POST /api/v1/users/{id}/deactivate` | Returns a fresh activation token ([users.py:128-153][users]). |
| CLI browser login (`kitaru login`) | `POST /api/v1/devices/{device_id}/verify` | The CLI opens `{DASHBOARD_URL}/devices/verify`, so the external UI must serve that route if it replaces the dashboard ([mapping/devices.py:76][devmap]). |

**Caveats:**

- **Old invitation links stop working.** With `EXTERNAL_UI=true`, an invitation link from the bundled dashboard (`https://<host>/activate-user?...`) is redirected to the bare `DASHBOARD_URL`, which loses the token. Issue new invitations after the switch.
- **The bootstrap account is unchanged.** It is still the `default` admin with `KITARU_SERVER_DEFAULT_ACCOUNT_PASSWORD` ([Authentication docs][d-auth]).

## 5. Is the dashboard open source?

- **The server is open source.** The Kitaru repo, including the server and `ui.py`, is Apache-2.0 ([LICENSE][lic]), and the 0.27.6 wheel on PyPI declares `License-Expression: Apache-2.0`.
- **No public source was found for the dashboard version 0.27.6 ships.**
  - Releases download `kitaru-ui.tar.gz` from GitHub releases of `zenml-io/zenml-frontend-monorepo` ([download-ui.sh:4, 29][dl], [FRONTEND-TESTING.md:5][fe]).
  - The release workflow passes a token secret, `KITARU_UI_RELEASE_TOKEN`, to download it ([release.yml:108][relwf]).
  - Anonymous `GET https://api.github.com/repos/zenml-io/zenml-frontend-monorepo` returns 404, so that repo is private or does not exist.
- **The bundle has no licence of its own.** The wheel's `kitaru/_ui/` directory contains no separate licence file and no source maps. The licence of the v0.5.x UI source is **unconfirmed**. Do not assume the wheel's Apache-2.0 tag covers it.
- **An older dashboard is public.** [`zenml-io/kitaru-ui`][kui] is public, Apache-2.0 according to the GitHub API, with releases v0.1.0–v0.1.6 (latest 2026-05-11, last push 2026-08-05).
  - It is the pre-rewrite dashboard: "Login / session management — cookie-based authentication with the ZenML backend", with types generated "from ZenML server OpenAPI spec" ([README][kui-readme]).
  - It can be forked, but it targets the ZenML API, not Kitaru 0.27.6's `/api/v1`. Treat it as a reference for the stack the vendor uses (Vite + React 19, TanStack Router/Query, openapi-typescript) and for its build-time `VITE_API_BASE_URL` pattern, not as a drop-in UI. Its `Justfile` entry (`server-dev-image`, [Justfile:312-318][just]) is stale and still refers to a ZenML server tag.
- **Practical guidance:**
  - The current dashboard cannot be forked without access to the monorepo.
  - The minified v0.5.2 bundle can show how the UI uses the API, for example the `/activate-user` route, CSRF handling and the device verify page. Do not copy code from it until the licence is clear.
  - Build against `openapi.json` and the Apache-2.0 `@zenml-io/kitaru` types. Borrow structure from `kitaru-ui` if useful.

## 6. Deployment shapes for this repo

**Facts about this repo that apply to every shape:**

- Caddy has one site block that proxies everything to `127.0.0.1:8000` ([infra/bootstrap.sh.tftpl:36-44](../../infra/bootstrap.sh.tftpl)).
- Server env is generated by `infra/refresh.py`, and `infra/tests/test_refresh.py` asserts some of its lines.
- `aws_instance.server` has `user_data_replace_on_change = true` ([infra/server.tf:62](../../infra/server.tf)), so any Caddyfile or bootstrap change replaces EC2. The README says the same for hostname changes.
- The security group already allows 80/443 from anywhere ([infra/network.tf:67-74](../../infra/network.tf)).
- Evalve's readiness probe only uses `/health` and `/api/v1/sessions`, both reserved API paths ([src/evalve/readiness.py:16-34](../../src/evalve/readiness.py)). It keeps working with `EXTERNAL_UI=true`.
- `up --restore` comes up at a new sslip.io hostname. Any UI origin, CORS entry or `DASHBOARD_URL` derived from the hostname must follow it.

### A. Same host, same origin (recommended)

The UI is served at `https://<hostname>/` and Kitaru at `https://<hostname>/api/...`.

- **Kitaru config:**
  - Optionally set `EXTERNAL_UI=true` and `DASHBOARD_URL=https://<hostname>` so the bundled UI is never served and device login points at the new UI.
  - Do not let Caddy forward UI paths to Kitaru in that mode, or `/` redirects to itself in a loop.
  - CORS and cookie settings stay as they are.
- **Caddy:**
  - Route `/api/*`, `/health*`, `/docs*`, `/redoc*` and `/openapi.json` to `127.0.0.1:8000`.
  - Serve everything else with `file_server` plus an SPA fallback (`try_files {path} /index.html`).
- **Terraform/bootstrap:**
  - Get the UI build onto the host. Options: bake it into an image that is pinned by digest like `server_image`, download a versioned archive from S3 (this needs an IAM grant beyond today's two-secret policy, which a test enforces, [infra/tests/deployment.tftest.hcl](../../infra/tests/deployment.tftest.hcl)), or add a UI container.
  - Mount the files into the Caddy container.
  - Add a variable for the UI image or version.
  - Update the Caddy assertion in `deployment.tftest.hcl` and the env assertions in `test_refresh.py`.
- **Coexisting with the bundled UI:** the bundled UI uses absolute `/assets/...` and `/favicon.svg` paths (observed in `index.html`), so it cannot move under a sub-path. The external UI would take a prefix such as `/app/` and must be built with that base path. `EXTERNAL_UI` stays false.

### B. Same host, separate subdomain

The UI is at `ui.<hostname>` or `ui-<ip>.sslip.io`, and Kitaru stays at `<hostname>`.

- **Kitaru config:**
  - Add the UI origin to `KITARU_SERVER_CORS_ALLOW_ORIGINS` in `refresh.py`.
  - Optionally set `DASHBOARD_URL` (bare origin) and `EXTERNAL_UI=true`.
  - Cookie auth keeps working because the hosts are same-site. Leave `AUTH_COOKIE_DOMAIN` unset.
- **Caddy:** a second site block serving the static UI. Caddy gets a second certificate.
- **DNS:**
  - With a custom domain, the operator adds a second A record. `variables.tf` takes one `domain` today, so a `ui_domain` variable or a derived name is needed.
  - With sslip.io, both `ui.203-0-113-10.sslip.io` and `ui-203-0-113-10.sslip.io` resolved to the embedded IP in a DNS check on 2026-11-10. sslip.io's own documentation was not confirmed: `https://sslip.io/` redirected to nip.io.
- **Terraform:** `local.hostname` and `output.url` need a UI counterpart. Same file-delivery work as shape A.

### C. Separate host or static hosting (S3 + CloudFront, another server, another cloud)

- **Kitaru config:** add the UI's exact origin to `CORS_ALLOW_ORIGINS`, and set `DASHBOARD_URL` (and `EXTERNAL_UI=true` to replace the dashboard).
- **Auth depends on whether the UI is same-site with the server:**
  - If the UI is on a subdomain of the same custom domain, it is same-site, and cookie + CSRF works.
  - If it is cross-site, it must use bearer tokens from the login body, with one-hour sessions and no refresh. A UI on a provider's default domain (for example `*.cloudfront.net`) is a different registrable domain from the API host, so it is cross-site.
  - A UI on CloudFront's default `*.cloudfront.net` name is cross-site with an sslip.io API. **Unconfirmed:** whether `cloudfront.net` is on the PSL was not checked.
- **This repo:** new resources (bucket, distribution, certificate, DNS) in this Terraform or a separate stack, plus an output for the UI URL. The server's env must learn the UI origin, which adds a cross-stack dependency. The EC2 host and Caddy are otherwise unchanged.
- **Restore problem:** after `up --restore` the API hostname changes, so the UI's API base URL must be reconfigured. Prefer a custom domain for the API in this shape.

## Open questions

- **Current UI source and licence:** whether `zenml-io/zenml-frontend-monorepo` is private or does not exist, and the licence of the `kitaru-ui-v0.5.x` source. Checked: anonymous GitHub API (404), the wheel's `kitaru/_ui/` contents (no licence file, no source maps), the release workflow (uses a token secret), and the public `zenml-io/kitaru-ui` repo (Apache-2.0, but the older ZenML-based UI).
- **API stability:** no written compatibility policy for `/api/v1` across 0.x releases. Checked: the docs site via search, `docs/book`, `CHANGELOG.md` and `DEPRECATIONS.md`.
- **Support status of `EXTERNAL_UI` and `DASHBOARD_URL` for self-hosted users:** they exist in 0.27.6 source and Helm Pro mode, but the user docs do not mention them, so they could change without notice.
- **Bundled UI details** (CSRF cookie name, `/activate-user` route, `localStorage` token key) come from reading the minified `kitaru-ui-v0.5.2` bundle, not from source. They may differ in other UI versions.
- **sslip.io subdomain behaviour:** observed through DNS, not confirmed from sslip.io documentation.
- **`ROOT_URL_PATH` behind a path-prefix proxy:** it is passed to FastAPI `root_path` ([app.py:384][app]). Serving the Kitaru API under a prefix such as `/kitaru/api` was not tested and is not needed for the shapes above.

## Sources

Kitaru source at tag `python/kitaru/v0.27.6`, commit `96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91`:

- `k`: <https://github.com/zenml-io/kitaru/tree/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91>
- `ui`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/api/ui.py>
- `cfg`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/api/config.py>
- `scfg`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/config.py>
- `app`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/api/app.py>
- `auth`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/auth.py>
- `deps`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/dependencies/auth.py>
- `svc`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/auth/auth_service.py>
- `grant`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/api_models/v1/auth.py>
- `users`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/users.py>
- `keys`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/api_keys.py>
- `perm`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/permissions/admin_flag.py>
- `devmap`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/mapping/devices.py>
- `info`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/info.py>
- `uirouter`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/ui.py>
- `oas`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/openapi/openapi.json>
- `dock`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docker/release-server.Dockerfile>
- `pyp`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/pyproject.toml>
- `rel`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/releases/python/kitaru/0.27.6.toml>
- `dl`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/scripts/download-ui.sh>
- `fe`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/FRONTEND-TESTING.md>
- `lic`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/LICENSE>
- `chg`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/CHANGELOG.md>
- `dep`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/DEPRECATIONS.md>
- `helmenv`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/helm/templates/_environment.tpl>
- `helmval`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/helm/values.yaml>
- `tspkg`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/packages/core/package.json>
- `tsgen`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/packages/core/scripts/generate-openapi.mjs>
- `tsres`: <https://github.com/zenml-io/kitaru/tree/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/packages/core/src/resources>
- `docker-md`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/deploy/docker.md>
- `auth-md`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/deploy/authentication.md>
- `sdks-md`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/deploy/sdks.md>
- `uth-md`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/concepts/under-the-hood.md>

Published Kitaru docs:

- `d-auth`: <https://docs.zenml.io/kitaru/running-in-production/authentication>
- `d-docker`: <https://docs.zenml.io/kitaru/getting-started/deploy/docker>
- `d-uth`: <https://docs.zenml.io/kitaru/core-concepts/under-the-hood>

Release artifacts and registries:

- Kitaru 0.27.6 on PyPI, wheel inspected for `kitaru/_ui/bundle_manifest.json` (`kitaru-ui-v0.5.2`) and the bundled `dist/`: https://pypi.org/project/kitaru/0.27.6/
- Docker Hub image `zenmldocker/kitaru-server`, tag `0.27.6` → digest `sha256:05ea00482c0adf4609a34d2c95575a6530ce5d60f6cea0e79716336d9e251d47`: https://hub.docker.com/r/zenmldocker/kitaru-server
- Public Suffix List, checked for `sslip.io`: https://publicsuffix.org/list/public_suffix_list.dat
- Dashboard source repo, as referenced by the release scripts (404 to anonymous API): https://github.com/zenml-io/zenml-frontend-monorepo
- Older public dashboard, Apache-2.0: <https://github.com/zenml-io/kitaru-ui>, [README][kui-readme]

Other Kitaru source files cited:

- `lock`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/uv.lock>
- `pyp21` (pyproject.toml at `v0.21.0`): <https://github.com/zenml-io/kitaru/blob/v0.21.0/pyproject.toml>
- `idem`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/idempotency.py>
- `just`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/Justfile>
- `ci`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/.github/workflows/ci.yml>
- `relwf`: <https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/.github/workflows/release.yml>

Standards and libraries (non-Kitaru claims):

- Starlette 1.3.1 `CORSMiddleware.is_allowed_origin` (`origin in self.allow_origins`): <https://github.com/encode/starlette/blob/1.3.1/starlette/middleware/cors.py#L98-L105>, docs <https://www.starlette.io/middleware/#corsmiddleware>
- RFC 6265bis, SameSite cookies: <https://httpwg.org/http-extensions/draft-ietf-httpbis-rfc6265bis.html#name-samesite-cookies>
- URL Standard, registrable domain: <https://url.spec.whatwg.org/#host-registrable-domain>

This repo:

- [README.md](../../README.md), [docs/team-quickstart.md](../team-quickstart.md), [docs/aws-pilot.md](../aws-pilot.md)
- [infra/bootstrap.sh.tftpl](../../infra/bootstrap.sh.tftpl), [infra/server.tf](../../infra/server.tf), [infra/variables.tf](../../infra/variables.tf), [infra/network.tf](../../infra/network.tf), [infra/refresh.py](../../infra/refresh.py), [infra/tests/](../../infra/tests/), [src/evalve/readiness.py](../../src/evalve/readiness.py)

[k]: https://github.com/zenml-io/kitaru/tree/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91
[ui]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/api/ui.py
[cfg]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/api/config.py
[scfg]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/config.py
[app]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/api/app.py
[auth]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/auth.py
[deps]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/dependencies/auth.py
[svc]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/auth/auth_service.py
[grant]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/api_models/v1/auth.py
[users]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/users.py
[keys]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/api_keys.py
[perm]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/permissions/admin_flag.py
[devmap]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/mapping/devices.py
[info]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/info.py
[uirouter]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/routers/ui.py
[oas]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/openapi/openapi.json
[dock]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docker/release-server.Dockerfile
[pyp]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/pyproject.toml
[rel]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/releases/python/kitaru/0.27.6.toml
[dl]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/scripts/download-ui.sh
[fe]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/FRONTEND-TESTING.md
[lic]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/LICENSE
[chg]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/CHANGELOG.md
[dep]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/DEPRECATIONS.md
[helmenv]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/helm/templates/_environment.tpl
[helmval]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/helm/values.yaml
[tspkg]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/packages/core/package.json
[tsgen]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/packages/core/scripts/generate-openapi.mjs
[tsres]: https://github.com/zenml-io/kitaru/tree/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/packages/core/src/resources
[docker-md]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/deploy/docker.md
[auth-md]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/deploy/authentication.md
[sdks-md]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/deploy/sdks.md
[uth-md]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/docs/book/concepts/under-the-hood.md
[d-auth]: https://docs.zenml.io/kitaru/running-in-production/authentication
[d-docker]: https://docs.zenml.io/kitaru/getting-started/deploy/docker
[d-uth]: https://docs.zenml.io/kitaru/core-concepts/under-the-hood
[kui]: https://github.com/zenml-io/kitaru-ui
[kui-readme]: https://github.com/zenml-io/kitaru-ui/blob/main/README.md
[lock]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/uv.lock
[pyp21]: https://github.com/zenml-io/kitaru/blob/v0.21.0/pyproject.toml
[idem]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/src/kitaru/server/adapters/rest/idempotency.py
[just]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/Justfile
[ci]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/.github/workflows/ci.yml
[relwf]: https://github.com/zenml-io/kitaru/blob/96dfb8236f09720a3d31a9c3a67cf4ac7e19ca91/.github/workflows/release.yml
[starlette-cors]: https://github.com/encode/starlette/blob/1.3.1/starlette/middleware/cors.py#L98-L105
[rfc-samesite]: https://httpwg.org/http-extensions/draft-ietf-httpbis-rfc6265bis.html#name-samesite-cookies
[url-regdom]: https://url.spec.whatwg.org/#host-registrable-domain
