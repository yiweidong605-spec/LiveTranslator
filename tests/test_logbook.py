"""The logbook transformation (bronze → silver → gold) used by the logbook_daily DAG."""
import io
import json

import pyarrow.parquet as pq
import pytest

from lt_lib import logbook as lb

DATE = "2026-11-09"


def card(i, conf=0.9, who="me", **kw):
    src, tgt = ("zh", "fr") if who == "me" else ("fr", "zh")
    rec = {"rid": f"20261109-2000{i:02d}-{i:03d}", "ts": 1794250000 + i, "date": DATE, "time": f"20:00:{i:02d}",
           "session": "s1", "source": "mic", "mode": "talk", "who": who, "src_lang": src, "src_text": f"src {i}",
           "tgt_lang": tgt, "tgt_text": f"tgt {i}", "confidence": conf, "spoken": True,
           "asr": {"model": "large-v3-turbo", "confidence": conf, "dur_s": 2.0, "ms": 250, "weak_words": []},
           "mt": {"model": "qwen2.5:7b", "confidence": conf, "first_token_s": 0.08, "total_s": 0.3}}
    rec.update(kw)
    return rec


def edit(i, field, value, ts):
    return json.dumps({"rid": f"20261109-2000{i:02d}-{i:03d}", "field": field, "value": value, "ts": ts})


def day(*recs, extra_lines=()):
    return "\n".join([json.dumps(r, ensure_ascii=False) for r in recs] + list(extra_lines)) + "\n"


def test_bad_lines_are_quarantined_not_fatal():
    raw = day(card(1), card(2), extra_lines=['{"rid": "x", "src_te', json.dumps({"rid": "y", "src_text": "a"})])
    recs, bad, dupes = lb.parse_day(raw, DATE)
    assert [r["rid"][-3:] for r in recs] == ["001", "002"]
    assert [b["reason"] for b in bad] == ["invalid JSON", "unknown src_lang None"]
    assert dupes == 0


def test_duplicates_keep_the_last_version():
    raw = day(card(1, tgt_text="old"), card(1, tgt_text="new"))
    recs, _, dupes = lb.parse_day(raw, DATE)
    assert dupes == 1 and len(recs) == 1 and recs[0]["tgt_text"] == "new"


def test_date_mismatch_and_bad_confidence_rejected():
    assert lb.validate(card(1, date="2026-11-08"), DATE).startswith("date")
    assert lb.validate(card(1, confidence=1.7), DATE).startswith("confidence out of range")


def test_latest_edit_wins_and_can_be_undone():
    lines = [edit(1, "correction", "A", 10), edit(1, "correction", "B", 20), edit(2, "star", True, 5),
             edit(2, "star", False, 6), edit(3, "bogus", 1, 1)]
    e = lb.latest_edits(lines)
    assert e["20261109-200001-001"]["correction"] == "B"
    assert e["20261109-200002-002"]["star"] is False
    assert "20261109-200003-003" not in e


def test_split_edits_by_sentence_date():
    text = "\n".join([edit(1, "star", True, 1), json.dumps({"rid": "20261108-1-1", "field": "star", "value": True}),
                      "garbage"])
    assert sorted(lb.split_edits(text)) == ["2026-11-08", "2026-11-09"]


def test_training_pairs_review_list_and_metrics():
    raw = day(card(1, conf=0.95), card(2, conf=0.4), card(3, conf=0.5, who="them"), card(4, conf=0.7),
              card(5, conf=0.3))
    edits = [edit(1, "star", True, 1), edit(2, "correction", "bonne", 2),
             edit(5, "ignore", True, 3), edit(5, "correction", "noise", 4)]
    out = lb.process_day(raw, edits, DATE)

    pairs = {p["rid"][-1]: (p["kind"], p["tgt"]) for p in out["pairs"]}
    assert pairs == {"1": ("approved", "tgt 1"), "2": ("correction", "bonne")}  # ignored #5 excluded

    review = [r["rid"][-1] for r in out["review"]]
    assert review == ["3", "4"]  # lowest confidence first; corrected/starred/ignored left out

    m = out["metrics"]
    assert m["n_utterances"] == 5 and m["n_corrected"] == 2 and m["n_starred"] == 1 and m["n_ignored"] == 1
    assert m["n_training_pairs"] == 2 and m["n_zh_to_fr"] == 4 and m["n_fr_to_zh"] == 1
    assert m["low_conf_rate"] == pytest.approx(3 / 5)


def test_parquet_has_a_stable_schema():
    out = lb.process_day(day(card(1)), [], DATE)
    table = pq.read_table(io.BytesIO(out["parquet"]))
    assert table.schema.names == [name for name, _ in lb.COLUMNS]
    assert table.num_rows == 1 and table.column("final_text")[0].as_py() == "tgt 1"
    empty = pq.read_table(io.BytesIO(lb.process_day("", [], DATE)["parquet"]))
    assert empty.schema == table.schema  # same columns even on an empty day


def test_fingerprint_changes_with_raw_edits_and_pipeline_version(monkeypatch):
    base = lb.fingerprint("etag1", ["e1"])
    assert base == lb.fingerprint("etag1", ["e1"])
    assert base != lb.fingerprint("etag2", ["e1"])
    assert base != lb.fingerprint("etag1", ["e1", "e2"])
    monkeypatch.setattr(lb, "PIPELINE_VERSION", "999")
    assert base != lb.fingerprint("etag1", ["e1"])
