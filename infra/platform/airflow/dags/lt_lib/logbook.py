"""Pure transformation logic for the logbook pipeline (no Airflow, no S3: easy to unit-test).

bronze  raw jsonl exactly as the desktop wrote it        s3://lt-logs/raw/
silver  validated, de-duplicated, edits merged, typed     s3://lt-datasets/logbook/records/date=…/
gold    daily metrics (MLflow), review list, training pairs
"""
from __future__ import annotations

import hashlib
import io
import json
import statistics
from collections import Counter, defaultdict

# Bump when the transformation changes: every date is then re-processed automatically.
PIPELINE_VERSION = "1"

LOW_CONF = 0.6      # same threshold as the history page
REVIEW_MAX_CONF = 0.8
REVIEW_TOP_N = 10
EDIT_FIELDS = {"correction", "note", "star", "ignore"}

# (column, pyarrow type name). Explicit schema = stable files, even on days where a field is always empty.
COLUMNS = [
    ("rid", "string"), ("ts", "float64"), ("date", "string"), ("time", "string"),
    ("session", "string"), ("source", "string"), ("mode", "string"), ("who", "string"),
    ("src_lang", "string"), ("tgt_lang", "string"), ("src_text", "string"), ("tgt_text", "string"),
    ("final_text", "string"), ("confidence", "float64"),
    ("asr_model", "string"), ("asr_conf", "float64"), ("asr_geo_prob", "float64"),
    ("asr_avg_logprob", "float64"), ("asr_weak_words", "string"), ("asr_dur_s", "float64"),
    ("asr_ms", "int64"),
    ("mt_model", "string"), ("mt_style", "string"), ("mt_conf", "float64"), ("mt_geo_prob", "float64"),
    ("mt_min_prob", "float64"), ("mt_weak_tokens", "string"), ("mt_n_tokens", "int64"),
    ("mt_context_turns", "int64"), ("mt_first_token_s", "float64"), ("mt_total_s", "float64"),
    ("spoken", "bool"), ("audio", "string"), ("error", "string"),
    ("correction", "string"), ("note", "string"), ("star", "bool"), ("ignore", "bool"),
    ("edited_at", "float64"), ("pipeline_version", "string"),
]


# ---------------------------------------------------------------- edits
def rid_date(rid: str) -> str | None:
    """rid looks like 20261110-021530-001 → 2026-11-10."""
    if isinstance(rid, str) and len(rid) >= 8 and rid[:8].isdigit():
        return f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}"
    return None


def split_edits(text: str) -> dict[str, list[str]]:
    """edits.jsonl is one file for all days; split its lines by the date of the edited sentence."""
    by_date: dict[str, list[str]] = defaultdict(list)
    for line in text.splitlines():
        try:
            d = rid_date(json.loads(line).get("rid"))
        except Exception:
            continue
        if d:
            by_date[d].append(line)
    return dict(by_date)


def latest_edits(lines: list[str]) -> dict[str, dict]:
    """rid → {field: value, '_ts': last edit time}; later edits win (ordered by ts, then file order)."""
    rows = []
    for i, line in enumerate(lines):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("field") in EDIT_FIELDS and e.get("rid"):
            rows.append((float(e.get("ts") or 0), i, e))
    out: dict[str, dict] = defaultdict(dict)
    for ts, _, e in sorted(rows, key=lambda r: (r[0], r[1])):
        out[e["rid"]][e["field"]] = e.get("value")
        out[e["rid"]]["_ts"] = ts
    return dict(out)


def fingerprint(raw_etag: str, edit_lines: list[str]) -> str:
    h = hashlib.sha1(f"v{PIPELINE_VERSION}|{raw_etag}|".encode())
    for line in edit_lines:
        h.update(line.encode())
    return h.hexdigest()


# ---------------------------------------------------------------- parse + validate
def parse_day(text: str, date: str) -> tuple[list[dict], list[dict], int]:
    """Return (good records, quarantined lines with reason, number of duplicates dropped)."""
    good: dict[str, dict] = {}
    bad, dupes = [], 0
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            # typical cause: the file was uploaded while the app was still writing the last line
            bad.append({"line": n, "reason": "invalid JSON", "raw": line[:2000]})
            continue
        reason = validate(rec, date)
        if reason:
            bad.append({"line": n, "reason": reason, "raw": line[:2000]})
            continue
        if rec["rid"] in good:
            dupes += 1
        good[rec["rid"]] = rec  # keep the last version of a sentence
    return list(good.values()), bad, dupes


