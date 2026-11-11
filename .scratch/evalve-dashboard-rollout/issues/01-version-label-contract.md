# 01: Choose the version label contract

**What to build:** A recorded decision on how `up` learns the Kitaru version of both images. Check whether the pinned `zenmldocker/kitaru-server` image carries a Kitaru version label that can be read from the public registry without Docker. If it does not, choose a Kitaru version setting that sits next to `server_image` and is validated with it. Choose the label key that every `evalve-ui` image must set to name the Kitaru version it targets. This ticket produces a decision, not code. Spec: `.scratch/evalve-dashboard-rollout/spec.md` (Further Notes).

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] The labels of the pinned server image are checked from the registry, and the result is written down with the exact label key and value, or with the fact that no version label exists.
- [x] The source of the server's Kitaru version is chosen: a registry label, or a new setting next to `server_image`.
- [x] The `evalve-ui` label key is chosen.
- [x] Both choices are added to this repo's ADR 0001 and to an ADR or the docs in `evalve-ui`.

## Comments

### Answer (2026-11-11)

- **Server labels:** the pinned index `zenmldocker/kitaru-server@sha256:05ea00482c0adf4609a34d2c95575a6530ce5d60f6cea0e79716336d9e251d47` has no version label. The `linux/amd64` config `sha256:5cdb892ff57fecf31dc153bbdd0fa2cff4c968e990f0d602b79fc7422bbad857` and the `linux/arm64` config `sha256:93c9dab9a324a0ef44088df65982fbdc788a70e8d55e7d45ac86acfaa342534a` have `Labels: null`. No index, manifest or descriptor carries a version annotation. Checked with an anonymous Docker Hub token.
- **Server version source:** a new `kitaru_version` setting next to `server_image`, default `0.27.6`. `up` checks that Docker Hub tag `kitaru-server:<kitaru_version>` resolves to the `server_image` digest. On 2026-11-11, tag `0.27.6` resolved to the pinned digest.
- **`evalve-ui` label key:** `io.github.evalve-app.kitaru.version`, an image config label read from the `linux/amd64` image, compared as an exact string.
- **Records:** `docs/adr/0001-evalve-dashboard-as-pinned-ui-image.md` (section "Kitaru version contract") and `evalve-ui` `docs/adr/0004-image-labels-target-kitaru-version.md`.
- **Note for ticket 05:** the setting, its default pairing, the tag check and the "stop when no version" rule are now part of its scope.
