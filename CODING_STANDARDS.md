# Coding standards

Rules for review. `scripts/check.sh` covers the format, the validation and the tests, so this file holds only judgement calls.

## Words

- Names in code, comments, test names and error messages use the words of `GLOSSARY.md`, and none of its "Avoid" words.
- One term for one thing in a diff. If a file near the diff has a term for the thing (for example "ECR token"), the diff uses that term.
- A comment is one or more full sentences that give the reason, not the action that the code already shows.

## Documents agree with the diff

- `README.md` describes each behaviour that the diff changes for the Operator or for a Teammate.
- When the diff changes a rule that an ADR states, the diff changes the ADR, or the PR body says why not.

## Terraform (`infra/`)

- The name of a local shows what it holds. A local that holds an ARN ends in `_arn`, and one that holds an ARN pattern or a list ends in `_arns`.
- A comment on an IAM policy or statement cites the ADR point that sets its limit.
- A resource that exists only with a backend has `count = local.loop_task_enabled ? 1 : 0`.
- A value that a comment calls a placeholder names the story that settles it.

## Terraform tests (`infra/tests/`)

- An IAM policy is compared with a literal list of its statements, so a new statement fails the test.
- An expected value is a literal (an ARN, a name, a port). It refers to a resource attribute only when the mock provider makes the value.
- The name of a run is a sentence that states one behaviour.
- An error message says what must be true, in the words of the glossary.
