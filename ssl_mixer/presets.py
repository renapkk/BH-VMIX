"""プリセット（ミックス設定のスナップショット）の保存と読み出し。

自動保存される config.toml は「今の状態」。プリセットは、あとで戻れるように
好きな名前・好きな場所に保存しておく別ファイル（拡張子 .bhvmix、中身は設定ファイルと同じ TOML）。
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

from .config import Config, ConfigError, dump_config, parse_config, read_toml

EXTENSION = "bhvmix"


def save_preset(cfg: Config, path: Path) -> Path:
    """今の設定を path に保存する。拡張子が無ければ .bhvmix を付ける。保存したパスを返す。"""
    path = Path(path)
    if path.suffix.lower() != f".{EXTENSION}":
        path = path.with_name(path.name + f".{EXTENSION}")
    # 書き込み途中で失敗しても元のファイルが壊れないよう、同じフォルダの一時ファイルに書いてから置き換える
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump_config(cfg))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load_preset(path: Path) -> Config:
    """プリセットのファイルを読み込む。"""
    data = read_toml(path, "プリセット")
    # 空のファイルや別の TOML を読んで、黙って全部が初期化されないようにする
    if not data.get("inputs"):
        raise ConfigError(f"BH VMIX のプリセットではありません（[[inputs]] がありません）: {Path(path).name}")
    return parse_config(data)


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
