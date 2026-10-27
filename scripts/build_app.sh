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

echo "できました: $APP"
echo "配布用: dist/BH-VMIX.zip"
