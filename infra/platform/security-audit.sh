#!/usr/bin/env bash
# Read-only security check of the mini PC — changes nothing.
#   sudo bash ~/platform/security-audit.sh [days, default 14]
#
# Answers "has anyone been in here?" the way an incident responder starts:
#   1 who logged in (and who tried)      5 what is listening / exposed
#   2 accounts and admin rights          6 what runs: containers, timers, cron (persistence)
#   3 SSH keys and SSH settings          7 suspicious processes and outbound connections
#   4 devices in the tailnet             8 modified system files, missing updates
# ✓ = as expected, ⚠ = look at it (not necessarily an attack), plus the raw facts to judge yourself.
set -uo pipefail
for c in ss ufw journalctl docker tailscale; do command -v "$c" >/dev/null || echo "(note: $c not installed, its checks are skipped)"; done
[ "$(id -u)" -eq 0 ] || { echo "run with sudo:  sudo bash $0"; exit 1; }
DAYS="${1:-14}"
OWNER="${SUDO_USER:-swt17}"
PLATFORM="$(getent passwd "$OWNER" | cut -d: -f6)/platform"
# where logins may legitimately come from: your tailnet (100.64.0.0/10) and the apartment LAN
TRUSTED='^(100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|10\.188\.|127\.|::1$)'
WARN=0
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ⚠ $*"; WARN=$((WARN + 1)); }
info() { echo "    $*"; }
hdr()  { echo; echo "== $*"; }

SSHLOG="$(journalctl -t sshd -t sshd-session --since "$DAYS days ago" -o cat 2>/dev/null)"

hdr "1/8 SSH logins in the last $DAYS days"
accepted="$(grep -E '^Accepted ' <<< "$SSHLOG" || true)"
if [ -z "$accepted" ]; then info "no successful logins logged"; else
  # "Accepted publickey for USER from IP port N ssh2: ED25519 SHA256:…"
  awk '{print $4, $6, $2, $NF}' <<< "$accepted" | sort | uniq -c | sort -rn \
    | awk '{printf "    %5d× %-14s from %-15s %s %s\n", $1, $2, $3, $4, $5}'
  grep -q '^Accepted password' <<< "$accepted" && bad "a PASSWORD login succeeded — password logins should be off" \
    || ok "every login used an SSH key"
  outside="$(awk '{print $6}' <<< "$accepted" | sort -u | grep -Ev "$TRUSTED" || true)"
  if [ -n "$outside" ]; then bad "logins from outside your tailnet/LAN: $(tr '\n' ' ' <<< "$outside")"
  else ok "all logins came from your tailnet or the apartment LAN"; fi
fi
failed="$(grep -E 'Failed |Invalid user|authentication failure|Connection closed by (invalid|authenticating) user' <<< "$SSHLOG" \
  | grep -oE 'from [0-9a-f.:]+|rhost=[0-9a-f.:]+' | sed 's/from //; s/rhost=//' | sort | uniq -c | sort -rn || true)"
if [ -z "$failed" ]; then ok "no failed login attempts"
else
  info "failed attempts by source:"; head -5 <<< "$failed" | sed 's/^/      /'
  ext="$(awk '{print $2}' <<< "$failed" | grep -Ev "$TRUSTED" || true)"
  [ -n "$ext" ] && bad "failed attempts from outside: SSH should not be reachable from there (check ufw)" \
    || ok "failed attempts only from your own devices (typos, old keys)"
fi
info "recent sessions:"; last -n 8 2>/dev/null | head -8 | sed 's/^/      /'

hdr "2/8 accounts and admin rights"
root_like="$(awk -F: '$3 == 0 && $1 != "root" {print $1}' /etc/passwd)"
[ -n "$root_like" ] && bad "accounts with uid 0 besides root: $root_like" || ok "only root has uid 0"
shells="$(awk -F: '$7 !~ /(nologin|false|sync)$/ {print $1}' /etc/passwd | tr '\n' ' ')"
info "accounts that can get a shell: $shells"
[ "$(wc -w <<< "$shells")" -le 2 ] && ok "no unexpected login accounts (root + $OWNER)" || bad "more login accounts than expected"
for g in sudo docker; do
  members="$(getent group "$g" | cut -d: -f4)"
  [ "$members" = "$OWNER" ] && ok "group $g: only $OWNER" || bad "group $g has: ${members:-nobody}"
done
[ -n "$(find /etc/sudoers.d -type f ! -name README -newer /etc/hostname 2>/dev/null)" ] \
  && bad "files added to /etc/sudoers.d after installation: $(ls /etc/sudoers.d)" || ok "no extra sudo rules"

