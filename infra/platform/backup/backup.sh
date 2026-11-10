#!/usr/bin/env bash
# Daily platform backup (run by the systemd timer lt-backup.timer, or by hand: bash backup/backup.sh).
#
#   1. PostgreSQL  logical dumps (pg_dump -Fc) of mlflow + airflow, roles (pg_dumpall --globals-only)
#                  + row counts at dump time (the restore drill compares against them)
#   2. RustFS      incremental mirror of the buckets "mlflow" and "lt-logs" through the S3 API (rclone)
#                  ("lt-datasets" is derived data: rebuild it with logbook_daily reprocess_all)
#   3. Config      .env (secrets; without it Airflow cannot decrypt its Fernet-encrypted fields)
#   4. restic      two encrypted, de-duplicated snapshots:
#                    tag "core"     = db + mlflow artifacts + config   (also pulled by the desktop)
#                    tag "raw-logs" = lt-logs                          (original lives on the desktop)
#   5. Retention   7 daily, 4 weekly, 12 monthly (GFS) — then prune
#   6. Sunday      restic check --read-data-subset=5% (detects silent corruption)
#   7. Status      backup/status/backup-status.json, read by the Airflow healthcheck
#
# Deliberately NOT an Airflow DAG: a backup must not depend on the system it backs up.
set -Eeuo pipefail
cd "$(dirname "$0")/.."                       # ~/platform
PLATFORM="$PWD"
STATUS_DIR="$PLATFORM/backup/status"
STATUS="$STATUS_DIR/backup-status.json"
BACKUP_ROOT="${LT_BACKUP_ROOT:-/srv/backup}"
STAGE="$BACKUP_ROOT/staging"
export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-$BACKUP_ROOT/restic}"
HOST_TAG="$(hostname)"
START=$(date +%s)
STEP="start"
mkdir -p "$STATUS_DIR" "$STAGE"/{db,s3,config}

exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || { echo "another backup is running, exiting"; exit 0; }

# secrets: read only the keys we need from .env (never `source` a file with arbitrary content)
envval() { grep -E "^$1=" "$PLATFORM/.env" | tail -1 | cut -d= -f2-; }
RESTIC_PASSWORD="$(envval RESTIC_PASSWORD)"; export RESTIC_PASSWORD
[ -n "$RESTIC_PASSWORD" ] || { echo "RESTIC_PASSWORD missing in .env — run backup/install-backup.sh"; exit 1; }

write_status() {  # write_status ok|failed [error message]
  local ok=$1 err=${2:-}
  python3 - "$STATUS" "$ok" "$err" "$STEP" "$START" <<'PY'
import json, os, sys, time
path, ok, err, step, start = sys.argv[1], sys.argv[2] == "ok", sys.argv[3], sys.argv[4], int(sys.argv[5])
extra = {}
for name in ("summary.json",):
    p = os.path.join(os.path.dirname(path), name)
    if ok and os.path.exists(p):
        extra = json.load(open(p))
st = {"ok": ok, "finished_at": int(time.time()), "duration_s": int(time.time()) - start,
      "failed_step": None if ok else step, "error": err or None, **extra}
# keep the last successful run so the healthcheck can say how old the newest *good* backup is
try:
    prev = json.load(open(path))
except Exception:
    prev = {}
st["last_success_at"] = st["finished_at"] if ok else prev.get("last_success_at")
tmp = path + ".tmp"
json.dump(st, open(tmp, "w"), indent=1)
os.replace(tmp, path)
PY
}
trap 'write_status failed "command failed: ${BASH_COMMAND:0:200}"; echo "✗ backup failed at step: $STEP"' ERR

dc() { docker compose "$@"; }

# ---------------------------------------------------------------- 1. PostgreSQL
STEP="postgres"
echo "== 1/5 PostgreSQL dumps"
for db in mlflow airflow; do
  dc exec -T postgres pg_dump -U postgres -Fc "$db" > "$STAGE/db/$db.dump.tmp"
  mv "$STAGE/db/$db.dump.tmp" "$STAGE/db/$db.dump"
  echo "  $db: $(du -h "$STAGE/db/$db.dump" | cut -f1)"
done
dc exec -T postgres pg_dumpall -U postgres --globals-only > "$STAGE/db/globals.sql.tmp"
mv "$STAGE/db/globals.sql.tmp" "$STAGE/db/globals.sql"
q() { dc exec -T postgres psql -U postgres -d "$1" -Atc "$2" 2>/dev/null || echo -1; }
cat > "$STAGE/db/row_counts.json" <<EOF
{"mlflow": {"experiments": $(q mlflow "select count(*) from experiments"),
            "runs": $(q mlflow "select count(*) from runs"),
            "metrics": $(q mlflow "select count(*) from metrics")},
 "airflow": {"dag": $(q airflow "select count(*) from dag"),
             "dag_run": $(q airflow "select count(*) from dag_run"),
             "task_instance": $(q airflow "select count(*) from task_instance")}}
