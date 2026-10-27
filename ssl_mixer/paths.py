"""アプリ版で使うファイルの置き場所。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "BH VMIX"
BUNDLE_ID = "com.koichi.sslmixer"

SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
LOG_DIR = Path.home() / "Library" / "Logs" / APP_NAME
APP_CONFIG_PATH = SUPPORT_DIR / "config.toml"
LOG_PATH = LOG_DIR / "ssl_mixer.log"
# 旧名のときの保存先（初回起動時にここから引き継ぐ）
OLD_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "SSL Mixer"


def resource_dir() -> Path:
    """config.example.toml などの同梱ファイルがある場所。

    .app として動いているときは py2app が RESOURCEPATH を設定する。
    そうでなければプロジェクト直下。
    """
    if getattr(sys, "frozen", False) and "RESOURCEPATH" in os.environ:
        return Path(os.environ["RESOURCEPATH"])
    return Path(__file__).resolve().parent.parent


def ensure_app_config() -> Path:
    """アプリ用の設定ファイルが無ければ見本からコピーして作り、そのパスを返す。"""
    if not SUPPORT_DIR.exists() and OLD_SUPPORT_DIR.exists():
        # 旧名「SSL Mixer」のときの設定とプリセットを引き継ぐ（元のフォルダは残しておく）
        shutil.copytree(OLD_SUPPORT_DIR, SUPPORT_DIR)
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not APP_CONFIG_PATH.exists():
        shutil.copyfile(resource_dir() / "config.example.toml", APP_CONFIG_PATH)
    return APP_CONFIG_PATH


def app_bundle_path() -> Path | None:
    """実行中の .app のパス（.app として動いていなければ None）。"""
    if not getattr(sys, "frozen", False):
        return None
    # 実行ファイルは「BH VMIX.app/Contents/MacOS/BH VMIX」にある
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None
