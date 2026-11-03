#!/bin/zsh
# BH VMIX.app をビルドし、配布用の zip も作る
# 使い方: ./scripts/build_app.sh
set -euo pipefail

cd "$(dirname "$0")/.."
APP="dist/BH VMIX.app"
PACKAGE="dist/BH VMIX"

# ---- アプリを作るための Python 環境（.venv-build）----
# Homebrew の Python は「今の macOS 専用」に作られているので、アプリに入れると古い Mac で起動できない。
# 配布用は、古い macOS でも動くように作られた python.org の Python を使う
BUILD_PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"
VENV=.venv-build
PY="$VENV/bin/python"
if [[ ! -x "$PY" ]]; then
  if [[ ! -x "$BUILD_PYTHON" ]]; then
    echo "python.org の Python 3.13 が見つかりません: $BUILD_PYTHON" >&2
    echo "https://www.python.org/downloads/macos/ から Python 3.13 の macOS 用インストーラーを入れてください" >&2
    exit 1
  fi
  echo "アプリ用の Python 環境（$VENV）を作ります…"
  "$BUILD_PYTHON" -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
  # numpy は、新しい Mac 向け（macOS 14 以降）の版が自動で選ばれるので、macOS 11 向けの版を先に入れる
  NUMPY_VERSION=$(sed -n 's/^numpy==//p' requirements-build.txt)
  WHEELS="$VENV/wheels"
  "$PY" -m pip download --quiet --only-binary=:all: --no-deps --dest "$WHEELS" \
    --platform macosx_11_0_arm64 --python-version 3.13 --implementation cp "numpy==$NUMPY_VERSION"
  "$PY" -m pip install --quiet "$WHEELS"/numpy-"$NUMPY_VERSION"-*macosx_11_0_arm64.whl
  "$PY" -m pip install --quiet -r requirements-build.txt
fi

# 前回のビルド結果を消す
rm -rf build dist

# .app を作る
"$PY" setup.py py2app

# ---- 古い Mac で動くかの確認 ----
# アプリの中のプログラムがどれも、動作条件（LSMinimumSystemVersion）以下の macOS 向けに作られているか調べる
autoload -U is-at-least
MIN_OS=$(/usr/libexec/PlistBuddy -c "Print LSMinimumSystemVersion" "$APP/Contents/Info.plist")
TOO_NEW=()
while IFS= read -r -d '' file; do
  [[ "$(file -b "$file")" == Mach-O* ]] || continue
  minos=$(vtool -show-build "$file" 2>/dev/null | awk '/minos/ {print $2; exit}')
  [[ -n "$minos" ]] || continue
  is-at-least "$minos" "$MIN_OS" || TOO_NEW+=("$minos  ${file#$APP/}")
done < <(find "$APP" -type f -print0)
if (( ${#TOO_NEW} )); then
  echo "macOS $MIN_OS では動かない部品があります（左が必要な macOS）:" >&2
  printf '  %s\n' "${TOO_NEW[@]}" >&2
  exit 1
fi
echo "確認OK: すべての部品が macOS $MIN_OS 以降で動きます"

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
