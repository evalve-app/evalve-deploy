# An Experiment of a Loop is a commit and an agent version

Decided 2026-11-16. Story [sc-234](https://app.shortcut.com/team-4-1/story/234).

In the MVP, each Experiment of a Loop is an override on one pinned agent version, with no git. The target flow ends with a draft PR that a person can merge, so each Experiment must be a real change in the agent repo. We decided that the Teammate's coding agent writes the change, and that the backend checks it, commits it to `loop/<id>` and registers one Kitaru agent version for the commit. We chose this because the backend stays the one owner of the Loop rules and the one writer to GitHub, and it needs no model key.

The terms Loop, Experiment, Control Experiment and Run are in `GLOSSARY.md`. Older text says "Round" for Experiment.

## Decision

1. **Who writes the change.** The Teammate's Claude Code session writes the change for each Experiment and sends it with the Evalve MCP. The backend checks the change, commits it, and starts the Runs. Only the backend writes to GitHub, through the GitHub App. A backend LLM that writes the change ([sc-258](https://app.shortcut.com/team-4-1/story/258)) is parked.
2. **Two stages.**
   * Stage 1 (epic 01): an Experiment is one override, `system_prompt` or `model`, with no git. The Loop rules, the store, the API and the runner are built on this.
   * Stage 2 (epic 05): an Experiment is one commit on `loop/<id>` and one Kitaru agent version for that commit, with `EVALVE_COMMIT=<sha>` in `run.env` and the commit trailer `Kitaru-Experiment: <id>`.
3. **No overrides in stage 2.** In stage 2, a prompt change is a file change only. The `system_prompt` override is removed. The `model` override is not available until the model is in a file on the allow-list.
4. **The format of a change.** The full new content of each changed file, with a one-line reason. Not a unified diff.
5. **Which files a change can touch.** An allow-list of exact paths. The Operator sets the allow-list for the agent. A Loop setup can make it narrower, and cannot make it wider. In v1, each path must be a text file (`.md` or `.txt`) that holds a prompt. The backend refuses each other path, and it refuses a change that the Loop already tried.
6. **Changes do not accumulate.** Each Experiment applies its change to the tree of the base SHA of `loop/<id>`. A failed Experiment keeps its commit and gets a revert commit, whose tree is the tree of the base SHA.
7. **The order for each Experiment.** Find or create the Kitaru experiment, commit with the trailer, register the agent version for the SHA, start the Run. Each step is safe to do again after a restart.
8. **The Control Experiment** is Experiment 0. It has no commit, and it runs the base SHA of `loop/<id>`.
9. **Passing.** An Experiment passes when it has N consecutive green Runs (`required_consecutive_greens`). A Run is green only when it is complete and each session of the cohort version passes each selected binary evaluator. A red Run ends the Experiment. An incomplete Run is not red: it gets a retry, and two incomplete Runs in a row make the Loop `failed` with the outcome `error`.
10. **End states.** A Loop that stops for one of these reasons has the status `completed` and one outcome:
    * `passed`: an Experiment passed.
    * `already_passing`: the Control Experiment passed. The Loop makes no change, no branch and no PR.
    * `budget_reached`: the budget limit stopped the Loop.
    * `stopped_idle`: the Loop stayed in `needs_change` for 24 hours with no change. The Loop keeps its branch and gets no PR.
11. **The result of a passed Loop** is one draft PR from `loop/<id>` to the default branch. The PR shows a commit and a revert for each failed Experiment, then the commit of the Experiment that passed. The GitHub App cannot merge the PR and cannot mark it ready for review. There is no approval step in the dashboard ([sc-260](https://app.shortcut.com/team-4-1/story/260) is parked).
12. **One agent repo for v1.** The pilot agent repo is [`evalve-app/JavaScript-chatbot`](https://github.com/evalve-app/JavaScript-chatbot). Its prompts are in `backend/app/agents/prompts.py`, which is code. [sc-243](https://app.shortcut.com/team-4-1/story/243) moves them to text files before a Loop can change them.
13. **The worker (provisional).** One one-shot Fargate task for each Experiment, limited to one job with `KITARU_WORKER_SCOPE__JOB_ID`. The task downloads the agent source for the commit. The source is not in the image. Change this point when the spike [sc-235](https://app.shortcut.com/team-4-1/story/235) has results. Fargate work belongs to the epic "04 Rounds on Fargate".

## Considered options

* **An LLM in the backend writes the change** (parked, sc-258). A Loop would run with no person and no laptop. Rejected for v1: it adds a model key and a cost source to the backend, and the backend would need the repo content to write a good change. This ADR does not block it, because a backend author would send the same change to the same route.
* **Claude Code commits and pushes with the Teammate's git credentials.** Rejected: the backend could not enforce the allow-list, the trailer or the revert, and each Teammate would need write access to the agent repo.
* **A unified diff as the change format.** Rejected: the backend has no clone, so it would have to fetch each file and apply the diff, and a diff that does not apply is a new failure case. With full content, the allow-list is a path check, the commit is one call to the Git Data API, and the "already tried" check is a hash of the content.
* **An allow-list that the Teammate sets at Loop start.** Rejected: the limit would protect nothing.

## Consequences

* A reviewer sees the full Loop in the PR history. A session traces to a commit, and a commit traces to a Kitaru experiment.
* A Loop moves only while a coding agent is connected. The idle limit in point 10 ends a Loop that nobody continues.
* **Security.** Agent code runs in the worker task and can read the worker's keys. The allow-list of prompt text files is what makes this acceptable (the secrets ADR, story [sc-233](https://app.shortcut.com/team-4-1/story/233)). A wider allow-list, or a prompt in a code file, reopens both ADRs.
* The store and the API get two new outcomes, `already_passing` and `stopped_idle`. They get no new status.
* The term "Round" goes out of use. The MCP tool `propose_round` becomes `propose_experiment`, and the stories and the Loop API change their wording.
* Revisit when the spike sc-235 has results (point 13), when a Loop must run unattended, when a Loop must change code or the model in stage 2, or when a second agent repo is necessary.
