"""语音识别：faster-whisper（Whisper 的高速版本，完全离线）。"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .confidence import seq_confidence

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models" / "whisper"

# Whisper 在静音/噪音上常见的“幻觉”句子
HALLUCINATIONS = [
    "amara.org", "sous-titres", "sous-titrage", "merci d'avoir regardé",
    "thank you for watching", "thanks for watching", "subscribe",
    "字幕由", "请不吝点赞", "订阅", "明镜", "点点栏目", "字幕志愿者",
    "優優獨播劇場", "yoyo television", "中文字幕",
]

ZH_PROMPT = "以下是普通话的句子，使用简体中文和标点符号。"


def add_nvidia_dll_paths():
    """Windows 上让 CTranslate2 找到 pip 安装的 CUDA/cuDNN DLL。"""
    if sys.platform != "win32":
        return
    for sp in sys.path:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
            except Exception:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


add_nvidia_dll_paths()  # 必须在导入 faster_whisper / ctranslate2 之前


def gpu_info() -> tuple[str, int] | None:
    """返回 (显卡名, 显存MB)，没有 NVIDIA 显卡时返回 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()
        if out:
            name, mem = [s.strip() for s in out[0].split(",")]
            return name, int(float(mem))
    except Exception:
        pass
    return None


def choose_model(cfg: dict) -> tuple[str, str, str]:
    """根据配置和硬件选 (模型名, device, compute_type)。"""
    add_nvidia_dll_paths()
    device = cfg.get("device", "auto")
    if device == "auto":
        device = "cpu"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:
            pass
    model = cfg.get("model", "auto")
    if model == "auto":
        model = "large-v3-turbo" if device == "cuda" else "small"
    ct = cfg.get("compute_type", "auto")
    if ct == "auto":
        if device == "cuda":
            gi = gpu_info()
            ct = "float16" if (gi and gi[1] >= 10000) else "int8_float16"
        else:
            ct = "int8"
    return model, device, ct


def local_model_path(name: str) -> str:
    p = MODEL_DIR / name
    if (p / "model.bin").exists():
        return str(p)
    return name  # 交给 faster-whisper 自己从 HuggingFace 下载/缓存


class WhisperASR:
    def __init__(self, cfg: dict, log=print):
        from faster_whisper import WhisperModel

        self.cfg = cfg
        self.model_name, self.device, self.compute_type = choose_model(cfg)
        path = local_model_path(self.model_name)
        log(f"Loading speech model {self.model_name} ({self.device}, {self.compute_type}) …")
        try:
            self.model = WhisperModel(path, device=self.device, compute_type=self.compute_type)
        except Exception as e:
            if self.device != "cuda":
                raise
            log(f"GPU load failed ({e}); falling back to CPU + small model. Re-run setup.bat to fix GPU support.")
            self.device, self.compute_type = "cpu", "int8"
            self.model_name = "small"
            self.model = WhisperModel(local_model_path("small"), device="cpu", compute_type="int8")
        self.beam = int(cfg.get("beam_size", 3))
        # 预热
        self.model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1)
        log("Speech model ready")

    def detect(self, audio: np.ndarray, allowed: list[str]) -> str:
        """在允许的语言中挑最可能的一个（例如只在 中/法 之间判断）。"""
        try:
            lang, prob, all_probs = self.model.detect_language(audio[: 16000 * 30])
            probs = {k: v for k, v in all_probs if k in allowed}
            if probs:
                return max(probs, key=probs.get)
            return lang if lang in allowed else allowed[0]
        except Exception:
            return allowed[0]

    def transcribe(self, audio: np.ndarray, language: str, partial: bool = False,
                   prompt: str | None = None, hotwords: str | None = None) -> "ASRResult":
        if language == "zh":
            prompt = ZH_PROMPT + (prompt or "")
        want_words = not partial
        segments, _info = self.model.transcribe(
            audio,
            language=language,
            beam_size=1 if partial else self.beam,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=not want_words,
            word_timestamps=want_words,
            initial_prompt=prompt or None,
            hotwords=hotwords or None,
            temperature=0.0 if partial else [0.0, 0.2, 0.4],
        )
        parts, words, logps = [], [], []
        for s in segments:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -0.8:
                continue
            if s.avg_logprob < -1.2:
                continue
            parts.append(s.text.strip())
            logps.append(s.avg_logprob)
            for w in (s.words or []):
                words.append({"w": w.word, "p": round(float(w.probability), 3)})
        text = (" " if language != "zh" else "").join(p for p in parts if p).strip()
        text = clean_text(text)
        res = ASRResult(text=text, lang=language, words=words if text else [])
        if text and words:
            conf, geo, mn = seq_confidence([w["p"] for w in words if w["w"].strip(" .,!?;:。，！？")])
            res.confidence = None if conf is None else round(conf, 3)
            res.geo_prob = None if geo is None else round(geo, 3)
            res.weak_words = [w for w in sorted(words, key=lambda w: w["p"])[:3] if w["p"] < 0.6]
        if logps:
            res.avg_logprob = round(sum(logps) / len(logps), 3)
        return res


@dataclass
class ASRResult:
    text: str
    lang: str = ""
    confidence: float | None = None   # 0~1，识别置信度
    geo_prob: float | None = None
    avg_logprob: float | None = None
    words: list = field(default_factory=list)       # [{w, p}] 每个词听得多清楚
    weak_words: list = field(default_factory=list)  # 最可能听错的词

    def __bool__(self):
        return bool(self.text)


def clean_text(text: str) -> str:
    low = text.lower()
    if len(text) < 80 and any(h in low for h in HALLUCINATIONS):
        return ""
    # 去掉重复刷屏（如 “谢谢谢谢谢谢…”）
    text = re.sub(r"(.{2,12}?)\1{3,}", r"\1", text)
    if len(re.sub(r"[\W_]", "", text)) == 0:
        return ""
    return text
