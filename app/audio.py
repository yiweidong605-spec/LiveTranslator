"""音频输入：麦克风 / 电脑系统声音（用于视频面试，听 Teams/Zoom 里对方说话）。

所有输入统一转换成 16kHz 单声道 float32，回调给上层。
"""
from __future__ import annotations

import sys
import threading
import time
import warnings
from typing import Callable

import numpy as np

SR = 16000


def _make_resampler(in_rate: int):
    if in_rate == SR:
        return lambda x: x
    try:
        import soxr

        rs = soxr.ResampleStream(in_rate, SR, 1, dtype="float32", quality="HQ")
        return lambda x: rs.resample_chunk(x)
    except Exception:
        ratio = SR / in_rate

        def lin(x: np.ndarray) -> np.ndarray:
            n = int(round(len(x) * ratio))
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            return np.interp(
                np.linspace(0, len(x) - 1, n), np.arange(len(x)), x
            ).astype(np.float32)

        return lin


def list_input_devices() -> list[dict]:
    out = []
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        default_in = sd.default.device[0]
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                api = hostapis[d["hostapi"]]["name"]
                # Windows 上同一设备会在多个驱动接口重复出现，只保留 MME 和 WASAPI
                if sys.platform == "win32" and not any(k in api for k in ("MME", "WASAPI")):
                    continue
                out.append(
                    {"id": i, "name": f"{d['name']} ({api})", "default": i == default_in}
                )
    except Exception as e:
        print(f"[audio] cannot list microphones: {e}")
    return out


def list_output_devices() -> list[dict]:
    out = []
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        default_out = sd.default.device[1]
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                api = hostapis[d["hostapi"]]["name"]
                if sys.platform == "win32" and "MME" not in api:
                    continue
                out.append({"id": i, "name": d["name"], "default": i == default_out})
    except Exception as e:
        print(f"[audio] cannot list speakers: {e}")
    return out


class MicSource:
    def __init__(self, on_audio: Callable[[np.ndarray], None], device=None):
        self.on_audio = on_audio
        self.device = None if device in (None, "", "default") else int(device)
        self.stream = None

    def start(self):
        import sounddevice as sd

        info = sd.query_devices(self.device, "input")
        rate = int(info["default_samplerate"]) or 48000
        resample = _make_resampler(rate)

        def cb(indata, frames, t, status):
            x = indata[:, 0] if indata.ndim > 1 else indata
            y = resample(np.ascontiguousarray(x, dtype=np.float32))
            if len(y):
                self.on_audio(y)

        self.stream = sd.InputStream(
            device=self.device,
            channels=1,
            samplerate=rate,
            dtype="float32",
            blocksize=int(rate * 0.05),
            callback=cb,
        )
        self.stream.start()
        return info["name"]

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


class LoopbackSource:
    """录制电脑正在播放的声音（WASAPI loopback），视频面试时用来听对方。"""

    def __init__(self, on_audio: Callable[[np.ndarray], None]):
        self.on_audio = on_audio
        self.running = False
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.name = ""

    def start(self):
        ready = threading.Event()
        self.running = True
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self.thread.start()
        ready.wait(5)
        if self.error:
            raise RuntimeError(self.error)
        return self.name

    def _run(self, ready: threading.Event):
        try:
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.ole32.CoInitializeEx(None, 0)
            import soundcard as sc

            warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
            self.name = f"System audio: {spk.name}"
            rate = 48000
            resample = _make_resampler(rate)
            with mic.recorder(samplerate=rate, blocksize=1024) as rec:
                ready.set()
                while self.running:
                    data = rec.record(numframes=int(rate * 0.05))
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    y = resample(np.ascontiguousarray(data, dtype=np.float32))
                    if len(y):
                        self.on_audio(y)
        except Exception as e:
            self.error = f"Cannot capture system audio: {e}"
            print(f"[audio] {self.error}")
            ready.set()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None


def play_audio(audio: np.ndarray, sr: int, device=None):
    import sounddevice as sd

    dev = None if device in (None, "", "default") else int(device)
    sd.play(audio.astype(np.float32), sr, device=dev, blocking=True)
