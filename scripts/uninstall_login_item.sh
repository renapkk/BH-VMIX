#!/bin/zsh
# ログイン時の自動起動を解除する（アプリが無くても plist を消せるよう、ここでは直接消す）
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.koichi.sslmixer.login.plist"
if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm "$PLIST"
  echo "解除しました"
else
  echo "登録されていません"
fi
