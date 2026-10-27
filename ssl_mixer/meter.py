"""ターミナルに簡易ピークメーターを表示する。"""

from __future__ import annotations

import shutil
import sys
import threading
import time
import unicodedata

from .dsp import LIMITER_CEILING_DB, lin_to_db
from .engine import Engine

FLOOR_DB = -60.0
BAR_WIDTH = 40
REFRESH_SEC = 0.1
# メーターの戻りの速さ（dB/秒）
FALL_DB_PER_SEC = 20.0


class Console:
    """メーター表示とログ表示が混ざらないように、画面への出力をまとめて管理する。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._drawn_lines = 0

    def log(self, message: str) -> None:
        with self._lock:
            self._erase()
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] {message}", flush=True)

    def draw(self, lines: list[str]) -> None:
        # メーターの最後の行のあとに改行を入れない（画面がスクロールして位置がずれるのを防ぐ）
        # 行が折り返すとカーソルを戻す行数がずれるので、画面の幅に収まるよう切り詰める
        width = shutil.get_terminal_size().columns - 1
        with self._lock:
            self._to_top()
            sys.stdout.write("\n".join(f"\r\x1b[2K{_fit(line, width)}" for line in lines))
            sys.stdout.flush()
            self._drawn_lines = len(lines)

    def _to_top(self) -> None:
        # 前回描いたメーターの1行目の先頭へカーソルを戻す
        if self._drawn_lines > 1:
            sys.stdout.write(f"\x1b[{self._drawn_lines - 1}A")
        sys.stdout.write("\r")

    def _erase(self) -> None:
        # 前回描いたメーターを1行ずつ消し、1行目の先頭に戻る
        if self._drawn_lines:
            n = self._drawn_lines
            self._to_top()
            sys.stdout.write("\n".join(["\x1b[2K"] * n))
            if n > 1:
                sys.stdout.write(f"\x1b[{n - 1}A")
            sys.stdout.write("\r")
            self._drawn_lines = 0


def _char_width(ch: str) -> int:
    # 全角文字（漢字・かななど）は画面上で2文字分の幅になる
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _fit(line: str, width: int) -> str:
    """画面上の幅が width を超えないように行を切り詰める。"""
    used = 0
    for i, ch in enumerate(line):
        used += _char_width(ch)
        if used > width:
            return line[:i]
    return line


def _bar(db: float, bar_width: int) -> str:
    # 端末によって幅が変わる記号（█ や ·）は避けて ASCII で描く
    filled = int(round((db - FLOOR_DB) / -FLOOR_DB * bar_width))
    filled = max(0, min(bar_width, filled))
    return "#" * filled + "-" * (bar_width - filled)


class Meter(threading.Thread):
    def __init__(self, engine: Engine, console: Console):
        super().__init__(daemon=True)
        self.engine = engine
        self.console = console
        self._display: list[float] = []

    def run(self) -> None:
        last = time.monotonic()
        while self.engine.state != "停止":
            time.sleep(REFRESH_SEC)
            now = time.monotonic()
            dt, last = now - last, now
            self.console.draw(self._lines(dt))

    def _lines(self, dt: float) -> list[str]:
        cfg = self.engine.cfg
        mixer = self.engine.mixer
        names = [inp.name for inp in cfg.inputs] + ["Out L", "Out R"]
        if mixer is not None:
            # ピークを読み取ってから 0 に戻す（次の表示までの最大値になる）
            peaks = list(mixer.peak_in) + list(mixer.peak_out)
            mixer.peak_in.fill(0.0)
            mixer.peak_out.fill(0.0)
        else:
            peaks = [0.0] * len(names)
        if len(self._display) != len(names):
            self._display = [FLOOR_DB] * len(names)

        name_width = max(_text_width(n) for n in names)
        # 名前・数値・MUTE 表示の分を残して、バーの長さを画面の幅に合わせる
        columns = shutil.get_terminal_size().columns - 1
        bar_width = max(10, min(BAR_WIDTH, columns - name_width - 20))
        lines = []
        for i, (name, peak) in enumerate(zip(names, peaks)):
            db = lin_to_db(float(peak), FLOOR_DB)
            # 上がるときは即座に、下がるときはゆっくり
            self._display[i] = max(db, self._display[i] - FALL_DB_PER_SEC * dt)
            shown = self._display[i]
            mute = ""
            if i < len(cfg.inputs) and cfg.inputs[i].mute:
                mute = " MUTE"
            over = " LIM" if i >= len(cfg.inputs) and shown >= LIMITER_CEILING_DB - 0.05 else ""
            text = f"{shown:6.1f} dB" if shown > FLOOR_DB else "  -inf dB"
            pad = " " * (name_width - _text_width(name))
            lines.append(f"{name}{pad} {_bar(shown, bar_width)} {text}{mute}{over}")

        status = self.engine.state
        if mixer is not None:
            status += f" | xrun {mixer.xrun_count}"
            if mixer.limiter_enabled:
                status += f" | リミッター {lin_to_db(mixer.limiter.gain):.1f} dB"
        lines.append(status + " | Ctrl+C で終了")
        return lines


def _text_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)