def validate(rec, date: str) -> str | None:
    if not isinstance(rec, dict):
        return "not an object"
    if not isinstance(rec.get("rid"), str) or not rec["rid"]:
        return "missing rid"
    if not isinstance(rec.get("src_text"), str):
        return "missing src_text"
    if rec.get("src_lang") not in ("zh", "fr", "en"):
        return f"unknown src_lang {rec.get('src_lang')!r}"
    if not isinstance(rec.get("ts"), (int, float)):
        return "missing ts"
    if rec.get("date") not in (None, date):
        return f"date {rec.get('date')} does not match file {date}"
    for k in ("confidence",):
        v = rec.get(k)
        if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= 1):
            return f"{k} out of range: {v!r}"
    return None


# ---------------------------------------------------------------- flatten
def _num(v, typ=float):
    try:
        return None if v is None or isinstance(v, bool) else typ(v)
    except (TypeError, ValueError):
        return None


def _json(v):
    return None if not v else json.dumps(v, ensure_ascii=False)


def flatten(rec: dict, edits: dict | None, date: str) -> dict:
    a, m, e = rec.get("asr") or {}, rec.get("mt") or {}, edits or {}
    correction = e.get("correction") or None
    return {
        "rid": rec["rid"], "ts": _num(rec.get("ts")), "date": date, "time": rec.get("time"),
        "session": _str(rec.get("session")), "source": rec.get("source"), "mode": rec.get("mode"),
        "who": rec.get("who"), "src_lang": rec.get("src_lang"), "tgt_lang": rec.get("tgt_lang"),
        "src_text": rec.get("src_text"), "tgt_text": rec.get("tgt_text") or "",
        "final_text": correction or rec.get("tgt_text") or "",
        "confidence": _num(rec.get("confidence")),
        "asr_model": a.get("model"), "asr_conf": _num(a.get("confidence")),
        "asr_geo_prob": _num(a.get("geo_prob")), "asr_avg_logprob": _num(a.get("avg_logprob")),
        "asr_weak_words": _json(a.get("weak_words")), "asr_dur_s": _num(a.get("dur_s")),
        "asr_ms": _num(a.get("ms"), int),
        "mt_model": m.get("model"), "mt_style": m.get("style"), "mt_conf": _num(m.get("confidence")),
        "mt_geo_prob": _num(m.get("geo_prob")), "mt_min_prob": _num(m.get("min_prob")),
        "mt_weak_tokens": _json(m.get("weak_tokens")), "mt_n_tokens": _num(m.get("n_tokens"), int),
        "mt_context_turns": _num(m.get("context_turns"), int),
        "mt_first_token_s": _num(m.get("first_token_s")), "mt_total_s": _num(m.get("total_s")),
        "spoken": bool(rec.get("spoken")), "audio": rec.get("audio"), "error": rec.get("error"),
        "correction": correction, "note": e.get("note") or None,
        "star": bool(e.get("star")), "ignore": bool(e.get("ignore")),
        "edited_at": _num(e.get("_ts")), "pipeline_version": PIPELINE_VERSION,
    }


def _str(v):
    return None if v is None else str(v)


