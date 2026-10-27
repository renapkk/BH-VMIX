"""信号処理・設定・ch判定のテスト。実行: .venv/bin/python -m unittest discover -s tests"""

import math
import tomllib
import unittest
from pathlib import Path

import numpy as np

from ssl_mixer.config import ConfigError, InputConfig, dump_config, parse_config
from ssl_mixer.coreaudio import CADevice
from ssl_mixer.devices import DeviceError, compute_offsets
from ssl_mixer.dsp import (ChannelLimiter, Limiter, Mixer, ToneGenerator, build_mix_matrix, db_to_lin,
                           pan_gains)

PROJECT_DIR = Path(__file__).resolve().parent.parent


class PanTest(unittest.TestCase):
    def test_center_is_minus_3db(self):
        left, right = pan_gains(0.0)
        self.assertAlmostEqual(20 * math.log10(left), -3.01, places=2)
        self.assertAlmostEqual(left, right)

    def test_hard_left_and_right(self):
        self.assertEqual(tuple(round(v, 6) for v in pan_gains(-1.0)), (1.0, 0.0))
        self.assertEqual(tuple(round(v, 6) for v in pan_gains(1.0)), (0.0, 1.0))

    def test_constant_power(self):
        for pan in np.linspace(-1, 1, 21):
            left, right = pan_gains(float(pan))
            self.assertAlmostEqual(left ** 2 + right ** 2, 1.0)


class MixMatrixTest(unittest.TestCase):
    def test_stereo_gain_pan_mute(self):
        inputs = [InputConfig("a", gain_db=-6.0, pan=-1.0), InputConfig("b", mute=True),
                  InputConfig("c", pan=1.0)]
        m = build_mix_matrix(inputs, "stereo", 0.0)
        self.assertAlmostEqual(m[0, 0], db_to_lin(-6.0), places=6)
        self.assertAlmostEqual(m[0, 1], 0.0, places=6)
        self.assertTrue(np.all(m[1] == 0.0))
        self.assertAlmostEqual(m[2, 1], 1.0, places=6)

    def test_mono_ignores_pan(self):
        inputs = [InputConfig("a", pan=-1.0), InputConfig("b", gain_db=-6.0, pan=0.5)]
        m = build_mix_matrix(inputs, "mono", -6.0)
        master = db_to_lin(-6.0)
        np.testing.assert_allclose(m[0], [master, master], rtol=1e-6)
        np.testing.assert_allclose(m[1], [master * db_to_lin(-6.0)] * 2, rtol=1e-6)


class LinkMatrixTest(unittest.TestCase):
    def _inputs(self):
        return [InputConfig("a", gain_db=-6.0, pan=0.7), InputConfig("b", gain_db=0.0, mute=True),
                InputConfig("c", pan=-1.0)]

    def test_stereo_link_uses_in1_settings(self):
        m = build_mix_matrix(self._inputs(), "stereo", 0.0, linked=True)
        g = db_to_lin(-6.0)
        # In 1 は L だけ、In 2 は R だけ。In 2 のミュート・ゲインは無視して In 1 の設定を使う
        np.testing.assert_allclose(m[0], [g, 0.0], rtol=1e-6)
        np.testing.assert_allclose(m[1], [0.0, g], rtol=1e-6)
        # In 3 はふつうのパン
        np.testing.assert_allclose(m[2], [1.0, 0.0], atol=1e-6)

    def test_balance(self):
        m = build_mix_matrix(self._inputs(), "stereo", 0.0, linked=True, balance=0.5)
        g = db_to_lin(-6.0)
        np.testing.assert_allclose([m[0, 0], m[1, 1]], [g * 0.5, g], rtol=1e-6)

    def test_link_mute_and_mono(self):
        inputs = self._inputs()
        inputs[0].mute = True
        m = build_mix_matrix(inputs, "stereo", 0.0, linked=True)
        self.assertTrue(np.all(m[:2] == 0.0))
        inputs[0].mute = False
        m = build_mix_matrix(inputs, "mono", 0.0, linked=True)
        expected = db_to_lin(-6.0) / math.sqrt(2.0)
        np.testing.assert_allclose(m[:2], [[expected] * 2] * 2, rtol=1e-6)


