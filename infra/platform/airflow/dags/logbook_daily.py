"""
### Logbook daily (translator)

Turns the raw conversation logbook uploaded by the desktop (`sync.bat`) into a clean dataset,
daily quality metrics and training material.

**When it runs**: as soon as `sync.bat` finishes (asset event on `s3://lt-logs/raw`), plus a
safety-net run every day at 08:00 Paris time.

**What it processes**: only the dates whose raw file or edits changed since the last run
(fingerprints in `s3://lt-datasets/logbook/_state.json`). Trigger with `reprocess_all = true`
to rebuild everything (e.g. after changing the transformation; bumping `PIPELINE_VERSION` in
`lt_lib/logbook.py` does this automatically).

| Layer | Where | What |
|---|---|---|
| bronze | `lt-logs/raw/` | exact copy of `data/logs` from the desktop (never modified) |
| silver | `lt-datasets/logbook/records/date=…/part-0.parquet` | validated, de-duplicated, edits merged, typed |
| quarantine | `lt-datasets/logbook/quarantine/date=…/bad_lines.jsonl` | lines that failed validation, with the reason |
| gold | MLflow experiment **logbook-daily** (one run per date) | utterances, confidence, corrections, latency … |
| gold | `lt-datasets/logbook/review/date=…/review.json` | the 10 sentences most worth reviewing |
| gold | `lt-datasets/logbook/training_pairs/all.jsonl` | your corrections + starred translations, for fine-tuning |

Every step is idempotent: re-running a date overwrites its outputs and reuses its MLflow run.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import pendulum
from airflow.sdk import Asset, Param, dag, task
from airflow.timetables.assets import AssetOrTimeSchedule
from airflow.timetables.trigger import CronTriggerTimetable

from lt_lib import logbook as lb
from lt_lib import mlflow_rest as mlf

RAW_BUCKET, RAW_PREFIX = "lt-logs", "raw/"
OUT_BUCKET, OUT_PREFIX = "lt-datasets", "logbook/"
STATE_KEY = OUT_PREFIX + "_state.json"
EXPERIMENT = "logbook-daily"
LOGS_RAW = Asset("s3://lt-logs/raw")


def _s3():
    import boto3
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=os.environ.get("LT_S3_ENDPOINT", "http://rustfs:9000"),
                        region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))


def _get_text(s3, bucket: str, key: str) -> str | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", errors="replace")
    except s3.exceptions.NoSuchKey:
        return None


def _put(s3, key: str, body: bytes, content_type: str):
    s3.put_object(Bucket=OUT_BUCKET, Key=key, Body=body, ContentType=content_type)


def _jsonl(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode()


@dag(
    dag_id="logbook_daily",
    schedule=AssetOrTimeSchedule(
        timetable=CronTriggerTimetable("0 8 * * *", timezone="Europe/Paris"),
        assets=[LOGS_RAW],
    ),
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Paris"),
    catchup=False,
    max_active_runs=1,  # runs share _state.json; never let two of them write it at once
    tags=["translator", "data"],
    doc_md=__doc__,
    params={"reprocess_all": Param(False, type="boolean",
                                   description="Rebuild every date, not only the changed ones")},
    default_args={"retries": 2, "retry_delay": pendulum.duration(minutes=1)},
)
def logbook_daily():
    @task
    def plan(params: dict | None = None) -> list[dict]:
        """Find the dates whose raw file or edits changed since the last successful processing."""
        s3 = _s3()
        raw = {}
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=RAW_BUCKET, Prefix=RAW_PREFIX,
                                                                  Delimiter="/"):
            for o in page.get("Contents", []):
                name = o["Key"][len(RAW_PREFIX):]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", name):
                    raw[name[:-6]] = o["ETag"].strip('"')
        edits = lb.split_edits(_get_text(s3, RAW_BUCKET, RAW_PREFIX + "edits.jsonl") or "")
        state = json.loads(_get_text(s3, OUT_BUCKET, STATE_KEY) or "{}").get("dates", {})
        full = bool((params or {}).get("reprocess_all"))
        todo = []
        for date in sorted(raw):
            fp = lb.fingerprint(raw[date], edits.get(date, []))
            if full or state.get(date) != fp:
                todo.append({"date": date, "fp": fp})
        print(f"{len(raw)} date(s) on the platform, {len(todo)} to process"
              f"{' (reprocess_all)' if full else ''}: {[t['date'] for t in todo]}")
        return todo

    @task(map_index_template="{{ item['date'] }}")
    def process(item: dict) -> dict:
        date, s3 = item["date"], _s3()
        raw = _get_text(s3, RAW_BUCKET, f"{RAW_PREFIX}{date}.jsonl") or ""
        edit_lines = lb.split_edits(_get_text(s3, RAW_BUCKET, RAW_PREFIX + "edits.jsonl") or "").get(date, [])
        out = lb.process_day(raw, edit_lines, date)
        part = f"date={date}/"

        _put(s3, f"{OUT_PREFIX}records/{part}part-0.parquet", out["parquet"], "application/vnd.apache.parquet")
        _put(s3, f"{OUT_PREFIX}review/{part}review.json",
             json.dumps(out["review"], ensure_ascii=False, indent=1).encode(), "application/json")
        _put(s3, f"{OUT_PREFIX}training_pairs/{part}pairs.jsonl", _jsonl(out["pairs"]), "application/x-ndjson")
        qkey = f"{OUT_PREFIX}quarantine/{part}bad_lines.jsonl"
        if out["quarantine"]:
            _put(s3, qkey, _jsonl(out["quarantine"]), "application/x-ndjson")
            print(f"⚠ {len(out['quarantine'])} line(s) quarantined, first: {out['quarantine'][0]['reason']}")
        else:
            s3.delete_object(Bucket=OUT_BUCKET, Key=qkey)  # a fixed re-upload clears old quarantine

        m = out["metrics"]
        exp = mlf.experiment_id(EXPERIMENT)
        first_ts = next((r["ts"] for r in out["rows"] if r["ts"]), None)
        run_id = mlf.find_or_create_run(
            exp, "logbook_date", date, run_name=date,
            start_ms=int(first_ts * 1000) if first_ts else None,
            tags={**out["models"], "pipeline_version": lb.PIPELINE_VERSION, "fingerprint": item["fp"],
                  "source": "airflow/logbook_daily"},
        )
        mlf.log_metrics(run_id, m)
        mlf.log_artifact(exp, run_id, "review.json", json.dumps(out["review"], ensure_ascii=False, indent=1).encode())
        if out["quarantine"]:
            mlf.log_artifact(exp, run_id, "quarantine.jsonl", _jsonl(out["quarantine"]), "application/x-ndjson")
        mlf.finish(run_id)
        print(f"{date}: {m.get('n_utterances', 0)} utterances, avg confidence {m.get('avg_confidence')}, "
              f"{m.get('n_corrected', 0)} corrected, {m.get('n_training_pairs', 0)} training pairs")
        return {"date": date, "fp": item["fp"], "n": m.get("n_utterances", 0)}

    @task(trigger_rule="all_done", retries=0)
    def finalize(planned: list[dict], results) -> dict:
        """Remember what succeeded, rebuild the combined training set, log running totals."""
        s3 = _s3()
        done = [r for r in (results or []) if isinstance(r, dict)]
        state = json.loads(_get_text(s3, OUT_BUCKET, STATE_KEY) or "{}")
        state.setdefault("dates", {}).update({r["date"]: r["fp"] for r in done})
        state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state["pipeline_version"] = lb.PIPELINE_VERSION
        _put(s3, STATE_KEY, json.dumps(state, indent=1, sort_keys=True).encode(), "application/json")

        pairs = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=OUT_BUCKET,
                                                                  Prefix=OUT_PREFIX + "training_pairs/date="):
            for o in sorted(page.get("Contents", []), key=lambda o: o["Key"]):
                text = _get_text(s3, OUT_BUCKET, o["Key"]) or ""
                pairs += [json.loads(line) for line in text.splitlines() if line.strip()]
        _put(s3, OUT_PREFIX + "training_pairs/all.jsonl", _jsonl(pairs), "application/x-ndjson")

        totals = {"days_total": len(state["dates"]), "training_pairs_total": len(pairs),
                  "corrections_total": sum(p["kind"] == "correction" for p in pairs),
                  "approved_total": sum(p["kind"] == "approved" for p in pairs)}
        if done or not planned:
            exp = mlf.experiment_id(EXPERIMENT)
            run_id = mlf.find_or_create_run(exp, "logbook_kind", "totals", run_name="_totals",
                                            tags={"source": "airflow/logbook_daily"})
            mlf.log_metrics(run_id, totals)  # timestamped, so MLflow charts how the training set grows
        print(f"processed {len(done)}/{len(planned)} date(s); totals: {totals}")
        failed = sorted({p["date"] for p in planned} - {r["date"] for r in done})
        if failed:
            raise RuntimeError(f"these dates failed and will be retried next run: {failed}")
        return totals

    todo = plan()
    finalize(todo, process.expand(item=todo))


logbook_daily()