def to_parquet(rows: list[dict]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    types = {"string": pa.string(), "float64": pa.float64(), "int64": pa.int64(), "bool": pa.bool_()}
    schema = pa.schema([(name, types[typ]) for name, typ in COLUMNS])
    table = pa.Table.from_pylist(rows, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


# ---------------------------------------------------------------- gold outputs
def review_list(rows: list[dict]) -> list[dict]:
    """Sentences most worth a look: lowest confidence, not yet corrected / starred / ignored."""
    cand = [r for r in rows if r["confidence"] is not None and r["confidence"] < REVIEW_MAX_CONF
            and not (r["correction"] or r["star"] or r["ignore"] or r["error"]) and r["tgt_text"]]
    cand.sort(key=lambda r: r["confidence"])
    keep = ("rid", "time", "who", "src_lang", "tgt_lang", "src_text", "tgt_text", "confidence",
            "asr_conf", "mt_conf", "asr_weak_words", "mt_weak_tokens")
    return [{k: r[k] for k in keep} for r in cand[:REVIEW_TOP_N]]


def training_pairs(rows: list[dict]) -> list[dict]:
    """Your corrections (source → corrected translation) and starred translations (approved as-is)."""
    out = []
    for r in rows:
        if r["ignore"] or not r["src_text"] or not r["tgt_lang"]:
            continue
        if r["correction"]:
            kind, tgt = "correction", r["correction"]
        elif r["star"] and r["tgt_text"]:
            kind, tgt = "approved", r["tgt_text"]
        else:
            continue
        out.append({"rid": r["rid"], "date": r["date"], "src_lang": r["src_lang"], "tgt_lang": r["tgt_lang"],
                    "src": r["src_text"], "tgt": tgt, "mt_output": r["tgt_text"], "mt_model": r["mt_model"],
                    "kind": kind, "note": r["note"]})
    return out


def daily_metrics(rows: list[dict], n_bad: int, n_dupes: int) -> dict:
    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.fmean(xs), 4) if xs else None

    def pct(xs, q):
        xs = sorted(x for x in xs if x is not None)
        return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None

    n = len(rows)
    confs = [r["confidence"] for r in rows]
    known = [c for c in confs if c is not None]
    pairs = training_pairs(rows)
    m = {
        "n_utterances": n,
        "n_me": sum(r["who"] == "me" for r in rows),
        "n_them": sum(r["who"] == "them" for r in rows),
        "n_typed": sum(r["source"] == "text" for r in rows),
        "n_sessions": len({r["session"] for r in rows if r["session"]}),
        "n_errors": sum(bool(r["error"]) for r in rows),
        "n_quarantined": n_bad,
        "n_duplicates": n_dupes,
        "avg_confidence": avg(confs),
        "median_confidence": round(statistics.median(known), 4) if known else None,
        "low_conf_rate": round(sum(c < LOW_CONF for c in known) / len(known), 4) if known else None,
        "avg_asr_conf": avg(r["asr_conf"] for r in rows),
        "avg_mt_conf": avg(r["mt_conf"] for r in rows),
        "n_corrected": sum(bool(r["correction"]) for r in rows),
        "n_starred": sum(r["star"] for r in rows),
        "n_ignored": sum(r["ignore"] for r in rows),
        "n_training_pairs": len(pairs),
        "speech_minutes": round(sum(r["asr_dur_s"] or 0 for r in rows) / 60, 2),
        "avg_asr_ms": avg(r["asr_ms"] for r in rows),
        "avg_mt_first_token_ms": avg((r["mt_first_token_s"] or 0) * 1000 if r["mt_first_token_s"] else None
                                     for r in rows),
        "p90_mt_total_ms": (lambda v: round(v * 1000, 1) if v is not None else None)(
            pct([r["mt_total_s"] for r in rows], 0.9)),
    }
    if n:
        m["correction_rate"] = round(m["n_corrected"] / n, 4)
    for (s, t), c in Counter((r["src_lang"], r["tgt_lang"]) for r in rows if r["tgt_lang"]).items():
        m[f"n_{s}_to_{t}"] = c
    return {k: v for k, v in m.items() if v is not None}


def models_used(rows: list[dict]) -> dict:
    return {
        "asr_models": ",".join(sorted({r["asr_model"] for r in rows if r["asr_model"]})) or "-",
        "mt_models": ",".join(sorted({r["mt_model"] for r in rows if r["mt_model"]})) or "-",
    }


def process_day(raw_text: str, edit_lines: list[str], date: str) -> dict:
    """Everything for one date. Returns the outputs as bytes/objects, ready to be stored."""
    recs, bad, dupes = parse_day(raw_text, date)
    edits = latest_edits(edit_lines)
    rows = sorted((flatten(r, edits.get(r["rid"]), date) for r in recs), key=lambda r: r["ts"] or 0)
    return {
        "rows": rows,
        "parquet": to_parquet(rows),
        "quarantine": bad,
        "review": review_list(rows),
        "pairs": training_pairs(rows),
        "metrics": daily_metrics(rows, len(bad), dupes),
        "models": models_used(rows),
    }
