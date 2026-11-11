# 01: Choose the version label contract

**What to build:** A recorded decision on how `up` learns the Kitaru version of both images. Check whether the pinned `zenmldocker/kitaru-server` image carries a Kitaru version label that can be read from the public registry without Docker. If it does not, choose a Kitaru version setting that sits next to `server_image` and is validated with it. Choose the label key that every `evalve-ui` image must set to name the Kitaru version it targets. This ticket produces a decision, not code. Spec: `.scratch/evalve-dashboard-rollout/spec.md` (Further Notes).

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] The labels of the pinned server image are checked from the registry, and the result is written down with the exact label key and value, or with the fact that no version label exists.
- [ ] The source of the server's Kitaru version is chosen: a registry label, or a new setting next to `server_image`.
- [ ] The `evalve-ui` label key is chosen.
- [ ] Both choices are added to this repo's ADR 0001 and to an ADR or the docs in `evalve-ui`.
