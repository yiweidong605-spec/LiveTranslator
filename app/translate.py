"""翻译：调用本地大模型（Ollama，完全离线）。

支持两类模型：
  - 通用对话模型（Qwen2.5 / Qwen3 等）：用“同声传译”系统提示词，能结合上下文、纠正识别错字
  - 腾讯混元翻译模型 HY-MT（专用翻译模型）：用官方推荐的提示词模板
另外支持 backend: hf —— 直接用 transformers 加载本地 HuggingFace 模型（需要 torch）。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .confidence import seq_confidence
from typing import Callable, Iterator

import requests

ROOT = Path(__file__).resolve().parent.parent

LANG_ZH = {"zh": "中文", "fr": "法语", "en": "英语"}
LANG_EN = {"zh": "Chinese", "fr": "French", "en": "English"}


# Bump when the prompt changes, so evaluations (MLflow param "prompt_version") can be compared.
#   v1  2026-11-01  first prompt
#   v2  2026-11-14  slang / idioms are translated by meaning (literal "这很严重！我此刻正在水下" for
#                   "C'est grave bien ! Je suis sous l'eau"). The examples below are deliberately NOT
#                   taken from the test set, otherwise the evaluation would measure memorisation.
PROMPT_VERSION = "v2"

IDIOM_EXAMPLES = {
    ("fr", "zh"): ["Ça déchire ! → 太酷了！", "Il m'a posé un lapin. → 他放我鸽子了。",
                   "J'ai la pêche aujourd'hui. → 我今天精神特别好。"],
    ("zh", "fr"): ["加油！ → Bon courage !", "随便，你定吧。 → Comme tu veux, c'est toi qui choisis.",
                   "他放我鸽子了。 → Il m'a posé un lapin."],
    ("en", "zh"): ["Break a leg! → 祝你好运！", "I'm swamped. → 我忙得不可开交。"],
    ("zh", "en"): ["加油！ → You've got this!", "他放我鸽子了。 → He stood me up."],
}


def load_glossary(path: Path) -> list[tuple[str, dict]]:
    """每行：中文 = 法语 = 英语（英语可省略）。"""
    items = []
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        parts = [p.strip() for p in line.split("=")]
        zh = parts[0]
        d = {}
        if len(parts) > 1 and parts[1]:
            d["fr"] = parts[1]
        if len(parts) > 2 and parts[2]:
            d["en"] = parts[2]
        if zh and d:
            items.append((zh, d))
    return items


def ensure_ollama(url: str = "http://127.0.0.1:11434"):
    """Ollama 没在运行时尝试后台启动它。"""
    import subprocess
    import time

    try:
        requests.get(f"{url}/api/tags", timeout=1.5)
        return
    except Exception:
        pass
    from .paths import find_ollama, ollama_env

    exe = find_ollama()
    if not exe:
        return
    print("Starting Ollama in the background …", flush=True)
    subprocess.Popen([exe, "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ollama_env())
    for _ in range(15):
        time.sleep(1)
        try:
            requests.get(f"{url}/api/tags", timeout=1.5)
            return
        except Exception:
            pass


class Translator:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.backend = cfg.get("backend", "ollama")
        self.model = cfg.get("model", "qwen2.5:7b")
        style = cfg.get("style", "auto")
        if style == "auto":
            m = self.model.lower()
            if "hy-mt" in m or "hunyuan-mt" in m or "hymt" in m:
                style = "hymt"
            elif "translategemma" in m:
                style = "tgemma"
            else:
                style = "chat"
        self.style = style
        self.url = cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
        self.scenario = cfg.get("scenario", "在法国求职、实习面试和日常交流")
        self.context_turns = int(cfg.get("context_turns", 4))
        self.glossary = load_glossary(ROOT / cfg.get("glossary_file", "glossary.txt"))
        self.history: list[tuple[str, str]] = []  # (原文, 译文)
        self.lock = threading.Lock()
        self._hf = None
        if self.backend == "hf":
            self._load_hf()
        else:
            ensure_ollama(self.url)

    # ------------------------------------------------------------------ 检查
    def check(self) -> tuple[bool, str]:
        if self.backend == "hf":
            return True, f"本地模型 {self.cfg.get('hf_model')}"
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=3)
            names = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return False, "Cannot reach Ollama. Start Ollama (https://ollama.com), then restart run.bat."
        want = self.model if ":" in self.model else self.model + ":latest"
        if not any(n == want or n == self.model for n in names):
            return False, f"Model {self.model} is not in Ollama yet. Run: ollama pull {self.model}"
        return True, f"Ollama · {self.model}"

    def warmup(self):
        try:
            for _ in self.stream("Bonjour", "fr", "zh", context=[]):
                pass
        except Exception as e:
            self.log(f"Translation warm-up failed: {e}")

    # ------------------------------------------------------------------ 提示词
    def _glossary_for(self, text: str, src: str, tgt: str) -> list[tuple[str, str]]:
        hits = []
        foreign = tgt if src == "zh" else src
        for zh, d in self.glossary:
            fx = d.get(foreign)
            if not fx:
                continue
            if src == "zh" and zh in text:
                hits.append((zh, fx))
            elif tgt == "zh" and fx.lower() in text.lower():
                hits.append((fx, zh))
        return hits[:12]

    def build_messages(self, text: str, src: str, tgt: str, use_context=True,
                       context: list[tuple[str, str]] | None = None) -> list[dict]:
        if context is not None:
            ctx = context[-self.context_turns:]
        else:
            ctx = self.history[-self.context_turns:] if use_context else []
        terms = self._glossary_for(text, src, tgt)
        if self.style == "tgemma":
            code = {"zh": "zh-Hans", "fr": "fr", "en": "en"}
            sl, tl = LANG_EN[src], LANG_EN[tgt]
            prompt = (
                f"You are a professional {sl} ({code[src]}) to {tl} ({code[tgt]}) translator. "
                f"Your goal is to accurately convey the meaning and nuances of the original {sl} text "
                f"while adhering to {tl} grammar, vocabulary, and cultural sensitivities.\n"
                f"Produce only the {tl} translation, without any additional explanations or commentary. "
                f"Please translate the following {sl} text into {tl}:\n\n\n{text}"
            )
            return [{"role": "user", "content": prompt}]
        if self.style == "hymt":
            parts = []
            if terms:
                parts.append("参考下面的翻译：\n" + "\n".join(f"{a} 翻译成 {b}" for a, b in terms) + "\n")
            if ctx:
                parts.append("\n".join(s for s, _ in ctx))
                parts.append(
                    f"参考上面的信息，把下面的文本翻译成{LANG_ZH[tgt]}，注意不需要翻译上文，也不要额外解释：\n{text}"
                )
            elif "zh" in (src, tgt):
                parts.append(f"将以下文本翻译为{LANG_ZH[tgt]}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}")
            else:
                parts.append(
                    f"Translate the following segment into {LANG_EN[tgt]}, without additional explanation.\n\n{text}"
                )
            return [{"role": "user", "content": "\n".join(parts)}]

        # 通用对话模型
        rules = [
            "只输出译文本身，不要解释、不要加引号、不要回答问题、不要加拼音或注释。",
            "原文来自语音识别，可能有错字、断句不全或口头禅，请根据上下文理解后翻译成通顺的话，口头禅可省略。",
            "保持原意和语气，不要擅自增加内容。",
            "俚语、习语、口头说法按实际意思翻成对方语言里对应的说法，不要逐字直译；"
            "但要看语境，词语用的是本义时照本义翻（例如 La situation est grave 就是“情况很严重”）。",
        ]
        if tgt == "fr":
            rules.append("对面试官/陌生人用 vous，用词得体自然，像母语者在职场里说话。")
        elif tgt == "en":
            rules.append("用自然、礼貌的职场口语。")
        elif tgt == "zh":
            rules.append("使用简体中文。")
        sys_prompt = (
            f"你是一名专业的同声传译员，场景是：{self.scenario}。\n"
            f"任务：把用户发来的{LANG_ZH[src]}口语，翻译成地道、自然的{LANG_ZH[tgt]}口语。\n"
            "规则：\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))
        )
        examples = IDIOM_EXAMPLES.get((src, tgt))
        if examples:
            sys_prompt += "\n口语/习语的译法示例：\n" + "\n".join(f"- {e}" for e in examples)
        if terms:
            sys_prompt += "\n术语表（必须遵守）：\n" + "\n".join(f"- {a} → {b}" for a, b in terms)
        if ctx:
            sys_prompt += "\n\n对话上文（仅供理解，不要翻译）：\n" + "\n".join(
                f"- {s} ⇒ {t}" for s, t in ctx
            )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ]

    # ------------------------------------------------------------------ 调用
    def stream(self, text: str, src: str, tgt: str, use_context=True,
               context: list[tuple[str, str]] | None = None) -> Iterator[tuple[str, list]]:
        """逐块产出 (文字片段, 这一块的 logprobs 列表)。"""
        msgs = self.build_messages(text, src, tgt, use_context, context)
        if self.backend == "hf":
            for piece in self._hf_stream(msgs):
                yield piece, []
            return
        if self.style == "hymt":
            options = {"temperature": 0.3, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05}
        else:
            options = {"temperature": 0.2, "top_p": 0.9}
        options["num_predict"] = 400
        payload = {
            "model": self.model,
            "messages": msgs,
            "stream": True,
            "think": False,
            "keep_alive": "60m",
            "options": options,
            "logprobs": True,      # 让模型告诉我们每个词有多大把握 → 置信度
            "top_logprobs": 3,
        }
        r = requests.post(f"{self.url}/api/chat", json=payload, stream=True, timeout=(5, 120))
        if r.status_code == 400:
            # 老版本 Ollama 不认识 think / logprobs 参数
            for k in ("think", "logprobs", "top_logprobs"):
                payload.pop(k, None)
            r.close()
            r = requests.post(f"{self.url}/api/chat", json=payload, stream=True, timeout=(5, 120))
        with r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if "error" in data:
                    raise RuntimeError(data["error"])
                piece = data.get("message", {}).get("content", "")
                lps = data.get("logprobs") or []
                if piece or lps:
                    yield piece, lps
                if data.get("done"):
                    break

    def translate(self, text: str, src: str, tgt: str,
                  on_delta: Callable[[str], None] | None = None,
                  context: list[tuple[str, str]] | None = None,
                  remember: bool = True) -> "TranslationResult":
        out, lps = [], []
        t0 = time.time()
        first = None
        with self.lock:
            filt = ThinkFilter()
            for piece, lp in self.stream(text, src, tgt, context=context):
                lps.extend(lp)
                piece = filt.feed(piece)
                if piece:
                    if first is None:
                        first = time.time() - t0
                    out.append(piece)
                    if on_delta:
                        on_delta(piece)
            tail = filt.flush()
            if tail:
                out.append(tail)
                if on_delta:
                    on_delta(tail)
        result = postprocess("".join(out))
        conf = mt_confidence(lps)
        if result and remember and context is None:
            self.history.append((text, result))
            self.history = self.history[-20:]
        return TranslationResult(
            text=result, model=self.model, first_token_s=first or 0.0,
            total_s=time.time() - t0, **conf,
        )

    def reset_context(self):
        self.history.clear()

    # ------------------------------------------------------------------ HF 后端
    def _load_hf(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        name = self.cfg.get("hf_model", "Qwen/Qwen2.5-3B-Instruct")
        self.log(f"Loading local translation model {name} …")
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        tok = AutoTokenizer.from_pretrained(name)
        model = AutoModelForCausalLM.from_pretrained(
            name, torch_dtype=torch.float16 if dev == "cuda" else torch.float32
        ).to(dev)
        model.eval()
        self._hf = (tok, model, dev)

    def _hf_stream(self, msgs: list[dict]) -> Iterator[str]:
        from transformers import TextIteratorStreamer

        tok, model, dev = self._hf
        ids = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt").to(dev)
        streamer = TextIteratorStreamer(tok, skip_prompt=True, skip_special_tokens=True)
        th = threading.Thread(
            target=model.generate,
            kwargs=dict(input_ids=ids, max_new_tokens=400, do_sample=False, streamer=streamer),
            daemon=True,
        )
        th.start()
        for piece in streamer:
            yield piece
        th.join()


@dataclass
class TranslationResult:
    text: str
    model: str = ""
    confidence: float | None = None     # 0~1，翻译置信度
    geo_prob: float | None = None       # 所有 token 概率的几何平均
    min_prob: float | None = None       # 最没把握的那个 token 的概率
    weak_tokens: list = field(default_factory=list)  # [{token, p, alts:[{token,p}]}]
    n_tokens: int = 0
    first_token_s: float = 0.0
    total_s: float = 0.0

    def __str__(self):
        return self.text


SKIP_TOKEN = re.compile(r"^[\s\.,!?;:'\"«»“”‘’。，！？；：、…\-()（）]*$")


def mt_confidence(lps: list) -> dict:
    items = []
    for e in lps:
        tok = e.get("token", "")
        lp = e.get("logprob")
        if lp is None or SKIP_TOKEN.match(tok) or tok.startswith("<"):
            continue
        alts = [{"token": a.get("token", ""), "p": round(math.exp(a.get("logprob", -99)), 3)}
                for a in (e.get("top_logprobs") or []) if a.get("token") != tok][:2]
        items.append({"token": tok, "p": round(math.exp(lp), 3), "alts": alts})
    conf, geo, mn = seq_confidence([i["p"] for i in items])
    weak = sorted(items, key=lambda i: i["p"])[:3]
    weak = [w for w in weak if w["p"] < 0.8]
    return {"confidence": None if conf is None else round(conf, 3),
            "geo_prob": None if geo is None else round(geo, 3),
            "min_prob": None if mn is None else round(mn, 3),
            "weak_tokens": weak, "n_tokens": len(items)}


class ThinkFilter:
    """去掉某些模型输出的 <think>...</think> 思考过程。"""

    def __init__(self):
        self.buf = ""
        self.in_think = False
        self.started = False

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = ""
        while self.buf:
            if self.in_think:
                i = self.buf.find("</think>")
                if i < 0:
                    self.buf = self.buf[-8:]
                    return out
                self.buf = self.buf[i + 8:]
                self.in_think = False
                continue
            i = self.buf.find("<think>")
            if i >= 0:
                out += self.buf[:i]
                self.buf = self.buf[i + 7:]
                self.in_think = True
                continue
            # 可能是不完整的 "<thi"
            j = self.buf.rfind("<")
            if j >= 0 and len(self.buf) - j < 7:
                out += self.buf[:j]
                self.buf = self.buf[j:]
                break
            out += self.buf
            self.buf = ""
        if not self.started:
            out = out.lstrip()
            if out:
                self.started = True
        return out

    def flush(self) -> str:
        out = "" if self.in_think else self.buf
        self.buf = ""
        return out


def postprocess(text: str) -> str:
    t = text.strip()
    pairs = {'"': '"', "“": "”", "«": "»", "「": "」"}
    if len(t) > 2 and t[0] in pairs and t.endswith(pairs[t[0]]) and t.count(t[0]) == 1 + (t[0] == '"'):
        t = t[1:-1].strip()
    for prefix in ("译文：", "译文:", "翻译：", "翻译:", "Traduction :", "Translation:"):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    return t