EOF

# ---------------------------------------------------------------- 2. RustFS (S3) mirror
STEP="s3-mirror"
echo "== 2/5 RustFS buckets (incremental)"
export RCLONE_CONFIG_RUSTFS_TYPE=s3 RCLONE_CONFIG_RUSTFS_PROVIDER=Other
export RCLONE_CONFIG_RUSTFS_ENDPOINT="${LT_BACKUP_S3_ENDPOINT:-http://127.0.0.1:9000}"
export RCLONE_CONFIG_RUSTFS_ACCESS_KEY_ID="$(envval S3_ACCESS_KEY)"
export RCLONE_CONFIG_RUSTFS_SECRET_ACCESS_KEY="$(envval S3_SECRET_KEY)"
export RCLONE_CONFIG_RUSTFS_FORCE_PATH_STYLE=true
for b in mlflow lt-logs; do
  rclone sync "rustfs:$b" "$STAGE/s3/$b" --exclude "raw/_check/**" --fast-list --stats-one-line --stats 0 \
    --log-level ERROR
  echo "  $b: $(find "$STAGE/s3/$b" -type f | wc -l) files, $(du -sh "$STAGE/s3/$b" | cut -f1)"
done

# ---------------------------------------------------------------- 3. config
STEP="config"
install -m 600 "$PLATFORM/.env" "$STAGE/config/.env"
cp docker-compose.yml "$STAGE/config/"
git -C "$PLATFORM" rev-parse HEAD > "$STAGE/config/git-commit.txt" 2>/dev/null || echo "not a git checkout" > "$STAGE/config/git-commit.txt"

# ---------------------------------------------------------------- 4. restic snapshots
STEP="restic-backup"
echo "== 3/5 restic snapshots"
snap() {  # snap TAG PATH...   → prints "id bytes_added"
  local tag=$1; shift
  restic backup --host "$HOST_TAG" --tag "$tag" --json --quiet "$@" | python3 -c '
import json, sys
s = [json.loads(l) for l in sys.stdin if l.strip()]
s = [x for x in s if x.get("message_type") == "summary"][-1]
print(s["snapshot_id"][:8], s.get("data_added", 0), s.get("total_bytes_processed", 0))'
}
# (assignments, not `read < <(...)`: a failing restic inside process substitution would go unnoticed)
CORE="$(cd "$STAGE" && snap core db s3/mlflow config)"
RAW="$(cd "$STAGE" && snap raw-logs s3/lt-logs)"
read -r CORE_ID CORE_ADDED CORE_TOTAL <<< "$CORE"
read -r RAW_ID RAW_ADDED RAW_TOTAL <<< "$RAW"
echo "  core     $CORE_ID  (+$((CORE_ADDED / 1024)) KB new)"
echo "  raw-logs $RAW_ID  (+$((RAW_ADDED / 1024)) KB new)"

# ---------------------------------------------------------------- 5. retention + check
STEP="retention"
echo "== 4/5 retention (7 daily / 4 weekly / 12 monthly)"
restic forget --host "$HOST_TAG" --group-by host,tags --keep-daily 7 --keep-weekly 4 --keep-monthly 12 \
  --prune --quiet
CHECKED=0
if [ "$(date +%u)" = "7" ] || [ "${LT_BACKUP_FORCE_CHECK:-0}" = "1" ]; then
  STEP="check"
  echo "== 5/5 integrity check (5% of the data)"
  restic check --read-data-subset=5% --quiet
  CHECKED=1
else
  echo "== 5/5 integrity check: Sundays only"
fi

STEP="status"
REPO_BYTES=$(du -sb "$RESTIC_REPOSITORY" | cut -f1)
N_SNAPS=$(restic snapshots --json --quiet | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
cat > "$STATUS_DIR/summary.json" <<EOF
{"snapshots": {"core": "$CORE_ID", "raw-logs": "$RAW_ID"},
 "bytes_added": $((CORE_ADDED + RAW_ADDED)), "bytes_processed": $((CORE_TOTAL + RAW_TOTAL)),
 "repo_bytes": $REPO_BYTES, "n_snapshots": $N_SNAPS, "integrity_checked": $CHECKED,
 "row_counts": $(cat "$STAGE/db/row_counts.json")}
EOF
write_status ok
rm -f "$STATUS_DIR/summary.json"
echo "✓ backup done in $(( $(date +%s) - START ))s · repo $(du -sh "$RESTIC_REPOSITORY" | cut -f1) · $N_SNAPS snapshots"
