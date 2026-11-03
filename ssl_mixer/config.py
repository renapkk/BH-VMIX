"""設定ファイル（TOML）の読み込みと値チェック。"""

from __future__ import annotations

import math
import shutil
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 設定ファイルの既定の場所（プロジェクト直下の config.toml）
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.toml"

OUTPUT_MODES = ("stereo", "mono")

# 受け付ける値の範囲（設定ウィンドウの Fader・Buffer Size と同じ）
GAIN_MIN_DB, GAIN_MAX_DB = -60.0, 12.0
BLOCKSIZES = (128, 256, 512, 1024)
SAMPLERATES = (44100, 48000, 88200, 96000)
RECONNECT_MIN_SEC, RECONNECT_MAX_SEC = 0.5, 60.0
NAME_MAX_LEN = 32


class ConfigError(ValueError):
    """設定ファイルの内容に問題があるときに投げる例外。"""


@dataclass
class InputConfig:
    """入力chごとの設定。"""
    name: str
    gain_db: float = 0.0
    pan: float = 0.0
    mute: bool = False
    limiter: bool = False
    limiter_threshold_db: float = -6.0


@dataclass
class Config:
    """ミキサー全体の設定。ch番号は Audio MIDI 設定の表示に合わせて1始まり。"""
    device: str = "SSL12+BH"
    source_device: str = "SSL 12"
    sink_device: str = "BlackHole 2ch"
    samplerate: int = 48000
    blocksize: int = 256
    output_mode: str = "stereo"
    master_gain_db: float = 0.0
    limiter: bool = True
    reconnect_interval: float = 3.0
    input_channels: list[int] | None = None
    output_channels: list[int] | None = None
    # In 1 と In 2 をステレオリンクするか。リンク中は In 1 の設定で両方を動かし、
    # In 1 を L、In 2 を R に固定して、パンの代わりに link_balance（-1〜+1）を使う
    link_in_1_2: bool = True
    link_balance: float = 0.0
    inputs: list[InputConfig] = field(
        default_factory=lambda: [InputConfig(name=f"In {i}") for i in range(1, 5)])

    @property
    def is_linked(self) -> bool:
        """リンクが有効か（入力が2つ以上あるときだけ有効）。"""
        return self.link_in_1_2 and len(self.inputs) >= 2


def clean_name(text: str) -> str:
    """ch 名から改行・制御文字を取り除き、長さをそろえる（保存した設定が読めなくならないように）。"""
    text = "".join(c for c in text if not _is_control(c) and c not in "\u2028\u2029")
    return text.strip()[:NAME_MAX_LEN]


def _is_control(c: str) -> bool:
    """改行・タブなどの制御文字か（TOML の文字列にそのまま書けない文字）。"""
    return ord(c) < 0x20 or ord(c) == 0x7F


def read_toml(path: Path, what: str) -> dict:
    """TOML ファイルを読む。読めない理由はすべて ConfigError（日本語の説明つき）にする。"""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{what}が見つかりません: {path}") from None
    except IsADirectoryError:
        raise ConfigError(f"{what}ではなくフォルダです: {path}") from None
    except PermissionError:
        raise ConfigError(f"{what}を読む権限がありません: {path}") from None
    except UnicodeDecodeError:
        raise ConfigError(
            f"{what}の文字コードが UTF-8 ではありません（{path}）。"
            "テキストエディットなら「フォーマット > 標準テキストにする」→ UTF-8 で保存し直してください") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{what}の書式エラー（{path}）: {e}") from None
    except OSError as e:
        raise ConfigError(f"{what}を読めません（{path}）: {e}") from None


def _number_in(value: float, lo: float, hi: float, name: str) -> float:
    """数値が有限で lo〜hi の範囲かを確かめる（nan や inf も弾く）。"""
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ConfigError(f"{name} は {lo:g}〜{hi:g} で指定してください（値: {value}）")
    return float(value)


