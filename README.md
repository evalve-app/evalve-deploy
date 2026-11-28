# Evalve

Evalve deploys one shared Kitaru server to AWS. The operator uses the Evalve CLI. Teammates sign in through a browser invitation and connect their agents with an API key from the Bundled dashboard or the Evalve dashboard.

- [Teammate quickstart](docs/team-quickstart.md)
- [AWS pilot runbook and cost estimate](docs/aws-pilot.md)

## Operator setup

Install Python 3.11.4+, uv, and AWS CLI v2 on your workstation.

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

Caddy sends `https://<hostname>/evalve/*` to the backend. `start` waits until `/evalve/health` reports a working database and the pinned image. `up` checks it after it installs the dashboard. If the backend doesn't come up, `up` ends with an error that points to `journalctl -u evalve-backend`, and Kitaru and the dashboard keep running. Fix the cause and run `up` again. A new image restarts only the backend container. Turning the backend on or off replaces EC2, and `up` installs the dashboard on the new server. The dashboard address also sends `/evalve/*` to the backend, so the dashboard can call it.

The backend logs in as its own role, `evalve`, which can't reach Kitaru's database (ADR 0005). Before each start, the server runs `setup-database` with the master login to create the role and set its password from the secret `evalve/backend`. `journalctl -u evalve-backend` on the server shows what it did.

The first `up` after this version replaces EC2 once on every deployment, including those without a backend. Expect a brief outage. The URL and the RDS data stay.

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

Terraform also adds one policy to the instance role, for the work of the backend. With it the backend runs, stops, describes and tags Loop tasks of the cluster `evalve`, registers task definitions of the families `evalve-loop-task-*`, passes the execution role to ECS tasks, assumes the push-only role, and checks that an image is in the repository. The policy refuses a task with ECS Exec on. The role cannot list tasks.

## Build an agent image locally

Teammates and the Operator use `evalve build` from a clean clone of the agent
repository. It requires Python 3.11.4+, Git, and Docker with a running daemon, or
Podman when Docker is absent from `PATH`. It needs no AWS profile, AWS CLI or
Terraform. Docker being installed but unavailable is an error; the command
does not switch engines in that case.

```sh
export KITARU_API_URL=https://kitaru.example.com
evalve build --agent my-agent \
  --command 'uv run --no-sync python -m app.kitaru_entry' \
  --no-push
```

This version requires `--no-push`. It prints a local image name such as
`evalve-agent-image:abc123def456-20261127153000-<32 hex UUID>` and
"Nothing was pushed and nothing was registered." Push and registration are
the next part of the command. Even a local build needs the server's public
`/api/v1/info` endpoint and pulls its matching worker base image. It sends no
API credential. The version request times out after 15 seconds and requires
HTTPS. Plain HTTP is allowed only for exact `localhost` or a literal loopback
IP, such as `127.0.0.1` or `::1`; other hosts, including private-network
addresses, must use HTTPS.

The context is `git archive` of the current commit, from the repository root.
Commit or remove pending changes first; ignored, untracked files such as a
local `.env` are excluded. Submodules and Git LFS files are currently refused.
Safe archive extraction also refuses committed symlinks with absolute targets
or targets outside the archived repository; use relative links within it.
The Dockerfile is trusted project code and may download dependencies: this
certifies the committed context and the worker checks, not reproducibility.

The team's Dockerfile must:

- Start from `zenmldocker/kitaru-worker:<server version>`. The base layers must
  remain in order. Builds target `linux/amd64`, including on ARM machines.
- Install `kitaru[cli]` at that exact version in `/app/.venv`. Both the package
  version and `kitaru worker start --help` must work. If the commit has a
  `uv.lock`, every `kitaru` entry must match the server version too.
- Give `/app`, its contents, the working directory and its parents to root,
  without write access for uid 1000. For example, use `USER root`, copy source
  with `COPY --chown=root:root . /app`, install dependencies, then
  `RUN chown -R root:root /app && chmod -R go-w /app`.
