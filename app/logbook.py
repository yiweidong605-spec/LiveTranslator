"""对话记账本：每句话一条记录（属性卡），按天存成 jsonl，原声存成 wav。

data/logs/
  2026-11-02.jsonl          每行一句话的完整属性卡
  audio/2026-11-02/<rid>.wav 这句话的原声（可在复盘页面回放）
  edits.jsonl               你在复盘页面里做的修改（改译文、备注、标星），不改动原始记录
"""
from __future__ import annotations

import json
import threading
import time
import wave
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .paths import ROOT

LOG_DIR = ROOT / "data" / "logs"
EDITABLE = {"correction", "note", "star", "ignore"}


class Logbook:
    def __init__(self, cfg: dict | None = None, log_dir: Path = LOG_DIR):
        cfg = cfg or {}
        self.enabled = cfg.get("enabled", True)
        self.save_audio = cfg.get("save_audio", True)
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.counter = 0

    # ------------------------------------------------------------------ 写
    def new_rid(self) -> str:
        with self.lock:
            self.counter += 1
            return datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{self.counter:03d}"

    def save_clip(self, rid: str, audio: np.ndarray, sr: int = 16000) -> str | None:
        if not (self.enabled and self.save_audio) or audio is None or len(audio) == 0:
            return None
        day = rid[:4] + "-" + rid[4:6] + "-" + rid[6:8]
        rel = Path("audio") / day / f"{rid}.wav"
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        return rel.as_posix()

    def append(self, rec: dict):
        if not self.enabled:
            return
        rec.setdefault("ts", time.time())
        dt = datetime.fromtimestamp(rec["ts"])
        rec.setdefault("date", dt.strftime("%Y-%m-%d"))
        rec.setdefault("time", dt.strftime("%H:%M:%S"))
        line = json.dumps(rec, ensure_ascii=False)
        with self.lock, open(self.dir / f"{rec['date']}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def edit(self, rid: str, field: str, value):
        if field not in EDITABLE:
            raise ValueError(f"Field cannot be edited: {field}")
        line = json.dumps({"rid": rid, "field": field, "value": value, "ts": time.time()},
                          ensure_ascii=False)
        with self.lock, open(self.dir / "edits.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ------------------------------------------------------------------ 读
    def _edits(self) -> dict[str, dict]:
        out: dict[str, dict] = defaultdict(dict)
        p = self.dir / "edits.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    out[e["rid"]][e["field"]] = e["value"]
                except Exception:
                    continue
        return out

    @staticmethod
    def _read(path: Path) -> list[dict]:
        recs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
        return recs

    def days(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("????-??-??.jsonl"), reverse=True):
            recs = self._read(p)
            if not recs:
                continue
            last = recs[-1]
            confs = [r["confidence"] for r in recs if r.get("confidence") is not None]
            out.append({
                "date": p.stem,
                "count": len(recs),
                "last_time": last.get("time", ""),
                "preview": (last.get("tgt_text") or last.get("src_text") or "")[:40],
                "low_conf": sum(1 for c in confs if c < 0.6),
                "avg_conf": round(sum(confs) / len(confs), 3) if confs else None,
            })
        return out

    def day(self, date: str) -> list[dict]:
        p = self.dir / f"{date}.jsonl"
        if not p.exists() or len(date) != 10:
            return []
        edits = self._edits()
        recs = self._read(p)
        for r in recs:
            if r.get("rid") in edits:
                r["edits"] = edits[r["rid"]]
        return recs

    def search(self, q: str, limit: int = 200) -> list[dict]:
        q = q.lower().strip()
        if not q:
            return []
        hits = []
        for p in sorted(self.dir.glob("????-??-??.jsonl"), reverse=True):
            for r in self._read(p):
                if q in (r.get("src_text", "") + " " + r.get("tgt_text", "")).lower():
                    hits.append(r)
                    if len(hits) >= limit:
                        return hits
        return hits

    def audio_path(self, rel: str) -> Path | None:
        p = (self.dir / rel).resolve()
        if self.dir.resolve() in p.parents and p.suffix == ".wav" and p.exists():
            return p
        return None
