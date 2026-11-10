"""Make a small zip to share with friends: code + config + eval set only.

Excludes everything setup.bat downloads again (env, models, ollama, caches)
and all personal data (conversation logs, recordings, eval runs, MLflow).
Output: dist/LiveTranslator-YYYYMMDD.zip  (about 100 KB)
"""
from __future__ import annotations

import fnmatch
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ignore_patterns() -> list[str]:
    pats = [".git/"]
    for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line)
    return pats


def ignored(rel: str, pats: list[str]) -> bool:
    for p in pats:
        if p.endswith("/"):
            if rel == p[:-1] or rel.startswith(p):
                return True
        elif fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p):
            return True
    return False


def main():
    pats = ignore_patterns()
    out = ROOT / "dist" / f"LiveTranslator-{date.today():%Y%m%d}.zip"
    out.parent.mkdir(exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if ignored(rel, pats):
                continue
            z.write(p, f"LiveTranslator/{rel}")
            n += 1
    print(f"Packed {n} files -> {out} ({out.stat().st_size / 1024:.0f} KB)")
    print("Your friend unzips it anywhere, double-clicks setup.bat, then run.bat.")


if __name__ == "__main__":
    main()