- Keep `.env` files out of the source and keep `/usr/bin/timeout` and
  `/usr/local/bin/python3` executable. The working directory must exist.

Evalve adds the Supervisor from the installed CLI as the final layer, locks
`/tmp`, `/var/tmp`, `/run/lock`, `/home/kitaru` and their contents, and creates
`/tmp/evalve` owned by uid 1000 with mode 0700. Only `/tmp/evalve` in the image
may be writable by the worker user. Other writable image locations are a
refusal: correct their ownership and modes in the team's Dockerfile. The
engine creates `/dev/shm` and `/dev/mqueue` at runtime; the Supervisor clears
them between Runs. The final layer does not fix writable source files.

Each check runs as uid 1000 with networking disabled and a 60-second timeout.
Unexpected failures refuse the image. The image build itself remains
cancellable and has no fixed timeout. The base-image pull and both image builds
stream engine output live, including anything the Dockerfile prints. Check-container output stays
captured. Source-tree checks use `find` without following symlinks.
They require root ownership of the source files, directories and parents,
including files whose current mode is read-only: a file owner can restore
write access with `chmod`.
Source symlinks get a separate check of their full target chains, reachable
source directories and parent ownership. Mutable targets and broken or cyclic
links are refused with the source path in the error. The final image scan also
refuses any uid-1000-owned path outside `/tmp/evalve`, even if its current mode
is read-only; runtime paths under `/proc`, `/sys` and `/dev` remain excluded.
Source paths and link chains cannot use `/tmp/evalve` or its contents, even
when protected in the team image: the final layer gives that directory to the
worker. Keep the working directory and source targets outside that private area.
Evalve removes its temporary files,
check containers and intermediate image references on success, failure or
interruption, while retaining the successful named image, pulled base and
normal engine cache. Cleanup failures produce a nonzero exit and identify
resources needing removal, including the final image name if one was kept.
On SIGTERM, SIGHUP or SIGINT, the CLI stops its active child process group and
runs the same cleanup. Signals arriving during cleanup are deferred until all
engine and directory removals finish; further signals do not abort cleanup.
The first interruption determines the exit code (`128 + signal number`), even
if cleanup also fails. Errors name retained resources and any validated image.
Inherited ignored signals stay ignored. SIGKILL and machine failure cannot run cleanup.
The full invocation UUID in each final tag keeps concurrent builds of the
same commit in the same second from replacing each other's names.

Options:

| Flag | Meaning |
| --- | --- |
| `--dockerfile PATH` | Path within the commit, from its root; default `Dockerfile`. |
| `--working-dir DIR` | Absolute directory in the image; default `/app`. |
| `--env-file PATH` | `NAME=value` lines, read only for `KITARU_API_URL` and `KITARU_API_KEY`. Environment variables win, including empty ones. No shell expansion or quoting is performed. |
| `--timeout-seconds N` | Positive integer for the future run spec, not the build or checks. |
| `--tool TOOL` | Future capability; repeatable, duplicates removed in order. |
| `--secret NAME` | Future Kitaru secret; repeatable, duplicates removed in order. Local builds read no secret values. |
| `--env NAME=value` | Future plain run-spec value; repeatable. Empty values are allowed; duplicate names and names also used as secrets are refused. |

Both environment and secret names must avoid the reserved prefixes `EVALVE_`
and `KITARU_`. The future run-spec options are validated now but are not passed
to the build or checks. A command starting `uv run` must include `--no-sync`
among uv's options; an explicit environment Python such as
`/app/.venv/bin/python -m app.kitaru_entry` is also accepted. This catches a
known sync mistake; it does not prove an arbitrary command cannot sync.

To start the final image locally, export `EVALVE_BACKEND_URL`, `EVALVE_LOOP_ID`,
`EVALVE_LOOP_TASK_SECRET` and `KITARU_API_URL`. Use a real Loop ID and the secret
issued for its Loop task. Substitute the printed image name below. The four
numeric settings are example positive values:

