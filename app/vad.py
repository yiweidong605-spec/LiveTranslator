"""流式语音活动检测（VAD）+ 分句。

使用 faster-whisper 自带的 Silero VAD v6 模型（无需额外下载），
每 32ms (512 个采样点 @16kHz) 判断一次是否有人声，
检测到一句话结束（停顿）时把整句音频交给识别模块。
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass

import numpy as np

SR = 16000
FRAME = 512  # 32 ms
CONTEXT = 64


class SileroStream:
    """有状态的 Silero VAD，逐帧输出人声概率。"""

    def __init__(self):
        import onnxruntime
        from faster_whisper.utils import get_assets_path

        path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.sess = onnxruntime.InferenceSession(
            path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.ctx, frame]).astype(np.float32)[None, :]
        out, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = frame[-CONTEXT:].copy()
        return float(np.asarray(out).reshape(-1)[0])


class EnergyVAD:
    """备用：简单能量阈值（Silero 加载失败时使用）。"""

    def __init__(self):
        self.noise = 0.003

    def reset(self):
        pass

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(frame**2)) + 1e-9)
        self.noise = 0.995 * self.noise + 0.005 * min(rms, self.noise * 3)
        return float(np.clip((rms / (self.noise * 4) - 0.5), 0, 1))


@dataclass
class SegmenterConfig:
    threshold: float = 0.5
    min_silence_ms: int = 600
    min_speech_ms: int = 300
    max_segment_s: float = 12.0
    pre_roll_ms: int = 300
    partial_interval_s: float = 0.8


@dataclass
class SegEvent:
    kind: str  # "start" | "partial" | "final"
    audio: np.ndarray | None = None
    seg_id: int = 0


class Segmenter:
    """把连续音频流切成一句一句。feed() 返回事件列表。"""

    def __init__(self, cfg: SegmenterConfig | None = None, vad=None):
        self.cfg = cfg or SegmenterConfig()
        if vad is None:
            try:
                vad = SileroStream()
            except Exception as e:  # pragma: no cover
                print(f"[VAD] Silero failed to load, using energy detection: {e}")
                vad = EnergyVAD()
        self.vad = vad
        self.seg_counter = 0
        self.reset()

    # ------------------------------------------------------------------
    def reset(self):
        self.vad.reset()
        self.pending = np.zeros(0, dtype=np.float32)
        pre = max(1, int(self.cfg.pre_roll_ms / 32))
        self.pre_roll: deque[np.ndarray] = deque(maxlen=pre)
        self.in_speech = False
        self.speech: list[np.ndarray] = []
        self.speech_frames = 0
        self.silence_frames = 0
        self.voiced_frames = 0
        self.frames_since_partial = 0
        self.last_prob = 0.0

    def _finish(self, events: list[SegEvent], trim_silence: bool):
        frames = self.speech
        if trim_silence and self.silence_frames > 6:
            keep = len(frames) - (self.silence_frames - 6)  # 保留约 200ms 尾音
            frames = frames[: max(keep, 1)]
        voiced_ms = self.voiced_frames * 32
        if voiced_ms >= self.cfg.min_speech_ms and frames:
            events.append(SegEvent("final", np.concatenate(frames), self.seg_counter))
        else:
            events.append(SegEvent("cancel", None, self.seg_counter))
        self.in_speech = False
        self.speech = []
        self.speech_frames = 0
        self.silence_frames = 0
        self.voiced_frames = 0
        self.frames_since_partial = 0

    def feed(self, audio: np.ndarray) -> list[SegEvent]:
        events: list[SegEvent] = []
        if audio.size == 0:
            return events
        self.pending = np.concatenate([self.pending, audio.astype(np.float32)])
        n = len(self.pending) // FRAME
        if n == 0:
            return events
        frames = self.pending[: n * FRAME].reshape(n, FRAME)
        self.pending = self.pending[n * FRAME :]

        c = self.cfg
        max_frames = int(c.max_segment_s * 1000 / 32)
        sil_needed = max(1, int(c.min_silence_ms / 32))
        partial_every = max(1, int(c.partial_interval_s * 1000 / 32))
        off_thr = max(c.threshold - 0.15, 0.05)

        for fr in frames:
            p = self.vad(fr)
            self.last_prob = p
            if not self.in_speech:
                self.pre_roll.append(fr)
                if p >= c.threshold:
                    self.in_speech = True
                    self.seg_counter += 1
                    self.speech = list(self.pre_roll)
                    self.pre_roll.clear()
                    self.speech_frames = len(self.speech)
                    self.voiced_frames = 1
                    self.silence_frames = 0
                    self.frames_since_partial = 0
                    events.append(SegEvent("start", None, self.seg_counter))
                continue

            # 正在说话
            self.speech.append(fr)
            self.speech_frames += 1
            self.frames_since_partial += 1
            if p >= off_thr:
                self.voiced_frames += 1
                self.silence_frames = 0
            else:
                self.silence_frames += 1

            if self.silence_frames >= sil_needed:
                self._finish(events, trim_silence=True)
            elif self.speech_frames >= max_frames:
                self._finish(events, trim_silence=False)
                # 长句被切断后立即开始新的一段（对方还在说）
                self.in_speech = True
                self.seg_counter += 1
                self.voiced_frames = 1
                events.append(SegEvent("start", None, self.seg_counter))
            elif self.frames_since_partial >= partial_every:
                self.frames_since_partial = 0
                events.append(
                    SegEvent("partial", np.concatenate(self.speech), self.seg_counter)
                )
        return events

    def flush(self) -> list[SegEvent]:
        events: list[SegEvent] = []
        if self.in_speech:
            self._finish(events, trim_silence=True)
        return events
