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

### Dashboards

**Bundled dashboard**:
The web UI that ships inside the Kitaru server image.
_Avoid_: Kitaru dashboard, default UI

**Evalve dashboard**:
The web UI from the separate `evalve-ui` repo that teammates use to sign in, manage API keys, and view agents and sessions.
_Avoid_: external UI, custom UI
