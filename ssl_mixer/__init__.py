"""BH VMIX — SSL 12 の入力をミックスして BlackHole 2ch へ送る仮想ミキサー。"""

import os

# 古い Mac 向けの numpy は計算ライブラリに OpenBLAS を使い、裏で計算用のスレッドを待機させる。
# そのスレッドが CPU を取り合い、音声処理がときどき大きく遅れる（バッファ 128 で間に合わないことがある）ので、
# numpy を読み込む前に1スレッドに固定する。BH VMIX の計算は小さいので、遅くはならない
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# アプリのバージョン（setup.py・設定ウィンドウ・--version がここを読む）
__version__ = "1.0"
