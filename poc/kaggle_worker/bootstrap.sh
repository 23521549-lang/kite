#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${KITE_DATA_DIR:-/var/lib/kite}"
cd "$REPO_ROOT"

if [[ "${KITE_BOOTSTRAP_DRY_RUN:-0}" == "1" ]]; then
  DRY_RUN=(--dry-run)
else
  DRY_RUN=()
fi

exec python3 -m poc.kaggle_worker.bootstrap --repo "$REPO_ROOT" --data-dir "$DATA_DIR" "${DRY_RUN[@]}"