class ChannelLimiterTest(unittest.TestCase):
    def test_threshold_per_channel(self):
        inputs = [InputConfig("a", limiter=True, limiter_threshold_db=-6.0),
                  InputConfig("b", limiter=True, limiter_threshold_db=-20.0),
                  InputConfig("c", limiter=False)]
        lim = ChannelLimiter(3, 256, 48000)
        lim.set_settings(inputs, linked=False)
        rng = np.random.default_rng(1)
        for _ in range(50):
            buf = (rng.standard_normal((256, 3)) * 0.8).astype(np.float32)
            before = buf.copy()
            lim.process(buf)
            peaks = np.abs(buf).max(axis=0)
            self.assertLessEqual(peaks[0], db_to_lin(-6.0) + 1e-6)
            self.assertLessEqual(peaks[1], db_to_lin(-20.0) + 1e-6)
            np.testing.assert_array_equal(buf[:, 2], before[:, 2])   # OFF のチャンネルはそのまま

    def test_linked_reduces_both_equally(self):
        inputs = [InputConfig("L", limiter=True, limiter_threshold_db=-6.0),
                  InputConfig("R", limiter=False, limiter_threshold_db=0.0)]
        lim = ChannelLimiter(2, 256, 48000)
        lim.set_settings(inputs, linked=True)
        buf = np.zeros((256, 2), dtype=np.float32)
        buf[:, 0] = 0.1      # L は小さい
        buf[:, 1] = 1.0      # R が大きい
        lim.process(buf)
        # R のピークに合わせて、L も同じ量だけ下がる（R の設定は無視して In 1 の設定を使う）
        self.assertAlmostEqual(float(lim.gain[0]), float(lim.gain[1]), places=6)
        self.assertLessEqual(float(np.abs(buf[:, 1]).max()), db_to_lin(-6.0) + 1e-6)

    def test_mixer_applies_channel_limiter_before_fader(self):
        inputs = [InputConfig(f"In {i}") for i in range(4)]
        inputs[0].limiter = True
        inputs[0].limiter_threshold_db = -12.0
        mixer = Mixer(in_channels=[0, 1, 2, 3], out_channels=[4, 5],
                      matrix=build_mix_matrix(inputs, "mono", 0.0), blocksize=64,
                      samplerate=48000, limiter=False, test_tone=False)
        mixer.channel_limiter.set_settings(inputs, linked=False)
        indata = np.zeros((64, 6), dtype=np.float32)
        indata[:, 0] = 1.0
        outdata = np.zeros((64, 6), dtype=np.float32)
        mixer.process(indata, outdata, 64, None)
        self.assertLessEqual(float(np.abs(outdata[:, 4]).max()), db_to_lin(-12.0) + 1e-6)
        # 入力メーターはリミッター前の値
        self.assertAlmostEqual(float(mixer.peak_in[0]), 1.0, places=6)


class LimiterTest(unittest.TestCase):
    def test_never_exceeds_ceiling(self):
        lim = Limiter(256, 48000)
        rng = np.random.default_rng(0)
        for _ in range(200):
            buf = (rng.standard_normal((256, 2)) * 2.0).astype(np.float32)
            lim.process(buf)
            self.assertLessEqual(float(np.abs(buf).max()), lim.ceiling + 1e-6)

    def test_quiet_signal_untouched(self):
        lim = Limiter(256, 48000)
        buf = np.full((256, 2), 0.5, dtype=np.float32)
        before = buf.copy()
        lim.process(buf)
        np.testing.assert_array_equal(buf, before)

    def test_gain_recovers(self):
        lim = Limiter(256, 48000)
        lim.process(np.full((256, 2), 4.0, dtype=np.float32))
        self.assertLess(lim.gain, 0.3)
        for _ in range(500):  # 約2.7秒分
            lim.process(np.full((256, 2), 0.1, dtype=np.float32))
        self.assertGreater(lim.gain, 0.99)


class ToneTest(unittest.TestCase):
    def test_phase_is_continuous(self):
        tone = ToneGenerator(256, 48000)
        a = np.empty(256, dtype=np.float32)
        b = np.empty(256, dtype=np.float32)
        tone.generate(a)
        tone.generate(b)
        n = np.arange(512)
        expected = db_to_lin(-12.0) * np.sin(2 * np.pi * 1000 * n / 48000)
        np.testing.assert_allclose(np.concatenate([a, b]), expected, atol=1e-5)


