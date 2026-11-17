#!/usr/bin/env bash
# Run every local check. Stop at the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."

run() {
  echo "==> $*"
  "$@"
}

PYTHONPATH=src run python3 -m unittest discover -s tests
run python3 -m unittest discover -s infra/tests -p 'test_*.py'
run terraform -chdir=infra init -backend=false -input=false -lockfile=readonly
run terraform -chdir=infra fmt -check -recursive
run terraform -chdir=infra validate
run terraform -chdir=infra test
echo "All local checks pass."
