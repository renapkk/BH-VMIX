"""機器セットを1本の入出力ストリームとして開き、ミックスし続ける。抜き差し時は再接続する。"""

from __future__ import annotations

import gc
import threading
import time
from typing import Callable

import sounddevice as sd

from . import devices
from .config import Config, InputConfig
from .devices import DeviceError, Routing
from .dsp import Mixer, build_mix_matrix

# ストリーム監視の間隔（秒）
POLL_SEC = 0.5
# 機器セットの構成を確かめる間隔（秒）
LAYOUT_CHECK_SEC = 2.0
# コールバックがこの秒数呼ばれなければデバイスが止まったとみなす
STALL_SEC = 2.0
# 入力がこの秒数ずっと完全な 0 ならマイク権限を疑う
SILENCE_WARN_SEC = 3.0
# ストリームを開く処理がこの秒数で終わらなければ案内を出す
OPEN_TIMEOUT_SEC = 5.0

OPEN_HANG_MESSAGE = (
    "オーディオの準備が終わりません。\n"
    "  マイクへのアクセスを求める画面が出ていたら「許可」を押してください（許可すると自動で始まります）。\n"
    "  それでも始まらず、afplay など他のアプリでも音が出ない場合は Core Audio（coreaudiod）が"
    "固まっている可能性があります。Mac を再起動するか、ターミナルで sudo killall coreaudiod を実行してください。"
)

MIC_PERMISSION_MESSAGE = (
    "入力が完全な無音（デジタルゼロ）です。マイク権限が無い可能性があります。\n"
    "  システム設定 > プライバシーとセキュリティ > マイク で、"
    "このツールを実行しているアプリ（ターミナルなど）を ON にし、そのアプリを再起動してください。\n"
    "  権限が ON なのに出る場合は、SSL 12 の入力が本当に無信号でないか確認してください。"
)


