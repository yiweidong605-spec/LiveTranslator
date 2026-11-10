# LiveTranslator platform (orchestration node)

Runs on the Ubuntu mini PC: PostgreSQL + RustFS (S3) + MLflow + Airflow 3.4 (LocalExecutor).

| Service | Local port | Tailnet URL | Purpose |
|---|---|---|---|
| MLflow | 127.0.0.1:5000 | http://swt17:5000 | experiment tracking, model registry |
| Airflow | 127.0.0.1:8080 | http://swt17:8080 | DAGs (login `admin`, password in `.env`) |
| RustFS console | 127.0.0.1:9001 | http://swt17:9001 | browse buckets |
| RustFS S3 API | 127.0.0.1:9000 | http://swt17:9000 | logs, datasets, artifacts |
| PostgreSQL | 127.0.0.1:5432 | — (local only) | metadata for MLflow + Airflow |

## First start
```bash
bash gen-env.sh                 # random secrets -> .env (chmod 600); re-run safely, only adds missing keys
docker compose up -d --build    # first build takes a few minutes
docker compose ps               # all services "running"/"healthy"
bash tailscale-serve.sh         # expose ports to the tailnet only
```

## Airflow
- DAGs live in `airflow/dags/` (mounted into the containers; new files are picked up within ~30 s).
- `platform_healthcheck` (hourly): RustFS write/read, MLflow health, outbound internet (detects an expired
  captive-portal session), disk space; results go to the MLflow experiment `platform-health`.
- `logbook_daily` (after every `sync.bat`, plus 08:00 daily): raw logbook → validated Parquet dataset,
  daily metrics (MLflow experiment `logbook-daily`), review list and training pairs in `lt-datasets/logbook/`.
  Only changed dates are processed; trigger with `{"reprocess_all": true}` to rebuild everything.
  Helpers shared by DAGs live in `airflow/dags/lt_lib/` (excluded from parsing by `.airflowignore`).
- Admin password: `grep AIRFLOW_ADMIN_PASSWORD .env`

## Desktop → platform sync
`./provision-sync.sh` creates two least-privilege accounts and writes `sync-credentials.env`
(copy it to `E:\translater\secrets\sync.env`, then delete it here):

| Account | Can | Cannot |
|---|---|---|
| RustFS `lt-sync` (policy `lt-sync-put`) | PUT objects under `lt-logs/raw/` | read, list, delete, touch any other bucket |
| Airflow `lt-sync` (role `LtSync`) | read assets, create asset events | see DAGs, trigger runs, read connections/variables |

So a compromised desktop can neither read nor wipe the platform. `sync.bat` only runs when started
by hand (no background process); option 3 checks the connection and that the key has no extra rights.
- Differences from the official compose file: LocalExecutor instead of Celery/Redis, reuses the platform
  PostgreSQL, random admin password, API server bound to 127.0.0.1.

## Backups (restic, daily 08:00)
`bash backup/install-backup.sh` once: installs restic + rclone, creates `/srv/backup/{restic,staging}`,
adds `RESTIC_PASSWORD` to `.env` (**also keep it in a password manager**), installs the systemd timer
`lt-backup.timer` and runs the first backup.

| What | How | Snapshot tag |
|---|---|---|
| PostgreSQL `mlflow`, `airflow` + roles | `pg_dump -Fc` / `pg_dumpall --globals-only` (consistent, no downtime) + row counts | `core` |
| RustFS bucket `mlflow` (artifacts) | rclone mirror through the S3 API (version-independent) | `core` |
| `.env`, `docker-compose.yml` | copied | `core` |
| RustFS bucket `lt-logs` (raw logbook, audio) | rclone mirror | `raw-logs` |
| `lt-datasets` | **not backed up**: derived, rebuild with `logbook_daily` → `reprocess_all` | — |

Retention 7 daily / 4 weekly / 12 monthly; `restic check --read-data-subset=5%` on Sundays.
The backup runs from systemd, not Airflow (it must not depend on what it backs up); Airflow only
watches `backup/status/backup-status.json` (`check_backup` in `platform_healthcheck`, red after 36 h).

**Pull copy on the desktop** (sync.bat → 4 once, then every sync.bat → 1): the desktop copies the
`core` snapshots over read-only SFTP to `I:\lt-backup` and, when a drive with `LT-BACKUP-DRIVE.txt`
is plugged in, on to that USB drive (offline copy). `backup/provision-backup-reader.sh` creates the
account `backup-reader`: key-only, `ForceCommand internal-sftp -R` (no shell, no tunnels, cannot
write or delete), repository group-readable via `ltbackup` + setgid, `staging/` (contains `.env`)
stays owner-only. The mini PC holds no credentials for the desktop, so it cannot delete those copies.

```bash
systemctl list-timers lt-backup.timer        # next run
journalctl -u lt-backup -n 50                # last run's log
sudo systemctl start lt-backup               # back up now
RESTIC_REPOSITORY=/srv/backup/restic RESTIC_PASSWORD=$(grep ^RESTIC_PASSWORD= .env | cut -d= -f2-) restic snapshots
```

## Day to day
```bash
docker compose ps
docker compose logs -f mlflow
docker compose restart mlflow
docker compose down             # stop (data stays in volumes)
```

## Design notes
- Ports are bound to 127.0.0.1 because Docker-published ports bypass ufw; `tailscale serve --tcp`
  forwards them to the tailnet, survives reboots, and keeps everything off the apartment LAN.
- MinIO community edition was archived in 2026; RustFS is the S3-compatible replacement used in
  MLflow's own docker-compose example.
- MLflow is pinned to >=3.15 (CVE-2026-64849). `MLFLOW_SERVER_ALLOWED_HOSTS` must list every host name
  you use in the browser, otherwise MLflow answers "Invalid Host header".
- The web UI also needs `MLFLOW_SERVER_CORS_ALLOWED_ORIGINS`: browsers attach an `Origin` header to POST
  requests (e.g. searching runs) and MLflow only trusts localhost origins by default, answering
  "You do not have permission to access this resource" / 403 "Cross-origin request blocked".
- Data lives in the named volumes `lt-platform_pgdata` and `lt-platform_s3data` (see Backups).
