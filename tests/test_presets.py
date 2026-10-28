"""プリセットの保存・読み出しのテスト。実行: .venv/bin/python -m unittest discover -s tests -t ."""

import tempfile
import unittest
from pathlib import Path

from ssl_mixer.config import Config, ConfigError, InputConfig
from ssl_mixer.presets import apply_preset, load_preset, save_preset


class PresetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extension_is_added(self):
        cfg = Config()
        self.assertEqual(save_preset(cfg, self.folder / "ライブ用").name, "ライブ用.bhvmix")
        # すでに拡張子が付いていれば、そのまま
        self.assertEqual(save_preset(cfg, self.folder / "配信.bhvmix").name, "配信.bhvmix")
        # 同じ名前なら上書き（Mac の保存画面が上書きの確認をしてくれる）
        self.assertEqual(save_preset(cfg, self.folder / "配信.bhvmix").name, "配信.bhvmix")
        self.assertEqual(len(list(self.folder.glob("*.bhvmix"))), 2)

    def test_round_trip_and_apply(self):
        saved = Config()
        saved.inputs[0] = InputConfig("Vo", gain_db=-4.5, pan=0.3, mute=True,
                                      limiter=True, limiter_threshold_db=-9.0)
        saved.master_gain_db = -2.0
        saved.output_mode = "mono"
        saved.link_in_1_2 = False
        saved.link_balance = 0.4
        saved.blocksize = 1024           # これは読み込まれないはず
        path = save_preset(saved, self.folder / "test")

        current = Config()
        changed = apply_preset(current, load_preset(path))
        self.assertFalse(changed)
        self.assertEqual(current.inputs, saved.inputs)
        self.assertEqual((current.master_gain_db, current.output_mode, current.link_in_1_2,
                          current.link_balance), (-2.0, "mono", False, 0.4))
        self.assertEqual(current.blocksize, 256)
        # 読み込んだ入力を書き換えても、プリセット側に影響しない（コピーされている）
        loaded = load_preset(path)
        apply_preset(current, loaded)
        current.inputs[0].gain_db = 0.0
        self.assertEqual(loaded.inputs[0].gain_db, -4.5)

    def test_broken_file(self):
        bad = self.folder / "壊れた.bhvmix"
        bad.write_text("これは = = TOML ではない", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_preset(bad)
        with self.assertRaises(ConfigError):
            load_preset(self.folder / "ない.bhvmix")


if __name__ == "__main__":
    unittest.main()
