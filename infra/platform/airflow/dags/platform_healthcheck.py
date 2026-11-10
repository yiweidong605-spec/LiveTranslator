"""
### Platform healthcheck (hourly)

Checks every part of the orchestration node and records the result in MLflow
(experiment **platform-health**), so the history of the platform's health can be charted.

| Task | What it checks |
|---|---|
| `check_object_storage` | RustFS: write, read back and delete a small object in `lt-logs` |
| `check_mlflow` | MLflow server answers `/health` |
| `check_internet` | Outbound internet works: several independent probes, healthy if any one succeeds; detects the captive portal |
| `check_disk` | Free space on the mini PC's disk |
| `check_backup` | The daily restic backup (systemd timer `lt-backup`) succeeded and is less than 36 h old |
| `report` | Logs all results as one MLflow run (runs even if some checks failed) |

`check_internet` does not use neverssl.com any more: it is a single server in AWS Oregon and only
~30% of connections to it succeeded from the apartment network (2026-11-10 diagnosis), which made the
check fail while the internet was actually fine. The probes below are the endpoints that Android,
Windows and Apple use to detect captive portals, plus one HTTPS probe.

If `check_internet` fails, the error message says why (portal / DNS / timeout / no route) and what to do.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import statistics
import time
import uuid

import pendulum
import requests
from airflow.sdk import dag, task

S3_ENDPOINT = os.environ.get("LT_S3_ENDPOINT", "http://rustfs:9000")
MLFLOW_URL = os.environ.get("LT_MLFLOW_URL", "http://mlflow:5000").rstrip("/")
EXPERIMENT = "platform-health"
BACKUP_STATUS = "/opt/airflow/backup-status/backup-status.json"  # written by ~/platform/backup/backup.sh
BACKUP_MAX_AGE_H = 36


def _s3():
    import boto3

    return boto3.client("s3", endpoint_url=S3_ENDPOINT)


def _mlflow(method: str, path: str, **kw):
    r = requests.request(method, f"{MLFLOW_URL}/api/2.0/mlflow/{path}", timeout=15, **kw)
    return r


# ---------- internet probes ----------
# (name, url, check(response) -> bool). Plain-HTTP probes reveal a captive portal (it answers instead of
# the real site); the HTTPS probe cannot be faked by a portal (it would fail with a TLS error instead).
PROBES = [
    ("google", "http://connectivitycheck.gstatic.com/generate_204", lambda r: r.status_code == 204),
    ("microsoft", "http://www.msftconnecttest.com/connecttest.txt",
     lambda r: r.status_code == 200 and r.text.strip() == "Microsoft Connect Test"),
    ("apple", "http://captive.apple.com/hotspot-detect.html",
     lambda r: r.status_code == 200 and "Success" in r.text),
    ("google_https", "https://www.google.com/generate_204", lambda r: r.status_code == 204),
]

FIX = {
    "portal": ("The apartment captive portal is intercepting traffic (session expired). Log in again from the "
               "desktop: `ssh -D 1080 swt17`, start Edge with --proxy-server=socks5://127.0.0.1:1080, open "
               "http://captive.apple.com, press Ctrl+F5 and log in (it shows 'Success' once you are online)."),
    "dns": ("DNS does not resolve. On the mini PC run `resolvectl status` and `resolvectl query google.com`; "
            "`sudo resolvectl flush-caches` if answers look wrong."),
    "timeout": ("Connections are silently dropped. Usually the portal expired without redirecting, or the "
                "apartment network is down. Check `ping -c 3 1.1.1.1` on the mini PC, then try the portal login."),
    "unreachable": ("No route to the internet. Check the cable and that `ip route` shows a default route "
                    "(`sudo netplan apply` re-runs DHCP)."),
    "error": "Unexpected error, see the per-probe details above.",
}


def _ipv4_only():
    """The apartment has no IPv6. Without this, a failed IPv4 attempt is reported as the (misleading)
    IPv6 'Network is unreachable' error, which is exactly what confused the first diagnosis."""
    import urllib3.util.connection as uc

    uc.allowed_gai_family = lambda: socket.AF_INET


def probe(session, name: str, url: str, check, timeout: float = 5.0) -> dict:
    """Return {name, outcome, ms, detail}; outcome is ok / portal / dns / timeout / unreachable / error."""
    t0 = time.perf_counter()
    try:
        r = session.get(url, timeout=timeout, allow_redirects=False)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if check(r):
            return {"name": name, "outcome": "ok", "ms": ms, "detail": f"HTTP {r.status_code}"}
        where = r.headers.get("location", "")
        return {"name": name, "outcome": "portal", "ms": ms,
                "detail": f"unexpected HTTP {r.status_code} {where}".strip()}
    except requests.exceptions.SSLError as e:  # someone answered HTTPS with the wrong certificate
        outcome, err = "portal", e
    except requests.exceptions.Timeout as e:
        outcome, err = "timeout", e
    except requests.exceptions.ConnectionError as e:
        msg, err = str(e), e
        if any(k in msg for k in ("NameResolution", "Failed to resolve", "Name or service not known",
                                  "Temporary failure in name resolution")):
            outcome = "dns"
        elif any(k in msg for k in ("Network is unreachable", "No route to host", "Connection refused")):
            outcome = "unreachable"
        else:
            outcome = "error"
    except Exception as e:  # noqa: BLE001
        outcome, err = "error", e
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {"name": name, "outcome": outcome, "ms": ms, "detail": f"{type(err).__name__}: {str(err)[:160]}"}


def run_probes(probes=PROBES, timeout: float = 5.0, retries: int = 1) -> list[dict]:
    session = requests.Session()
    session.trust_env = False  # measure the direct connection, never an HTTP proxy from the environment
    results = []
    for name, url, check in probes:
        res = probe(session, name, url, check, timeout)
        for _ in range(retries):  # retry transient failures, but a portal answer is a real answer
            if res["outcome"] in ("ok", "portal"):
                break
            res = probe(session, name, url, check, timeout)
        results.append(res)
    return results


def summarize(results: list[dict]) -> tuple[dict, str | None]:
    """Metrics for MLflow, plus an error message if the internet is down (None when healthy)."""
    ok = [r for r in results if r["outcome"] == "ok"]
    metrics = {
        "internet_ok": int(bool(ok)),
        "internet_probes_ok": len(ok),
        "internet_probes_total": len(results),
        "portal_detected": int(any(r["outcome"] == "portal" for r in results)),
    }
    for r in results:
        metrics[f"probe_{r['name']}_ok"] = int(r["outcome"] == "ok")
    if ok:
        metrics["internet_latency_ms"] = round(statistics.median(r["ms"] for r in ok), 1)
        return metrics, None
    # all failed: name the most telling cause (a portal answer beats a timeout, etc.)
    outcomes = [r["outcome"] for r in results]
    cause = next(c for c in ("portal", "dns", "unreachable", "timeout", "error") if c in outcomes)
    return metrics, f"No internet: all {len(results)} probes failed, cause = {cause}. {FIX[cause]}"


def _experiment_id() -> str:
    r = _mlflow("GET", "experiments/get-by-name", params={"experiment_name": EXPERIMENT})
    if r.status_code == 200:
        return r.json()["experiment"]["experiment_id"]
    r = _mlflow("POST", "experiments/create", json={
        "name": EXPERIMENT,
        "tags": [{"key": "mlflow.experimentKind", "value": "custom_model_development"}],
    })
    r.raise_for_status()
    return r.json()["experiment_id"]


@dag(
    dag_id="platform_healthcheck",
    schedule="@hourly",
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Paris"),
    catchup=False,
    max_active_runs=1,
    tags=["platform", "monitoring"],
    doc_md=__doc__,
    default_args={"retries": 1, "retry_delay": pendulum.duration(minutes=2)},
)
def platform_healthcheck():
    @task
    def check_object_storage() -> dict:
        s3 = _s3()
        key = f"healthcheck/{uuid.uuid4().hex}.txt"
        body = f"ok {time.time()}".encode()
        t0 = time.perf_counter()
        s3.put_object(Bucket="lt-logs", Key=key, Body=body)
        back = s3.get_object(Bucket="lt-logs", Key=key)["Body"].read()
        s3.delete_object(Bucket="lt-logs", Key=key)
        ms = (time.perf_counter() - t0) * 1000
        if back != body:
            raise RuntimeError("object storage returned different content")
        buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        print(f"RustFS OK in {ms:.0f} ms, buckets: {buckets}")
        return {"s3_ok": 1, "s3_roundtrip_ms": round(ms, 1), "s3_buckets": len(buckets)}

    @task
    def check_mlflow() -> dict:
        t0 = time.perf_counter()
        r = requests.get(f"{MLFLOW_URL}/health", timeout=10)
        r.raise_for_status()
        ms = (time.perf_counter() - t0) * 1000
        print(f"MLflow OK in {ms:.0f} ms")
        return {"mlflow_ok": 1, "mlflow_latency_ms": round(ms, 1)}

    @task(retries=0)
    def check_internet() -> dict:
        _ipv4_only()
        results = run_probes()
        for r in results:
            mark = "OK  " if r["outcome"] == "ok" else "FAIL"
            print(f"{mark} {r['name']:<13} {r['outcome']:<11} {r['ms']:>7.0f} ms  {r['detail']}")
        metrics, error = summarize(results)
        if error:
            raise RuntimeError(error)
        if metrics["portal_detected"]:
            print("WARNING: a probe was answered by something else (captive portal?) but others worked.")
        print(f"Internet OK: {metrics['internet_probes_ok']}/{metrics['internet_probes_total']} probes, "
              f"median {metrics['internet_latency_ms']:.0f} ms")
        return metrics

    @task
    def check_disk() -> dict:
        # /opt/airflow/logs is a bind mount from the host, so this is the mini PC's root disk
        total, used, free = shutil.disk_usage("/opt/airflow/logs")
        gb = 1024 ** 3
        pct = used / total * 100
        print(f"Disk: {free / gb:.1f} GB free of {total / gb:.1f} GB ({pct:.1f}% used)")
        if pct > 90:
            raise RuntimeError(f"Disk almost full: {pct:.1f}% used")
        return {"disk_free_gb": round(free / gb, 1), "disk_used_pct": round(pct, 2)}

    # retries=0: re-running the report cannot fix a failed check, it only delayed the red run by 2 minutes
    @task(retries=0)
    def check_backup() -> dict:
        # The backup itself runs outside Airflow (a backup must not depend on what it backs up);
        # Airflow only watches the status file it leaves behind.
        try:
            with open(BACKUP_STATUS) as f:
                st = json.load(f)
        except FileNotFoundError:
            raise RuntimeError("No backup has run yet. On the mini PC: bash ~/platform/backup/install-backup.sh")
        last_ok = st.get("last_success_at")
        age_h = (time.time() - last_ok) / 3600 if last_ok else None
        if not st.get("ok"):
            raise RuntimeError(
                f"The last backup FAILED at step '{st.get('failed_step')}': {st.get('error')}. "
                f"Newest good backup: {f'{age_h:.0f} h ago' if age_h is not None else 'none'}. "
                "Details: journalctl -u lt-backup -n 50")
        if age_h is None or age_h > BACKUP_MAX_AGE_H:
            raise RuntimeError(f"Newest backup is {age_h:.0f} h old (limit {BACKUP_MAX_AGE_H} h). "
                               "Is the timer active? systemctl list-timers lt-backup.timer")
        mb = 1024 ** 2
        print(f"Backup OK: {age_h:.1f} h ago, took {st.get('duration_s')} s, snapshots {st.get('snapshots')}")
        return {"backup_ok": 1, "backup_age_h": round(age_h, 2), "backup_duration_s": st.get("duration_s", 0),
                "backup_repo_mb": round(st.get("repo_bytes", 0) / mb, 1),
                "backup_added_mb": round(st.get("bytes_added", 0) / mb, 2),
                "backup_snapshots": st.get("n_snapshots", 0)}

    @task(trigger_rule="all_done", retries=0)
    def report(results: list) -> dict:
        merged = {}
        for r in results:
            if isinstance(r, dict):
                merged.update(r)
        for k in ("s3_ok", "mlflow_ok", "internet_ok", "backup_ok"):
            merged.setdefault(k, 0)
        merged["all_ok"] = int(all(merged[k] == 1 for k in ("s3_ok", "mlflow_ok", "internet_ok", "backup_ok")))
        now = int(time.time() * 1000)
        try:
            exp = _experiment_id()
            run = _mlflow("POST", "runs/create", json={
                "experiment_id": exp, "start_time": now,
                "run_name": pendulum.now("Europe/Paris").format("YYYY-MM-DD HH:mm"),
                "tags": [{"key": "source", "value": "airflow/platform_healthcheck"}],
            })
            run.raise_for_status()
            run_id = run.json()["run"]["info"]["run_id"]
            metrics = [{"key": k, "value": float(v), "timestamp": now, "step": 0}
                       for k, v in merged.items() if isinstance(v, (int, float))]
            _mlflow("POST", "runs/log-batch", json={"run_id": run_id, "metrics": metrics}).raise_for_status()
            _mlflow("POST", "runs/update", json={"run_id": run_id, "status": "FINISHED",
                                                  "end_time": int(time.time() * 1000)}).raise_for_status()
            print(f"logged to MLflow run {run_id}")
        except Exception as e:  # MLflow itself may be the broken part
            print(f"could not log to MLflow: {e}")
        print(merged)
        if not merged["all_ok"]:
            raise RuntimeError(f"platform unhealthy: {merged}")
        return merged

    report([check_object_storage(), check_mlflow(), check_internet(), check_disk(), check_backup()])


platform_healthcheck()
