## Agent skills

### Issue tracker

Work lives in Shortcut, workspace `team-4-1`: epic → story (the spec) → sub-tasks (the tickets). Use the Shortcut MCP connector, not GitHub issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Default labels: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Git

Stage files by name, after you read `git status`. A different session can leave changes in the working tree.

Name the branch `<name>/sc-<id>/<short-slug>` and put `[sc-<id>]` in the PR title, so that Shortcut attaches the work to the story.
