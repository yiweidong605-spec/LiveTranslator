# Changelog

## v0.1.0 — 2026-11-14

First tagged version: the offline translator, the evidence for its model, and the platform around it.

**Translator**
- Offline real-time zh ⇄ fr/en: Silero VAD → faster-whisper large-v3-turbo → Ollama → Piper; web UI with
  face-to-face, listen-only (system audio, video calls) and speak modes.
- Production model **qwen3:14b** (best of 8 in the 2026-11-06 evaluation); setup wizard recommends by VRAM.
- Prompt **v1** in production. v2 (slang by meaning) is kept for comparison: the seeded A/B test showed no
  overall gain and regressions on context-dependent sentences.
- Per-sentence confidence cards; WeChat-style review log with corrections, notes, stars, ignore and a
  "To review" list.

**Evaluation**
- 246-sentence test set (v1.1), chrF/BLEU, keyword and ambiguity checks, bootstrap CIs, MLflow tracking.
- Prompt versions + fixed seeds + `eval.bat` option 9 (A/B with seed-to-seed noise, paired bootstrap,
  consistent per-sentence flips); a test keeps prompt examples out of the test set.

**Platform** (Ubuntu mini PC, Docker Compose, Tailscale only)
- PostgreSQL 17, RustFS, MLflow ≥ 3.15, Airflow 3.4.
- DAGs: `platform_healthcheck` (hourly, four internet probes, backup age), `logbook_daily`
  (asset-triggered bronze → silver → gold).
- `sync.bat` with least-privilege accounts; restic backups (daily, 3-2-1, pull to desktop + USB).
- CI (lint, shellcheck, gitleaks, 25 unit tests, DAG integrity) and pull-based CD with rollback.
- Read-only security audit script.
