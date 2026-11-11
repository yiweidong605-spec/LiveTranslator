#!/usr/bin/env bash
# Pull-based continuous deployment of the platform (run every 5 min by lt-deploy.timer, or by hand).
#
#   you: git push main ──► GitHub Actions CI ──(all checks green)──► moves branch "deploy"
#                                                                         │
#   mini PC (this script): git fetch deploy ── new commit? ── apply ──────┘
#
# * Only commits that passed CI reach the mini PC: this follows "deploy", never "main".
# * Pull, not push: GitHub holds no credentials for the mini PC; the mini PC uses a read-only
#   deploy key that works for this one repository only.
# * What gets restarted depends on what changed:
#     DAGs only                      → nothing (Airflow picks them up by itself)
#     compose file / Dockerfiles     → docker compose up -d --build (only changed services restart)
#     systemd units, provisioning    → not automatic (needs sudo): listed as a manual step
# * Automatic rollback: if bringing the stack up fails, the previous commit is restored and started.
# * Status: deploy/status/last-deploy.json (the Airflow healthcheck tags its runs with the commit).
set -Eeuo pipefail

main() {  # everything in a function: bash then parses the whole file before git rewrites it mid-run
  local repo platform status_dir old new changed actions=() notes=()
  platform="$(cd "$(dirname "$0")/.." && pwd -P)"           # …/LiveTranslator/infra/platform
  repo="$(git -C "$platform" rev-parse --show-toplevel)"
  status_dir="$platform/deploy/status"
  mkdir -p "$status_dir"
  exec 9>"$status_dir/.lock"
  flock -n 9 || { echo "another deploy is running"; return 0; }
  cd "$repo"

  git fetch --quiet origin deploy
  old="$(git rev-parse HEAD)"
  new="$(git rev-parse origin/deploy)"
  if [ "$old" = "$new" ]; then
    [ "${1:-}" = "--verbose" ] && echo "up to date at ${old:0:8}"
    return 0
  fi
  # a commit that already failed is not retried every 5 minutes; the next push (a fix) will be
  if [ "$(last_field attempted_commit)" = "$new" ]; then
    [ "${1:-}" = "--verbose" ] && echo "${new:0:8} failed before, waiting for a new commit"
    return 0
  fi
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    # (no ATTEMPTED here: once the server copy is clean again, the next run deploys normally)
    status false "$old" "$old" "files were edited directly on the server; commit them via GitHub instead (git status)"
    echo "✗ local edits on the server — not deploying. See: git -C $repo status"; return 1
  fi

  changed="$(git diff --name-only "$old" "$new" -- infra/platform)"
  echo "deploying ${old:0:8} → ${new:0:8} ($(git log -1 --format=%s "$new"))"
  git reset --quiet --hard "$new"     # tree is clean (checked above); ignored files (.env, logs) untouched

  if grep -qE '^infra/platform/(docker-compose\.yml|airflow/Dockerfile|mlflow/)' <<< "$changed"; then
    actions+=("docker compose up -d --build")
    if ! (cd "$platform" && docker compose up -d --build --remove-orphans --quiet-pull); then
      echo "✗ starting the new version failed — rolling back to ${old:0:8}"
      git reset --quiet --hard "$old"
      (cd "$platform" && docker compose up -d --build --remove-orphans --quiet-pull) || true
      ATTEMPTED="$new" status false "$old" "$old" "docker compose up failed for ${new:0:8}; rolled back to ${old:0:8}"
      return 1
    fi
  fi
  grep -qE '^infra/platform/airflow/dags/' <<< "$changed" && actions+=("DAGs updated (Airflow reloads them)")
  grep -qE '^infra/platform/backup/lt-backup\.(service|timer)' <<< "$changed" \
    && notes+=("backup timer changed: run  bash ~/platform/backup/install-backup.sh")
  grep -qE '^infra/platform/deploy/lt-deploy\.(service|timer)' <<< "$changed" \
    && notes+=("deploy timer changed: run  bash ~/platform/deploy/install-deploy.sh")
  grep -qE '^infra/platform/(provision-sync\.sh|backup/provision-backup-reader\.sh|gen-env\.sh|tailscale-serve\.sh|postgres/init/)' <<< "$changed" \
    && notes+=("a setup script changed: re-run it if the change matters for you (see git log)")

  ACTIONS="$(IFS=';'; echo "${actions[*]:-}")" NOTES="$(IFS=';'; echo "${notes[*]:-}")" \
    status true "$new" "$old" ""
  echo "✓ deployed ${new:0:8} · actions: ${actions[*]:-none}"
  for n in "${notes[@]}"; do echo "  ⚠ manual step: $n"; done
}

last_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' \
    "$status_dir/last-deploy.json" "$1" 2>/dev/null || true
}

status() {  # status ok deployed previous error   (env: ATTEMPTED, ACTIONS, NOTES)
  python3 - "$status_dir/last-deploy.json" "$@" <<'PY'
import json, os, sys, time
path, ok, deployed, previous, err = sys.argv[1:6]
st = {"ok": ok == "true", "deployed_commit": deployed, "previous_commit": previous, "at": int(time.time()),
      "attempted_commit": os.environ.get("ATTEMPTED") or None,
      "actions": [a for a in os.environ.get("ACTIONS", "").split(";") if a],
      "manual_steps": [n for n in os.environ.get("NOTES", "").split(";") if n], "error": err or None}
tmp = path + ".tmp"
json.dump(st, open(tmp, "w"), indent=1)
os.replace(tmp, path)
PY
}

main "$@"
exit
