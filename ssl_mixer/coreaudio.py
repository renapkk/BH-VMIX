"""macOS の Core Audio を ctypes で直接呼び出し、デバイス情報を取得する。

sounddevice（PortAudio）からは機器セット（Aggregate Device）の中身が見えないため、
サブデバイスの並び順・ch数・クロックソース・ドリフト補正をここで調べる。
ctypes は Python 標準ライブラリなので追加パッケージは不要。
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import POINTER, Structure, byref, c_bool, c_char_p, c_int32, c_long, c_uint32, c_void_p
from dataclasses import dataclass, field

_CA = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_CF = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _fourcc(code: str) -> int:
    """'dev#' のような4文字コードを Core Audio が使う整数に変換する。"""
    return int.from_bytes(code.encode("ascii"), "big")


# Core Audio の定数（AudioHardware.h / AudioHardwareBase.h より）
_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = _fourcc("glob")
_SCOPE_INPUT = _fourcc("inpt")
_SCOPE_OUTPUT = _fourcc("outp")
_ELEMENT_MAIN = 0

_PROP_DEVICES = _fourcc("dev#")
_PROP_NAME = _fourcc("lnam")
_PROP_UID = _fourcc("uid ")
_PROP_CLASS = _fourcc("clas")
_PROP_OWNED_OBJECTS = _fourcc("ownd")
_PROP_STREAM_CONFIG = _fourcc("slay")
_PROP_NOMINAL_SR = _fourcc("nsrt")
_PROP_IS_ALIVE = _fourcc("livn")
_PROP_AGG_ACTIVE_SUBDEVICES = _fourcc("agrp")
_PROP_AGG_MAIN_SUBDEVICE = _fourcc("amst")
_PROP_SUB_DRIFT_COMP = _fourcc("drft")

_CLASS_AGGREGATE = _fourcc("aagg")
_CLASS_SUBDEVICE = _fourcc("asub")

_CF_UTF8 = 0x08000100


class _PropertyAddress(Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


class _AudioBuffer(Structure):
    _fields_ = [("mNumberChannels", c_uint32), ("mDataByteSize", c_uint32), ("mData", c_void_p)]


class _AudioBufferList(Structure):
    _fields_ = [("mNumberBuffers", c_uint32), ("mBuffers", _AudioBuffer * 1)]


_CA.AudioObjectGetPropertyDataSize.argtypes = [
    c_uint32, POINTER(_PropertyAddress), c_uint32, c_void_p, POINTER(c_uint32)]
_CA.AudioObjectGetPropertyDataSize.restype = c_int32
_CA.AudioObjectGetPropertyData.argtypes = [
    c_uint32, POINTER(_PropertyAddress), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
_CA.AudioObjectGetPropertyData.restype = c_int32

_CF.CFStringGetLength.argtypes = [c_void_p]
_CF.CFStringGetLength.restype = c_long
_CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
_CF.CFStringGetCString.restype = c_bool
_CF.CFRelease.argtypes = [c_void_p]
_CF.CFRelease.restype = None
_CF.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
_CF.CFStringCreateWithCString.restype = c_void_p
_CF.CFNumberCreate.argtypes = [c_void_p, c_long, c_void_p]
_CF.CFNumberCreate.restype = c_void_p
_CF.CFDictionaryCreateMutable.argtypes = [c_void_p, c_long, c_void_p, c_void_p]
_CF.CFDictionaryCreateMutable.restype = c_void_p
_CF.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
_CF.CFDictionarySetValue.restype = None
_CF.CFArrayCreateMutable.argtypes = [c_void_p, c_long, c_void_p]
_CF.CFArrayCreateMutable.restype = c_void_p
_CF.CFArrayAppendValue.argtypes = [c_void_p, c_void_p]
_CF.CFArrayAppendValue.restype = None

_CA.AudioHardwareCreateAggregateDevice.argtypes = [c_void_p, POINTER(c_uint32)]
_CA.AudioHardwareCreateAggregateDevice.restype = c_int32
_CA.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]
_CA.AudioHardwareDestroyAggregateDevice.restype = c_int32
_CA.AudioObjectSetPropertyData.argtypes = [
    c_uint32, POINTER(_PropertyAddress), c_uint32, c_void_p, c_uint32, c_void_p]
_CA.AudioObjectSetPropertyData.restype = c_int32

# CFDictionary / CFArray に入れた値を自動で管理させるための設定（CoreFoundation の定数）
_DICT_KEY_CALLBACKS = ctypes.addressof(ctypes.c_char.in_dll(_CF, "kCFTypeDictionaryKeyCallBacks"))
_DICT_VALUE_CALLBACKS = ctypes.addressof(ctypes.c_char.in_dll(_CF, "kCFTypeDictionaryValueCallBacks"))
_ARRAY_CALLBACKS = ctypes.addressof(ctypes.c_char.in_dll(_CF, "kCFTypeArrayCallBacks"))
_CF_NUMBER_SINT32 = 3


@dataclass
class CADevice:
    """Core Audio から見た1台のデバイス。"""
    id: int
    name: str
    uid: str
    in_ch: int
    out_ch: int
    samplerate: float
    is_aggregate: bool


@dataclass
class SubDeviceSetting:
    """機器セット内のサブデバイスごとの設定（ドリフト補正）。"""
    uid: str
    name: str
    drift_compensation: bool | None


@dataclass
class AggregateInfo:
    """機器セットの構成。subdevices は Core Audio 上の ch の並び順と同じ。"""
    device: CADevice
    subdevices: list[CADevice]
    clock_uid: str | None
    settings: list[SubDeviceSetting] = field(default_factory=list)

    @property
    def clock_name(self) -> str | None:
        for sub in self.subdevices:
            if sub.uid == self.clock_uid:
                return sub.name
        return None

    def drift_for(self, uid: str) -> bool | None:
        for s in self.settings:
            if s.uid == uid:
                return s.drift_compensation
        return None


def _address(selector: int, scope: int = _SCOPE_GLOBAL) -> _PropertyAddress:
    return _PropertyAddress(selector, scope, _ELEMENT_MAIN)


def _get_raw(obj_id: int, selector: int, scope: int = _SCOPE_GLOBAL) -> bytes | None:
    """プロパティの値を生のバイト列で取得する。取得できなければ None。"""
    addr = _address(selector, scope)
    size = c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(obj_id, byref(addr), 0, None, byref(size)) != 0:
        return None
    if size.value == 0:
        return b""
    buf = ctypes.create_string_buffer(size.value)
    if _CA.AudioObjectGetPropertyData(obj_id, byref(addr), 0, None, byref(size), buf) != 0:
        return None
    return buf.raw[: size.value]


def _get_u32(obj_id: int, selector: int, scope: int = _SCOPE_GLOBAL) -> int | None:
    raw = _get_raw(obj_id, selector, scope)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack_from("<I", raw)[0]


def _get_u32_list(obj_id: int, selector: int) -> list[int]:
    raw = _get_raw(obj_id, selector) or b""
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def _cfstring_to_str(ref: int) -> str:
    length = _CF.CFStringGetLength(ref)
    bufsize = length * 4 + 1
    buf = ctypes.create_string_buffer(bufsize)
    if not _CF.CFStringGetCString(ref, buf, bufsize, _CF_UTF8):
        return ""
    return buf.value.decode("utf-8")


def _get_string(obj_id: int, selector: int) -> str | None:
    """CFString 型のプロパティを Python の文字列で取得する。"""
    addr = _address(selector)
    ref = c_void_p()
    size = c_uint32(ctypes.sizeof(c_void_p))
    if _CA.AudioObjectGetPropertyData(obj_id, byref(addr), 0, None, byref(size), byref(ref)) != 0:
        return None
    if not ref.value:
        return None
    try:
        return _cfstring_to_str(ref.value)
    finally:
        _CF.CFRelease(ref.value)


def _channel_count(dev_id: int, scope: int) -> int:
    """入力または出力の ch 数（全ストリームの合計）を返す。"""
    raw = _get_raw(dev_id, _PROP_STREAM_CONFIG, scope)
    if not raw or len(raw) < 4:
        return 0
    n_buffers = struct.unpack_from("<I", raw)[0]
    offset = _AudioBufferList.mBuffers.offset
    step = ctypes.sizeof(_AudioBuffer)
    total = 0
    for i in range(n_buffers):
        pos = offset + step * i
        if pos + 4 > len(raw):
            break
        total += struct.unpack_from("<I", raw, pos)[0]
    return total


def _device(dev_id: int) -> CADevice:
    raw_sr = _get_raw(dev_id, _PROP_NOMINAL_SR)
    samplerate = struct.unpack_from("<d", raw_sr)[0] if raw_sr and len(raw_sr) >= 8 else 0.0
    return CADevice(
        id=dev_id,
        name=_get_string(dev_id, _PROP_NAME) or "",
        uid=_get_string(dev_id, _PROP_UID) or "",
        in_ch=_channel_count(dev_id, _SCOPE_INPUT),
        out_ch=_channel_count(dev_id, _SCOPE_OUTPUT),
        samplerate=samplerate,
        is_aggregate=_get_u32(dev_id, _PROP_CLASS) == _CLASS_AGGREGATE,
    )


def list_devices() -> list[CADevice]:
    """システム上の全オーディオデバイスを返す。"""
    return [_device(dev_id) for dev_id in _get_u32_list(_SYSTEM_OBJECT, _PROP_DEVICES)]


def find_device(name: str) -> CADevice | None:
    """名前が完全一致するデバイスを返す。"""
    for dev in list_devices():
        if dev.name == name:
            return dev
    return None


def is_alive(dev_id: int) -> bool:
    """デバイスが現在使用可能か（抜かれていないか）を返す。"""
    return _get_u32(dev_id, _PROP_IS_ALIVE) == 1


class AggregateCreateError(RuntimeError):
    """機器セットを作れなかったときに投げる例外。"""


def _cfstr(text: str) -> int:
    return _CF.CFStringCreateWithCString(None, text.encode("utf-8"), _CF_UTF8)


def _cfint(value: int) -> int:
    v = ctypes.c_int32(value)
    return _CF.CFNumberCreate(None, _CF_NUMBER_SINT32, byref(v))


def _cfdict(items: dict[str, int]) -> int:
    """{キー: CF の値} から CFDictionary を作る（値の所有権は辞書に移す）。"""
    d = _CF.CFDictionaryCreateMutable(None, 0, _DICT_KEY_CALLBACKS, _DICT_VALUE_CALLBACKS)
    for key, value in items.items():
        k = _cfstr(key)
        _CF.CFDictionarySetValue(d, k, value)
        _CF.CFRelease(k)
        _CF.CFRelease(value)
    return d


def create_aggregate(name: str, uid: str, clock: CADevice, others: list[CADevice]) -> int:
    """機器セットを作り、そのデバイスIDを返す。

    clock をクロックソース（先頭のサブデバイス）にし、others はドリフト補正 ON で後ろに並べる。
    private=0 にすると Audio MIDI 設定にも表示される、通常の機器セットになる。
    """
    subs = _CF.CFArrayCreateMutable(None, 0, _ARRAY_CALLBACKS)
    for dev, drift in [(clock, 0)] + [(d, 1) for d in others]:
        sub = _cfdict({"uid": _cfstr(dev.uid), "drift": _cfint(drift)})
        _CF.CFArrayAppendValue(subs, sub)
        _CF.CFRelease(sub)
    desc = _cfdict({
        "name": _cfstr(name),
        "uid": _cfstr(uid),
        "subdevices": subs,
        "master": _cfstr(clock.uid),   # クロックソース（kAudioAggregateDeviceMainSubDeviceKey）
        "private": _cfint(0),
        "stacked": _cfint(0),
    })
    out_id = c_uint32(0)
    try:
        status = _CA.AudioHardwareCreateAggregateDevice(desc, byref(out_id))
    finally:
        _CF.CFRelease(desc)
    if status != 0:
        raise AggregateCreateError(f"Core Audio がエラーを返しました（コード {status}）")
    _ensure_drift(out_id.value, {d.uid for d in others})
    return out_id.value


def _ensure_drift(agg_id: int, uids: set[str]) -> None:
    """念のため、指定したサブデバイスのドリフト補正を ON にし直す。"""
    one = c_uint32(1)
    for obj_id in _get_u32_list(agg_id, _PROP_OWNED_OBJECTS):
        if _get_u32(obj_id, _PROP_CLASS) != _CLASS_SUBDEVICE:
            continue
        if _get_string(obj_id, _PROP_UID) in uids:
            addr = _address(_PROP_SUB_DRIFT_COMP)
            _CA.AudioObjectSetPropertyData(obj_id, byref(addr), 0, None, ctypes.sizeof(one), byref(one))


def destroy_aggregate(dev_id: int) -> None:
    """機器セットを削除する（テストで作ったものの後片付け用）。"""
    status = _CA.AudioHardwareDestroyAggregateDevice(dev_id)
    if status != 0:
        raise AggregateCreateError(f"機器セットを削除できませんでした（コード {status}）")


def get_aggregate_info(dev: CADevice) -> AggregateInfo | None:
    """機器セットのサブデバイス構成を返す。機器セットでなければ None。"""
    if not dev.is_aggregate:
        return None
    subdevices = [_device(sub_id) for sub_id in _get_u32_list(dev.id, _PROP_AGG_ACTIVE_SUBDEVICES)]
    settings = []
    for obj_id in _get_u32_list(dev.id, _PROP_OWNED_OBJECTS):
        if _get_u32(obj_id, _PROP_CLASS) != _CLASS_SUBDEVICE:
            continue
        drift = _get_u32(obj_id, _PROP_SUB_DRIFT_COMP)
        settings.append(SubDeviceSetting(
            uid=_get_string(obj_id, _PROP_UID) or "",
            name=_get_string(obj_id, _PROP_NAME) or "",
            drift_compensation=None if drift is None else bool(drift),
        ))
    return AggregateInfo(
        device=dev,
        subdevices=subdevices,
        clock_uid=_get_string(dev.id, _PROP_AGG_MAIN_SUBDEVICE),
        settings=settings,
    )
