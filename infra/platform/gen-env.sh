#!/usr/bin/env bash
# Create .env with random secrets, or add only the keys that are missing (existing values are kept).
# Never commit .env.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "# secrets for lt-platform — keep private, never commit" > .env; }
chmod 600 .env
added=0
add() {  # add KEY VALUE  (only if KEY is not in .env yet)
  if ! grep -q "^$1=" .env; then
    echo "$1=$2" >> .env
    echo "  + $1"
    added=$((added + 1))
  fi
}
rand() { openssl rand -hex 16; }

TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
HOSTS="localhost,localhost:*,127.0.0.1,127.0.0.1:*,swt17,swt17:*,*.ts.net,*.ts.net:*,100.*"
ORIGINS="http://localhost:5000,http://127.0.0.1:5000,http://swt17:5000,http://*.ts.net:5000"
if [ -n "$TS_IP" ]; then HOSTS="$HOSTS,$TS_IP,$TS_IP:*"; ORIGINS="$ORIGINS,http://$TS_IP:5000"; fi

echo "checking .env …"
# core services
add POSTGRES_PASSWORD "$(rand)"
add MLFLOW_DB_PASSWORD "$(rand)"
add AIRFLOW_DB_PASSWORD "$(rand)"
add S3_ACCESS_KEY "lt-$(openssl rand -hex 6)"
add S3_SECRET_KEY "$(rand)$(rand)"
add MLFLOW_ALLOWED_HOSTS "$HOSTS"
add MLFLOW_CORS_ORIGINS "$ORIGINS"
add BUCKETS "mlflow,lt-logs,lt-datasets,lt-models"
# airflow
add AIRFLOW_UID "$(id -u)"
add AIRFLOW_ADMIN_PASSWORD "$(openssl rand -hex 12)"
add AIRFLOW_FERNET_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"
add AIRFLOW_JWT_SECRET "$(rand)$(rand)"
add AIRFLOW_API_SECRET_KEY "$(rand)$(rand)"

if [ "$added" -eq 0 ]; then echo "nothing to add — .env is complete"; else echo "added $added key(s) to .env"; fi
