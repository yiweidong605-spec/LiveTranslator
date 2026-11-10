"""Connect evaluation scripts to MLflow.

Order of preference:
  1. MLFLOW_TRACKING_URI environment variable
  2. config.yaml -> eval.mlflow_tracking_uri  (the platform server, e.g. http://swt17:5000)
  3. local fallback: sqlite:///mlflow.db in this folder

A remote server is health-checked first; if it is unreachable (mini PC off, captive portal expired,
Tailscale down) we fall back to the local database so no evaluation result is ever lost.
"""
from __future__ import annotations

import os
import socket
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LOCAL_URI = f"sqlite:///{(ROOT / 'mlflow.db').as_posix()}"


def configured_uri() -> str | None:
    if os.environ.get("MLFLOW_TRACKING_URI"):
        return os.environ["MLFLOW_TRACKING_URI"]
    try:
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        return (cfg.get("eval") or {}).get("mlflow_tracking_uri") or None
    except Exception:
        return None


def remote_ok(uri: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(uri.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def resolve_uri(log=print) -> tuple[str, bool]:
    """Return (tracking_uri, is_remote)."""
    uri = configured_uri()
    if uri and uri.startswith("http"):
        if remote_ok(uri):
            return uri, True
        log(f"⚠ 平台 MLflow（{uri}）连不上，这次先记到本地 mlflow.db。"
            "检查：小主机是否开机、Tailscale 是否在线、门户认证是否过期。")
        return LOCAL_URI, False
    return (uri or LOCAL_URI), False


def setup(experiment: str, log=print):
    """Point mlflow at the right server and experiment. Returns (mlflow, uri, is_remote) or (None, None, False)."""
    try:
        import mlflow
    except ImportError:
        log("（没装 MLflow，跳过记录。eval.bat 菜单 1 可以安装）")
        return None, None, False
    uri, is_remote = resolve_uri(log)
    mlflow.set_tracking_uri(uri)
    if mlflow.get_experiment_by_name(experiment) is None:
        kwargs = {"tags": {"mlflow.experimentKind": "custom_model_development"}}
        if not is_remote:  # the server decides where artifacts go (RustFS); locally we pick a folder
            kwargs["artifact_location"] = (ROOT / "mlruns").as_uri()
        mlflow.create_experiment(experiment, **kwargs)
    mlflow.set_experiment(experiment)
    return mlflow, uri, is_remote


def run_tags() -> dict:
    return {"source_host": socket.gethostname()}
