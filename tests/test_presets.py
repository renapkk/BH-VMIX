"""プリセットの保存・読み出しのテスト。実行: .venv/bin/python -m unittest discover -s tests -t ."""

import tempfile
import time
import unittest
from pathlib import Path

from ssl_mixer.config import Config, InputConfig
from ssl_mixer.presets import (apply_preset, clean_name, list_presets, load_preset,
                               save_preset, timestamp_prefix)

# 2026年10月27日 22:59（ローカル時刻）
NOW = time.mktime((2026, 10, 27, 22, 59, 30, 0, 0, -1))


class PresetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_timestamp_prefix(self):
        self.assertEqual(timestamp_prefix(NOW), "261027_2259_")

    def test_save_names(self):
        cfg = Config()
        self.assertEqual(save_preset(cfg, "Live", self.folder, NOW), "261027_2259_Live")
        # 同じ名前は上書きせずに番号を付ける
        self.assertEqual(save_preset(cfg, "Live", self.folder, NOW), "261027_2259_Live_2")
        # 名前なしなら日付と時刻だけ
        self.assertEqual(save_preset(cfg, "", self.folder, NOW), "261027_2259")
        later = NOW + 3600
        save_preset(cfg, "配信", self.folder, later)
        self.assertEqual(list_presets(self.folder)[0], "261027_2359_配信")   # 新しい順

    def test_clean_name(self):
        self.assertEqual(clean_name(' a/b:c? '), "a_b_c_")
        self.assertEqual(len(clean_name("x" * 200)), 60)

    def test_round_trip_and_apply(self):
        saved = Config()
        saved.inputs[0] = InputConfig("Vo", gain_db=-4.5, pan=0.3, mute=True,
                                      limiter=True, limiter_threshold_db=-9.0)
        saved.master_gain_db = -2.0
        saved.output_mode = "mono"
        saved.link_in_1_2 = False
        saved.link_balance = 0.4
        saved.blocksize = 1024           # これは読み込まれないはず
        name = save_preset(saved, "test", self.folder, NOW)

        current = Config()
        changed = apply_preset(current, load_preset(name, self.folder))
        self.assertFalse(changed)
        self.assertEqual(current.inputs, saved.inputs)
        self.assertEqual((current.master_gain_db, current.output_mode, current.link_in_1_2,
                          current.link_balance), (-2.0, "mono", False, 0.4))
        self.assertEqual(current.blocksize, 256)
        # 読み込んだ入力を書き換えても、プリセット側に影響しない（コピーされている）
        loaded = load_preset(name, self.folder)
        apply_preset(current, loaded)
        current.inputs[0].gain_db = 0.0
        self.assertEqual(loaded.inputs[0].gain_db, -4.5)


if __name__ == "__main__":
    unittest.main()
