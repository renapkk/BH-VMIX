"""プリセット（ミックス設定のスナップショット）の保存と読み出し。

自動保存される config.toml は「今の状態」。プリセットは、あとで戻れるように
好きな名前・好きな場所に保存しておく別ファイル（拡張子 .bhvmix、中身は設定ファイルと同じ TOML）。
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

from .config import Config, ConfigError, dump_config, parse_config

EXTENSION = "bhvmix"


def save_preset(cfg: Config, path: Path) -> Path:
    """今の設定を path に保存する。拡張子が無ければ .bhvmix を付ける。保存したパスを返す。"""
    path = Path(path)
    if path.suffix.lower() != f".{EXTENSION}":
        path = path.with_name(path.name + f".{EXTENSION}")
    path.write_text(dump_config(cfg), encoding="utf-8")
    return path


def load_preset(path: Path) -> Config:
    """プリセットのファイルを読み込む。"""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            return parse_config(tomllib.load(f))
    except FileNotFoundError:
        raise ConfigError(f"プリセットが見つかりません: {path.name}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"プリセットの書式エラー（{path.name}）: {e}") from None


def apply_preset(cfg: Config, preset: Config) -> bool:
    """プリセットのミックス設定を cfg に写す。入力の数が変わったら True を返す。

    デバイス・ch 割り当て・サンプルレート・バッファサイズは写さない
    （読み込んだときに音が止まったり、別のデバイスを開いたりしないように）。
    """
    count_changed = len(preset.inputs) != len(cfg.inputs)
    cfg.output_mode = preset.output_mode
    cfg.master_gain_db = preset.master_gain_db
    cfg.limiter = preset.limiter
    cfg.link_in_1_2 = preset.link_in_1_2
    cfg.link_balance = preset.link_balance
    cfg.inputs = copy.deepcopy(preset.inputs)
    if count_changed:
        # 入力の数が変わると ch の手動指定と合わなくなるので、自動判定に戻す
        cfg.input_channels = None
    return count_changed
