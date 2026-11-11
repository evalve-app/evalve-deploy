# Evalve

Evalve deploys one shared Kitaru server to AWS. The operator uses the Evalve CLI. Teammates sign in through a browser invitation and connect their agents with an API key from the Bundled dashboard.

- [Teammate quickstart](docs/team-quickstart.md)
- [AWS pilot runbook and cost estimate](docs/aws-pilot.md)

## Operator setup

Install Python 3.11+, uv, AWS CLI v2, and Terraform 1.11+ in the 1.x series. From a checkout of this repository, install the development CLI and set up its AWS profile.

```sh
uv tool install .
aws login --profile evalve
evalve up --help
```

If the AWS profile has no region configured, pass `--region` to `up`. The CLI checks the target account before and after confirmation and asks the AWS CLI to refresh credentials during Terraform operations. [AWS console sign-in for local development](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)

## Pilot provisioning

Review the monthly cost for the selected region and instance sizes before the pilot. Include Linux EC2, 20 GiB of gp3 EBS, one Elastic IP, single-AZ RDS PostgreSQL with 20 GiB of gp3, backups, Secrets Manager, S3, and expected data transfer. Check Free Tier coverage in the account's billing console. [AWS Pricing Calculator](https://calculator.aws/)

```sh
evalve up --server-type t3.small --database-class db.t4g.micro --backup-retention-days 1
```

The sizes above are examples. Use one day of automated RDS backup retention for the Free plan pilot. A new team deployment defaults to seven days. `up` prompts for your estimate in USD before credits and shows the account, region, resources, and estimate for confirmation. It sets up the private, versioned S3 state bucket on the first run. When HTTPS is ready, `up` prints the Bundled dashboard URL.

Run `evalve credentials` with the same AWS profile and region to print the `default` username and bootstrap password in your terminal. If you changed the password in Kitaru, use your current password.

Use the same command and options to retry an interrupted provisioning attempt, adjusting any settings AWS rejected. After a successful apply, `up` reads the deployed sizes, hostname, and backup retention from S3 state. Run lifecycle operations from one operator machine at a time. Terraform uses S3 locking, and Evalve also takes a local operation lock.

If Terraform fails to save state to S3, Evalve prints the path of its local recovery file and blocks the next operation on that machine. Reconcile the recovery file with the S3 backend before retrying.

To switch to a custom domain, point its A record at the deployment's Elastic IP, wait for DNS to resolve, then run `evalve up --domain <hostname>`. Changing the hostname replaces EC2, so expect a short outage. The Elastic IP and RDS database are retained.

## Evalve dashboard

The Evalve dashboard is the web UI from the `evalve-ui` repo. The Bundled dashboard is the web UI inside the Kitaru server image. To test the Evalve dashboard on the shared server while Teammates keep the Bundled dashboard, pass its image by digest:

```sh
evalve up --ui-image ghcr.io/evalve-app/evalve-ui@sha256:<digest>
```

`up` accepts only an immutable digest of the `evalve-ui` image in GitHub Container Registry under `evalve-app`. A tag such as `:latest` is rejected. Use an image that serves a build for the `/app/` base path on container port 3000.

The Evalve dashboard image must target the same Kitaru version as `server_image`. Before Terraform runs, `up` reads the image label `io.github.evalve-app.kitaru.version` from the public registry and compares it with the Kitaru version of the server (`0.27.6` by default). `up` also checks that the Docker Hub tag `zenmldocker/kitaru-server:<version>` points to the `server_image` digest. If the versions differ, `up` stops and names both versions. If `up` cannot read a label or the tag, it stops with a message. The check never passes silently. Without `--ui-image`, `up` does not call a registry. If the saved `server_image` is not the default, pass its version with `--kitaru-version`.

In step 1 (dashboard step `bundled`, the default), Caddy routes the paths on the deployment's hostname as follows:

- `/api/*`, `/health*`, `/docs*`, `/redoc*` and `/openapi.json` go to Kitaru.
- `/app` and `/app/*` go to the Evalve dashboard container.
- All other paths go to Kitaru, so the Bundled dashboard stays at `/`.

The Evalve dashboard container listens only on `127.0.0.1:3000`. It restarts on failure, starts on boot, and rotates its logs like the Kitaru container. After `up`, Evalve prints the Bundled dashboard URL and the Evalve dashboard URL.

### Step 2: switch to the Evalve dashboard

When the Evalve dashboard has full parity with the Bundled dashboard, switch the step and pass an image that serves a build for the `/` base path:

```sh
evalve up --dashboard evalve --ui-image ghcr.io/evalve-app/evalve-ui@sha256:<digest of the build for />
```

In step 2 (dashboard step `evalve`), Caddy routes the paths as follows:

- `/api/*`, `/health*`, `/docs*`, `/redoc*` and `/openapi.json` go to Kitaru.
- `/app` and `/app/*` redirect permanently (HTTP 301) to the same path and query without the `/app` prefix. For example, `/app/sessions/42?tab=steps` goes to `/sessions/42?tab=steps`.
- All other paths go to the Evalve dashboard container. The Bundled dashboard is no longer reachable.

Evalve does not set Kitaru's `EXTERNAL_UI` or `DASHBOARD_URL`, and the CORS and cookie settings do not change. `--dashboard evalve` with no saved or given image fails before any AWS change. After `up`, Evalve prints only the Evalve dashboard URL. To go back to step 1, run `evalve up --dashboard bundled --ui-image ghcr.io/evalve-app/evalve-ui@sha256:<digest of the build for /app/>`. Always pass the `/app/` build. Without `--ui-image`, `up` reuses the saved build for `/`.

### Saved settings

Evalve saves the image and the dashboard step in S3 state. A later `up` without `--ui-image` or `--dashboard` reuses the saved values, and `stop`, `start`, `down` and `up --restore` keep both values. The `up` summary shows the step and the image before you confirm. When an image is set, `up` checks two more things on the public URL after Kitaru is ready: the dashboard path (`/app/` in step 1, `/` in step 2) must return the Evalve dashboard, and `/api/v1/info` must return Kitaru JSON. If a check fails, `up` names the path and one next step. A change of the step or the image replaces EC2, so expect a short outage. The Elastic IP and RDS database are retained. Without `--ui-image`, the deployment works as before and serves only the Bundled dashboard.

## Stop, start, and recover

```sh
evalve stop
evalve start
```

`stop` drains Kitaru through Systems Manager and stops EC2. RDS stays running and the Elastic IP stays associated. `start` brings the same instance back and checks the existing HTTPS endpoint.

```sh
evalve down
evalve up --restore <snapshot-id>
```

`down` asks you to type `evalve` before making changes. It stops EC2, takes an encrypted manual RDS snapshot, verifies the snapshot and its application secret version, and saves the restore metadata to S3 before deleting the runtime infrastructure. It prints the snapshot ID and restore command. The snapshot, its application secret, and the S3 state bucket remain in AWS.

If teardown is interrupted, rerun `down` to use the same snapshot. If restoration is interrupted, rerun `up --restore` with the same snapshot ID and options. While either operation is pending, Evalve blocks other lifecycle commands. Restore starts at a new `sslip.io` URL and reuses the saved Kitaru credentials. Update your agents' URLs after restoring, or point your custom domain at the new Elastic IP and switch it again.

## Local checks

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m unittest discover -s infra/tests -p 'test_*.py'
terraform -chdir=infra init -backend=false
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
terraform -chdir=infra test
```

The Terraform tests use a mocked AWS provider. The live pilot verifies HTTPS, teammate sign-in and recording, and the full backup and restore workflow in AWS.
