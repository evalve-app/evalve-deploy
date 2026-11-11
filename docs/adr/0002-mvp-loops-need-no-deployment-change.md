# MVP Loops need no deployment change

Decided 2026-11-11.

In the MVP, a Loop is a series of automated Kitaru experiments on one agent and one cohort. Each Round registers a new Kitaru agent version that changes only the system prompt, then runs one experiment for that version. A Loop has no Gates, and the Evalve dashboard reads Loops and Rounds from Kitaru experiments through `/api/v1`. So Evalve deploys no Loop state API: no container, no `/loops-api/` route in the Caddyfile, and no Loop schema in RDS. We chose this because Kitaru already stores everything that a Loop without Gates needs, and a separate store would add a container, a route, and a database schema for state that Kitaru keeps.

This replaces, for the MVP, the part of `evalve-ui` ADR 0002 that puts a Loop state API module in this repo.

## Consequences

- The Caddy routes in ADR 0001 do not change. The Loop pages use the same Kitaru API paths as the rest of the Evalve dashboard.
- `evalve-ui` must change before step 2. Its Loop client calls `/loops-api/` (`src/api/loops/`), its spec allows changes to the model and the model parameters, and its tickets 13 and 14 add Gates and Loop decisions. This repo records the conflict but does not edit `evalve-ui`.
- `evalve-ui` ADR 0002 limits the local worker with `KITARU_WORKER_SCOPE` to the agent version that the Loop tests. Because each Round makes a new agent version, that rule must change in `evalve-ui`.
- If a later version adds Gates or other Loop state that Kitaru cannot store, reopen this decision. The Loop state API would then need a container, a route, and a schema here.