def _expect(data: dict, key: str, types: tuple[type, ...], where: str):
    """キーの型をチェックして値を返す。キーが無ければ None。"""
    if key not in data:
        return None
    value = data[key]
    # bool は int の一種なので、数値を期待している所に true/false が来たら弾く
    if isinstance(value, bool) and bool not in types:
        raise ConfigError(f"{where}{key} の型が違います（値: {value!r}）")
    if not isinstance(value, types):
        raise ConfigError(f"{where}{key} の型が違います（値: {value!r}）")
    return value


def _parse_channels(data: dict, key: str) -> list[int] | None:
    value = _expect(data, key, (list,), "")
    if value is None:
        return None
    if not value or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in value):
        raise ConfigError(f"{key} は1以上の整数のリストで書いてください（値: {value!r}）")
    return list(value)


def _parse_input(index: int, data: dict) -> InputConfig:
    where = f"inputs[{index}]."
    if not isinstance(data, dict):
        raise ConfigError(f"inputs[{index}] は [[inputs]] テーブルで書いてください")
    known = {"name", "gain_db", "pan", "mute", "limiter", "limiter_threshold_db"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where} に不明な項目があります: {', '.join(sorted(unknown))}")
    name = clean_name(_expect(data, "name", (str,), where) or "")
    cfg = InputConfig(name=name or f"In {index + 1}")
    gain = _expect(data, "gain_db", (int, float), where)
    if gain is not None:
        cfg.gain_db = _number_in(gain, GAIN_MIN_DB, GAIN_MAX_DB, f"{where}gain_db")
    pan = _expect(data, "pan", (int, float), where)
    if pan is not None:
        cfg.pan = _number_in(pan, -1.0, 1.0, f"{where}pan")
    for key in ("mute", "limiter"):
        value = _expect(data, key, (bool,), where)
        if value is not None:
            setattr(cfg, key, value)
    threshold = _expect(data, "limiter_threshold_db", (int, float), where)
    if threshold is not None:
        cfg.limiter_threshold_db = _number_in(threshold, -60.0, 0.0, f"{where}limiter_threshold_db")
    return cfg


def parse_config(data: dict) -> Config:
    """TOML を読み込んだ dict から Config を作る。"""
    known = {
        "device", "source_device", "sink_device", "samplerate", "blocksize", "output_mode",
        "master_gain_db", "limiter", "reconnect_interval", "input_channels", "output_channels",
        "link_in_1_2", "link_balance", "inputs",
    }
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"不明な項目があります: {', '.join(sorted(unknown))}")

    cfg = Config()
    for key in ("device", "source_device", "sink_device", "output_mode"):
        value = _expect(data, key, (str,), "")
        if value is not None:
            if not value.strip():
                raise ConfigError(f"{key} が空です")
            setattr(cfg, key, value)
    if cfg.output_mode not in OUTPUT_MODES:
        raise ConfigError(f'output_mode は "stereo" か "mono" です（値: {cfg.output_mode!r}）')

    for key, allowed in (("samplerate", SAMPLERATES), ("blocksize", BLOCKSIZES)):
        value = _expect(data, key, (int,), "")
        if value is not None:
            if value not in allowed:
                choices = " / ".join(str(v) for v in allowed)
                raise ConfigError(f"{key} は {choices} のどれかで指定してください（値: {value}）")
            setattr(cfg, key, value)

    master = _expect(data, "master_gain_db", (int, float), "")
    if master is not None:
        cfg.master_gain_db = _number_in(master, GAIN_MIN_DB, GAIN_MAX_DB, "master_gain_db")
    interval = _expect(data, "reconnect_interval", (int, float), "")
    if interval is not None:
        cfg.reconnect_interval = _number_in(
            interval, RECONNECT_MIN_SEC, RECONNECT_MAX_SEC, "reconnect_interval")

    for key in ("limiter", "link_in_1_2"):
        value = _expect(data, key, (bool,), "")
        if value is not None:
            setattr(cfg, key, value)
    balance = _expect(data, "link_balance", (int, float), "")
    if balance is not None:
        cfg.link_balance = _number_in(balance, -1.0, 1.0, "link_balance")

    cfg.input_channels = _parse_channels(data, "input_channels")
    cfg.output_channels = _parse_channels(data, "output_channels")

    inputs = _expect(data, "inputs", (list,), "")
    if inputs is not None:
        if not inputs:
            raise ConfigError("[[inputs]] を1つ以上書いてください")
        cfg.inputs = [_parse_input(i, d) for i, d in enumerate(inputs)]

    if cfg.input_channels is not None and len(cfg.input_channels) != len(cfg.inputs):
        raise ConfigError(
            f"input_channels の数（{len(cfg.input_channels)}）と [[inputs]] の数"
            f"（{len(cfg.inputs)}）が一致しません")
    if cfg.output_channels is not None:
        need = 2 if cfg.output_mode == "stereo" else (1, 2)
        n = len(cfg.output_channels)
        if (isinstance(need, int) and n != need) or (isinstance(need, tuple) and n not in need):
            raise ConfigError(
                'output_channels は stereo なら2つ、mono なら1つか2つ指定してください'
                f"（値: {cfg.output_channels}）")
    return cfg


