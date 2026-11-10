#!/bin/sh
# Wait for object storage, create buckets if missing, then start the MLflow server.
set -e
python - <<'PY'
import os, time, boto3
s3 = boto3.client("s3", endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"])
wanted = [b.strip() for b in os.environ.get("BUCKETS", "mlflow").split(",") if b.strip()]
for attempt in range(60):
    try:
        have = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
        for b in wanted:
            if b not in have:
                s3.create_bucket(Bucket=b)
                print(f"created bucket {b}", flush=True)
        print("object storage ready:", ", ".join(wanted), flush=True)
        break
    except Exception as e:
        print(f"waiting for object storage ({type(e).__name__})", flush=True)
        time.sleep(2)
else:
    raise SystemExit("object storage not reachable")
PY
exec mlflow server \
  --host 0.0.0.0 --port 5000 \
  --backend-store-uri "$MLFLOW_BACKEND_STORE_URI" \
  --artifacts-destination s3://mlflow/artifacts \
  --serve-artifacts
