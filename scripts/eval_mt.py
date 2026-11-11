"""翻译评测：把评测集里的每句话交给每个候选模型翻译，打分、对比、出报告。

用法（在 eval.bat 里选菜单即可，也可以命令行）：
  env\\python.exe scripts\\eval_mt.py                       # 评测 config.yaml 里 eval.candidates 的全部模型
  env\\python.exe scripts\\eval_mt.py --models qwen2.5:7b,gemma3:12b --limit 30
  env\\python.exe scripts\\eval_mt.py --comet               # 额外用 COMET 神经网络打分（需先装 COMET）

输出：
  data/eval/runs/<时间>/report.html   可视化对比报告（排行榜 + 每句话逐条对比）
  data/eval/runs/<时间>/results.jsonl 每个模型每句话的译文、得分、置信度、耗时
  data/eval/leaderboard.csv           历次评测的汇总（一直累积）
  MLflow（mlflow.db）                 每个模型一条 run，可在 MLflow 界面里画图对比
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
import yaml  # noqa: E402

from app.translate import Translator  # noqa: E402

EVAL_DIR = ROOT / "data" / "eval"
DIRS = ["fr→zh", "zh→fr", "en→zh", "zh→en"]


# ----------------------------------------------------------------------------- 工具
def load_items(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def file_hash(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x, y):
    """置信度和质量的等级相关：越接近 1 说明置信度越可信。"""
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    if len(pairs) < 5:
        return None
    rx, ry = rank([p[0] for p in pairs]), rank([p[1] for p in pairs])
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 3) if den else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 4) if xs else None


def p90(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(len(xs) * 0.9))], 3) if xs else None


def expect_hit(hyp: str, expect: list[str] | None):
    if not expect:
        return None
    h = hyp.lower()
    return any(e.lower() in h for e in expect)


def ollama_unload(url: str, model: str):
    try:
        requests.post(f"{url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


# ----------------------------------------------------------------------------- 评测一个模型
def run_model(model: str, items: list[dict], tcfg: dict, log) -> list[dict]:
    cfg = dict(tcfg)
    cfg["model"] = model
    cfg["style"] = "auto"
    tr = Translator(cfg, log=lambda *a: None)
    ok, msg = tr.check()
    if not ok:
        raise RuntimeError(msg)
    log(f"  预热 {model} …")
    tr.warmup()
    rows = []
    t_start = time.time()
    for n, it in enumerate(items, 1):
        ctx = [(c["src"], c["tgt"]) for c in it.get("context", [])]
        row = {"model": model, "style": tr.style, "id": it["id"], "cat": it["cat"],
               "dir": f"{it['src_lang']}→{it['tgt_lang']}", "src": it["src"], "ref": it["ref"],
               "with_context": it.get("with_context"), "group": it.get("group")}
        try:
            r = tr.translate(it["src"], it["src_lang"], it["tgt_lang"], context=ctx)
            row.update({"hyp": r.text, "conf": r.confidence, "geo_prob": r.geo_prob,
                        "min_prob": r.min_prob, "weak_tokens": r.weak_tokens,
                        "first_s": round(r.first_token_s, 3), "total_s": round(r.total_s, 3)})
        except Exception as e:
            row.update({"hyp": "", "error": str(e)})
        row["hit"] = expect_hit(row["hyp"], it.get("expect"))
        rows.append(row)
        if n % 20 == 0 or n == len(items):
            el = time.time() - t_start
            log(f"  {model}: {n}/{len(items)}  已用 {el:.0f}s，预计还要 {el / n * (len(items) - n):.0f}s")
    return rows


# ----------------------------------------------------------------------------- 打分
def score_rows(rows: list[dict]):
    from sacrebleu.metrics import CHRF

    chrf = CHRF()
    for r in rows:
        refs = r["ref"].split(" / ") if r.get("with_context") is False else [r["ref"]]
        r["chrf"] = round(max(chrf.sentence_score(r["hyp"], [x]).score for x in refs), 2) if r["hyp"] else 0.0


def corpus_scores(rows: list[dict]) -> dict:
    from sacrebleu.metrics import BLEU, CHRF

    out = {}
    for d in DIRS:
        rs = [r for r in rows if r["dir"] == d and r.get("with_context") is not False]
        if not rs:
            continue
        hyps, refs = [r["hyp"] for r in rs], [[r["ref"] for r in rs]]
        tok = "zh" if d.endswith("zh") else "13a"
        key = d.replace("→", "_")
        out[f"chrf_{key}"] = round(CHRF().corpus_score(hyps, refs).score, 2)
        out[f"bleu_{key}"] = round(BLEU(tokenize=tok).corpus_score(hyps, refs).score, 2)
    return out


def comet_score(all_rows: list[dict], log):
    try:
        from comet import download_model, load_from_checkpoint
    except ImportError:
        log("⚠ 没装 COMET，跳过（eval.bat 菜单 1 可以安装）")
        return False
    log("COMET 打分中（第一次会下载约 2.3GB 模型）…")
    path = download_model("Unbabel/wmt22-comet-da", saving_directory=str(ROOT / "models" / "comet"))
    model = load_from_checkpoint(path)
    try:
        import torch
        gpus = 1 if torch.cuda.is_available() else 0
    except Exception:
        gpus = 0
    data = [{"src": r["src"], "mt": r["hyp"], "ref": r["ref"].split(" / ")[0]} for r in all_rows]
    pred = model.predict(data, batch_size=32, gpus=gpus, progress_bar=True)
    for r, s in zip(all_rows, pred.scores):
        r["comet"] = round(float(s), 4)
    return True


def summarize(model: str, rows: list[dict]) -> dict:
    s = {"model": model, "style": rows[0]["style"] if rows else "", "n": len(rows)}
    s.update(corpus_scores(rows))
    main = [r for r in rows if r.get("with_context") is not False]
    s["chrf_avg"] = mean([r["chrf"] for r in main])
    if any("comet" in r for r in rows):
        s["comet_avg"] = mean([r.get("comet") for r in main])
        for d in DIRS:
            v = mean([r.get("comet") for r in main if r["dir"] == d])
            if v is not None:
                s[f"comet_{d.replace('→', '_')}"] = v
    hits = [r["hit"] for r in rows if r["hit"] is not None]
    s["keyword_acc"] = round(sum(hits) / len(hits), 3) if hits else None
    amb_ctx = [r["hit"] for r in rows if r["cat"] == "ambiguity" and r.get("with_context") and r["hit"] is not None]
    s["ambiguity_ctx_acc"] = round(sum(amb_ctx) / len(amb_ctx), 3) if amb_ctx else None
    s["conf_avg"] = mean([r.get("conf") for r in rows])
    s["conf_amb_no_ctx"] = mean([r.get("conf") for r in rows if r["cat"] == "ambiguity" and r.get("with_context") is False])
    s["conf_amb_ctx"] = mean([r.get("conf") for r in rows if r["cat"] == "ambiguity" and r.get("with_context")])
    s["conf_vs_chrf"] = spearman([r.get("conf") for r in main], [r["chrf"] for r in main])
    if "comet_avg" in s:
        s["conf_vs_comet"] = spearman([r.get("conf") for r in main], [r.get("comet") for r in main])
    s["first_token_s"] = mean([r.get("first_s") for r in rows])
    s["latency_s"] = mean([r.get("total_s") for r in rows])
    s["latency_p90_s"] = p90([r.get("total_s") for r in rows])
    s["errors"] = sum(1 for r in rows if r.get("error"))
    for cat in sorted({r["cat"] for r in rows}):
        s[f"chrf_cat_{cat}"] = mean([r["chrf"] for r in main if r["cat"] == cat])
    return s


def add_significance(summaries: list[dict], rows: list[dict], n: int = 2000):
    """配对 bootstrap：每个模型 chrF 的 95% 置信区间，以及“第一名比它好”的概率。
    差距很小时（概率 < 0.95）说明两者在这套评测集上分不出高下。"""
    import random

    rng = random.Random(0)
    by = {}
    for r in rows:
        if r.get("with_context") is not False:
            by.setdefault(r["model"], {})[r["id"]] = r["chrf"]
    if not summaries:
        return
    best = summaries[0]["model"]
    ids = sorted(set.intersection(*(set(v) for v in by.values())))
    if len(ids) < 10:
        return
    idx = [[rng.randrange(len(ids)) for _ in ids] for _ in range(n)]
    for s in summaries:
        m = s["model"]
        vals = [by[m][i] for i in ids]
        means = sorted(statistics.mean(vals[k] for k in sample) for sample in idx)
        s["chrf_ci_low"] = round(means[int(n * 0.025)], 2)
        s["chrf_ci_high"] = round(means[int(n * 0.975)], 2)
        if m != best:
            d = [by[best][i] - by[m][i] for i in ids]
            wins = sum(1 for sample in idx if sum(d[k] for k in sample) > 0)
            s["p_best_better"] = round(wins / n, 3)


# ----------------------------------------------------------------------------- 报告
def write_report(out_dir: Path, summaries: list[dict], rows: list[dict], meta: dict):
    tpl = (ROOT / "scripts" / "report_template.html").read_text(encoding="utf-8")
    payload = json.dumps({"meta": meta, "summaries": summaries, "rows": rows}, ensure_ascii=False)
    html = tpl.replace("/*__DATA__*/null", payload.replace("</", "<\\/"))
    (out_dir / "report.html").write_text(html, encoding="utf-8")


def append_leaderboard(summaries: list[dict], meta: dict):
    path = EVAL_DIR / "leaderboard.csv"
    keys = ["run", "testset", "model", "chrf_avg", "comet_avg", "keyword_acc", "ambiguity_ctx_acc",
            "chrf_fr_zh", "chrf_zh_fr", "chrf_en_zh", "chrf_zh_en", "conf_vs_chrf",
            "first_token_s", "latency_s", "latency_p90_s", "errors"]
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        if new:
            w.writeheader()
        for s in summaries:
            w.writerow({"run": meta["run"], "testset": meta["testset_hash"], **s})


def log_mlflow(summaries, rows, meta, out_dir, log):
    from scripts.tracking import run_tags, setup

    mlflow, uri, is_remote = setup("translation-eval", log)
    if mlflow is None:
        return
    for s in summaries:
        with mlflow.start_run(run_name=s["model"]):
            mlflow.set_tags({"run_group": meta["run"], "testset": meta["testset_hash"], "stage": "baseline",
                             "rescore_of": meta.get("rescore_of", ""), **run_tags()})
            mlflow.log_params({"model": s["model"], "style": s["style"], "n_items": s["n"],
                               "glossary": meta["glossary"], "testset_file": meta["testset"]})
            mlflow.log_metrics({k: v for k, v in s.items() if isinstance(v, (int, float)) and v is not None
                                and k not in ("n",)})
            mrows = [r for r in rows if r["model"] == s["model"]]
            mlflow.log_dict({"rows": mrows}, "results.json")
            mlflow.log_artifact(str(out_dir / "report.html"))
    where = f"平台 {uri}" if is_remote else "本地 mlflow.db"
    log(f"✓ 已记录到 MLflow（{where}；eval.bat 菜单 6 打开看板）")


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="翻译模型评测")
    ap.add_argument("--models", help="逗号分隔；默认用 config.yaml 的 eval.candidates")
    ap.add_argument("--testset", default=str(EVAL_DIR / "mt_testset.jsonl"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 句（快速试跑）")
    ap.add_argument("--cats", help="只跑某些类别，逗号分隔，如 interview,slang")
    ap.add_argument("--comet", action="store_true", help="用 COMET 打分（更接近人工判断，需要先安装）")
    ap.add_argument("--no-glossary", action="store_true", help="不使用术语表")
    ap.add_argument("--no-mlflow", action="store_true")
    ap.add_argument("--rescore", metavar="RUN",
                    help="不重新翻译，用已保存的译文按当前评测集重新打分，例如 --rescore 20261106-135255")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    tcfg = dict(cfg.get("translator", {}))
    if args.no_glossary:
        tcfg["glossary_file"] = "__no_glossary__.txt"
    models = [m.strip() for m in (args.models.split(",") if args.models else cfg.get("eval", {}).get("candidates", [])) if m.strip()]
    if not models and not args.rescore:
        sys.exit("没有要评测的模型：用 --models 指定，或在 config.yaml 的 eval.candidates 里填写")

    testset = Path(args.testset)
    items = load_items(testset)
    if args.cats:
        keep = set(args.cats.split(","))
        items = [i for i in items if i["cat"] in keep]
    if args.limit:
        items = items[: args.limit]

    run = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-rescore" if args.rescore else "")
    out_dir = EVAL_DIR / "runs" / run
    out_dir.mkdir(parents=True, exist_ok=True)
    logf = open(out_dir / "log.txt", "w", encoding="utf-8")

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    meta = {"run": run, "testset": testset.name, "testset_hash": file_hash(testset),
            "n_items": len(items), "models": models, "glossary": not args.no_glossary,
            "time": datetime.now().isoformat(timespec="seconds")}

    all_rows, done = [], []
    if args.rescore == "latest":
        runs = sorted(p.name for p in (EVAL_DIR / "runs").glob("*") if p.is_dir()
                      and not p.name.endswith("-rescore") and (p / "results.jsonl").exists())
        if not runs:
            sys.exit("还没有评测结果，先跑一次评测")
        args.rescore = runs[-1]
    if args.rescore:
        src = EVAL_DIR / "runs" / args.rescore / "results.jsonl"
        if not src.exists():
            sys.exit(f"找不到 {src}")
        by_id = {i["id"]: i for i in items}
        for line in src.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            it = by_id.get(r["id"])
            if it is None:          # 这句已从评测集删除
                continue
            r.update({"ref": it["ref"], "cat": it["cat"], "with_context": it.get("with_context"),
                      "group": it.get("group")})
            r["hit"] = expect_hit(r.get("hyp", ""), it.get("expect"))
            r.pop("comet", None)
            all_rows.append(r)
            if r["model"] not in done:
                done.append(r["model"])
        src_meta = json.loads((src.parent / "summary.json").read_text(encoding="utf-8"))["meta"]
        meta.update({"models": done, "rescore_of": args.rescore, "glossary": src_meta.get("glossary", True),
                     "n_items": len(by_id)})
        log(f"重新打分：{args.rescore} 的 {len(done)} 个模型 × {len(by_id)} 句（评测集 {meta['testset_hash']}）")
    else:
        log(f"评测 {len(models)} 个模型 × {len(items)} 句  →  {out_dir}")
        url = tcfg.get("ollama_url", "http://127.0.0.1:11434")
        for i, m in enumerate(models, 1):
            log(f"\n[{i}/{len(models)}] {m}")
            try:
                rows = run_model(m, items, tcfg, log)
                all_rows += rows
                done.append(m)
            except Exception as e:
                log(f"  ✗ 跳过 {m}：{e}")
                if not isinstance(e, RuntimeError):
                    traceback.print_exc()
            ollama_unload(url, m)  # 释放显存给下一个模型

    if not all_rows:
        sys.exit("没有任何模型跑成功。")
    score_rows(all_rows)
    if args.comet:
        comet_score(all_rows, log)
    summaries = [summarize(m, [r for r in all_rows if r["model"] == m]) for m in done]
    summaries.sort(key=lambda s: -(s.get("comet_avg") or 0) * 100 - (s.get("chrf_avg") or 0))
    add_significance(summaries, all_rows)

    with open(out_dir / "results.jsonl", "w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out_dir / "summary.json").write_text(json.dumps({"meta": meta, "summaries": summaries},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(out_dir, summaries, all_rows, meta)
    append_leaderboard(summaries, meta)
    if not args.no_mlflow:
        try:
            log_mlflow(summaries, all_rows, meta, out_dir, log)
        except Exception as e:
            log(f"⚠ MLflow 记录失败：{e}")

    log("\n===== 排行榜 =====")
    log(f"{'模型':42s} {'chrF':>6s} {'COMET':>6s} {'关键词':>6s} {'歧义':>6s} {'置信相关':>8s} {'延迟':>6s}")
    for s in summaries:
        f = lambda v, p=1: "—" if v is None else (f"{v:.{p}f}")
        log(f"{s['model'][:42]:42s} {f(s.get('chrf_avg')):>6s} {f(s.get('comet_avg'), 3):>6s} "
            f"{f(s.get('keyword_acc'), 2):>6s} {f(s.get('ambiguity_ctx_acc'), 2):>6s} "
            f"{f(s.get('conf_vs_chrf'), 2):>8s} {f(s.get('latency_s'), 2):>5s}s")
    log(f"\n报告：{out_dir / 'report.html'}")
    try:
        import webbrowser
        webbrowser.open((out_dir / "report.html").as_uri())
    except Exception:
        pass


if __name__ == "__main__":
    main()
