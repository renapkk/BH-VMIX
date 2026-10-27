"""ログイン時の自動起動（launchd の LaunchAgent）の登録と解除。

~/Library/LaunchAgents に plist（設定ファイル）を置くと、ログイン時に launchd が実行してくれる。
open コマンド経由で起動するので、すでにアプリが起動していれば二重には起動しない。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .paths import BUNDLE_ID

LABEL = f"{BUNDLE_ID}.login"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_installed() -> bool:
    return PLIST_PATH.exists()


def install(app_path: Path) -> None:
    """ログイン時に app_path（BH VMIX.app）を起動するよう登録する。"""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        # --hidden: ログイン時は設定ウィンドウを開かず、メニューバーだけで起動する
        "ProgramArguments": ["/usr/bin/open", "-a", str(app_path), "--args", "--hidden"],
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)


def uninstall() -> None:
    """自動起動の登録を解除する。"""
    if PLIST_PATH.exists():
        # 読み込まれていれば launchd から外す（読み込まれていなくてもエラーは無視）
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)],
                       capture_output=True)
        PLIST_PATH.unlink()
