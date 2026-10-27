"""デバイス一覧の表示と、機器セット内の入出力chの判定。"""

from __future__ import annotations

import time
from dataclasses import dataclass

import sounddevice as sd

from . import coreaudio
from .config import Config


class DeviceError(RuntimeError):
    """デバイスが見つからない・構成が想定と違うときに投げる例外。"""


@dataclass
class Routing:
    """実際に開くデバイスとchの対応（ch番号は0始まり）。"""
    device_index: int        # sounddevice のデバイス番号
    device_name: str
    ca_device_id: int        # Core Audio のデバイスID（抜き差し監視に使う）
    samplerate: float
    in_total: int            # 機器セット全体の入力ch数
    out_total: int           # 機器セット全体の出力ch数
    in_channels: list[int]   # ミックスに使う入力ch
    out_channels: list[int]  # 書き込む出力ch（BlackHole 側）
    auto_detected: bool
    layout_signature: tuple  # 構成が変わったかを見るための値


def refresh_portaudio() -> None:
    """PortAudio のデバイス一覧を取り直す（抜き差し後に必要）。

    PortAudio は初期化時の一覧をキャッシュするため、いったん終了して初期化し直す。
    ストリームを開いていない時にだけ呼ぶこと。
    """
    sd._terminate()
    sd._initialize()


def find_portaudio_device(name: str) -> int:
    """名前が完全一致する sounddevice のデバイス番号を返す。"""
    for i, dev in enumerate(sd.query_devices()):
        if dev["name"] == name:
            return i
    raise DeviceError(f"デバイス「{name}」が見つかりません")


def _match_subdevice(subdevices: list[coreaudio.CADevice], name: str) -> int | None:
    """サブデバイスの中から名前で探す（完全一致を優先し、なければ部分一致）。"""
    for i, sub in enumerate(subdevices):
        if sub.name == name:
            return i
    for i, sub in enumerate(subdevices):
        if name.lower() in sub.name.lower():
            return i
    return None


def compute_offsets(subdevices: list[coreaudio.CADevice], source_name: str,
                    sink_name: str) -> tuple[int, int, coreaudio.CADevice, coreaudio.CADevice]:
    """機器セット内で、入力元の先頭入力chと出力先の先頭出力chの位置（0始まり）を返す。

    機器セットの ch はサブデバイスの並び順に連結されるので、
    手前にあるサブデバイスの ch 数を足していけば位置が分かる。
    """
    src = _match_subdevice(subdevices, source_name)
    if src is None:
        raise DeviceError(f"機器セット内に「{source_name}」がありません（抜けている可能性があります）")
    dst = _match_subdevice(subdevices, sink_name)
    if dst is None:
        raise DeviceError(f"機器セット内に「{sink_name}」がありません")
    in_offset = sum(s.in_ch for s in subdevices[:src])
    out_offset = sum(s.out_ch for s in subdevices[:dst])
    return in_offset, out_offset, subdevices[src], subdevices[dst]


def resolve_routing(cfg: Config) -> Routing:
    """設定とデバイス情報から、開くデバイスと使うchを決める。"""
    ca_dev = coreaudio.find_device(cfg.device)
    if ca_dev is None:
        raise DeviceError(f"機器セット「{cfg.device}」が見つかりません")
    if not coreaudio.is_alive(ca_dev.id):
        raise DeviceError(f"機器セット「{cfg.device}」が現在使用できません")
    index = find_portaudio_device(cfg.device)
    pa_dev = sd.query_devices(index)
    in_total = int(pa_dev["max_input_channels"])
    out_total = int(pa_dev["max_output_channels"])
    n_inputs = len(cfg.inputs)
    n_outputs = 2  # モノ出力でも BlackHole の両chに同じ信号を出す

    info = coreaudio.get_aggregate_info(ca_dev)
    signature = _signature(ca_dev, info)

    auto = cfg.input_channels is None or cfg.output_channels is None
    in_channels = [c - 1 for c in cfg.input_channels] if cfg.input_channels else None
    out_channels = [c - 1 for c in cfg.output_channels] if cfg.output_channels else None

    if auto:
        if info is None:
            raise DeviceError(
                f"「{cfg.device}」は機器セットではないため ch を自動判定できません。"
                "設定ファイルで input_channels / output_channels を指定してください")
        in_offset, out_offset, src, dst = compute_offsets(
            info.subdevices, cfg.source_device, cfg.sink_device)
        if in_channels is None:
            if src.in_ch < n_inputs:
                raise DeviceError(f"「{src.name}」の入力が {src.in_ch}ch しかありません")
            in_channels = list(range(in_offset, in_offset + n_inputs))
        if out_channels is None:
            if dst.out_ch < n_outputs:
                raise DeviceError(f"「{dst.name}」の出力が {dst.out_ch}ch しかありません")
            out_channels = list(range(out_offset, out_offset + n_outputs))

    if len(out_channels) == 1:
        out_channels = out_channels * 2
    for c in in_channels:
        if c >= in_total:
            raise DeviceError(f"入力 ch {c + 1} は範囲外です（「{cfg.device}」の入力は {in_total}ch）")
    for c in out_channels:
        if c >= out_total:
            raise DeviceError(f"出力 ch {c + 1} は範囲外です（「{cfg.device}」の出力は {out_total}ch）")

    samplerate = float(pa_dev["default_samplerate"]) or float(cfg.samplerate)
    return Routing(
        device_index=index, device_name=cfg.device, ca_device_id=ca_dev.id,
        samplerate=samplerate, in_total=in_total, out_total=out_total,
        in_channels=in_channels, out_channels=out_channels,
        auto_detected=auto, layout_signature=signature,
    )


