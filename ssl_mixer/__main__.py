"""コマンドラインの入口。`python -m ssl_mixer` で実行する。"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ssl_mixer",
        description="SSL 12 の入力をミックスして BlackHole 2ch へ送る",
    )
    parser.add_argument("--version", action="version", version=f"BH VMIX {__version__}")
    parser.add_argument("--list", action="store_true", help="オーディオデバイスと ch 数を一覧表示して終了")
    parser.add_argument("--config", type=Path, help="設定ファイル（省略時はプロジェクト直下の config.toml）")
    parser.add_argument("--test-tone", action="store_true", help="入力の代わりに 1kHz サイン波を出力する")
    parser.add_argument("--test-tone-lr", action="store_true",
                        help="1kHz サイン波を左だけ → 右だけ と交互に出力する（ステレオの確認用）")
    parser.add_argument("--meter", action="store_true", help="ピークメーターを表示する")
    args = parser.parse_args(argv)

    try:
        cfg, path = load_config(args.config)
    except ConfigError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        return 2
    if path is None:
        print("config.toml が無いので既定値で動きます（config.example.toml をコピーすると調整できます）")
    else:
        print(f"設定ファイル: {path}")

    # sounddevice の読み込みは少し時間がかかるので、必要になってから行う
    from . import devices
    from .engine import Engine
    from .meter import Console, Meter

    if args.list:
        devices.print_device_list(cfg)
        return 0

    console = Console()
    tone = "lr" if args.test_tone_lr else args.test_tone
    engine = Engine(cfg, test_tone=tone, log=console.log)
    # launchd などから SIGTERM（終了の合図）が来たら止める
    signal.signal(signal.SIGTERM, lambda *_: engine.stop())
    if args.meter:
        Meter(engine, console).start()
    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()
    console.log("終了しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
