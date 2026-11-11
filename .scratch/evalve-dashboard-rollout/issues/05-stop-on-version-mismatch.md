# 05: Stop up on a Kitaru version mismatch

**What to build:** Before Terraform runs, `up` reads the Kitaru version that the Evalve dashboard image targets and the Kitaru version of the server, as ticket 01 decided. If they differ, `up` stops and names both versions. If a label cannot be read, `up` stops with a clear message. The check never passes silently. The check does not run when `ui_image` is empty. A new function reads image labels from the public registry without Docker. Tests mock it in the same way as `verify_dns`. Spec: `.scratch/evalve-dashboard-rollout/spec.md`.

**Blocked by:** 01 (Choose the version label contract), 02 (Run the Evalve dashboard at /app/).

Status: resolved

- [x] A function reads the labels of a public image digest through the anonymous registry API.
- [x] `up` test: matching versions continue to Terraform.
- [x] `up` test: a mismatch stops before `terraform.apply` and before any AWS write, and the message names both versions.
- [x] `up` test: an unreadable or missing label stops `up`.
- [x] `up` test: an empty `ui_image` skips the check and the registry is not called.
- [x] The README states that the Evalve dashboard image must target the same Kitaru version as `server_image`.
- [x] All local checks from the README pass.

## Comments

- Scope follows ticket 01 and ADR 0001, "Kitaru version contract". The new `kitaru_version` setting (default `0.27.6`) is in `infra/variables.tf`, in the `deployment` output and in `lifecycle.TF_SETTINGS`, so `stop`, `start`, `down` and restore keep it. It is not in `user_data`, so it does not replace EC2.
- `src/evalve/registry.py` has two boundary functions. `image_labels` reads the config labels of the `linux/amd64` image of a digest. `tag_digest` hashes the Docker Hub manifest of a tag. Both get an anonymous token from the `WWW-Authenticate` challenge, check the content against its digest, and send the token as an unredirected header so that it does not follow a blob redirect. Every failure becomes an `EvalveError` that names the image or the tag.
- Live run on 2026-11-11: `tag_digest("zenmldocker/kitaru-server", "0.27.6")` returned the pinned digest `sha256:05ea0048…1d47`. `image_labels` read the labels of a public `ghcr.io` index. `ghcr.io/evalve-app/evalve-ui` returned HTTP 403. The probable cause is that `evalve-ui` has not published a public image yet.
- Decision: `up --kitaru-version <version>` gives the version when the saved `server_image` is not the default. `up` has no `server_image` option, so without this option the "stop and ask for the version" rule has no answer. ADR 0001 records it.
- `up` runs the checks after the `--dashboard evalve` guard and before the summary and the confirmation prompt. A mismatch therefore stops before any AWS write, and also before `record_restore` on `up --restore`.
- Order: the tag check runs first, then the label check. A missing label and a different value both stop `up`.
- Gap: when `ui_image` is empty and the version of a non-default `server_image` is unknown, Terraform records its default `0.27.6`. The tag check catches this pair on the next `up` with an image. ADR 0001 now records this rule.
- `image_labels` also requires that the config of a single-manifest image is `linux/amd64`. A registry reply larger than 4 MiB stops `up` with that cause.
- Gap: `--kitaru-version` with an empty `ui_image` is saved without a tag check, because `up` calls no registry then. The tag check runs on the next `up` that sets `ui_image`.
