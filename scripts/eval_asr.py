"""语音识别评测：词错误率（WER）/ 字错误率（CER）、速度、识别置信度是否靠谱。

数据来源（可以同时用）：
  1) 真实录音（推荐，最能反映实际效果）：
       data/eval/asr/real/ 下放 xxx.wav + 同名 xxx.txt（正确文本），
       文件名以语言开头：fr_xxx.wav / zh_xxx.wav / en_xxx.wav
  2) --synth：用 Piper 把评测集里的法语/英语句子合成成语音（干净的机器声音，只能当基线）

用法：
  env\\python.exe scripts\\eval_asr.py --synth
  env\\python.exe scripts\\eval_asr.py --models large-v3-turbo,small
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import yaml  # noqa: E402

from scripts.eval_mt import mean, spearman  # noqa: E402

ASR_DIR = ROOT / "data" / "eval" / "asr"


def norm(text: str, lang: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    t = re.sub(r"[^\w\s']", " ", t)
    if lang == "zh":
        return "".join(t.split())
    return " ".join(t.split())


def error_rate(ref: str, hyp: str, lang: str) -> float:
    import jiwer

    r, h = norm(ref, lang), norm(hyp, lang)
    if lang == "zh":
        return jiwer.cer(r, h) if r else 0.0
    return jiwer.wer(r, h) if r else 0.0


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        import soxr
        x = soxr.resample(x, sr, 16000)
    return x.astype(np.float32)


def build_synth(cfg: dict, limit: int) -> list[dict]:
    """用 Piper 把评测集的外语原句合成成语音。"""
    from app.tts import PiperTTS

    out = ASR_DIR / "synth"
    out.mkdir(parents=True, exist_ok=True)
    tts = PiperTTS(cfg.get("tts", {}).get("voices", {}), 1.0, log=lambda *a: None)
    items = [json.loads(l) for l in (ROOT / "data/eval/mt_testset.jsonl").read_text(encoding="utf-8").splitlines() if l]
    items = [i for i in items if i["src_lang"] in tts.voices and i.get("with_context") is not False][:limit]
    clips = []
    for it in items:
        p = out / f"{it['src_lang']}_{it['id']}.wav"
        if not p.exists():
            audio, sr = tts.synth(it["src"], it["src_lang"])
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
        clips.append({"audio": p, "text": it["src"], "lang": it["src_lang"], "kind": "synth"})
    return clips


def load_real() -> list[dict]:
    clips = []
    for wav in sorted((ASR_DIR / "real").glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.exists():
            print(f"⚠ {wav.name} 没有对应的 .txt，跳过")
            continue
        lang = wav.stem.split("_")[0] if wav.stem.split("_")[0] in ("fr", "zh", "en") else "fr"
        clips.append({"audio": wav, "text": txt.read_text(encoding="utf-8").strip(), "lang": lang, "kind": "real"})
    return clips


def main():
    ap = argparse.ArgumentParser(description="语音识别评测")
    ap.add_argument("--models", default="large-v3-turbo,small")
    ap.add_argument("--synth", action="store_true", help="用 Piper 合成评测语音（基线）")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--no-mlflow", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    clips = load_real()
    if args.synth:
        print("合成评测语音 …")
        clips += build_synth(cfg, args.limit)
    if not clips:
        sys.exit("没有评测音频：把录音放进 data/eval/asr/real/，或加 --synth")
    print(f"共 {len(clips)} 段音频（真实 {sum(c['kind']=='real' for c in clips)}，合成 {sum(c['kind']=='synth' for c in clips)}）")

    from app.asr import WhisperASR

    run = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ASR_DIR / "runs" / run
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries, rows = [], []
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        acfg = dict(cfg.get("asr", {}))
        acfg["model"] = name
        try:
            asr = WhisperASR(acfg, log=print)
        except Exception as e:
            print(f"✗ 跳过 {name}：{e}")
            continue
        mrows = []
        for c in clips:
            audio = read_wav(c["audio"])
            t0 = time.time()
            res = asr.transcribe(audio, c["lang"])
            dt = time.time() - t0
            er = error_rate(c["text"], res.text, c["lang"])
            mrows.append({"model": name, "audio": c["audio"].name, "kind": c["kind"], "lang": c["lang"],
                          "ref": c["text"], "hyp": res.text, "err": round(er, 4), "conf": res.confidence,
                          "dur": round(len(audio) / 16000, 2), "secs": round(dt, 3)})
        rows += mrows
        s = {"model": name, "device": asr.device, "n": len(mrows)}
        for kind in ("real", "synth"):
            for lang in ("fr", "en", "zh"):
                v = mean([r["err"] for r in mrows if r["kind"] == kind and r["lang"] == lang])
                if v is not None:
                    s[f"{'cer' if lang == 'zh' else 'wer'}_{kind}_{lang}"] = v
        s["rtf"] = round(sum(r["secs"] for r in mrows) / max(1e-6, sum(r["dur"] for r in mrows)), 3)
        s["conf_avg"] = mean([r["conf"] for r in mrows])
        s["conf_vs_accuracy"] = spearman([r["conf"] for r in mrows], [-r["err"] for r in mrows])
        summaries.append(s)
        print(json.dumps(s, ensure_ascii=False))
        del asr

    with open(out_dir / "results.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_mlflow:
        try:
            from scripts.tracking import run_tags, setup

            mlflow, uri, is_remote = setup("asr-eval")
            if mlflow is None:
                raise ImportError
            for s in summaries:
                with mlflow.start_run(run_name=s["model"]):
                    mlflow.set_tags({"run_group": run, **run_tags()})
                    mlflow.log_params({"model": s["model"], "device": s["device"], "n_clips": s["n"]})
                    mlflow.log_metrics({k: v for k, v in s.items() if isinstance(v, (int, float)) and v is not None and k != "n"})
                    mlflow.log_artifact(str(out_dir / "results.jsonl"))
            print(f"✓ 已记录到 MLflow（{'平台 ' + uri if is_remote else '本地 mlflow.db'}）")
        except ImportError:
            pass
    print("\n词错误率越低越好；rtf < 1 表示比实时快（0.1 = 1 秒音频 0.1 秒识别完）。")
    print(f"结果：{out_dir}")


if __name__ == "__main__":
    main()
