"""ミックス・パン・リミッター・テストトーンの信号処理。

オーディオコールバックから呼ばれる process 系メソッドでは、
配列の新規確保をしないよう、作業用バッファを最初に確保して out= で書き込む。
（Python なので小さなオブジェクトの生成までは避けられないが、配列データの確保はしない）
"""

from __future__ import annotations

import math

import numpy as np

from .config import InputConfig

DTYPE = np.float32
LIMITER_CEILING_DB = -1.0
LIMITER_RELEASE_SEC = 0.1
TEST_TONE_HZ = 1000.0
TEST_TONE_DB = -12.0
# テストトーンの種類：両方から鳴る / 左だけ → 右だけ と交互に鳴る（ステレオで届いているかの確認用）
TONE_BOTH = "both"
TONE_LR = "lr"
# L/R テストの周期：0.0〜0.9秒は左だけ、1.0〜1.9秒は右だけ（間は無音）
LR_CYCLE_SEC = 2.0
LR_ON_SEC = 0.9
LR_FADE_SEC = 0.01   # 切り替えでプチッと鳴らないよう、10ms かけて出し入れする
# ステレオリンク中にモノで出すとき、L+R を -3dB して合計する
MONO_SUM_GAIN = 1.0 / math.sqrt(2.0)


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def lin_to_db(lin: float, floor_db: float = -120.0) -> float:
    if lin <= 0.0:
        return floor_db
    return max(20.0 * math.log10(lin), floor_db)


def pan_gains(pan: float) -> tuple[float, float]:
    """コンスタントパワーパン。センターで左右とも -3dB（約0.707）。

    pan = -1 で左のみ、+1 で右のみ。左右の二乗和は常に1になる。
    """
    theta = (pan + 1.0) * math.pi / 4.0
    return math.cos(theta), math.sin(theta)


def balance_gains(balance: float) -> tuple[float, float]:
    """ステレオリンク時のバランス。センターで L/R とも 1.0、片側に寄せると反対側だけ下がる。"""
    return min(1.0, 1.0 - balance), min(1.0, 1.0 + balance)


def build_mix_matrix(inputs: list[InputConfig], output_mode: str, master_gain_db: float,
                     linked: bool = False, balance: float = 0.0) -> np.ndarray:
    """入力 (frames, n_in) に掛けて出力 (frames, 2) を得るための行列 (n_in, 2) を作る。

    stereo: 各入力にゲインとパンを掛けて L/R に振り分ける。
    mono:   パンは無視し、ゲインだけ掛けて合計した同じ信号を L/R 両方に出す。
    linked: In 1 を L、In 2 を R に固定し、In 1 のフェーダー・ミュートで両方を動かす。
            mono のときは L+R を -3dB して両方に出す。
    マスターゲインもこの行列に含めてしまう。
    """
    master = db_to_lin(master_gain_db)
    matrix = np.zeros((len(inputs), 2), dtype=DTYPE)
    start = 0
    if linked and len(inputs) >= 2:
        start = 2
        if not inputs[0].mute:
            gain = db_to_lin(inputs[0].gain_db) * master
            left, right = balance_gains(balance)
            if output_mode == "mono":
                matrix[0, :] = gain * left * MONO_SUM_GAIN
                matrix[1, :] = gain * right * MONO_SUM_GAIN
            else:
                matrix[0, 0] = gain * left
                matrix[1, 1] = gain * right
    for i in range(start, len(inputs)):
        inp = inputs[i]
        if inp.mute:
            continue
        gain = db_to_lin(inp.gain_db) * master
        if output_mode == "mono":
            matrix[i, 0] = gain
            matrix[i, 1] = gain
        else:
            left, right = pan_gains(inp.pan)
            matrix[i, 0] = gain * left
            matrix[i, 1] = gain * right
    return matrix


