"""壊れた設定・プリセット・おかしな値への備えのテスト（QA の指摘 C-2, C-3, M-1〜M-5）。
実行: .venv/bin/python -m unittest discover -s tests -t ."""

import math
import tempfile
import tomllib
import unittest
from pathlib import Path

import numpy as np

from ssl_mixer.config import (Config, ConfigError, InputConfig, backup_broken, dump_config, load_config,
                              parse_config, save_config)
from ssl_mixer.dsp import Mixer, build_mix_matrix
from ssl_mixer.engine import Engine
from ssl_mixer.presets import load_preset, save_preset


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class ReadErrorTest(TempDirTest):
    """読めないファイルは、すべて ConfigError（画面に出せるエラー）になる。"""

    def test_config_read_errors(self):
        sjis = self.folder / "sjis.toml"
        sjis.write_bytes('name = "ボーカル"\n'.encode("shift_jis"))
        binary = self.folder / "binary.toml"
        binary.write_bytes(bytes(range(256)))
        for path in (sjis, binary, self.folder, self.folder / "ない.toml"):
            with self.subTest(path=path.name), self.assertRaises(ConfigError):
                load_config(path)

    def test_preset_read_errors(self):
        sjis = self.folder / "sjis.bhvmix"
        sjis.write_bytes('[[inputs]]\nname = "ボーカル"\n'.encode("shift_jis"))
        empty = self.folder / "empty.bhvmix"
        empty.write_bytes(b"")
        other = self.folder / "other.bhvmix"
        other.write_text('device = "SSL12+BH"\n', encoding="utf-8")
        folder = self.folder / "folder.bhvmix"
        folder.mkdir()
        for path in (sjis, empty, other, folder):
            with self.subTest(path=path.name), self.assertRaises(ConfigError):
                load_preset(path)


class BackupTest(TempDirTest):
    def test_broken_config_is_kept(self):
        path = self.folder / "config.toml"
        path.write_text('blocksize = 128\n[[inputs]]\nname = "Vo"\npan = 1.5\n', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)
        backup = backup_broken(path)
        # 既定値で上書き保存されても、元の内容はコピーに残っている
        save_config(Config(), path)
        self.assertIn('name = "Vo"', backup.read_text(encoding="utf-8"))
        self.assertTrue(backup.name.startswith("config.toml.broken-"))

    def test_no_backup_when_missing(self):
        self.assertIsNone(backup_broken(self.folder / "ない.toml"))


class RangeTest(unittest.TestCase):
    def test_rejects_out_of_range_and_non_finite(self):
        bad = [
            {"master_gain_db": 1e6},
            {"master_gain_db": math.nan},
            {"inputs": [{"gain_db": math.inf}]},
            {"inputs": [{"gain_db": -100.0}]},
            {"inputs": [{"pan": math.nan}]},
            {"link_balance": math.nan},
            {"reconnect_interval": math.nan},
            {"reconnect_interval": math.inf},
            {"reconnect_interval": 0.01},
            {"blocksize": 100},
            {"blocksize": 1_000_000_000},
            {"samplerate": 1},
            {"device": ""},
            {"sink_device": "  "},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                parse_config(data)

    def test_accepts_ui_range(self):
        cfg = parse_config({"master_gain_db": 12.0, "blocksize": 128, "samplerate": 44100,
                            "reconnect_interval": 0.5,
                            "inputs": [{"gain_db": -60.0}, {"gain_db": 12}]})
        self.assertEqual((cfg.blocksize, cfg.inputs[1].gain_db), (128, 12.0))


class NameTest(unittest.TestCase):
    def test_control_characters_survive_round_trip(self):
        cfg = Config()
        cfg.inputs[0].name = "a\rb\x07c\nd"
        cfg.inputs[1].name = '👨‍👩‍👧 "Gt" \\'
        again = parse_config(tomllib.loads(dump_config(cfg)))
        # 制御文字は読み込むときに取り除く。絵文字・記号はそのまま
        self.assertEqual(again.inputs[0].name, "abcd")
        self.assertEqual(again.inputs[1].name, '👨‍👩‍👧 "Gt" \\')

    def test_long_name_is_trimmed(self):
        cfg = parse_config({"inputs": [{"name": "x" * 5000}]})
        self.assertEqual(len(cfg.inputs[0].name), 32)


class PresetSaveTest(TempDirTest):
    def test_save_leaves_no_temp_file(self):
        save_preset(Config(), self.folder / "a")
        save_preset(Config(), self.folder / "a")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["a.bhvmix"])


class InputCountTest(unittest.TestCase):
    def test_engine_ignores_matrix_with_other_input_count(self):
        """入力4で動作中に2入力のプリセットを当てても、音声処理は例外で止まらない。"""
        inputs = [InputConfig(f"In {i}") for i in range(4)]
        mixer = Mixer(in_channels=[0, 1, 2, 3], out_channels=[4, 5],
                      matrix=build_mix_matrix(inputs, "stereo", 0.0), blocksize=64,
                      samplerate=48000, limiter=True, test_tone=False)
        cfg = Config()
        cfg.inputs = cfg.inputs[:2]
        engine = Engine(cfg, log=lambda _: None)
        engine._apply_to(mixer)
        self.assertEqual(mixer.channel_limiter.gain.shape, (4,))

        indata = np.zeros((64, 6), dtype=np.float32)
        outdata = np.zeros((64, 6), dtype=np.float32)
        mixer.process(indata, outdata, 64, None)   # 例外が出なければよい


class MixMathTest(unittest.TestCase):
    def test_mix_matches_matrix_product_without_warnings(self):
        """音声処理のミックスは行列の積と同じ結果で、警告（OpenBLAS の誤検知など）を出さない。"""
        import warnings
        cfg = Config()
        cfg.inputs[2].pan = -0.4
        cfg.inputs[3].gain_db = -6.0
        matrix = build_mix_matrix(cfg.inputs, "stereo", -2.0, linked=True, balance=0.3)
        mixer = Mixer(in_channels=[0, 1, 2, 3], out_channels=[4, 5], matrix=matrix, blocksize=128,
                      samplerate=48000, limiter=False, test_tone=False)
        rng = np.random.default_rng(1)
        indata = (rng.standard_normal((128, 6)) * 0.3).astype(np.float32)
        outdata = np.zeros((128, 6), dtype=np.float32)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            mixer.process(indata, outdata, 128, None)
        expected = indata[:, :4].astype(np.float64) @ matrix.astype(np.float64)
        np.testing.assert_allclose(outdata[:, 4:6], expected, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
