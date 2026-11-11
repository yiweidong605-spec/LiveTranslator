"""sync.bat's incremental upload, against an in-process S3 server (moto)."""
import json

import boto3
import pytest
import requests

moto_server = pytest.importorskip("moto.server")

from scripts import sync_logs  # noqa: E402


@pytest.fixture()
def s3(monkeypatch):
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    for k in ("AWS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    endpoint = f"http://{host}:{port}"
    requests.post(f"{endpoint}/moto-api/reset", timeout=5)  # moto keeps state across servers in one process
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                          aws_access_key_id="testing", aws_secret_access_key="testing")
    client.create_bucket(Bucket="lt-logs")
    yield endpoint, client
    server.stop()


@pytest.fixture()
def logs(tmp_path, monkeypatch):
    d = tmp_path / "data" / "logs"
    (d / "audio" / "2026-11-09").mkdir(parents=True)
    (d / "2026-11-09.jsonl").write_text('{"rid": "1"}\n')
    (d / "edits.jsonl").write_text('{"rid": "1", "field": "star", "value": true}\n')
    (d / "audio" / "2026-11-09" / "1.wav").write_bytes(b"RIFF" + bytes(100))
    monkeypatch.setattr(sync_logs, "LOG_DIR", d)
    monkeypatch.setattr(sync_logs, "STATE", d / ".sync_state.json")
    return d


def env(endpoint):
    return {"LT_SYNC_S3_ENDPOINT": endpoint, "LT_SYNC_S3_ACCESS_KEY": "testing", "LT_SYNC_S3_SECRET_KEY": "testing",
            "LT_SYNC_BUCKET": "lt-logs", "LT_SYNC_PREFIX": "raw/", "LT_SYNC_ASSET_URI": "s3://lt-logs/raw"}


def keys(client):
    return sorted(o["Key"] for o in client.list_objects_v2(Bucket="lt-logs").get("Contents", []))


def test_incremental_upload(s3, logs, capsys):
    endpoint, client = s3
    assert sync_logs.sync(env(endpoint), dry_run=False) == 0
    assert keys(client) == ["raw/2026-11-09.jsonl", "raw/audio/2026-11-09/1.wav", "raw/edits.jsonl"]
    state = json.loads((logs / ".sync_state.json").read_text())
    assert set(state) == {"2026-11-09.jsonl", "edits.jsonl", "audio/2026-11-09/1.wav"}

    capsys.readouterr()
    assert sync_logs.sync(env(endpoint), dry_run=False) == 0  # nothing changed → nothing sent
    assert "Everything is already synced" in capsys.readouterr().out

    with open(logs / "edits.jsonl", "a") as f:
        f.write('{"rid": "1", "field": "note", "value": "x"}\n')
    assert sync_logs.sync(env(endpoint), dry_run=False) == 0
    out = capsys.readouterr().out
    assert "Uploaded 1 file(s)" in out
    body = client.get_object(Bucket="lt-logs", Key="raw/edits.jsonl")["Body"].read().decode()
    assert body.count("\n") == 2


def test_dry_run_uploads_nothing(s3, logs):
    endpoint, client = s3
    assert sync_logs.sync(env(endpoint), dry_run=True) == 0
    assert keys(client) == []


def test_notify_without_airflow_settings_is_only_a_message(s3):
    endpoint, _ = s3
    assert "daily run" in sync_logs.notify_airflow(env(endpoint), {})