class ChannelLimiter:
    """入力チャンネルごとのリミッター（フェーダーの前にかける）。

    考え方はマスターの Limiter と同じで、全チャンネルを numpy でまとめて処理する。
    ステレオリンク中は In 1 と In 2 の大きい方のピークで両方のゲインを決め、
    L/R が同じ量だけ下がるようにする（定位がずれないように）。
    """

    def __init__(self, n_channels: int, blocksize: int, samplerate: float,
                 release_sec: float = LIMITER_RELEASE_SEC):
        self.release_coef = 1.0 - math.exp(-blocksize / (samplerate * release_sec))
        self.gain = np.ones(n_channels, dtype=DTYPE)          # 今のゲイン（減衰量の表示にも使う）
        self._settings = (np.ones(n_channels, dtype=DTYPE),   # スレッショルド（リニア）
                          np.zeros(n_channels, dtype=bool),   # ON/OFF
                          False)                              # In 1-2 リンク
        self._steps = np.arange(1, blocksize + 1, dtype=DTYPE)[:, np.newaxis]
        self._abs = np.empty((blocksize, n_channels), dtype=DTYPE)
        self._curve = np.empty((blocksize, n_channels), dtype=DTYPE)
        self._peak = np.empty(n_channels, dtype=DTYPE)
        self._target = np.empty(n_channels, dtype=DTYPE)
        self._new = np.empty(n_channels, dtype=DTYPE)
        self._start = np.empty(n_channels, dtype=DTYPE)

    def set_settings(self, inputs: list[InputConfig], linked: bool) -> None:
        """スレッショルドと ON/OFF を差し替える（メインスレッドから呼ぶ）。"""
        src = list(inputs)
        if linked and len(src) >= 2:
            # リンク中は In 2 も In 1 の設定で動かす
            src[1] = src[0]
        thresholds = np.array([db_to_lin(i.limiter_threshold_db) for i in src], dtype=DTYPE)
        enabled = np.array([i.limiter for i in src], dtype=bool)
        self._settings = (thresholds, enabled, linked and len(src) >= 2)

    def process(self, buf: np.ndarray) -> None:
        """buf (frames, n_channels) をその場で書き換える。"""
        thresholds, enabled, linked = self._settings
        frames = buf.shape[0]
        absbuf = self._abs[:frames]
        np.abs(buf, out=absbuf)
        np.max(absbuf, axis=0, out=self._peak)
        if linked:
            both = max(self._peak[0], self._peak[1])
            self._peak[0] = both
            self._peak[1] = both

        # このブロックで許されるゲインの上限（ピークがスレッショルド以下なら 1.0）
        np.maximum(self._peak, thresholds, out=self._target)
        np.divide(thresholds, self._target, out=self._target)
        # リリース：今のゲインからゆっくり 1.0 へ戻す
        np.subtract(1.0, self.gain, out=self._new)
        self._new *= self.release_coef
        self._new += self.gain
        np.minimum(self._new, self._target, out=self._new)
        # OFF のチャンネルは常に 1.0
        np.copyto(self._new, 1.0, where=~enabled)

        # 下げるときは即座に、戻すときは前のゲインから直線で変化させる
        np.minimum(self._new, self.gain, out=self._start)
        curve = self._curve[:frames]
        np.subtract(self._new, self._start, out=self._target)
        self._target /= frames
        np.multiply(self._steps[:frames], self._target, out=curve)
        curve += self._start
        buf *= curve
        self.gain[:] = self._new


class Limiter:
    """マスター段の簡易セーフティリミッター（ブロック単位のピーク検出）。

    ブロック内のピークが上限を超えそうならゲインを即座に下げ（アタック0）、
    超えなくなったらリリース時間をかけてゆっくり 1.0 に戻す。
    ゲインを戻すときはブロック内で直線的に変化させてノイズ（ジッパー音）を防ぐ。
    最後に念のため上限でクリップする。
    """

    def __init__(self, blocksize: int, samplerate: float,
                 ceiling_db: float = LIMITER_CEILING_DB,
                 release_sec: float = LIMITER_RELEASE_SEC):
        self.ceiling = db_to_lin(ceiling_db)
        self.release_coef = 1.0 - math.exp(-blocksize / (samplerate * release_sec))
        self.gain = 1.0
        # 1, 2, 3, … の連番（ゲインを直線で変化させるのに使う）
        self._steps = np.arange(1, blocksize + 1, dtype=DTYPE)
        self._gain_curve = np.empty(blocksize, dtype=DTYPE)
        self._abs = np.empty((blocksize, 2), dtype=DTYPE)

    def process(self, buf: np.ndarray) -> None:
        """buf (frames, 2) をその場で書き換える。"""
        frames = buf.shape[0]
        absbuf = self._abs[:frames]
        np.abs(buf, out=absbuf)
        peak = float(absbuf.max())

        # このブロックで許されるゲインの上限
        target = self.ceiling / peak if peak > self.ceiling else 1.0
        released = self.gain + (1.0 - self.gain) * self.release_coef
        new_gain = min(released, target)

        if new_gain < self.gain:
            # 下げるときは即座に（ブロック全体に一定のゲイン）
            if new_gain < 1.0:
                buf *= new_gain
        elif new_gain != 1.0 or self.gain != 1.0:
            # 戻すときは前のゲイン→新しいゲインへ直線で変化させる
            curve = self._gain_curve[:frames]
            np.multiply(self._steps[:frames], (new_gain - self.gain) / frames, out=curve)
            curve += self.gain
            buf *= curve[:, np.newaxis]
        self.gain = new_gain
        np.clip(buf, -self.ceiling, self.ceiling, out=buf)