class Engine:
    def __init__(self, cfg: Config, test_tone: bool = False,
                 log: Callable[[str], None] = print):
        self.cfg = cfg
        self.test_tone = test_tone
        self.log = log
        self.mixer: Mixer | None = None
        self.routing: Routing | None = None
        self.state = "起動中"
        self._stop = threading.Event()
        self._last_message = ""
        # 入力元（SSL 12）が見つからない状態か。Core Audio への問い合わせはこのスレッドで行い、
        # 画面側（メインスレッド）はこの値を読むだけにする（Core Audio が応答しないと画面ごと止まるため）
        self.source_missing = False

    # ---- 外部から操作するためのメソッド（フェーズ2のメニューバーからも使う） ----

    def stop(self) -> None:
        self._stop.set()

    def apply_settings(self) -> None:
        """self.cfg の内容（フェーダー・パン・ミュート・リミッター・リンクなど）を、
        再生を止めずに反映する。バッファサイズとデバイスの変更は再起動が必要。"""
        mixer = self.mixer
        if mixer is not None:
            self._apply_to(mixer)

    def set_inputs(self, inputs: list[InputConfig]) -> None:
        self.cfg.inputs = inputs
        self.apply_settings()

    def set_mute(self, index: int, mute: bool) -> None:
        self.cfg.inputs[index].mute = mute
        self.apply_settings()

    def set_output_mode(self, mode: str) -> None:
        """"stereo" / "mono" を切り替える（行列を差し替えるだけなので止めずに反映）。"""
        self.cfg.output_mode = mode
        self.apply_settings()

    # ---- 内部処理 ----

    def _matrix(self):
        cfg = self.cfg
        return build_mix_matrix(cfg.inputs, cfg.output_mode, cfg.master_gain_db,
                                linked=cfg.is_linked, balance=cfg.link_balance)

    def _apply_to(self, mixer: Mixer) -> None:
        mixer.set_matrix(self._matrix())
        mixer.channel_limiter.set_settings(self.cfg.inputs, self.cfg.is_linked)
        mixer.limiter_enabled = self.cfg.limiter

    def _source_present(self) -> bool:
        try:
            return devices.source_present(self.cfg)
        except Exception:   # 問い合わせに失敗したときは「ある」とみなして、警告を出さない
            return True

    def _log_once(self, message: str) -> None:
        """同じメッセージが続くときは1回だけ表示する（再試行のたびに出さない）。"""
        if message != self._last_message:
            self.log(message)
            self._last_message = message

    def run(self) -> None:
        """停止されるまで「接続→動作→切断されたら待って再接続」を繰り返す。"""
        while not self._stop.is_set():
            # 起動前に機器セットが作られた場合などに備えて、毎回デバイス一覧を取り直す
            devices.refresh_portaudio()
            try:
                routing = devices.resolve_routing(self.cfg)
            except (DeviceError, sd.PortAudioError) as e:
                self.source_missing = not self._source_present()
                self.state = "接続待ち"
                self._log_once(f"{e}。{self.cfg.reconnect_interval:g}秒おきに再接続を試みます…")
                self._stop.wait(self.cfg.reconnect_interval)
                continue
            try:
                self._run_stream(routing)
            except sd.PortAudioError as e:
                self.log(f"ストリームを開けませんでした: {e}")
            finally:
                self.mixer = None
            if not self._stop.is_set():
                self.state = "接続待ち"
                self._stop.wait(self.cfg.reconnect_interval)
        self.state = "停止"

    def _run_stream(self, routing: Routing) -> None:
        cfg = self.cfg
        samplerate = routing.samplerate
        if int(samplerate) != cfg.samplerate:
            self.log(f"機器セットが {int(samplerate)} Hz で動いているので、それに合わせます"
                     f"（設定は {cfg.samplerate} Hz）")
        for warning in devices.check_aggregate_settings(cfg):
            self.log(f"注意: {warning}")

        mixer = Mixer(
            in_channels=routing.in_channels, out_channels=routing.out_channels,
            matrix=self._matrix(), blocksize=cfg.blocksize, samplerate=samplerate,
            limiter=cfg.limiter, test_tone=self.test_tone,
        )
        self._apply_to(mixer)
        finished = threading.Event()

        def callback(indata, outdata, frames, _time, status):
            mixer.process(indata, outdata, frames, status)

        # 使う ch の一番大きい番号までだけ開く
        channels = (max(routing.in_channels) + 1, max(routing.out_channels) + 1)
        # Core Audio が応答しないとストリームを開く処理が戻ってこないので、一定時間で案内を出す
        watchdog = threading.Timer(OPEN_TIMEOUT_SEC, lambda: self.log(OPEN_HANG_MESSAGE))
        watchdog.daemon = True
        watchdog.start()
        try:
            stream = sd.Stream(
                device=(routing.device_index, routing.device_index),
                samplerate=samplerate, blocksize=cfg.blocksize, dtype="float32",
                channels=channels, latency="low", callback=callback,
                finished_callback=finished.set,
            )
        finally:
            watchdog.cancel()
        # 今あるオブジェクトをガベージコレクション（自動メモリ回収）の対象外にして、
        # 動作中に回収処理で音が途切れる可能性を減らす
        gc.collect()
        gc.freeze()

        self.mixer = mixer
        self.routing = routing
        try:
            stream.start()
            self.state = "動作中"
            self.source_missing = False
            mode = "テストトーン" if self.test_tone else cfg.output_mode
            self._last_message = ""
            self.log(
                f"開始: 「{routing.device_name}」 {int(samplerate)} Hz / バッファサイズ {cfg.blocksize} / "
                f"入力 ch {[c + 1 for c in routing.in_channels]} → 出力 ch "
                f"{[c + 1 for c in routing.out_channels]} / {mode} / "
                f"リミッター {'ON' if cfg.limiter else 'OFF'} / 遅延 {stream.latency[1] * 1000:.1f} ms")
            self._watch(stream, mixer, routing, finished, samplerate)
        finally:
            stream.abort(ignore_errors=True)
            stream.close(ignore_errors=True)

    def _watch(self, stream: sd.Stream, mixer: Mixer, routing: Routing,
               finished: threading.Event, samplerate: float) -> None:
        """ストリームの異常（抜き差し・停止・無音）を見張る。異常があれば戻る。"""
        last_count = mixer.callback_count
        last_progress = time.monotonic()
        last_layout_check = time.monotonic()
        silence_warned = False
        while not self._stop.wait(POLL_SEC):
            now = time.monotonic()
            if finished.is_set() or not stream.active:
                self.log("ストリームが停止しました。再接続します")
                return

            if mixer.callback_count != last_count:
                last_count = mixer.callback_count
                last_progress = now
            elif now - last_progress > STALL_SEC:
                self.log("オーディオが止まりました（デバイスが外れた可能性）。再接続します")
                return

            if now - last_layout_check > LAYOUT_CHECK_SEC:
                last_layout_check = now
                if devices.current_signature(routing) != routing.layout_signature:
                    self.log("機器セットの構成が変わりました（抜き差しなど）。再接続します")
                    return

            silent = mixer.silent_frames >= samplerate * SILENCE_WARN_SEC
            if silent and not silence_warned:
                self.log(MIC_PERMISSION_MESSAGE)
                silence_warned = True
            elif not silent and mixer.silent_frames == 0:
                silence_warned = False
