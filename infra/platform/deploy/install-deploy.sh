#!/usr/bin/env bash
# One-time switch from "scp the folder" to pull-based deployment from GitHub. Safe to re-run.
#   bash ~/platform/deploy/install-deploy.sh        (first time: from the old scp'd ~/platform)
#
#  1. read-only deploy key for this one repository (you add it on GitHub)
#  2. verify GitHub's host key against its published fingerprint (no blind trust on first use)
#  3. clone branch "deploy" to ~/LiveTranslator
#  4. move the running platform onto the clone: ~/platform becomes a link to
#     ~/LiveTranslator/infra/platform; .env and backup status are carried over; data volumes untouched
#  5. systemd timer lt-deploy.timer: every 5 min, deploy whatever CI approved
set -euo pipefail
GH_REPO="${LT_GH_REPO:-yiweidong605-spec/LiveTranslator}"
REPO="$HOME/LiveTranslator"
OLD="$HOME/platform"
KEY="$HOME/.ssh/lt_deploy_ed25519"
# GitHub's published ED25519 host key fingerprint (docs.github.com → "GitHub's SSH key fingerprints")
GITHUB_FP="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
ME="$(id -un)"

echo "== 1/5 deploy key (read-only, this repository only)"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ ! -f "$KEY" ]; then ssh-keygen -q -t ed25519 -N "" -C "swt17 deploy (read-only)" -f "$KEY"; fi
if ! grep -q "^Host github-lt$" "$HOME/.ssh/config" 2>/dev/null; then
  printf '\n# read-only deploy key for %s (deploy/install-deploy.sh)\nHost github-lt\n    HostName github.com\n    User git\n    IdentityFile %s\n    IdentitiesOnly yes\n' \
    "$GH_REPO" "$KEY" >> "$HOME/.ssh/config"
  chmod 600 "$HOME/.ssh/config"
fi

echo "== 2/5 GitHub host key"
if ! ssh-keygen -F github.com >/dev/null 2>&1; then
  scanned="$(ssh-keyscan -t ed25519 github.com 2>/dev/null)"
  fp="$(ssh-keygen -lf - <<< "$scanned" | awk '{print $2}')"
  [ "$fp" = "$GITHUB_FP" ] || { echo "✗ github.com presented $fp, expected $GITHUB_FP — stopping"; exit 1; }
  echo "$scanned" >> "$HOME/.ssh/known_hosts"
fi
echo "  ✓ github.com host key matches the published fingerprint"

until ssh -T github-lt 2>&1 | grep -q "successfully authenticated"; do
  cat <<MSG

  Add this key on GitHub: repository $GH_REPO → Settings → Deploy keys → Add deploy key
    Title: swt17 (read-only)        "Allow write access": leave it OFF
    Key:
$(sed 's/^/      /' "$KEY.pub")

MSG
  read -r -p "  Press Enter when it is added (Ctrl+C to stop) … "
done
echo "  ✓ the mini PC can read $GH_REPO"

echo "== 3/5 clone"
if [ ! -d "$REPO/.git" ]; then
  if ! git ls-remote --exit-code --heads "git@github-lt:$GH_REPO.git" deploy >/dev/null; then
    echo "✗ branch 'deploy' does not exist yet: CI creates it after the first green run on main."
    echo "  Check the Actions tab on GitHub, then re-run this script."; exit 1
  fi
  git clone --quiet --branch deploy "git@github-lt:$GH_REPO.git" "$REPO"
fi
git -C "$REPO" log -1 --format="  at %h  %s"

echo "== 4/5 move the running platform onto the clone"
NEW="$REPO/infra/platform"
if [ -L "$OLD" ]; then
  echo "  ~/platform already points to $(readlink "$OLD")"
else
  [ -f "$OLD/.env" ] || { echo "✗ $OLD/.env not found"; exit 1; }
  echo "  stopping the stack (data volumes are kept) …"
  (cd "$OLD" && docker compose down)
  BAK="$OLD.before-git-$(date +%Y%m%d-%H%M)"
  mv "$OLD" "$BAK"
  ln -s "$NEW" "$OLD"
  install -m 600 "$BAK/.env" "$NEW/.env"
  cp -p "$BAK"/backup/status/*.json "$NEW/backup/status/" 2>/dev/null || true
  echo "  old folder kept as $BAK (delete it after a week if all is well)"
  echo "  starting the stack from the clone …"
  (cd "$NEW" && docker compose up -d --build --quiet-pull)
fi
python3 - "$NEW/deploy/status/last-deploy.json" "$(git -C "$REPO" rev-parse HEAD)" <<'PY'
import json, sys, time, os
path, commit = sys.argv[1:3]
if not os.path.exists(path):
    json.dump({"ok": True, "deployed_commit": commit, "previous_commit": None, "at": int(time.time()),
               "attempted_commit": None, "actions": ["initial install"], "manual_steps": [], "error": None},
              open(path, "w"), indent=1)
PY

echo "== 5/5 systemd timer (every 5 minutes)"
sed "s#@USER@#$ME#g; s#@REPO@#$REPO#g" "$NEW/deploy/lt-deploy.service" | sudo tee /etc/systemd/system/lt-deploy.service >/dev/null
sudo install -m 644 "$NEW/deploy/lt-deploy.timer" /etc/systemd/system/lt-deploy.timer
sudo systemctl daemon-reload
sudo systemctl enable --now lt-deploy.timer >/dev/null
systemctl list-timers lt-deploy.timer --no-pager | head -2
bash "$NEW/deploy/deploy.sh" --verbose
echo
echo "✓ done. From now on: push to main → CI → deployed here within ~5 minutes."
echo "  Log of deployments:  journalctl -u lt-deploy -n 30     Status: cat ~/platform/deploy/status/last-deploy.json"
