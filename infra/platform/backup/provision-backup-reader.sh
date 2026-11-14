#!/usr/bin/env bash
# Give the desktop READ-ONLY access to the backup repository, for the pull backup (sync.bat).
#   sudo bash backup/provision-backup-reader.sh "<desktop public key>"
# (sync.bat → "Set up backups" runs this for you over ssh.)
#
#   user  backup-reader   no password, no shell, no home, key login only
#   ssh   ForceCommand internal-sftp -R   → read-only SFTP, nothing else (no shell, no tunnels)
#   files repository group-readable by "ltbackup"; staging (has .env in clear) stays owner-only
#
# So the desktop can copy snapshots but can never change or delete anything on the mini PC,
# and the mini PC holds no credentials for the desktop: a compromised mini PC cannot reach the
# desktop copy. Safe to re-run (replaces the key).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
PUBKEY="${1:-}"
[[ "$PUBKEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+ ]] || { echo "usage: sudo bash $0 \"ssh-ed25519 AAAA… comment\""; exit 1; }
OWNER="${SUDO_USER:-swt17}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
PLATFORM="$OWNER_HOME/platform"
REPO=/srv/backup/restic
[ -f "$REPO/config" ] || { echo "✗ no repository at $REPO — run backup/install-backup.sh first"; exit 1; }

echo "== user and group"
groupadd -f ltbackup
if id backup-reader >/dev/null 2>&1; then echo "  backup-reader exists"; else
  # fixed uid 1990: the default "next free system uid" (999) collides with the postgres container's uid
  useradd --system --uid 1990 --gid ltbackup --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin backup-reader
  echo "  + backup-reader (no password, no shell)"
fi

echo "== permissions"
# A group-readable config makes restic create every new file 0440 and directory 0750 itself.
chown -R "$OWNER:ltbackup" "$REPO"
chmod -R u+rwX,g+rX,g-w,o-rwx "$REPO"
find "$REPO" -type d -exec chmod g+s {} +   # setgid: files restic adds later also belong to ltbackup
chown "$OWNER:ltbackup" /srv/backup && chmod 750 /srv/backup     # lets the group reach restic/ …
chmod 700 /srv/backup/staging                                       # … but never staging/
echo "  $REPO readable by group ltbackup; staging owner-only"

echo "== ssh: read-only SFTP for backup-reader"
install -d -m 755 /etc/ssh/authorized_keys
printf '%s\n' "$PUBKEY" > /etc/ssh/authorized_keys/backup-reader
chmod 644 /etc/ssh/authorized_keys/backup-reader
CONF=/etc/ssh/sshd_config.d/20-backup-reader.conf
cat > "$CONF" <<'EOF'
# managed by ~/platform/backup/provision-backup-reader.sh
Match User backup-reader
    AuthorizedKeysFile /etc/ssh/authorized_keys/%u
    AuthenticationMethods publickey
    ForceCommand internal-sftp -R
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    PermitTunnel no
    X11Forwarding no
    PermitTTY no
# end the conditional block so later config lines apply to everyone again
Match all
EOF
if sshd -t; then
  # (Ubuntu may socket-activate ssh: reload if the daemon runs, otherwise new connections read it anyway)
  systemctl try-reload-or-restart ssh.service || true
  echo "  ✓ sshd config valid, reloaded (existing sessions are not touched)"
else
  rm -f "$CONF"
  echo "✗ sshd rejected the config — removed it again, nothing changed"; exit 1
fi

echo "== credentials for the desktop"
umask 077
cat > "$PLATFORM/backup-credentials.env" <<EOF
# pull-backup settings — generated $(date -Iseconds). Desktop: E:\\translater\\secrets\\backup.env
LT_BACKUP_SOURCE=sftp:swt17-backup:$REPO
LT_BACKUP_PASSWORD=$(grep '^RESTIC_PASSWORD=' "$PLATFORM/.env" | cut -d= -f2-)
EOF
chown "$OWNER:$OWNER" "$PLATFORM/backup-credentials.env"
echo "  ✓ $PLATFORM/backup-credentials.env"