def tone_mode(value) -> str | None:
    """テストトーンの指定をそろえる（False/None → なし、True → 両方、"lr" → L/R 交互）。"""
    if value in (None, False):
        return None
    if value is True:
        return TONE_BOTH
    if value not in (TONE_BOTH, TONE_LR):
        raise ValueError(f"テストトーンの種類が違います: {value!r}")
    return value


class ToneGenerator:
    """1kHz サイン波（位相がブロック間で途切れないようにする）。

    alternate=True なら、左だけ → 右だけ と交互に鳴らす（L/R テスト）。
    """

    def __init__(self, blocksize: int, samplerate: float,
                 freq: float = TEST_TONE_HZ, level_db: float = TEST_TONE_DB,
                 alternate: bool = False):
        self.amp = db_to_lin(level_db)
        self.step = 2.0 * math.pi * freq / samplerate
        self.phase = 0.0
        self.samplerate = samplerate
        self.alternate = alternate
        self.position = 0                       # L/R 周期の中の位置（サンプル数）
        self._cycle = int(round(LR_CYCLE_SEC * samplerate))
        self._index = np.arange(blocksize, dtype=np.float64)
        self._work = np.empty(blocksize, dtype=np.float64)
        self._t = np.empty(blocksize, dtype=np.float64)
        self._env_l = np.empty(blocksize, dtype=np.float64)
        self._env_r = np.empty(blocksize, dtype=np.float64)
        self._tmp = np.empty(blocksize, dtype=np.float64)

    def generate(self, out: np.ndarray) -> None:
        """out (frames,) にサイン波を書き込む。"""
        frames = out.shape[0]
        work = self._work[:frames]
        np.multiply(self._index[:frames], self.step, out=work)
        work += self.phase
        np.sin(work, out=work)
        work *= self.amp
        out[:] = work
        self.phase = (self.phase + self.step * frames) % (2.0 * math.pi)

    def generate_stereo(self, mix: np.ndarray) -> None:
        """mix (frames, 2) にテストトーンを書き込む。L/R テストなら左右を交互に鳴らす。"""
        self.generate(mix[:, 0])
        if not self.alternate:
            mix[:, 1] = mix[:, 0]
            return
        frames = mix.shape[0]
        t = self._t[:frames]
        np.add(self._index[:frames], self.position, out=t)
        t /= self.samplerate
        np.mod(t, LR_CYCLE_SEC, out=t)
        env_l, env_r = self._env_l[:frames], self._env_r[:frames]
        self._envelope(t, 0.0, LR_ON_SEC, env_l)
        self._envelope(t, LR_CYCLE_SEC / 2, LR_CYCLE_SEC / 2 + LR_ON_SEC, env_r)
        np.multiply(mix[:, 0], env_r, out=mix[:, 1], casting="unsafe")
        np.multiply(mix[:, 0], env_l, out=mix[:, 0], casting="unsafe")
        self.position = (self.position + frames) % self._cycle

    def _envelope(self, t: np.ndarray, start: float, end: float, out: np.ndarray) -> None:
        """start〜end の間だけ 1.0（前後 LR_FADE_SEC で出し入れ）、ほかは 0.0 の音量カーブ。"""
        tmp = self._tmp[:t.shape[0]]
        np.subtract(t, start, out=out)
        np.subtract(end, t, out=tmp)
        np.minimum(out, tmp, out=out)
        out /= LR_FADE_SEC
        np.clip(out, 0.0, 1.0, out=out)


