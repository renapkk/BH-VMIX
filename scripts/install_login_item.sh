#!/bin/zsh
# ログイン時に BH VMIX.app を自動起動するよう登録する（メニューの「ログイン時に起動」と同じ）
# 使い方: ./scripts/install_login_item.sh [アプリのパス]
set -euo pipefail

APP="${1:-/Applications/BH VMIX.app}"
if [[ ! -d "$APP" ]]; then
  echo "アプリが見つかりません: $APP" >&2
  exit 1
fi
"$APP/Contents/MacOS/BH VMIX" --install-login-item