def _toml_value(value) -> str:
    """Python の値を TOML の書き方にする（この設定ファイルで使う型だけ対応）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(round(value, 3) if isinstance(value, float) else value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        # 改行などの制御文字は \uXXXX にする（そのまま書くと TOML として読めなくなる）
        escaped = "".join(f"\\u{ord(c):04x}" if _is_control(c) else c for c in escaped)
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"TOML に書けない型です: {type(value)}")


def dump_config(cfg: Config) -> str:
    """Config を TOML の文字列にする（設定ウィンドウからの自動保存用）。"""
    lines = [
        "# BH VMIX の設定ファイル",
        "# 設定ウィンドウで値を変えると自動で上書き保存されます（コメントは残りません）。",
        "# ch 番号は Audio MIDI 設定の表示と同じ「1始まり」です。",
        "",
    ]
    top = [
        ("device", cfg.device), ("source_device", cfg.source_device),
        ("sink_device", cfg.sink_device), ("samplerate", cfg.samplerate),
        ("blocksize", cfg.blocksize), ("output_mode", cfg.output_mode),
        ("master_gain_db", cfg.master_gain_db), ("limiter", cfg.limiter),
        ("reconnect_interval", cfg.reconnect_interval),
        ("link_in_1_2", cfg.link_in_1_2), ("link_balance", cfg.link_balance),
    ]
    if cfg.input_channels is not None:
        top.append(("input_channels", cfg.input_channels))
    if cfg.output_channels is not None:
        top.append(("output_channels", cfg.output_channels))
    lines += [f"{key} = {_toml_value(value)}" for key, value in top]
    for inp in cfg.inputs:
        lines += [
            "",
            "[[inputs]]",
            f"name = {_toml_value(inp.name)}",
            f"gain_db = {_toml_value(inp.gain_db)}",
            f"pan = {_toml_value(inp.pan)}",
            f"mute = {_toml_value(inp.mute)}",
            f"limiter = {_toml_value(inp.limiter)}",
            f"limiter_threshold_db = {_toml_value(inp.limiter_threshold_db)}",
        ]
    return "\n".join(lines) + "\n"


def save_config(cfg: Config, path: Path) -> None:
    """設定を保存する。書き込み途中で落ちても壊れないよう、一時ファイルに書いてから置き換える。"""
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(dump_config(cfg), encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path | None) -> tuple[Config, Path | None]:
    """設定ファイルを読む。path が None で既定ファイルも無ければ既定値を返す。"""
    if path is None:
        if not DEFAULT_CONFIG_PATH.exists():
            return Config(), None
        path = DEFAULT_CONFIG_PATH
    return parse_config(read_toml(path, "設定ファイル")), path


def backup_broken(path: Path) -> Path | None:
    """読めなかった設定ファイルを「config.toml.broken-日時」にコピーして残す。

    既定値で動かしたまま自動保存すると、手で書いた内容が消えてしまうため。
    """
    path = Path(path)
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.broken-{stamp}")
    shutil.copy2(path, backup)
    return backup
