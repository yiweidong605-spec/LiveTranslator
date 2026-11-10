#!/usr/bin/env bash
# Create the two least-privilege accounts the desktop's sync.bat uses, and write its credentials file.
#
#   RustFS  user "lt-sync"  policy "lt-sync-put": may only PUT objects under lt-logs/raw/
#                           (no read, no list, no delete, no other bucket)
#   Airflow user "lt-sync"  role  "LtSync":       may only read assets and create asset events
#                           (cannot see DAGs, trigger runs, read connections or variables)
#
# Safe to re-run: existing secrets in .env are kept, accounts are updated in place.
# Output: sync-credentials.env (chmod 600) → copy it to E:\translater\secrets\sync.env, then delete it here.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "✗ .env missing — run ./gen-env.sh first"; exit 1; }

# ---------------------------------------------------------------- secrets (kept if already there)
add() { grep -q "^$1=" .env || { echo "$1=$2" >> .env; echo "  + $1 added to .env"; }; }
add LT_SYNC_S3_SECRET "$(openssl rand -hex 24)"
add LT_SYNC_AIRFLOW_PASSWORD "$(openssl rand -hex 16)"
S3_SECRET="$(grep '^LT_SYNC_S3_SECRET=' .env | cut -d= -f2-)"
AF_PASSWORD="$(grep '^LT_SYNC_AIRFLOW_PASSWORD=' .env | cut -d= -f2-)"

# ---------------------------------------------------------------- 1. RustFS user + policy
echo "== RustFS: user lt-sync (write-only to lt-logs/raw/)"
# Runs inside the mlflow container: it has boto3 and already holds the RustFS admin keys.
set +e
docker compose exec -T -e LT_SYNC_S3_SECRET="$S3_SECRET" mlflow python - <<'PY'
import hashlib, json, os, sys, urllib.parse
import boto3, requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

EP = os.environ.get("MLFLOW_S3_ENDPOINT_URL", "http://rustfs:9000")
ADMIN = Credentials(os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"])
USER, SECRET, POLICY = "lt-sync", os.environ["LT_SYNC_S3_SECRET"], "lt-sync-put"
POLICY_DOC = {"Version": "2012-10-17", "Statement": [{
    "Sid": "WriteRawLogsOnly", "Effect": "Allow", "Action": ["s3:PutObject"],
    "Resource": ["arn:aws:s3:::lt-logs/raw/*"]}]}

def admin(method, path, params, body=None):
    """RustFS admin API (signed like an S3 request with the root keys)."""
    data = json.dumps(body).encode() if body is not None else b""
    url = f"{EP}/rustfs/admin/v3/{path}?{urllib.parse.urlencode(params)}"
    req = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"})
    S3SigV4Auth(ADMIN, "s3", "us-east-1").add_auth(req)
    r = requests.request(method, url, data=data, headers=dict(req.headers), timeout=15)
    ok = r.status_code < 300
    print(f"  {'✓' if ok else '✗'} {path} -> {r.status_code} {'' if ok else r.text[:200]}")
    return ok

ok = admin("PUT", "add-canned-policy", {"name": POLICY}, POLICY_DOC)
ok = admin("PUT", "add-user", {"accessKey": USER}, {"secretKey": SECRET, "status": "enabled"}) and ok
ok = admin("PUT", "set-user-or-group-policy",
           {"policyName": POLICY, "userOrGroup": USER, "isGroup": "false"}) and ok

# Verify with the new key itself: the only test that really counts.
s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=USER, aws_secret_access_key=SECRET,
                  region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))
verified = True
try:
    s3.put_object(Bucket="lt-logs", Key="raw/_check/provision.txt", Body=b"ok")
    print("  ✓ lt-sync can write lt-logs/raw/")
except ClientError as e:
    print(f"  ✗ lt-sync cannot write lt-logs/raw/: {e.response['Error'].get('Code')}")
    verified = False
