# Evalve

Evalve deploys and operates one shared Kitaru server for a team, together with the dashboard that teammates use.

## Language

### People

**Operator**:
The person who runs the Evalve CLI against the team's AWS account and signs in to the dashboard with an Admin account.
_Avoid_: admin (for the person), owner

**Teammate**:
A person whom the Operator invites, who signs in with a non-admin account.
_Avoid_: user, member

**Admin account**:
A Kitaru account with the admin flag, such as the `default` bootstrap account. In the pilot, only the Operator uses one.
_Avoid_: superuser

### Loops

**Loop**:
An ordered collection of Experiments on one agent and one cohort version, which starts with the Control Experiment and stops when an Experiment passes or a limit is reached.

**Experiment**:
One change to the agent, tested by one or more Runs. Kitaru uses the same term for the record that it stores.
_Avoid_: Round, lap, candidate

**Control Experiment**:
Experiment 0 of a Loop, which tests the agent with no change.
_Avoid_: baseline, Round 0

**Run**:
One replay of each session in the cohort version, with a score from each selected evaluator.
_Avoid_: attempt

**Loop owner**:
The Kitaru account that started a Loop.
_Avoid_: owner (alone), creator

**Evalve backend**:
The Evalve service that owns the Loop rules and keeps the state of each Loop.
_Avoid_: Loop API, Loop state API, server

### Dashboards

**Bundled dashboard**:
The web UI that ships inside the Kitaru server image.
_Avoid_: Kitaru dashboard, default UI

**Evalve dashboard**:
The web UI from the separate `evalve-ui` repo that teammates use to sign in, manage API keys, and view agents and sessions.
_Avoid_: external UI, custom UI
