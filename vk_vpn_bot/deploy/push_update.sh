#!/usr/bin/env bash
# Обновление живого VK-бота на сервере из git-ветки.
# Использование:
#   export VK_BOT_SSH_HOST=1.2.3.4
#   export VK_BOT_SSH_USER=root
#   export VK_BOT_SSH_PRIVATE_KEY=~/.ssh/id_rsa
#   ./deploy/push_update.sh [branch]
set -euo pipefail

BRANCH="${1:-cursor/vk-bot-beauty-legal-81f4}"
HOST="${VK_BOT_SSH_HOST:?Set VK_BOT_SSH_HOST}"
USER="${VK_BOT_SSH_USER:-root}"
KEY="${VK_BOT_SSH_PRIVATE_KEY:-}"
REMOTE_DIR="${VK_BOT_REMOTE_DIR:-/root/vk_vpn_bot}"
SERVICE="${VK_BOT_SERVICE:-vk-vpn-bot}"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
if [[ -n "$KEY" ]]; then
  SSH_OPTS+=(-i "$KEY")
fi

echo "==> Sync branch $BRANCH → $USER@$HOST:$REMOTE_DIR"
ssh "${SSH_OPTS[@]}" "$USER@$HOST" bash -s <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
if [[ -d .git ]]; then
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git pull origin "$BRANCH"
else
  echo "WARN: $REMOTE_DIR is not a git repo — copy files manually"
fi
if [[ -x .venv/bin/pip ]]; then
  .venv/bin/pip install -r requirements.txt
fi
systemctl restart "$SERVICE"
sleep 2
systemctl --no-pager --full status "$SERVICE" || true
echo "==> Done. Send /start in VK to refresh keyboard."
EOF
