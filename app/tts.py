"""语音合成（把译文读出来）：
  - piper：离线神经网络 TTS，法语/英语发音自然（推荐）
  - system：Windows 自带语音（需要在系统设置里安装法语语音包）
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PIPER_DIR = ROOT / "models" / "piper"


class PiperTTS:
    def __init__(self, voices: dict, speed: float = 1.0, log=print):
        from piper import PiperVoice

        self.voices = {}
        self.speed = speed
        for lang, name in voices.items():
            path = PIPER_DIR / f"{name}.onnx"
            if path.exists():
                self.voices[lang] = PiperVoice.load(str(path))
                log(f"TTS voice loaded: {name}")
            else:
                log(f"Voice {path.name} not found — run setup.bat to download it")
        if not self.voices:
            raise RuntimeError("No Piper voice available")

    def synth(self, text: str, lang: str) -> tuple[np.ndarray, int] | None:
        voice = self.voices.get(lang)
        if voice is None:
            return None
        from piper import SynthesisConfig

        cfg = SynthesisConfig(length_scale=1.0 / max(self.speed, 0.3))
        chunks = list(voice.synthesize(text, syn_config=cfg))
        if not chunks:
            return None
        sr = chunks[0].sample_rate
        audio = np.concatenate([c.audio_float_array for c in chunks])
        gap = np.zeros(int(sr * 0.05), dtype=np.float32)
        return np.concatenate([gap, audio.astype(np.float32), gap]), sr


class SystemTTS:
    """调用 Windows 自带的 System.Speech（零依赖、离线）。"""

    CULTURE = {"fr": "fr", "en": "en", "zh": "zh"}

    def __init__(self, speed: float = 1.0, log=print):
        if sys.platform != "win32":
            raise RuntimeError("System voice is Windows-only")
        self.rate = int(max(-10, min(10, round((speed - 1.0) * 10))))
        self.voices = {"fr": True, "en": True}

    def speak_blocking(self, text: str, lang: str):
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$v = $s.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Culture.Name -like '{self.CULTURE.get(lang, 'en')}*' }} | Select-Object -First 1;"
            "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
            f"$s.Rate = {self.rate};"
            "$s.Speak($env:LT_TEXT)"
        )
        env = dict(os.environ, LT_TEXT=text)
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            env=env, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def make_tts(cfg: dict, log=print):
    engine = cfg.get("engine", "piper")
    speed = float(cfg.get("speed", 1.0))
    if engine == "off":
        return None
    if engine == "piper":
        try:
            return PiperTTS(cfg.get("voices", {}), speed, log)
        except Exception as e:
            log(f"Piper unavailable ({e}); trying the Windows system voice")
    try:
        return SystemTTS(speed, log)
    except Exception as e:
        log(f"No text-to-speech available: {e}")
        return None
