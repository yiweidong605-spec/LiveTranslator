"""Tiny MLflow REST client.

The Airflow image deliberately does not install the `mlflow` package (it pins hundreds of
dependencies that would fight with Airflow's own). The REST API is stable and is all we need.
"""
from __future__ import annotations

import os
import time

import requests

MLFLOW_URL = os.environ.get("LT_MLFLOW_URL", "http://mlflow:5000").rstrip("/")


def call(method: str, path: str, **kw) -> dict:
    r = requests.request(method, f"{MLFLOW_URL}/api/2.0/mlflow/{path}", timeout=20, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"MLflow {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def experiment_id(name: str) -> str:
    r = requests.get(f"{MLFLOW_URL}/api/2.0/mlflow/experiments/get-by-name",
                     params={"experiment_name": name}, timeout=20)
    if r.status_code == 200:
        return r.json()["experiment"]["experiment_id"]
    return call("POST", "experiments/create", json={
        "name": name, "tags": [{"key": "mlflow.experimentKind", "value": "custom_model_development"}],
    })["experiment_id"]


def find_or_create_run(exp_id: str, key_tag: str, key_value: str, run_name: str,
                       start_ms: int | None = None, tags: dict | None = None) -> str:
    """One run per key (e.g. per logbook date). Re-processing reuses the run, so it stays idempotent."""
    found = call("POST", "runs/search", json={
        "experiment_ids": [exp_id], "max_results": 1,
        "filter": f"tags.{key_tag} = '{key_value}'",
    }).get("runs", [])
    if found:
        run_id = found[0]["info"]["run_id"]
    else:
        all_tags = {key_tag: key_value, **(tags or {})}
        run_id = call("POST", "runs/create", json={
            "experiment_id": exp_id, "run_name": run_name,
            "start_time": start_ms or int(time.time() * 1000),
            "tags": [{"key": k, "value": str(v)} for k, v in all_tags.items()],
        })["run"]["info"]["run_id"]
    if tags:
        call("POST", "runs/log-batch", json={
            "run_id": run_id, "tags": [{"key": k, "value": str(v)[:5000]} for k, v in tags.items()]})
    return run_id


def log_metrics(run_id: str, metrics: dict, step: int = 0, ts_ms: int | None = None):
    ts = ts_ms or int(time.time() * 1000)
    rows = [{"key": k, "value": float(v), "timestamp": ts, "step": step}
            for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for i in range(0, len(rows), 900):  # API limit is 1000 metrics per batch
        call("POST", "runs/log-batch", json={"run_id": run_id, "metrics": rows[i:i + 900]})


def log_artifact(exp_id: str, run_id: str, name: str, data: bytes, content_type: str = "application/json"):
    """Upload through the tracking server (--serve-artifacts), which stores it in RustFS."""
    r = requests.put(f"{MLFLOW_URL}/api/2.0/mlflow-artifacts/artifacts/{exp_id}/{run_id}/artifacts/{name}",
                     data=data, headers={"Content-Type": content_type}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"artifact upload {name} -> {r.status_code}: {r.text[:200]}")


def finish(run_id: str):
    call("POST", "runs/update", json={"run_id": run_id, "status": "FINISHED",
                                      "end_time": int(time.time() * 1000)})