hdr "3/8 SSH keys and settings"
for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys /etc/ssh/authorized_keys/*; do
  [ -s "$f" ] || continue
  info "$f:"
  ssh-keygen -lf "$f" 2>/dev/null | awk '{print "      " $2, $3, $4}'
done
cfg="$(sshd -T 2>/dev/null)"
for kv in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin no"; do
  grep -qx "$kv" <<< "$cfg" && ok "sshd: $kv" || bad "sshd: expected '$kv', got '$(grep "^${kv% *} " <<< "$cfg")'"
done

hdr "4/8 devices in your tailnet"
ts="$(tailscale status 2>/dev/null | grep -E '^100\.' || true)"
[ -n "$ts" ] && sed 's/^/    /' <<< "$ts"
[ "$(grep -c . <<< "$ts")" -le 2 ] && ok "only your 2 devices" \
  || bad "more than 2 devices in the tailnet — anything you don't recognise? (login.tailscale.com → Machines)"

hdr "5/8 what is listening"
if ufw status 2>/dev/null | grep -q "Status: active"; then ok "ufw firewall active"; else bad "ufw firewall is NOT active"; fi
listen="$(ss -Htulpn 2>/dev/null | awk '{print $1, $5, $7}')"
public="$(awk '$2 !~ /^(127\.|\[::1\]|100\.|\[fd7a:)/' <<< "$listen" | grep -v '%lo:' || true)"
info "reachable beyond this machine (ufw still filters these):"
[ -n "$public" ] && sed 's/users:(("\([^"]*\)".*/\1/; s/^/      /' <<< "$public"
unexp="$(grep -vE 'sshd|tailscaled|systemd-resolve|systemd-network|dhclient|chronyd|avahi' <<< "$public" || true)"
[ -z "$unexp" ] && ok "only SSH, Tailscale and system network services" || bad "review these listeners: $(awk '{print $2}' <<< "$unexp" | tr '\n' ' ')"

hdr "6/8 what runs: containers, timers, cron"
dps="$(docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null)"
[ -n "$dps" ] && awk -F'|' '{printf "    %-38s %s\n", $1, $2}' <<< "$dps"
grep -v '^lt-platform-' <<< "$dps" | grep -q . && bad "containers that are not part of the platform" || ok "only platform containers"
grep -E '0\.0\.0\.0:|\[::\]:' <<< "$dps" >/dev/null && bad "a container port is published on all interfaces (bypasses ufw)" \
  || ok "container ports bound to 127.0.0.1 only"
custom="$(find /etc/systemd/system -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) -printf '%f ' 2>/dev/null)"
info "self-installed systemd units: ${custom:-none}"
[ -z "$(tr ' ' '\n' <<< "$custom" | grep -vE '^(lt-backup|lt-deploy)\.(service|timer)$|^$')" ] \
  && ok "no unknown systemd services" || bad "unknown units in /etc/systemd/system: review them"
crons=""
while IFS=: read -r u _; do c="$(crontab -l -u "$u" 2>/dev/null | grep -Ev '^\s*(#|$)')"; [ -n "$c" ] && crons+="$u: $c"$'\n'; done < /etc/passwd
[ -z "$crons" ] && ok "no user crontabs" || { bad "user crontabs found:"; sed 's/^/      /' <<< "$crons"; }
extra_cron="$(find /etc/cron.d /etc/cron.daily /etc/cron.hourly -type f -newer /etc/hostname 2>/dev/null | grep -v placeholder || true)"
[ -z "$extra_cron" ] && ok "no cron jobs added after installation" || info "cron files changed since install (often package updates): $(tr '\n' ' ' <<< "$extra_cron")"

hdr "7/8 processes"
info "top CPU:"; ps -eo pcpu,user,comm --sort=-pcpu | head -6 | sed 's/^/      /'
sus="$(find /proc/[0-9]*/exe -maxdepth 0 -printf '%p -> %l\n' 2>/dev/null | grep -E ' -> (/tmp/|/dev/shm/|/var/tmp/)|\(deleted\)' \
  | grep -vE 'snap|containerd|memfd:runc' || true)"
[ -z "$sus" ] && ok "no programs running from /tmp, /dev/shm or deleted files" || { bad "processes running from suspicious places:"; sed 's/^/      /' <<< "$sus"; }
out="$(ss -Htnp state established 2>/dev/null | awk '{print $4, $5}' | grep -Ev '^\S+ (127\.|10\.|100\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|\[)' \
  | sed 's/.*users:(("\([^"]*\)".*/\1/' | sort | uniq -c | sort -rn || true)"
info "programs with internet connections right now: $(tr -s ' \n' ' ' <<< "${out:-none}")"

hdr "8/8 system files and updates"
# checksum changes ("??5??????") count; docs/man pages missing on purpose (minimal installs) do not
mod="$(dpkg --verify 2>/dev/null | awk '$2 != "c"' | grep -vE '^missing +/usr/share/(doc|man|info|locale|lintian)/' || true)"
[ -z "$mod" ] && ok "no installed system file differs from its package" || { bad "system files changed outside the package manager:"; head -10 <<< "$mod" | sed 's/^/      /'; }
upg="$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst .*security' || true)"
[ "${upg:-0}" -eq 0 ] && ok "no pending security updates" || bad "$upg security updates pending (sudo apt upgrade)"
[ -f /var/run/reboot-required ] && info "a reboot is pending to finish updates (sudo reboot when convenient)"

if [ -f "$PLATFORM/docker-compose.yml" ]; then
  hdr "extra: platform logins (Airflow)"
  (cd "$PLATFORM" && docker compose exec -T postgres psql -U postgres -d airflow -Atc \
    "select username, coalesce(to_char(last_login,'MM-DD HH24:MI'),'never'), coalesce(login_count,0) from ab_user order by 1" 2>/dev/null) \
    | awk -F'|' '{printf "    %-12s last login %-12s (%s logins)\n", $1, $2, $3}'
  info "expected: admin (you in the browser) and lt-sync (sync.bat)"
fi

echo
if [ "$WARN" -eq 0 ]; then echo "✓ nothing suspicious found ($DAYS days of logs)"
else echo "⚠ $WARN item(s) to look at — not necessarily an attack; send the output if unsure"; fi
