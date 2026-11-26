# Evalve

Evalve deploys one shared Kitaru server to AWS. The operator uses the Evalve CLI. Teammates sign in through a browser invitation and connect their agents with an API key from the Bundled dashboard or the Evalve dashboard.

- [Teammate quickstart](docs/team-quickstart.md)
- [AWS pilot runbook and cost estimate](docs/aws-pilot.md)

## Operator setup

Install Python 3.11+, uv, and AWS CLI v2 on your workstation.

### Install Terraform

Evalve requires Terraform 1.11 or newer in the 1.x series. On macOS, use Homebrew to install it.

```sh
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
```

For other installation methods, follow HashiCorp's instructions for your platform and make sure `terraform` is on your `PATH`. [HashiCorp Terraform installation guide](https://developer.hashicorp.com/terraform/install)

Run `terraform version` to confirm the command works and reports a supported version.

### Install Evalve and sign in to AWS

From the root of your repository checkout, install the Evalve CLI and set up its AWS profile.

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

The sizes above are examples. Use one day of automated RDS backup retention for the Free plan pilot. A new team deployment defaults to seven days. `up` prompts for your estimate in USD before credits and shows the account, region, resources, and estimate for confirmation. It sets up the private, versioned S3 state bucket on the first run. When HTTPS is ready, `up` prints the Bundled dashboard URL and the Evalve dashboard URL.

Run `evalve credentials` with the same AWS profile and region to print the `default` username and bootstrap password in your terminal. If you changed the password in Kitaru, use your current password.

Use the same command and options to retry an interrupted provisioning attempt, adjusting any settings AWS rejected. After a successful apply, `up` reads the deployed sizes, hostname, and backup retention from S3 state. Run lifecycle operations from one operator machine at a time. Terraform uses S3 locking, and Evalve also takes a local operation lock.

If Terraform fails to save state to S3, Evalve prints the path of its local recovery file and blocks the next operation on that machine. Reconcile the recovery file with the S3 backend before retrying.

To switch to a custom domain, point the A records of `<hostname>` and `dashboard.<hostname>` at the deployment's Elastic IP, wait for DNS to resolve, then run `evalve up --domain <hostname>`. `up` checks both names before it changes AWS. Changing the hostname replaces EC2, so expect a short outage. The Elastic IP and RDS database are retained.

## Evalve dashboard

`up` installs the Evalve dashboard at `https://dashboard.<hostname>` (ADR 0007). The Bundled dashboard keeps `https://<hostname>`. The bundle is an `evalve-ui` release, pinned by URL and SHA-256 in `DEFAULT_DASHBOARD_URL` and `DEFAULT_DASHBOARD_SHA256` in `src/evalve/cli.py`, so you pass no option.

Through Systems Manager, the server downloads the bundle from GitHub, checks the SHA-256, unpacks it into its own release folder, and switches the served `current` link to it. A bad download leaves the served dashboard as it was. A release that is already unpacked is not downloaded again. `up` and `start` then wait until the dashboard answers with the Evalve page, and fail if it doesn't.

The first `up` after an upgrade to this version replaces EC2 once, because the server setup changed. The URL and the RDS data stay. With a custom domain, add the `dashboard.<domain>` DNS record before that `up`.

## Evalve backend

The Evalve backend holds Loop state (ADR 0004). `up` deploys it by default. The image is pinned by digest in `DEFAULT_BACKEND_IMAGE` in `src/evalve/cli.py`, so you don't pass a digest.

To test another image, pin it:
```sh
evalve up --backend-image ghcr.io/evalve-app/evalve-backend@sha256:<digest>
```

`up` saves the image and whether you pinned it in S3 state. A pinned image stays until you run `evalve up --backend-image default`. Pass `--backend-image ""` to remove the backend. A plain `up` never turns on a backend that is off.

