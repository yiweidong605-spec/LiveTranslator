"""Upload the conversation logbook to the platform (RustFS on the mini PC), then tell Airflow.

Runs only when you start it (sync.bat): no background process, no scheduled task, no pop-ups.

  data/logs/2026-11-10.jsonl        ->  s3://lt-logs/raw/2026-11-10.jsonl
  data/logs/edits.jsonl             ->  s3://lt-logs/raw/edits.jsonl
  data/logs/audio/2026-11-10/x.wav  ->  s3://lt-logs/raw/audio/2026-11-10/x.wav

* Incremental: data/logs/.sync_state.json remembers what was uploaded (size + modified time + MD5),
  so only new or changed files are sent. Safe to interrupt and re-run.
* Integrity: every upload carries its MD5, the server rejects a corrupted transfer.
* Append-only: deleting a file locally never deletes it on the platform.
* Least privilege: the lt-sync key can only *write* under lt-logs/raw/. It cannot read, list or
  delete anything, so a compromised desktop cannot read or wipe the platform.

Credentials: secrets/sync.env (created on the mini PC by provision-sync.sh, never committed).

Usage:
  env\\python.exe scripts\\sync_logs.py            upload new/changed files, notify Airflow
  env\\python.exe scripts\\sync_logs.py --dry-run  only show what would be uploaded
  env\\python.exe scripts\\sync_logs.py --check    test the connection and permissions
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "data" / "logs"
STATE = LOG_DIR / ".sync_state.json"
SECRETS = ROOT / "secrets" / "sync.env"


# ---------------------------------------------------------------- config
def load_env(path: Path = SECRETS) -> dict:
    if not path.exists():
        sys.exit(f"✗ Missing {path.relative_to(ROOT)}.\n"
                 "  On the mini PC run:  cd ~/platform && ./provision-sync.sh\n"
                 "  Then on this PC:     scp swt17:~/platform/sync-credentials.env "
                 f"{path}")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    need = ["LT_SYNC_S3_ENDPOINT", "LT_SYNC_S3_ACCESS_KEY", "LT_SYNC_S3_SECRET_KEY"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        sys.exit(f"✗ {path.name} is missing: {', '.join(missing)}")
    env.setdefault("LT_SYNC_BUCKET", "lt-logs")
    env.setdefault("LT_SYNC_PREFIX", "raw/")
    env.setdefault("LT_SYNC_ASSET_URI", "s3://lt-logs/raw")
    return env


def s3_client(env: dict):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("✗ boto3 is not installed. Run sync.bat (it installs it), or: env\\python.exe -m pip install boto3")
    return boto3.client(
        "s3",
        endpoint_url=env["LT_SYNC_S3_ENDPOINT"],
        aws_access_key_id=env["LT_SYNC_S3_ACCESS_KEY"],
        aws_secret_access_key=env["LT_SYNC_S3_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 3, "mode": "standard"},
                      s3={"addressing_style": "path"}),
    )


# ---------------------------------------------------------------- what to upload
def local_files() -> list[Path]:
    files = sorted(LOG_DIR.glob("????-??-??.jsonl"))
    if (LOG_DIR / "edits.jsonl").exists():
        files.append(LOG_DIR / "edits.jsonl")
    files += sorted((LOG_DIR / "audio").glob("????-??-??/*.wav"))
    return files


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE)  # atomic: an interrupted run never leaves a half-written state file


def changed_files(state: dict) -> list[tuple[Path, str, os.stat_result]]:
    out = []
    for p in local_files():
        rel = p.relative_to(LOG_DIR).as_posix()
        st = p.stat()
        old = state.get(rel)
        if not old or old.get("size") != st.st_size or old.get("mtime_ns") != st.st_mtime_ns:
            out.append((p, rel, st))
    return out


def md5_b64(path: Path) -> tuple[str, str]:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest(), base64.b64encode(h.digest()).decode()


def fmt_mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


# ---------------------------------------------------------------- Airflow
def notify_airflow(env: dict, summary: dict) -> str:
    """Create an asset event so the logbook_daily DAG starts right away. Returns a status line."""
    url = env.get("LT_SYNC_AIRFLOW_URL", "").rstrip("/")
    user, pw = env.get("LT_SYNC_AIRFLOW_USER"), env.get("LT_SYNC_AIRFLOW_PASSWORD")
    if not (url and user and pw):
        return "Airflow not configured in sync.env: the DAG will pick the data up at its daily run (08:00)."
    import requests

    try:
        r = requests.post(f"{url}/auth/token", json={"username": user, "password": pw}, timeout=10)
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        uri = env["LT_SYNC_ASSET_URI"]
        r = requests.get(f"{url}/api/v2/assets", params={"uri_pattern": uri.split("://", 1)[-1]},
                         headers=h, timeout=10)
        r.raise_for_status()
        asset = next((a for a in r.json().get("assets", []) if a["uri"] == uri), None)
        if asset is None:
            return (f"Airflow does not know the asset {uri} yet (is the logbook_daily DAG deployed?). "
                    "The data is uploaded; trigger the DAG by hand.")
        r = requests.post(f"{url}/api/v2/assets/events", headers=h, timeout=10,
                          json={"asset_id": asset["id"], "extra": summary})
        r.raise_for_status()
        return "Airflow notified: logbook_daily starts now → http://swt17:8080"
    except Exception as e:  # uploading worked, so this is only a warning
        return f"⚠ Could not notify Airflow ({type(e).__name__}: {e}). Data is uploaded; the daily run at 08:00 will process it."


# ---------------------------------------------------------------- commands
def check(env: dict) -> int:
    from botocore.exceptions import ClientError, EndpointConnectionError

    s3, bucket, prefix = s3_client(env), env["LT_SYNC_BUCKET"], env["LT_SYNC_PREFIX"]
    ok = True
    print(f"Platform: {env['LT_SYNC_S3_ENDPOINT']}  bucket {bucket}/{prefix}  key {env['LT_SYNC_S3_ACCESS_KEY']}")
    try:
        s3.put_object(Bucket=bucket, Key=f"{prefix}_check/{socket.gethostname()}.txt",
                      Body=f"sync check {time.ctime()}".encode())
        print("  ✓ can write to", f"{bucket}/{prefix}")
    except EndpointConnectionError:
        print("  ✗ cannot reach the mini PC. Is it on? Is Tailscale connected on both machines?")
        return 1
    except ClientError as e:
        print(f"  ✗ write failed: {e.response['Error'].get('Code')} – {e}")
        ok = False
    for what, call in (("read other buckets (mlflow)", lambda: s3.list_objects_v2(Bucket="mlflow", MaxKeys=1)),
                       ("list its own bucket", lambda: s3.list_objects_v2(Bucket=bucket, MaxKeys=1)),
                       ("delete objects", lambda: s3.delete_object(Bucket=bucket, Key=f"{prefix}_check/x"))):
        try:
            call()
            print(f"  ⚠ can {what} – the key has MORE rights than it needs, check the lt-sync policy")
            ok = False
        except ClientError as e:
            code = e.response["Error"].get("Code", "")
            if code in ("AccessDenied", "403", "Forbidden"):
                print(f"  ✓ cannot {what} (least privilege works)")
            else:
                print(f"  ? {what}: unexpected answer {code}")
    print("  " + notify_airflow(env, {"check": True, "host": socket.gethostname()})
          .replace("logbook_daily starts now", "token and asset OK (a test run starts, harmless)"))
    return 0 if ok else 1


def sync(env: dict, dry_run: bool) -> int:
    from botocore.exceptions import ClientError, EndpointConnectionError

    if not LOG_DIR.exists():
        print("Nothing to sync yet (data/logs does not exist).")
        return 0
    state = load_state()
    todo = changed_files(state)
    total = sum(st.st_size for _, _, st in todo)
    print(f"{len(todo)} new or changed file(s), {fmt_mb(total)}"
          f"  (already on the platform: {len(state)})")
    if not todo:
        print("✓ Everything is already synced.")
        return 0
    if dry_run:
        for _, rel, st in todo[:50]:
            print(f"  {rel}  {fmt_mb(st.st_size)}")
        if len(todo) > 50:
            print(f"  … and {len(todo) - 50} more")
        return 0

    s3, bucket, prefix = s3_client(env), env["LT_SYNC_BUCKET"], env["LT_SYNC_PREFIX"]
    sent = sent_bytes = 0
    dates = set()
    t0 = time.time()
    try:
        for i, (p, rel, st) in enumerate(todo, 1):
            hexmd5, b64md5 = md5_b64(p)
            old = state.get(rel)
            if old and old.get("md5") == hexmd5:  # touched but identical: nothing to send
                state[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "md5": hexmd5}
                continue
            with open(p, "rb") as f:
                s3.put_object(Bucket=bucket, Key=prefix + rel, Body=f, ContentMD5=b64md5,
                              Metadata={"source-host": socket.gethostname()})
            state[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "md5": hexmd5,
                          "uploaded": int(time.time())}
            sent += 1
            sent_bytes += st.st_size
            if p.suffix == ".jsonl" and p.stem != "edits":
                dates.add(p.stem)
            if i % 25 == 0 or i == len(todo):
                save_state(state)
                print(f"\r  {i}/{len(todo)}  {fmt_mb(sent_bytes)}", end="", flush=True)
    except EndpointConnectionError:
        save_state(state)
        print("\n✗ Lost the connection to the mini PC. Files sent so far are remembered; run sync again later.")
        return 1
    except ClientError as e:
        save_state(state)
        code = e.response["Error"].get("Code")
        hint = " (wrong key, or the lt-sync policy is missing)" if code in ("AccessDenied", "InvalidAccessKeyId",
                                                                            "SignatureDoesNotMatch") else ""
        print(f"\n✗ Upload refused: {code}{hint}. Run sync.bat → check to diagnose.")
        return 1
    save_state(state)
    print(f"\n✓ Uploaded {sent} file(s), {fmt_mb(sent_bytes)} in {time.time() - t0:.1f}s")
    if sent:
        print(notify_airflow(env, {"files": sent, "bytes": sent_bytes, "dates": sorted(dates),
                                   "host": socket.gethostname()}))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Sync the logbook to the platform")
    ap.add_argument("--dry-run", action="store_true", help="only show what would be uploaded")
    ap.add_argument("--check", action="store_true", help="test connection and permissions")
    args = ap.parse_args()
    env = load_env()
    code = check(env) if args.check else sync(env, args.dry_run)
    if not args.dry_run:  # then the pull backup (and the USB copy, if that drive is plugged in)
        try:
            from scripts import backup_pull
        except ImportError:
            sys.path.insert(0, str(ROOT))
            from scripts import backup_pull
        benv = backup_pull.load_env()
        if benv is None:
            print("\nBackups are not set up on this PC yet: choose 4 in sync.bat (one time).")
        else:
            code = max(code, backup_pull.check(benv) if args.check else backup_pull.pull(benv))
    sys.exit(code)


if __name__ == "__main__":
    main()
