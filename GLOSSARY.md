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

**End state**:
The condition of a Loop that stopped and cannot continue.
_Avoid_: end (alone), terminal state

**Outcome**:
The reason for which a Loop reached its end state.
_Avoid_: result, end reason

**Next action**:
The step that a Loop needs next while it is not at an end state.
_Avoid_: state, phase

**Drive (a Loop)**:
To start a Run of a Loop. Only the coding agent of the Loop owner drives a Loop.
_Avoid_: advance, move forward (for the start of a Run)

**Refresh (a Loop)**:
To record the result of a finished Run, or an end state that needs no new Run. A refresh starts no Run.
_Avoid_: advance, poll

**Loop owner**:
The Kitaru account that started a Loop.
_Avoid_: owner (alone), creator

**Skill Loop**:
A Loop that a loop skill ran on a Teammate's machine against Kitaru, with no state on the Evalve backend. The Evalve dashboard shows it read-only.
_Avoid_: legacy Loop, old Loop, local Loop

**Evalve backend**:
The Evalve service that owns the Loop rules and keeps the state of each Loop.
_Avoid_: Loop API, Loop state API, server

**Evalve MCP**:
The local tool server through which a coding agent runs Loops on the Evalve backend.
_Avoid_: Loop MCP, MCP server (alone), Kitaru MCP

**Coding agent**:
The AI assistant on a Teammate's machine that calls the Evalve MCP. "Agent" alone is the agent that a Loop tests.
_Avoid_: agent (alone), client

**Agent image**:
The container image that holds one agent at one commit, with what it needs to run its Runs on AWS: the Kitaru worker and the Supervisor.

**Supervisor**:
The Evalve program in a Loop task that gets the Assignments and runs one Kitaru worker for each Replay.
_Avoid_: entrypoint, runner

**Loop task**:
The task on AWS that runs the Runs of one Loop. A Loop task serves one Loop only.
_Avoid_: worker task, Fargate worker, the Fargate task of a Loop

**Replay**:
One execution of the agent on one session of the cohort version, as a part of a Run. Kitaru uses the same term.
_Avoid_: case

**Assignment**:
What the Evalve backend gives a Loop task so that it can run one Replay.
_Avoid_: job (for what the Evalve backend gives)

**Idle (a Loop task)**:
The condition of a Loop task in which no Replay runs and no Replay waits to start. Only a Loop task is idle.
_Avoid_: idle (for a Loop)

**Failed start (a Loop task)**:
A Loop task that stopped, or gave no sign of life, before it was ready to run a Replay.
_Avoid_: launch failure, boot failure

**Wait limit**:
The longest time for which a Loop waits for a change before it stops.
_Avoid_: idle limit

### Secrets

**Application secret**:
The secret `evalve/application`, which holds the keys that the Kitaru server needs to start: the signing key, the encryption key and the password of the Admin account.
_Avoid_: Kitaru secret (that is a secret stored on the Kitaru server), app secret

**Database secret**:
The secret that holds the name and the password of the master user of the database.
_Avoid_: RDS secret, master secret

**Backend secret**:
The secret `evalve/backend`, which holds the keys that only the Evalve backend needs.
_Avoid_: the secret of the backend (for one key in it), worker secret

**Instance role**:
The AWS identity of the server on which Kitaru, the Evalve dashboard and the Evalve backend run. It is the only identity that reads the three secrets.
_Avoid_: server role, EC2 role

### Dashboards

**Bundled dashboard**:
The web UI that ships inside the Kitaru server image.
_Avoid_: Kitaru dashboard, default UI

**Evalve dashboard**:
The web UI from the separate `evalve-ui` repo that teammates use to sign in, manage API keys, and view agents and sessions.
_Avoid_: external UI, custom UI
