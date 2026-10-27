"""プリセット（ミックス設定のスナップショット）の保存と読み出し。

自動保存される config.toml は「今の状態」。プリセットは、あとで戻れるように
好きなタイミングで残しておく別ファイル。中身は設定ファイルと同じ TOML 形式。
"""

from __future__ import annotations

import copy
import time
import tomllib
from pathlib import Path

from .config import Config, ConfigError, dump_config, parse_config
from .paths import SUPPORT_DIR

PRESET_DIR = SUPPORT_DIR / "presets"
SUFFIX = ".toml"
# ファイル名に使えない・使いたくない文字
_BAD_CHARS = set('/\\:*?"<>|') | {chr(c) for c in range(32)}
MAX_NAME_LEN = 60


def timestamp_prefix(now: float | None = None) -> str:
    """プリセット名の先頭に付ける日付と時刻（例: 261027_2259_）。"""
    return time.strftime("%y%m%d_%H%M_", time.localtime(now))


def clean_name(text: str) -> str:
    """ファイル名に使えない文字を取り除く。"""
    cleaned = "".join("_" if ch in _BAD_CHARS else ch for ch in text).strip().strip(".")
    return cleaned[:MAX_NAME_LEN]


def list_presets(folder: Path = PRESET_DIR) -> list[str]:
    """保存済みのプリセット名（新しい順）。名前の先頭が日付なので、名前の逆順＝新しい順になる。"""
    if not folder.exists():
        return []
    return sorted((p.stem for p in folder.glob(f"*{SUFFIX}")), reverse=True)


def save_preset(cfg: Config, label: str, folder: Path = PRESET_DIR,
                now: float | None = None) -> str:
    """今の設定をプリセットとして保存し、付けた名前を返す。

    名前は「日付_時刻_ラベル」。同じ名前が既にあれば、末尾に _2, _3… を付けて上書きを避ける。
    """
    folder.mkdir(parents=True, exist_ok=True)
    label = clean_name(label)
    # ラベルが空なら末尾の「_」は付けない（例: 261027_2259）
    base = timestamp_prefix(now) + label if label else timestamp_prefix(now).rstrip("_")
    name, n = base, 2
    while (folder / f"{name}{SUFFIX}").exists():
        name = f"{base}_{n}"
        n += 1
    (folder / f"{name}{SUFFIX}").write_text(dump_config(cfg), encoding="utf-8")
    return name


def load_preset(name: str, folder: Path = PRESET_DIR) -> Config:
    """プリセットを読み込む。"""
    path = folder / f"{name}{SUFFIX}"
    try:
        with open(path, "rb") as f:
            return parse_config(tomllib.load(f))
    except FileNotFoundError:
        raise ConfigError(f"プリセットが見つかりません: {name}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"プリセットの書式エラー（{name}）: {e}") from None


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
