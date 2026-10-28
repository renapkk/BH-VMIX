#!/bin/zsh
# BH VMIX.app をビルドし、配布用の zip も作る
# 使い方: ./scripts/build_app.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
APP="dist/BH VMIX.app"
PACKAGE="dist/BH VMIX"

# 前回のビルド結果を消す
rm -rf build dist

# .app を作る
"$PY" setup.py py2app

# Apple シリコンでは署名が無いアプリは起動できないので、自分用の簡易署名（アドホック署名）をする
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

# ほかの Mac に渡すためのフォルダ（アプリ＋はじめにお読みください）を zip にする
# ditto は .app の中身を壊さずにコピー・圧縮できる
mkdir -p "$PACKAGE"
ditto "$APP" "$PACKAGE/BH VMIX.app"
cp "docs/はじめにお読みください.txt" "$PACKAGE/"
ditto -c -k --keepParent "$PACKAGE" "dist/BH-VMIX.zip"

# 身内に渡す用：BlackHole のインストーラー（vendor/BlackHole に置いたもの）も入れた zip
# ライセンス上、この zip は GitHub などの公開の場には置かない
VENDOR="vendor/BlackHole"
if [[ -f "$VENDOR/BlackHole2ch-0.7.1.pkg" ]]; then
  FAMILY="dist/BH VMIX（BlackHole付き）"
  mkdir -p "$FAMILY"
  ditto "$APP" "$FAMILY/BH VMIX.app"
  cp "$VENDOR/BlackHole2ch-0.7.1.pkg" "$VENDOR/BlackHole_LICENSE.txt" "$FAMILY/"
  cp "docs/はじめにお読みください（BlackHole付き）.txt" "$FAMILY/"
  ditto -c -k --keepParent "$FAMILY" "dist/BH-VMIX-with-BlackHole.zip"
  echo "身内用（BlackHole 付き）: dist/BH-VMIX-with-BlackHole.zip"
fi

echo "できました: $APP"
echo "配布用: dist/BH-VMIX.zip"