class MixerTest(unittest.TestCase):
    def _mixer(self, **kw):
        inputs = [InputConfig(f"In {i}") for i in range(4)]
        params = dict(in_channels=[2, 3, 4, 5], out_channels=[6, 7],
                      matrix=build_mix_matrix(inputs, "mono", 0.0),
                      blocksize=64, samplerate=48000, limiter=False, test_tone=False)
        params.update(kw)
        return Mixer(**params)

    def test_routing(self):
        mixer = self._mixer()
        indata = np.zeros((64, 8), dtype=np.float32)
        indata[:, 2] = 0.1   # 入力1
        indata[:, 5] = 0.2   # 入力4
        indata[:, 0] = 0.9   # ミックス対象外の ch
        outdata = np.full((64, 8), 9.0, dtype=np.float32)
        mixer.process(indata, outdata, 64, None)
        np.testing.assert_allclose(outdata[:, 6], 0.3, rtol=1e-6)
        np.testing.assert_allclose(outdata[:, 7], 0.3, rtol=1e-6)
        self.assertTrue(np.all(outdata[:, :6] == 0.0))   # 他の出力 ch は無音
        self.assertAlmostEqual(float(mixer.peak_in[0]), 0.1, places=6)
        self.assertAlmostEqual(float(mixer.peak_out[0]), 0.3, places=6)

    def test_silence_counter(self):
        mixer = self._mixer()
        out = np.zeros((64, 8), dtype=np.float32)
        mixer.process(np.zeros((64, 8), dtype=np.float32), out, 64, None)
        mixer.process(np.zeros((64, 8), dtype=np.float32), out, 64, None)
        self.assertEqual(mixer.silent_frames, 128)
        noise = np.zeros((64, 8), dtype=np.float32)
        noise[0, 3] = 1e-6
        mixer.process(noise, out, 64, None)
        self.assertEqual(mixer.silent_frames, 0)

    def test_matrix_swap_and_short_block(self):
        mixer = self._mixer()
        inputs = [InputConfig(f"In {i}", mute=True) for i in range(4)]
        mixer.set_matrix(build_mix_matrix(inputs, "mono", 0.0))
        indata = np.ones((32, 8), dtype=np.float32)
        outdata = np.ones((32, 8), dtype=np.float32)
        mixer.process(indata, outdata, 32, None)
        self.assertTrue(np.all(outdata == 0.0))

    def test_test_tone(self):
        mixer = self._mixer(test_tone=True)
        outdata = np.zeros((64, 8), dtype=np.float32)
        mixer.process(np.zeros((64, 8), dtype=np.float32), outdata, 64, None)
        np.testing.assert_array_equal(outdata[:, 6], outdata[:, 7])
        self.assertAlmostEqual(float(np.abs(outdata[:, 6]).max()), db_to_lin(-12.0), places=2)


class ConfigTest(unittest.TestCase):
    def test_example_config_is_valid(self):
        with open(PROJECT_DIR / "config.example.toml", "rb") as f:
            cfg = parse_config(tomllib.load(f))
        self.assertEqual(cfg.device, "SSL12+BH")
        self.assertEqual(len(cfg.inputs), 4)
        self.assertIsNone(cfg.input_channels)

    def test_dump_and_parse_round_trip(self):
        cfg = parse_config({
            "link_in_1_2": True, "link_balance": -0.25, "output_mode": "mono", "blocksize": 512,
            "inputs": [{"name": 'Vo "main"', "gain_db": -3.5, "pan": 0.2, "mute": True,
                        "limiter": True, "limiter_threshold_db": -9.0},
                       {"name": "Gt"}],
        })
        again = parse_config(tomllib.loads(dump_config(cfg)))
        self.assertEqual(again, cfg)

    def test_errors(self):
        bad = [
            {"output_mode": "surround"},
            {"inputs": [{"pan": 1.5}]},
            {"input_channels": [1, 2]},          # [[inputs]] は既定で4つ
            {"output_channels": [1, 2, 3]},
            {"blocksize": 0},
            {"limiter": "yes"},
            {"typo_key": 1},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                parse_config(data)


def _dev(name, in_ch, out_ch):
    return CADevice(id=0, name=name, uid=name, in_ch=in_ch, out_ch=out_ch,
                    samplerate=48000.0, is_aggregate=False)


class OffsetTest(unittest.TestCase):
    def test_ssl_first(self):
        subs = [_dev("SSL 12", 16, 8), _dev("BlackHole 2ch", 2, 2)]
        in_off, out_off, _, _ = compute_offsets(subs, "SSL 12", "BlackHole 2ch")
        self.assertEqual((in_off, out_off), (0, 8))

    def test_blackhole_first(self):
        subs = [_dev("BlackHole 2ch", 2, 2), _dev("SSL 12", 16, 8)]
        in_off, out_off, _, _ = compute_offsets(subs, "SSL 12", "BlackHole 2ch")
        self.assertEqual((in_off, out_off), (2, 0))

    def test_missing_device(self):
        with self.assertRaises(DeviceError):
            compute_offsets([_dev("BlackHole 2ch", 2, 2)], "SSL 12", "BlackHole 2ch")


if __name__ == "__main__":
    unittest.main()