```sh
docker run --rm --init --platform linux/amd64 \
  -e EVALVE_BACKEND_URL -e EVALVE_LOOP_ID \
  -e EVALVE_LOOP_TASK_SECRET -e KITARU_API_URL \
  -e EVALVE_MAX_REPLAYS=1 -e EVALVE_POLL_INTERVAL_SECONDS=2 \
  -e EVALVE_IDLE_TIMEOUT_SECONDS=300 -e EVALVE_SOFT_LIFETIME_SECONDS=3600 \
  evalve-agent-image:<printed-tag>
```

Both URLs must be reachable from the container and use HTTPS. HTTP is accepted
only when its hostname is exactly `localhost` or a literal loopback IP. For a
backend on the builder's laptop, use a container-reachable HTTPS endpoint or a
network mode that makes the backend reachable on the container's loopback.
`http://host.docker.internal` is refused. The Supervisor's entrypoint is already
in the image. On an Apple
Silicon Mac, Docker Desktop's x86 emulator (Rosetta) may exit with code 133
when a container program reads `/proc/<pid>/cmdline`: Rosetta tries to write
under the locked `/tmp`. Fargate Loop tasks have no emulator. Podman uses the
same command forms; its layer/digest parity still needs a real-engine check
on a machine with Podman installed.

When a cached multi-platform tag inspects as ARM despite an AMD64 pull,
Evalve requests `image inspect --platform linux/amd64`. An engine that cannot
resolve the AMD64 image is refused; update the engine or remove the conflicting
cached platform. Evalve never compares base layers from different architectures.

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

`stop` stops the Evalve backend, drains the Fargate Loop tasks, then stops Kitaru and EC2. It waits up to 150 seconds for ECS to confirm that the Loop tasks have stopped and warns about any tasks still active at the deadline. RDS stays running and the Elastic IP stays associated. `start` brings the same instance back and checks the existing HTTPS endpoint, the Evalve dashboard and, if the deployment has one, the Evalve backend.

```sh
evalve down
evalve up --restore <snapshot-id>
```

`down` asks you to type `evalve` before making changes. It uses the same shutdown sequence as `stop`, then takes an encrypted manual RDS snapshot, verifies the snapshot and its application secret version, and saves the restore metadata to S3 before deleting the runtime infrastructure. It prints the snapshot ID and restore command. The snapshot, its application secret, and the S3 state bucket remain in AWS.

The agent image repository, its images, and ECS task definitions also remain after `down`. The images continue to incur storage charges. Only the Operator deletes the repository, images, or task definitions using an AWS login. A restore needs the original images. A Loop restored from a snapshot fails if its image has been deleted.

If teardown is interrupted, rerun `down` to use the same snapshot. If restoration is interrupted, rerun `up --restore` with the same snapshot ID and options. While either operation is pending, Evalve blocks other lifecycle commands. Restore starts at a new `sslip.io` URL with the dashboard at `dashboard.<new hostname>`, and reuses the saved Kitaru credentials. Update your agents' URLs after restoring, or point your custom domain at the new Elastic IP and switch it again.

## Local checks

```sh
scripts/check.sh
```

The script runs the Python unit tests and the Terraform format, validate and test commands. It stops at the first failure. The `Local checks` GitHub Actions workflow runs the same script on every push and pull request.

The real-engine test intentionally runs automatically whenever Docker or
Podman is on `PATH`, including in CI. The engine must be running. It pulls
`python:3.11-slim` from Docker Hub and builds an AMD64 fixture, so it needs
registry access and uses emulation on an ARM machine. A registry outage or
rate limit can fail this check. When neither engine is installed, only this
test is skipped. Run it on its own with
`PYTHONPATH=src python3 -m unittest discover -s tests -p test_build_engine.py`.

To run all checks before each commit, enable the hook one time per clone:

```sh
git config core.hooksPath .githooks
```

The Terraform tests use a mocked AWS provider. The live pilot verifies HTTPS, teammate sign-in and recording, and the full backup and restore workflow in AWS.