Caddy sends `https://<hostname>/evalve/*` to the backend. `up` and `start` wait until `/evalve/health` reports a working database and the pinned image. A new image restarts only the backend container. Turning the backend on or off replaces EC2, and `up` installs the dashboard on the new server. The dashboard address also sends `/evalve/*` to the backend, so the dashboard can call it.

## Loop tasks and agent images

A deployment with a backend also gets what a Loop task needs (ADR 0006 point 13, and `GLOSSARY.md` for the term). A deployment with no backend gets none of this.

`up` makes the ECR repository `evalve-agent-images` before Terraform runs. The repository is private, has immutable tags and has no lifecycle policy. If it exists, `up` uses it and does not change it or its images. The AWS profile of the Operator needs `ecr:DescribeRepositories` and `ecr:CreateRepository`.

The repository stays after `evalve down` and after `evalve up --backend-image ""`. No Evalve command deletes the repository or an image, because a restore needs the agent images: a new build of the same commit gives a different digest. To remove the repository, delete it by hand in ECR.

Terraform adds a security group for Loop tasks and a push-only role for the repository. The security group has no ingress. Its egress is TCP 443, plus the ports that you set:

```sh
evalve up --loop-task-port 5432      # repeat the flag for more ports
evalve up --no-loop-task-ports       # back to TCP 443 only
```

The flags replace the saved list. A plain `up` keeps the list, and `down` and `up --restore` keep it too. Only the instance role can assume the push-only role, which can push an image and cannot delete one.

## Upgrade the backend or the dashboard

Both are pinned in `src/evalve/cli.py`, so an upgrade is a pull request:

1. Tag a release: `v*` in `evalve-backend`, whose workflow summary prints the image digest, or `v*` in `evalve-ui`, whose Release workflow summary prints the bundle URL and SHA-256.
2. In a pull request, set `DEFAULT_BACKEND_IMAGE`, or `DEFAULT_DASHBOARD_URL` and `DEFAULT_DASHBOARD_SHA256`, to the printed values.
3. After it merges, one Operator runs `git pull`, `uv tool install --force .`, and `evalve up`. Teammates need nothing, because the shared server is updated.

`uv tool install` copies the code, so the pins update only when you reinstall. Every Operator reinstalls after each pull, because an older install that runs `up` puts the older version back.

## Stop, start, and recover

```sh
evalve stop
evalve start
```

`stop` drains the Evalve backend and Kitaru through Systems Manager and stops EC2. RDS stays running and the Elastic IP stays associated. `start` brings the same instance back and checks the existing HTTPS endpoint, the Evalve dashboard and, if the deployment has one, the Evalve backend.

```sh
evalve down
evalve up --restore <snapshot-id>
```

`down` asks you to type `evalve` before making changes. It stops EC2, takes an encrypted manual RDS snapshot, verifies the snapshot and its application secret version, and saves the restore metadata to S3 before deleting the runtime infrastructure. It prints the snapshot ID and restore command. The snapshot, its application secret, the S3 state bucket, and the agent image repository remain in AWS.

If teardown is interrupted, rerun `down` to use the same snapshot. If restoration is interrupted, rerun `up --restore` with the same snapshot ID and options. While either operation is pending, Evalve blocks other lifecycle commands. Restore starts at a new `sslip.io` URL with the dashboard at `dashboard.<new hostname>`, and reuses the saved Kitaru credentials. Update your agents' URLs after restoring, or point your custom domain at the new Elastic IP and switch it again.

## Local checks

```sh
scripts/check.sh
```

The script runs the Python unit tests and the Terraform format, validate and test commands. It stops at the first failure. The `Local checks` GitHub Actions workflow runs the same script on every push and pull request. To run it before each commit, enable the hook one time per clone:

```sh
git config core.hooksPath .githooks
```

The Terraform tests use a mocked AWS provider. The live pilot verifies HTTPS, teammate sign-in and recording, and the full backup and restore workflow in AWS.
