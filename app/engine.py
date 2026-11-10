"""把各模块串起来：音频 → 分句 → 识别 → 翻译 → 朗读，事件推送给界面。"""
from __future__ import annotations

import itertools
import queue
import re
import threading
import time
import traceback
from typing import Callable

import numpy as np

from .audio import LoopbackSource, MicSource, play_audio
from .logbook import Logbook
from .vad import Segmenter, SegmenterConfig

CJK = re.compile(r"[一-鿿]")


def overall_confidence(card: dict) -> float | None:
    """整句置信度 = 听对的把握 × 翻对的把握（两步都要对，这句话才算对）。"""
    a = (card.get("asr") or {}).get("confidence")
    m = (card.get("mt") or {}).get("confidence")
    vals = [v for v in (a, m) if v is not None]
    if not vals:
        return None
    out = 1.0
    for v in vals:
        out *= v
    return round(out, 3)


class Engine:
    def __init__(self, cfg: dict, emit: Callable[[dict], None]):
        self.cfg = cfg
        self.emit = emit
        self.asr = None
        self.translator = None
        self.tts = None
        self.ready = False
        self.status = "Starting…"
        self.translator_ok = False

        self.source = None
        self.running = False
        self.session = 0
        self.opts: dict = {}
        self.audio_q: queue.Queue = queue.Queue(maxsize=400)
        self.final_q: queue.Queue = queue.Queue()
        self.trans_q: queue.Queue = queue.Queue()
        self.tts_q: queue.Queue = queue.Queue()
        self.partial_slot = None
        self.partial_lock = threading.Lock()
        self.seg_lang: dict[str, str] = {}
        self.speaking = False
        self.speaking_until = 0.0
        self.text_ids = itertools.count(1)
        self.logbook = Logbook(cfg.get("logbook", {}))
        self.pending: dict[str, dict] = {}   # sid → 正在生成的属性卡
        self.my_lang = cfg.get("my_lang", "zh")
        self.other_lang = cfg.get("other_lang", "fr")

        for target in (self._asr_loop, self._trans_loop, self._tts_loop):
            threading.Thread(target=target, daemon=True).start()

    # ------------------------------------------------------------------ 加载
    def log(self, msg: str):
        print(msg, flush=True)
        self.status = msg
        self.emit({"type": "status", "text": msg, "ready": self.ready})

    def load(self):
        from .asr import WhisperASR
        from .translate import Translator
        from .tts import make_tts

        try:
            self.translator = Translator(self.cfg.get("translator", {}), self.log)
            ok, msg = self.translator.check()
            self.translator_ok = ok
            if not ok:
                self.emit({"type": "error", "text": msg})
            self.asr = WhisperASR(self.cfg.get("asr", {}), self.log)
            self.tts = make_tts(self.cfg.get("tts", {}), self.log)
            if ok:
                self.log("Warming up the translation model (first time can take ~15 s)…")
                self.translator.warmup()
            self.ready = True
            parts = [f"ASR: {self.asr.model_name} · {self.asr.device}"]
            parts.append(f"MT: {msg if ok else 'not connected'}")
            parts.append("TTS: " + ("on" if self.tts else "unavailable"))
            self.log("✅ Ready — " + " | ".join(parts))
        except Exception as e:
            traceback.print_exc()
            self.log(f"❌ Startup failed: {e}")
            self.emit({"type": "error", "text": f"Startup failed: {e}"})

    # ------------------------------------------------------------------ 控制
    def start(self, opts: dict):
        if not self.ready:
            raise RuntimeError("Models are still loading, please wait")
        self.stop()
        self.session += 1
        self.opts = {
            "mode": opts.get("mode", "auto"),        # auto 对话 / listen 只听 / speak 我说
            "source": opts.get("source", "mic"),     # mic / system
            "device": opts.get("device"),
            "listen_lang": opts.get("listen_lang", "auto"),
            "speak": bool(opts.get("speak", True)),
        }
        self.other_lang = opts.get("other_lang", self.other_lang)
        acfg = self.cfg.get("vad", {})
        seg_cfg = SegmenterConfig(
            threshold=float(acfg.get("threshold", 0.5)),
            min_silence_ms=int(acfg.get("min_silence_ms", 600)),
            min_speech_ms=int(acfg.get("min_speech_ms", 300)),
            max_segment_s=float(acfg.get("max_segment_s", 12)),
        )
        self.segmenter = Segmenter(seg_cfg)
        self.audio_q = queue.Queue(maxsize=400)
        if self.opts["source"] == "system":
            self.source = LoopbackSource(self._on_audio)
        else:
            self.source = MicSource(self._on_audio, self.opts["device"])
        name = self.source.start()
        self.running = True
        threading.Thread(target=self._vad_loop, args=(self.session,), daemon=True).start()
        self.emit({"type": "running", "running": True, "device": name, "opts": self.opts,
                   "other_lang": self.other_lang})

    def stop(self):
        was = self.running
        self.running = False
        if self.source is not None:
            self.source.stop()
            self.source = None
        with self.partial_lock:
            self.partial_slot = None
        if was:
            self.emit({"type": "running", "running": False})

    def _on_audio(self, chunk: np.ndarray):
        try:
            self.audio_q.put_nowait(chunk)
        except queue.Full:
            pass

    def _muted(self) -> bool:
        if not self.cfg.get("mute_while_speaking", True):
            return False
        return self.speaking or time.time() < self.speaking_until

    # ------------------------------------------------------------------ 线程：分句
    def _vad_loop(self, session: int):
        last_level = 0.0
        was_muted = False
        while self.running and session == self.session:
            try:
                chunk = self.audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            now = time.time()
            if now - last_level > 0.1:
                last_level = now
                rms = float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0
                self.emit({"type": "level", "v": min(1.0, rms * 8),
                           "speech": bool(self.segmenter.in_speech)})
            if self._muted():
                if not was_muted:
                    # 朗读期间不收音，避免把自己的翻译又识别一遍
                    for ev in self.segmenter.flush():
                        if ev.kind == "final":
                            self._push_final(session, ev)
                    self.segmenter.reset()
                was_muted = True
                continue
            was_muted = False
            for ev in self.segmenter.feed(chunk):
                sid = f"s{session}-{ev.seg_id}"
                if ev.kind == "start":
                    self.emit({"type": "speech_start", "id": sid})
                elif ev.kind == "partial":
                    with self.partial_lock:
                        self.partial_slot = (sid, ev.audio)
                elif ev.kind == "final":
                    self._push_final(session, ev)
                elif ev.kind == "cancel":
                    with self.partial_lock:
                        self.partial_slot = None
                    self.emit({"type": "cancel", "id": sid})

    def _push_final(self, session, ev):
        sid = f"s{session}-{ev.seg_id}"
        with self.partial_lock:
            if self.partial_slot and self.partial_slot[0] == sid:
                self.partial_slot = None
        self.final_q.put((sid, ev.audio, dict(self.opts)))

    # ------------------------------------------------------------------ 线程：识别
    def _lang_for(self, sid: str, audio: np.ndarray, opts: dict) -> str:
        mode = opts.get("mode", "auto")
        if mode == "speak":
            return self.my_lang
        if mode == "listen":
            ll = opts.get("listen_lang", "auto")
            if ll != "auto":
                return ll
            allowed = [self.other_lang] + [l for l in ("fr", "en") if l != self.other_lang]
        else:
            # conversation mode: Chinese, the target language, and English (people often switch to English)
            allowed = [self.my_lang, self.other_lang] + (["en"] if self.other_lang != "en" else [])
        if sid in self.seg_lang and len(audio) < 16000 * 3:
            return self.seg_lang[sid]
        lang = self.asr.detect(audio, allowed)
        self.seg_lang[sid] = lang
        if len(self.seg_lang) > 200:
            self.seg_lang = dict(list(self.seg_lang.items())[-50:])
        return lang

    def _partials_enabled(self) -> bool:
        p = self.cfg.get("asr", {}).get("partial_results", "auto")
        if p == "auto":
            return self.asr is not None and self.asr.device == "cuda"
        return bool(p)

    def _asr_loop(self):
        while True:
            try:
                sid, audio, opts = self.final_q.get(timeout=0.05)
            except queue.Empty:
                if self.asr is None or not self._partials_enabled():
                    continue
                with self.partial_lock:
                    item, self.partial_slot = self.partial_slot, None
                if item is None:
                    continue
                sid, audio = item
                try:
                    if len(audio) < 16000 * 0.6:
                        continue
                    lang = self._lang_for(sid, audio, self.opts)
                    res = self.asr.transcribe(audio, lang, partial=True)
                    if res:
                        self.emit({"type": "partial", "id": sid, "text": res.text, "lang": lang})
                except Exception as e:
                    print(f"[asr-partial] {e}")
                continue
            try:
                t0 = time.time()
                lang = self._lang_for(sid, audio, opts)
                res = self.asr.transcribe(audio, lang, hotwords=self.cfg.get("asr", {}).get("hotwords"))
                self.seg_lang.pop(sid, None)
                if not res:
                    self.emit({"type": "cancel", "id": sid})
                    continue
                asr_ms = int((time.time() - t0) * 1000)
                who = "me" if lang == self.my_lang else "them"
                rid = self.logbook.new_rid()
                card = {
                    "rid": rid, "ts": time.time(), "session": self.session,
                    "source": opts.get("source", "mic"), "mode": opts.get("mode"),
                    "who": who, "src_lang": lang, "src_text": res.text,
                    "asr": {
                        "model": self.asr.model_name, "confidence": res.confidence,
                        "geo_prob": res.geo_prob, "avg_logprob": res.avg_logprob,
                        "weak_words": res.weak_words, "words": res.words,
                        "dur_s": round(len(audio) / 16000, 2), "ms": asr_ms,
                    },
                    "audio": self.logbook.save_clip(rid, audio),
                }
                self.pending[sid] = card
                self.emit({"type": "final", "id": sid, "rid": rid, "text": res.text, "lang": lang,
                           "who": who, "asr_conf": res.confidence, "weak_words": res.weak_words,
                           "asr_ms": asr_ms, "dur": card["asr"]["dur_s"]})
                tgt = self.other_lang if lang == self.my_lang else self.my_lang
                speak = (lang == self.my_lang and opts.get("mode") != "listen"
                         and opts.get("speak", True))
                self.trans_q.put((sid, res.text, lang, tgt, speak))
            except Exception as e:
                traceback.print_exc()
                self.emit({"type": "error", "text": f"Speech recognition error: {e}"})

    # ------------------------------------------------------------------ 线程：翻译
    def translate_text(self, text: str, speak: bool = True) -> str:
        text = text.strip()
        if not text:
            return ""
        tid = f"t{next(self.text_ids)}"
        src = self.my_lang if CJK.search(text) else self.other_lang
        tgt = self.other_lang if src == self.my_lang else self.my_lang
        who = "me" if src == self.my_lang else "them"
        rid = self.logbook.new_rid()
        self.pending[tid] = {"rid": rid, "ts": time.time(), "session": self.session,
                             "source": "text", "mode": "text", "who": who,
                             "src_lang": src, "src_text": text, "asr": None, "audio": None}
        self.emit({"type": "final", "id": tid, "rid": rid, "text": text, "lang": src, "who": who,
                   "typed": True})
        self.trans_q.put((tid, text, src, tgt, speak and src == self.my_lang))
        return tid

    def _trans_loop(self):
        while True:
            sid, text, src, tgt, speak = self.trans_q.get()
            if self.translator is None or not self.translator_ok:
                card = self.pending.pop(sid, None)
                if card:
                    card.update({"tgt_lang": tgt, "tgt_text": "", "error": "Translation model not connected"})
                    self.logbook.append(card)
                self.emit({"type": "trans_done", "id": sid, "text": "", "error":
                           "Translation model not connected (start Ollama, then restart run.bat)"})
                continue
            card = self.pending.pop(sid, None) or {
                "rid": self.logbook.new_rid(), "ts": time.time(), "who": "me" if src == self.my_lang else "them",
                "src_lang": src, "src_text": text, "asr": None, "audio": None}
            try:
                n_ctx = min(len(self.translator.history), self.translator.context_turns)
                r = self.translator.translate(
                    text, src, tgt,
                    on_delta=lambda d, sid=sid: self.emit({"type": "trans_delta", "id": sid, "delta": d}),
                )
                card.update({
                    "tgt_lang": tgt, "tgt_text": r.text, "spoken": bool(speak and r.text and self.tts),
                    "mt": {"model": r.model, "style": self.translator.style,
                           "confidence": r.confidence, "geo_prob": r.geo_prob, "min_prob": r.min_prob,
                           "weak_tokens": r.weak_tokens, "n_tokens": r.n_tokens,
                           "context_turns": n_ctx,
                           "first_token_s": round(r.first_token_s, 3), "total_s": round(r.total_s, 3)},
                })
                card["confidence"] = overall_confidence(card)
                self.logbook.append(card)
                self.emit({"type": "trans_done", "id": sid, "text": r.text, "lang": tgt,
                           "mt_ms": int(r.total_s * 1000), "conf": card["confidence"], "card": card})
                if speak and r.text and self.tts:
                    self.tts_q.put((r.text, tgt, sid))
            except Exception as e:
                traceback.print_exc()
                card.update({"tgt_lang": tgt, "tgt_text": "", "error": str(e)})
                self.logbook.append(card)
                self.emit({"type": "trans_done", "id": sid, "text": "", "error": f"Translation error: {e}"})

    # ------------------------------------------------------------------ 线程：朗读
    def speak(self, text: str, lang: str, sid: str = ""):
        if self.tts:
            self.tts_q.put((text, lang, sid))

    def _tts_loop(self):
        while True:
            text, lang, sid = self.tts_q.get()
            try:
                self.speaking = True
                self.emit({"type": "speaking", "id": sid, "on": True})
                out_dev = self.cfg.get("tts", {}).get("output_device")
                if hasattr(self.tts, "synth"):
                    r = self.tts.synth(text, lang)
                    if r is not None:
                        play_audio(r[0], r[1], out_dev)
                else:
                    self.tts.speak_blocking(text, lang)
            except Exception as e:
                traceback.print_exc()
                self.emit({"type": "error", "text": f"Text-to-speech error: {e}"})
            finally:
                self.speaking = False
                self.speaking_until = time.time() + 0.5
                self.emit({"type": "speaking", "id": sid, "on": False})