class Mixer:
    """コールバックの中身。

    入力chの選択 → チャンネルリミッター → 行列でミックス（フェーダー・パン・マスター）
    → マスターリミッター → 出力chへ書き込み。

    ゲインやミュートを変えるときは set_matrix() で行列を丸ごと差し替える。
    Python の属性の代入は一度に行われるので、ロックなしで安全に差し替えられる。
    """

    def __init__(self, in_channels: list[int], out_channels: list[int], matrix: np.ndarray,
                 blocksize: int, samplerate: float, limiter: bool, test_tone: bool):
        n_in = len(in_channels)
        self.in_channels = np.asarray(in_channels, dtype=np.intp)
        self.out_left, self.out_right = out_channels
        self.blocksize = blocksize
        self._matrix = matrix
        # リミッターは常に用意しておき、ON/OFF はフラグで切り替える（動作中に切り替えられるように）
        self.limiter = Limiter(blocksize, samplerate)
        self.limiter_enabled = limiter
        self.channel_limiter = ChannelLimiter(n_in, blocksize, samplerate)
        mode = tone_mode(test_tone)
        self.tone = (ToneGenerator(blocksize, samplerate, alternate=mode == TONE_LR)
                     if mode is not None else None)

        # 作業用バッファ（最初に確保して使い回す）
        self._in_sel = np.empty((blocksize, n_in), dtype=DTYPE)
        self._in_abs = np.empty((blocksize, n_in), dtype=DTYPE)
        self._mix = np.empty((blocksize, 2), dtype=DTYPE)
        self._mix_abs = np.empty((blocksize, 2), dtype=DTYPE)
        self._block_peak_in = np.empty(n_in, dtype=DTYPE)
        self._block_peak_out = np.empty(2, dtype=DTYPE)

        # メーター用のピーク（メーター側が読んだら 0 に戻す）
        self.peak_in = np.zeros(n_in, dtype=DTYPE)
        self.peak_out = np.zeros(2, dtype=DTYPE)

        # 状態の記録（メインスレッドが読む）
        self.callback_count = 0
        self.xrun_count = 0
        self.silent_frames = 0       # 入力が完全な 0 だった連続フレーム数
        self.oversize_count = 0      # 想定より大きいブロックが来た回数

    def set_matrix(self, matrix: np.ndarray) -> None:
        self._matrix = matrix

    def process(self, indata: np.ndarray, outdata: np.ndarray, frames: int, status) -> None:
        self.callback_count += 1
        if status:
            self.xrun_count += 1
        outdata.fill(0.0)
        if frames > self.blocksize:
            # 確保したバッファより大きいブロックは処理できないので無音を出す
            self.oversize_count += 1
            return

        in_sel = self._in_sel[:frames]
        mix = self._mix[:frames]
        np.take(indata, self.in_channels, axis=1, out=in_sel)

        # 入力メーターと無音検知
        in_abs = self._in_abs[:frames]
        np.abs(in_sel, out=in_abs)
        np.max(in_abs, axis=0, out=self._block_peak_in)
        np.maximum(self.peak_in, self._block_peak_in, out=self.peak_in)
        if self._block_peak_in.max() == 0.0:
            self.silent_frames += frames
        else:
            self.silent_frames = 0

        if self.tone is not None:
            self.tone.generate_stereo(mix)
        else:
            self.channel_limiter.process(in_sel)
            np.matmul(in_sel, self._matrix, out=mix)

        if self.limiter_enabled:
            self.limiter.process(mix)
        else:
            self.limiter.gain = 1.0

        # 出力メーター
        mix_abs = self._mix_abs[:frames]
        np.abs(mix, out=mix_abs)
        np.max(mix_abs, axis=0, out=self._block_peak_out)
        np.maximum(self.peak_out, self._block_peak_out, out=self.peak_out)

        outdata[:, self.out_left] = mix[:, 0]
        outdata[:, self.out_right] = mix[:, 1]