def _signature(ca_dev: coreaudio.CADevice, info: coreaudio.AggregateInfo | None) -> tuple:
    """デバイスIDと ch 構成をまとめた値。これが変われば抜き差しなどで構成が変わったと判断する。"""
    subs = tuple((s.uid, s.in_ch, s.out_ch) for s in info.subdevices) if info else ()
    return (ca_dev.id, ca_dev.in_ch, ca_dev.out_ch, subs)


def current_signature(routing: Routing) -> tuple | None:
    """今の機器セット構成の値を返す。消えていれば None。"""
    ca_dev = coreaudio.find_device(routing.device_name)
    if ca_dev is None or not coreaudio.is_alive(ca_dev.id):
        return None
    return _signature(ca_dev, coreaudio.get_aggregate_info(ca_dev))


def print_device_list(cfg: Config) -> None:
    """--list：全デバイスと in/out ch 数、機器セットの中身を表示する。"""
    print("=== オーディオデバイス一覧 ===")
    print(f"{'番号':>4}  {'入力':>4}  {'出力':>4}  {'レート':>7}  名前")
    for i, dev in enumerate(sd.query_devices()):
        sr = int(dev["default_samplerate"])
        print(f"{i:>4}  {dev['max_input_channels']:>4}  {dev['max_output_channels']:>4}  "
              f"{sr:>7}  {dev['name']}")

    aggregates = [d for d in coreaudio.list_devices() if d.is_aggregate]
    if aggregates:
        print("\n=== 機器セット（Aggregate Device）の中身 ===")
    for agg in aggregates:
        info = coreaudio.get_aggregate_info(agg)
        print(f"■ {agg.name}（入力 {agg.in_ch}ch / 出力 {agg.out_ch}ch）")
        if not info or not info.subdevices:
            print("    （有効なサブデバイスなし）")
            continue
        in_pos = out_pos = 1
        for sub in info.subdevices:
            ins = f"入力 {in_pos}〜{in_pos + sub.in_ch - 1}" if sub.in_ch else "入力なし"
            outs = f"出力 {out_pos}〜{out_pos + sub.out_ch - 1}" if sub.out_ch else "出力なし"
            clock = "  ★クロックソース" if sub.uid == info.clock_uid else ""
            drift = info.drift_for(sub.uid)
            drift_text = {True: "ドリフト補正ON", False: "ドリフト補正OFF", None: "ドリフト補正 不明"}[drift]
            print(f"    - {sub.name}: {ins} / {outs}  [{drift_text}]{clock}")
            in_pos += sub.in_ch
            out_pos += sub.out_ch

    print(f"\n=== 設定上の対象: 「{cfg.device}」 ===")
    try:
        r = resolve_routing(cfg)
    except DeviceError as e:
        print(f"  ✕ {e}")
        return
    how = "自動判定" if r.auto_detected else "設定ファイル指定"
    print(f"  入力 ch: {[c + 1 for c in r.in_channels]}（{how}）")
    print(f"  出力 ch: {[c + 1 for c in r.out_channels]}（{how}）")
    print(f"  サンプルレート: {int(r.samplerate)} Hz")
    warnings = check_aggregate_settings(cfg)
    for w in warnings:
        print(f"  ⚠ {w}")
    if not warnings:
        print("  ✓ クロックソース／ドリフト補正の設定は推奨どおりです")


def create_aggregate_for(cfg: Config, name: str | None = None) -> coreaudio.CADevice:
    """設定に合わせた機器セット（既定は「SSL12+BH」）を作る。

    入力元（SSL 12）をクロックソースにし、出力先（BlackHole 2ch）はドリフト補正 ON にする。
    """
    name = name or cfg.device
    if coreaudio.find_device(name) is not None:
        raise DeviceError(f"機器セット「{name}」はすでにあります")
    physical = [d for d in coreaudio.list_devices() if not d.is_aggregate]
    src = _match_subdevice(physical, cfg.source_device)
    if src is None:
        raise DeviceError(f"「{cfg.source_device}」が見つかりません。USB の接続を確認してください")
    dst = _match_subdevice(physical, cfg.sink_device)
    if dst is None:
        raise DeviceError(f"「{cfg.sink_device}」が見つかりません。BlackHole 2ch をインストールしてください")

    uid = "com.koichi.sslmixer.aggregate." + "".join(c if c.isalnum() else "-" for c in name)
    coreaudio.create_aggregate(name, uid, clock=physical[src], others=[physical[dst]])
    # 作成直後は一覧に出るまで少し時間がかかることがあるので待つ
    for _ in range(30):
        dev = coreaudio.find_device(name)
        if dev is not None and dev.in_ch > 0:
            return dev
        time.sleep(0.1)
    raise DeviceError(f"機器セット「{name}」を作りましたが、一覧に現れません")


def check_aggregate_settings(cfg: Config) -> list[str]:
    """クロックソースとドリフト補正の設定が推奨どおりかを調べ、警告文のリストを返す。"""
    warnings = []
    ca_dev = coreaudio.find_device(cfg.device)
    info = coreaudio.get_aggregate_info(ca_dev) if ca_dev else None
    if not info:
        return warnings
    src = _match_subdevice(info.subdevices, cfg.source_device)
    dst = _match_subdevice(info.subdevices, cfg.sink_device)
    if src is not None and info.subdevices[src].uid != info.clock_uid:
        warnings.append(f"クロックソースが「{info.clock_name}」になっています。"
                        f"「{info.subdevices[src].name}」にしてください")
    # クロックソース自身にはドリフト補正は要らないので、別デバイスのときだけ確認する
    if dst is not None:
        sink = info.subdevices[dst]
        if sink.uid != info.clock_uid and info.drift_for(sink.uid) is False:
            warnings.append(f"「{sink.name}」のドリフト補正が OFF です。ON にしてください")
    return warnings
