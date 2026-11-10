#!/usr/bin/env bash
# One-time setup of the daily backup (safe to re-run). Run as your normal user: bash backup/install-backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."                      # ~/platform
ME="$(id -un)"
[ -f .env ] || { echo "✗ .env missing — run ./gen-env.sh first"; exit 1; }

echo "== 1/5 packages: restic (encrypted backups) + rclone (S3 copy)"
sudo apt-get install -y -qq restic rclone >/dev/null
restic version | head -1; rclone version | head -1

echo "== 2/5 folders"
for d in /srv/backup /srv/backup/restic /srv/backup/staging; do
  # only create: re-running must not undo the read-only sharing set up by provision-backup-reader.sh
  [ -d "$d" ] || sudo install -d -o "$ME" -g "$ME" -m 700 "$d"
done
mkdir -p backup/status
echo "  /srv/backup/restic   repository (encrypted snapshots)"
echo "  /srv/backup/staging  working copy of what goes into each snapshot"

echo "== 3/5 repository"
NEWPW=0
if ! grep -q '^RESTIC_PASSWORD=' .env; then
  echo "RESTIC_PASSWORD=$(openssl rand -base64 33 | tr -d '/+=\n' | cut -c1-40)" >> .env
  NEWPW=1
fi
export RESTIC_REPOSITORY=/srv/backup/restic
RESTIC_PASSWORD="$(grep '^RESTIC_PASSWORD=' .env | cut -d= -f2-)"; export RESTIC_PASSWORD
if restic cat config >/dev/null 2>&1; then echo "  repository already initialised"; else restic init; fi

echo "== 4/5 systemd timer (every day 08:00; catches up after the mini PC was off)"
sed "s#@USER@#$ME#g; s#@PLATFORM@#$PWD#g" backup/lt-backup.service | sudo tee /etc/systemd/system/lt-backup.service >/dev/null
sudo install -m 644 backup/lt-backup.timer /etc/systemd/system/lt-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now lt-backup.timer >/dev/null
systemctl list-timers lt-backup.timer --no-pager | head -2

echo "== 5/5 first backup (runs exactly like the daily one)"
if sudo systemctl start lt-backup.service; then
  journalctl -u lt-backup.service -n 25 --no-pager -o cat | sed -n '/== 1\/5/,$p'
else
  journalctl -u lt-backup.service -n 40 --no-pager -o cat
  echo "✗ first backup failed — send the lines above"; exit 1
fi

if [ "$NEWPW" = 1 ]; then
  cat <<MSG

!!! IMPORTANT: save the backup password in your password manager NOW.
    Without it the backups can never be opened (it is also in ~/platform/.env,
    but if the mini PC dies, that copy dies with it). Show it with:
      grep RESTIC_PASSWORD ~/platform/.env
MSG
fi
