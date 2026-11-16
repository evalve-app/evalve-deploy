# `evalve-ui` deploys the Evalve dashboard onto this repo's server

Decided 2026-11-15. Supersedes ADR 0001.

The `evalve-ui` repo deploys the Evalve dashboard itself. Its `scripts/deploy-dashboard.sh` uploads the static build to the Evalve state bucket. AWS Systems Manager then runs `scripts/remote-install.sh` on the EC2 server. That script adds a second Caddy site, `dashboard.<hostname>`, which serves the build and sends `/api/*` to Kitaru. The Bundled dashboard keeps `<hostname>`. This repo does not deploy, pin or check the Evalve dashboard. We chose this because the dashboard team could ship from `evalve-ui` without a change to `evalve up`, and the server team does not have to release a UI image.

## Contract with `remote-install.sh`

`remote-install.sh` reads and edits files that `infra/bootstrap.sh.tftpl` writes. A change to any item below can break the dashboard deploy. Change `evalve-ui` in the same step.

- `/etc/evalve/config.json` has a `hostname` key.
- Kitaru listens on `127.0.0.1:8000`.
- `/etc/evalve/Caddyfile` exists. The script replaces the whole file with the Kitaru site and the dashboard site. Any other site or setting that the bootstrap adds to the Caddyfile is lost on each dashboard deploy.
- `/etc/systemd/system/evalve-caddy.service` names the Caddy image as `caddy@sha256:<digest>` and has this exact line, which the script uses as an anchor to add the `/etc/evalve/dashboard` mount:
  `  --mount type=bind,src=/etc/evalve/Caddyfile,dst=/etc/caddy/Caddyfile,readonly \`
- The EC2 instance can read the state bucket through the presigned URL that `deploy-dashboard.sh` sends.

## Consequences

- `infra/server.tf` sets `user_data_replace_on_change = true`. When `evalve up` replaces EC2, or `evalve up --restore` makes a new server, the new server has no Evalve dashboard. The Operator must run `deploy-dashboard.sh` in `evalve-ui` again.
- `--restore` without `--domain` gives a new `sslip.io` hostname, so the dashboard URL also changes.
- With `--domain`, the Operator must also point `dashboard.<domain>` at the server in DNS. Caddy cannot get a certificate for that name otherwise.
- `evalve up` and `evalve start` print only the Bundled dashboard URL.
- Do not set Kitaru `EXTERNAL_UI` on the server. `evalve-ui` does not have the `/devices/verify` page yet, so `kitaru login` would break.
