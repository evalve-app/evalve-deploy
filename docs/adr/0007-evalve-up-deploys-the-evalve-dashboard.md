# `evalve up` deploys the Evalve dashboard from a pinned `evalve-ui` release

Decided 2026-11-18. Story [sc-237](https://app.shortcut.com/team-4-1/story/237). Supersedes ADR 0003.

Under ADR 0003, the Operator ran `evalve-ui/scripts/deploy-dashboard.sh` after each `evalve up` that made or replaced EC2, including `up --restore`. A forgotten run left a server with no Evalve dashboard. Now one `evalve up` deploys the Kitaru server, the Evalve backend and the Evalve dashboard. `evalve-ui` publishes each release as a static bundle, `dashboard.tgz`, on a GitHub release. This repo pins its URL and SHA-256 in `src/evalve/cli.py`. `up` installs it through Systems Manager, and Caddy serves it at `dashboard.<hostname>`. We chose this because the dashboard is static files, which Caddy already serves, and because it uses the same pin-and-upgrade path as the server and backend images.

## Decisions

1. **The bundle.** A `v*` tag in `evalve-ui` runs the CI checks, builds the app, and attaches `dashboard.tgz` (with `index.html` at the top level) and `dashboard.tgz.sha256` to a GitHub release. `gh release create` fails if the release exists, so a released file is never replaced. The tag alone sets the version.
2. **The pin.** `DEFAULT_DASHBOARD_URL` and `DEFAULT_DASHBOARD_SHA256` in `src/evalve/cli.py`. There is no Operator option: every `up` installs the repo default. To upgrade, a pull request changes both values. UI developers test with the Vite dev server, so a server never needs another build.
3. **The server setup.** The bootstrap creates `/etc/evalve/dashboard`, mounts it into Caddy at `/srv/dashboard`, and adds the site `dashboard.<hostname>`. The site serves `/srv/dashboard/current` as a single-page app, sends `/api/*` to Kitaru with Kitaru's hostname, and sends `/evalve/*` to the backend when the deployment has one. `index.html` and other pages get `Cache-Control: no-cache`. `/assets/*` files have content hashes in their names and are cached for a year.
4. **The install.** `up` sends one command through Systems Manager, as it does for the backend image, so a new release does not change `user_data` or replace EC2. The server downloads the asset from GitHub, checks the SHA-256, and unpacks it into `releases/<sha256>`. Then it renames a new relative `current` link over the old one. A bad download changes nothing that is served. An unpacked release is not downloaded again. The previous release stays for a rollback, and older ones are removed. The link is relative because Caddy sees the folder at another path.
5. **The checks.** After the install, `up` waits up to 3 minutes for `https://dashboard.<hostname>/` to return `<title>evalve</title>` (case-insensitive). `start` runs the same check on a server that has the `dashboard_url` output. The Bundled dashboard has `<title>Kitaru</title>`, so a route to Kitaru fails. A failed check fails the command, and `up --restore` keeps its checkpoint for a retry.
6. **Custom domains.** `up --domain` checks DNS for `<domain>` and `dashboard.<domain>` before any AWS change.
7. **No Kitaru version check.** The UI is built against a Kitaru `openapi.json`, but both versions change only through reviewed pull requests here. ADR 0001 planned a label check, which we did not build.

## Considered options

- **A UI Docker image (ADR 0001).** Rejected: it adds a container that only serves files, with label checks and an `/app/` prefix. The old code on tag `archive/zach-ui-test` no longer matches the server.
- **`up` runs `deploy-dashboard.sh`.** Rejected: each Operator would need an `evalve-ui` checkout and Node.js, and the two repos would be tied together on the Operator's machine.
- **`up` uploads the bundle to the state bucket**, as `deploy-dashboard.sh` does. Rejected: the server can download the public asset itself, and the SHA-256 guards what it gets.

## Consequences

- The first `up` after this change replaces EC2 once, because the bootstrap changed. The URL and the RDS data stay.
- With `--domain`, the Operator adds the `dashboard.<domain>` DNS record before the next `up`.
- `up` needs GitHub to answer. When it doesn't, `up` fails after Terraform, and a rerun is safe.
- Each `up` makes one Systems Manager call for the dashboard, even when the release is installed.
- `evalve-ui` keeps `deploy-dashboard.sh` and `remote-install.sh` for servers built before this ADR. On a new server, the script's Caddy validation sees two `dashboard.<hostname>` sites and stops without a change. Later UI work deletes the scripts, and with them the contract in ADR 0003.
