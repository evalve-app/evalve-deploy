# 05: Stop up on a Kitaru version mismatch

**What to build:** Before Terraform runs, `up` reads the Kitaru version that the Evalve dashboard image targets and the Kitaru version of the server, as ticket 01 decided. If they differ, `up` stops and names both versions. If a label cannot be read, `up` stops with a clear message. The check never passes silently. The check does not run when `ui_image` is empty. A new function reads image labels from the public registry without Docker. Tests mock it in the same way as `verify_dns`. Spec: `.scratch/evalve-dashboard-rollout/spec.md`.

**Blocked by:** 01 (Choose the version label contract), 02 (Run the Evalve dashboard at /app/).

**Status:** ready-for-agent

- [ ] A function reads the labels of a public image digest through the anonymous registry API.
- [ ] `up` test: matching versions continue to Terraform.
- [ ] `up` test: a mismatch stops before `terraform.apply` and before any AWS write, and the message names both versions.
- [ ] `up` test: an unreadable or missing label stops `up`.
- [ ] `up` test: an empty `ui_image` skips the check and the registry is not called.
- [ ] The README states that the Evalve dashboard image must target the same Kitaru version as `server_image`.
- [ ] All local checks from the README pass.