for what, call in (("read the mlflow bucket", lambda: s3.list_objects_v2(Bucket="mlflow", MaxKeys=1)),
                   ("list lt-logs", lambda: s3.list_objects_v2(Bucket="lt-logs", MaxKeys=1)),
                   ("read lt-logs objects", lambda: s3.get_object(Bucket="lt-logs", Key="raw/_check/provision.txt")),
                   ("delete objects", lambda: s3.delete_object(Bucket="lt-logs", Key="raw/_check/provision.txt"))):
    try:
        call()
        print(f"  ✗ lt-sync CAN {what} – too many rights")
        verified = False
    except ClientError as e:
        print(f"  ✓ lt-sync cannot {what} ({e.response['Error'].get('Code')})")
sys.exit(0 if verified else 2)
PY
rustfs_status=$?
set -e
if [ "$rustfs_status" -ne 0 ]; then
  cat <<EOF

⚠ The RustFS account could not be set up/verified automatically. Do it in the console instead:
  1. Open http://swt17:9001 and log in with S3_ACCESS_KEY / S3_SECRET_KEY from ~/platform/.env
  2. Identity → Policies → Create policy, name: lt-sync-put, content:
     {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:PutObject"],"Resource":["arn:aws:s3:::lt-logs/raw/*"]}]}
  3. Identity → Users → Create user, access key: lt-sync, secret key: the LT_SYNC_S3_SECRET line in .env
     (show it with:  grep LT_SYNC_S3_SECRET ~/platform/.env), policy: lt-sync-put
  4. Re-run ./provision-sync.sh — the check above must then show only ✓.
EOF
fi

# ---------------------------------------------------------------- 2. Airflow role + user
echo "== Airflow: user lt-sync (may only create asset events)"
docker compose exec -T -e PW="$AF_PASSWORD" airflow-apiserver bash -ec '
  airflow roles create LtSync >/dev/null 2>&1 || true
  airflow roles add-perms LtSync -r Assets -a can_read can_create >/dev/null
  airflow roles add-perms LtSync -r "Asset Aliases" -a can_read >/dev/null
  if airflow users list -o plain 2>/dev/null | grep -qw lt-sync; then
    airflow users reset-password -u lt-sync -p "$PW" >/dev/null && echo "  ✓ user lt-sync updated"
  else
    airflow users create -u lt-sync -r LtSync -p "$PW" -e lt-sync@localhost -f lt -l sync >/dev/null \
      && echo "  ✓ user lt-sync created"
  fi
  airflow roles list -p 2>/dev/null | grep "^LtSync" | sed "s/^/  /"
'
echo "  restarting the API server so it picks up the new permissions …"
docker compose restart airflow-apiserver >/dev/null

# ---------------------------------------------------------------- 3. credentials file for the desktop
umask 077
cat > sync-credentials.env <<EOF
# sync.bat credentials — generated by provision-sync.sh on $(hostname), $(date -Iseconds)
# Keep private. Desktop location: E:\\translater\\secrets\\sync.env
LT_SYNC_S3_ENDPOINT=http://swt17:9000
LT_SYNC_S3_ACCESS_KEY=lt-sync
LT_SYNC_S3_SECRET_KEY=$S3_SECRET
LT_SYNC_BUCKET=lt-logs
LT_SYNC_PREFIX=raw/
LT_SYNC_ASSET_URI=s3://lt-logs/raw
LT_SYNC_AIRFLOW_URL=http://swt17:8080
LT_SYNC_AIRFLOW_USER=lt-sync
LT_SYNC_AIRFLOW_PASSWORD=$AF_PASSWORD
EOF
echo
echo "✓ wrote ~/platform/sync-credentials.env"
echo "  On the desktop (PowerShell):"
echo "    mkdir E:\\translater\\secrets -Force"
echo "    scp swt17:~/platform/sync-credentials.env E:\\translater\\secrets\\sync.env"
echo "  Then delete the copy here:  rm ~/platform/sync-credentials.env"
[ "$rustfs_status" -eq 0 ] || exit "$rustfs_status"
