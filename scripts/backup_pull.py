"""Pull backup: copy the platform's restic snapshots from the mini PC to this PC (and to a USB drive).

Called by sync.bat option 1 after the logbook sync; one-time setup is sync.bat option 4.

  mini PC  /srv/backup/restic  (daily 08:00, tag "core": databases + MLflow artifacts + .env)
      │  read-only SFTP as "backup-reader" (cannot write or delete anything there)
      ▼
  this PC  I:\\lt-backup\\restic   retention 7 daily / 8 weekly / 24 monthly
      │  only if a drive with LT-BACKUP-DRIVE.txt in its root is plugged in
      ▼
  USB      X:\\lt-backup\\restic   offline copy — unplug it afterwards and keep it elsewhere

Why pull and not push: the mini PC holds no credentials for this PC, so even a fully compromised
mini PC (think of the ransomware attacks on NAS boxes) cannot delete the copies here.
The conversation logs themselves are not in these snapshots: their original lives on this PC.

Usage:
  env\\python.exe scripts\\backup_pull.py            pull now
  env\\python.exe scripts\\backup_pull.py --setup    one-time setup (ssh key, read-only account, first copy)
  env\\python.exe scripts\\backup_pull.py --check    test access and that it is really read-only
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets" / "backup.env"
TOOLS = ROOT / "tools" / "restic"
RESTIC_VERSION = "0.18.0"
SSH_ALIAS = "swt17-backup"
SSH_KEY = Path.home() / ".ssh" / "lt_backup_ed25519"
MARKER = "LT-BACKUP-DRIVE.txt"
KEEP = ["--keep-daily", "7", "--keep-weekly", "8", "--keep-monthly", "24"]
USB_WARN_DAYS = 14


# ---------------------------------------------------------------- helpers
def load_env() -> dict | None:
    if not SECRETS.exists():
        return None
    env = {}
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    env.setdefault("LT_BACKUP_DIR", r"I:\lt-backup")
    return env


def restic_exe() -> str:
    """tools\\restic\\restic.exe, or restic on PATH, or download the pinned release (checksum-verified)."""
    local = TOOLS / ("restic.exe" if os.name == "nt" else "restic")
    if local.exists():
        return str(local)
    if shutil.which("restic"):
        return shutil.which("restic")
    if os.name != "nt":
        sys.exit("restic not found: install it (apt install restic)")
    base = f"https://github.com/restic/restic/releases/download/v{RESTIC_VERSION}/"
    name = f"restic_{RESTIC_VERSION}_windows_amd64.zip"
    print(f"Downloading restic {RESTIC_VERSION} (one time) …")
    try:
        sums = urllib.request.urlopen(base + "SHA256SUMS", timeout=30).read().decode()
        blob = urllib.request.urlopen(base + name, timeout=120).read()
    except Exception as e:
        sys.exit(f"✗ could not download restic ({e}). Install it with:  winget install restic.restic")
    want = next((l.split()[0] for l in sums.splitlines() if l.strip().endswith(name)), None)
    if want != hashlib.sha256(blob).hexdigest():
        sys.exit("✗ restic download failed its checksum — not installing it")
    TOOLS.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        exe = next(n for n in z.namelist() if n.endswith(".exe"))
        local.write_bytes(z.read(exe))
    print(f"  ✓ {local.relative_to(ROOT)} (SHA-256 verified)")
    return str(local)


def restic(args: list[str], env: dict, src: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    e = dict(os.environ, RESTIC_PASSWORD=env["LT_BACKUP_PASSWORD"])  # one password for all copies
    if src:
        e["RESTIC_FROM_PASSWORD"] = env["LT_BACKUP_PASSWORD"]
    return subprocess.run([restic_exe(), *args], env=e, text=True,
                          capture_output=capture, encoding="utf-8", errors="replace")


def is_repo(path: Path) -> bool:
    return (path / "config").exists()


def latest_snapshot_time(repo: str, env: dict, src: bool = False) -> datetime | None:
    flag = ["--no-lock"] if src else []
    r = restic([*flag, "-r", repo, "snapshots", "--json", "--tag", "core", "--latest", "1"], env, capture=True)
    try:
        snaps = json.loads(r.stdout or "[]")
        return max(datetime.fromisoformat(s["time"][:19]) for s in snaps) if snaps else None
    except Exception:
        return None


def ago(t: datetime | None) -> str:
    if not t:
        return "never"
    h = (datetime.now() - t).total_seconds() / 3600
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} days ago"


def find_usb() -> Path | None:
    if os.name != "nt":
        return None
    for letter in string.ascii_uppercase[3:]:
        root = Path(f"{letter}:\\")
        try:
            if (root / MARKER).exists():
                return root
        except OSError:
            continue
    return None


def read_status(base: Path) -> dict:
    try:
        return json.loads((base / "status.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_status(base: Path, st: dict):
    (base / "status.json").write_text(json.dumps(st, indent=1), encoding="utf-8")


def copy_into(dest: Path, source: str, env: dict, label: str) -> bool:
    """Create dest repo if needed, copy new 'core' snapshots from source, apply retention."""
    remote = source.startswith("sftp:")
    if not is_repo(dest):
        dest.mkdir(parents=True, exist_ok=True)
        print(f"  creating the {label} repository at {dest}")
        r = restic(["-r", str(dest), "init", "--from-repo", source, "--copy-chunker-params"], env, src=True)
        if r.returncode:
            return False
    # --no-lock: the backup-reader account is read-only, so it cannot create lock files on the mini PC.
    # (It also skips locking dest, which only this script ever touches.)
    args = (["--no-lock"] if remote else []) + ["-r", str(dest), "copy", "--from-repo", source, "--tag", "core"]
    r = restic(args, env, src=True, capture=True)
    if r.returncode:
        print((r.stderr or r.stdout).strip()[-800:])
        return False
    restic(["-r", str(dest), "forget", "--group-by", "host,tags", *KEEP, "--prune", "--quiet"], env)
    return True


# ---------------------------------------------------------------- commands
def pull(env: dict) -> int:
    base = Path(env["LT_BACKUP_DIR"])
    repo = base / "restic"
    st = read_status(base) if base.exists() else {}
    print(f"\n== Backup: mini PC → {repo}")
    if not Path(base.anchor).exists():
        print(f"✗ drive {base.anchor} not found — change LT_BACKUP_DIR in secrets\\backup.env")
        return 1
    newest_src = latest_snapshot_time(env["LT_BACKUP_SOURCE"], env, src=True)
    if newest_src is None:
        print("✗ cannot read the mini PC's backups (is it on? Tailscale connected?). Try sync.bat → 3.")
        return 1
    if not copy_into(repo, env["LT_BACKUP_SOURCE"], env, "desktop"):
        print("✗ copying the backup failed (see above)")
        return 1
    st["last_pull"] = time.time()
    n = len(json.loads(restic(["-r", str(repo), "snapshots", "--json"], env, capture=True).stdout or "[]"))
    print(f"✓ {n} snapshot(s) on this PC · newest backup made {ago(newest_src)}")
    if (datetime.now() - newest_src).total_seconds() > 36 * 3600:
        print("⚠ the mini PC has not made a backup for more than 36 h — check: journalctl -u lt-backup -n 50")

    usb = find_usb()
    if usb:
        print(f"== USB drive {usb}: copying …")
        if copy_into(usb / "lt-backup" / "restic", str(repo), env, "USB"):
            st["last_usb"] = time.time()
            print(f"✓ copied to {usb} — now unplug the drive and keep it somewhere else")
        else:
            print(f"✗ copying to the USB drive {usb} failed")
    else:
        last = st.get("last_usb")
        days = (time.time() - last) / 86400 if last else None
        if days is None:
            print(f"  USB backup drive: never used (put an empty file {MARKER} in its root, plug it in, sync)")
        elif days > USB_WARN_DAYS:
            print(f"⚠ USB backup drive: last copy {days:.0f} days ago — plug it in on your next sync")
        else:
            print(f"  USB backup drive: last copy {days:.0f} day(s) ago")
    write_status(base, st)
    return 0


def check(env: dict) -> int:
    print(f"\n== Backup access ({SSH_ALIAS}, read-only)")
    ok = True
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_ALIAS, "id"], capture_output=True, text=True, timeout=30)
    if "sftp connections only" in (r.stdout + r.stderr):
        print("  ✓ no shell for backup-reader (SFTP only)")
    else:
        print(f"  ✗ unexpected answer to a shell login: {(r.stdout + r.stderr).strip()[:200]}")
        ok = False
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "probe.txt"
        f.write_text("x")
        batch = Path(tmp) / "batch"
        repo_path = env["LT_BACKUP_SOURCE"].split(":", 2)[-1]
        batch.write_text(f"put {f.as_posix()} {repo_path}/locks/probe\n")
        r = subprocess.run(["sftp", "-b", str(batch), SSH_ALIAS], capture_output=True, text=True, timeout=30)
        if r.returncode != 0 and "denied" in (r.stdout + r.stderr).lower():
            print("  ✓ cannot write to the mini PC's repository (read-only works)")
        else:
            print("  ✗ writing was NOT refused — re-run setup (sync.bat → 4)")
            ok = False
    t = latest_snapshot_time(env["LT_BACKUP_SOURCE"], env, src=True)
    print(f"  {'✓' if t else '✗'} can read the snapshots · newest made {ago(t)}")
    st = read_status(Path(env["LT_BACKUP_DIR"]))
    print(f"  this PC: last copy {ago(datetime.fromtimestamp(st['last_pull'])) if st.get('last_pull') else 'never'}"
          f" · USB: {ago(datetime.fromtimestamp(st['last_usb'])) if st.get('last_usb') else 'never'}")
    return 0 if ok and t else 1


def ensure_ssh_alias():
    cfg = Path.home() / ".ssh" / "config"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    if f"Host {SSH_ALIAS}" in text:
        return
    block = (f"\n# read-only account for the pull backup (LiveTranslator sync.bat)\n"
             f"Host {SSH_ALIAS}\n    HostName swt17\n    HostKeyAlias swt17\n    User backup-reader\n"
             f"    IdentityFile ~/.ssh/{SSH_KEY.name}\n    IdentitiesOnly yes\n")
    with open(cfg, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"  ✓ added 'Host {SSH_ALIAS}' to {cfg}")


def setup() -> int:
    print("== 1/6 restic")
    print("  using", restic_exe())
    print("== 2/6 ssh key for the read-only account")
    SSH_KEY.parent.mkdir(exist_ok=True)
    if not SSH_KEY.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lt-backup-reader",
                        "-f", str(SSH_KEY)], check=True)
        print(f"  ✓ created {SSH_KEY}")
    ensure_ssh_alias()
    pub = SSH_KEY.with_suffix(".pub").read_text().strip()

    print("== 3/6 creating the read-only account on the mini PC (type your mini PC password for sudo)")
    r = subprocess.run(["ssh", "-t", "swt17",
                        f"cd ~/platform && sudo bash backup/provision-backup-reader.sh '{pub}'"])
    if r.returncode:
        print("✗ setup on the mini PC failed (see above). Did you copy the platform folder first?")
        return 1
    print("== 4/6 fetching the settings")
    SECRETS.parent.mkdir(exist_ok=True)
    old = load_env() or {}
    if subprocess.run(["scp", "-q", "swt17:platform/backup-credentials.env", str(SECRETS)]).returncode:
        return 1
    subprocess.run(["ssh", "swt17", "rm -f ~/platform/backup-credentials.env"])
    default = old.get("LT_BACKUP_DIR", r"I:\lt-backup")
    where = input(f"  Where should the backups go on this PC? [{default}]: ").strip() or default
    with open(SECRETS, "a", encoding="utf-8") as f:
        f.write(f"LT_BACKUP_DIR={where}\n")
    env = load_env()

    print("== 5/6 checking access")
    if check(env):
        return 1
    print("== 6/6 USB drive (optional)")
    letter = input("  If your USB backup drive is plugged in, type its letter (e.g. F), or press Enter: ").strip()
    if letter:
        root = Path(f"{letter.rstrip(':').upper()}:\\")
        if root.exists():
            (root / MARKER).write_text("This drive receives LiveTranslator platform backups (sync.bat).\n",
                                       encoding="utf-8")
            print(f"  ✓ marked {root} as the backup drive")
        else:
            print(f"  ✗ {root} not found, skipped")
    return pull(env)


def main():
    ap = argparse.ArgumentParser(description="Pull the platform backup to this PC")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.setup:
        sys.exit(setup())
    env = load_env()
    if env is None:
        print("\nBackups are not set up on this PC yet: sync.bat → 4 (one time).")
        sys.exit(0)
    sys.exit(check(env) if args.check else pull(env))


if __name__ == "__main__":
    main()
