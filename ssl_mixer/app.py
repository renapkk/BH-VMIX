"""メニューバー常駐アプリ（rumps 使用）。

ミックス処理（Engine）は別スレッドで動かし、メニューの表示更新は1秒ごとのタイマーで
メインスレッドから行う。画面（メニュー）を触ってよいのはメインスレッドだけなので、
Engine からの知らせはリストに積んでおき、タイマーで取り出して表示する。
設定ウィンドウ（window.py）は、このアプリの cfg を書き換えて settings_changed() を呼ぶ。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import subprocess
import sys
import threading
import webbrowser

import objc
import rumps
import rumps.rumps
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSMenu, NSMenuItem)
from Foundation import NSObject

from . import devices, login_item, paths
from .config import Config, ConfigError, load_config, save_config
from .dsp import TONE_BOTH, TONE_LR, tone_mode
from .engine import MIC_PERMISSION_MESSAGE, OPEN_HANG_MESSAGE, Engine

BLACKHOLE_URL = "https://existential.audio/blackhole/"
STATE_MARKS = {"動作中": "●", "接続待ち": "…", "起動中": "…", "停止": "○"}
MODE_LABELS = {"stereo": "Stereo", "mono": "Mono"}

log = logging.getLogger("ssl_mixer")


def _setup_logging() -> None:
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        paths.LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stderr))
    log.setLevel(logging.INFO)


def _short(text: str, limit: int = 48) -> str:
    first = text.splitlines()[0] if text else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


# 起動中の MixerApp（Dock のクリックなど、macOS からの知らせを渡すため）
_current_app: "MixerApp | None" = None


class VmixAppDelegate(rumps.rumps.NSApp):
    """rumps が使うアプリの窓口（デリゲート）を拡張したもの。

    - Dock のアイコンや、起動中にもう一度アプリを開いたとき → 設定ウィンドウを開く
    - アプリが終了するとき（⌘Q や Dock の「終了」でも）→ 設定を保存して音を止める
    """

    def applicationShouldHandleReopen_hasVisibleWindows_(self, application, has_visible):
        if _current_app is not None:
            _current_app.on_open_window(None)
        return True

    def applicationWillTerminate_(self, notification):
        if _current_app is not None:
            _current_app.shutdown()
        objc.super(VmixAppDelegate, self).applicationWillTerminate_(notification)


# rumps が起動時に作るデリゲートを、上の拡張版に差し替える
rumps.rumps.NSApp = VmixAppDelegate


class MenuTarget(NSObject):
    """画面上部のアプリのメニューから呼ばれる処理。"""

    def openSettings_(self, sender):
        if _current_app is not None:
            _current_app.on_open_window(None)


def _build_main_menu(target: MenuTarget) -> NSMenu:
    """画面上部に出る「BH VMIX」と「ウィンドウ」のメニューを作る。"""
    def item(title, action, key="", tgt=None):
        menu_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if tgt is not None:
            menu_item.setTarget_(tgt)
        return menu_item

    main = NSMenu.alloc().init()
    app_menu = NSMenu.alloc().initWithTitle_(paths.APP_NAME)
    # Mac 標準の「について」画面（アイコンとバージョンが出る）
    app_menu.addItem_(item(f"{paths.APP_NAME} について", "orderFrontStandardAboutPanel:"))
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItem_(item("設定ウィンドウを開く", "openSettings:", ",", target))
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItem_(item(f"{paths.APP_NAME} を隠す", "hide:", "h"))
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItem_(item(f"{paths.APP_NAME} を終了", "terminate:", "q"))
    app_item = NSMenuItem.alloc().init()
    app_item.setSubmenu_(app_menu)
    main.addItem_(app_item)

    window_menu = NSMenu.alloc().initWithTitle_("ウィンドウ")
    window_menu.addItem_(item("しまう", "performMiniaturize:", "m"))
    window_menu.addItem_(item("閉じる", "performClose:", "w"))
    window_item = NSMenuItem.alloc().init()
    window_item.setSubmenu_(window_menu)
    main.addItem_(window_item)
    NSApplication.sharedApplication().setWindowsMenu_(window_menu)
    return main


class MixerApp(rumps.App):
    def __init__(self, test_tone=None, show_window: bool = True):
        super().__init__(paths.APP_NAME, title="VMIX …", quit_button=None)
        self.config_path = paths.ensure_app_config()
        self.test_tone = tone_mode(test_tone)   # None / "both"（両方）/ "lr"（L/R 交互）
        self.engine: Engine | None = None
        self.thread: threading.Thread | None = None
        self.window_ctl = None
        self.last_message = ""
        self._alerts: list[str] = []   # Engine のスレッドから積み、タイマーで表示する
        self._dirty = False            # 設定ファイルへの保存待ち
        self._mute_menu_key = None
        self.source_missing = False     # SSL 12 が見つからない状態か（設定ウィンドウの表示にも使う）
        self._source_alerted = False    # 見つからなくなってから、もうアラートを出したか
        self._blink = False             # メニューバーの点滅用
        self.cfg = self._load_config()

        self.status_item = rumps.MenuItem("状態: 起動中")
        self.message_item = rumps.MenuItem("")
        self.toggle_item = rumps.MenuItem("停止", callback=self.on_toggle)
        self.mute_menu = rumps.MenuItem("Mute")
        self.mode_menu = rumps.MenuItem("Output")
        for mode, label in MODE_LABELS.items():
            self.mode_menu.add(rumps.MenuItem(label, callback=self._mode_callback(mode)))
        self.tone_item = rumps.MenuItem("Test Tone（1kHz / -12 dBFS）", callback=self.on_test_tone)
        self.lr_item = rumps.MenuItem("L/R Test（左右に交互）", callback=self.on_lr_test)
        self.login_item = rumps.MenuItem("ログイン時に起動", callback=self.on_login_item)
        self.menu = [
            rumps.MenuItem("設定ウィンドウを開く", callback=self.on_open_window),
            None,
            self.status_item,
            self.message_item,
            None,
            self.toggle_item,
            self.mute_menu,
            self.mode_menu,
            self.tone_item,
            self.lr_item,
            None,
            rumps.MenuItem("機器セット「SSL12+BH」を作成", callback=self.on_create_aggregate),
            rumps.MenuItem("BlackHole のダウンロードページを開く",
                           callback=lambda _: webbrowser.open(BLACKHOLE_URL)),
            None,
            rumps.MenuItem("設定ファイルを開く", callback=self.on_open_config),
            rumps.MenuItem("設定を再読み込み", callback=self.on_reload),
            rumps.MenuItem("ログを開く", callback=self.on_open_log),
            self.login_item,
            None,
            rumps.MenuItem("終了", callback=self.on_quit),
        ]
        self._refresh_menu()
        # Dock と ⌘Tab に出る、ふつうのアプリとして動かす（メニューバーのアイコンも残す）
        global _current_app
        _current_app = self
        nsapp = NSApplication.sharedApplication()
        nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self._menu_target = MenuTarget.alloc().init()
        nsapp.setMainMenu_(_build_main_menu(self._menu_target))
        self.timer = rumps.Timer(self.on_tick, 1)
        self.timer.start()
        self.start_engine()
        if show_window:
            # 起動直後はアプリの準備が終わってから開く
            rumps.Timer(self._open_window_once, 0.3).start()

    # ---- 設定 ----

    def _load_config(self) -> Config:
        try:
            cfg, _ = load_config(self.config_path)
            return cfg
        except ConfigError as e:
            self._notify(f"設定ファイルにエラーがあるため、既定値で動きます。\n{e}")
            return Config()

    def settings_changed(self) -> None:
        """設定ウィンドウやメニューで値が変わったとき：すぐ音に反映し、保存を予約する。"""
        if self.engine is not None:
            self.engine.cfg = self.cfg
            self.engine.apply_settings()
        self._dirty = True
        self._refresh_menu()

    def _save_if_dirty(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        try:
            save_config(self.cfg, self.config_path)
        except OSError as e:
            log.info(f"設定を保存できませんでした: {e}")

    def _refresh_menu(self) -> None:
        """メニューのチェック状態を設定に合わせる。"""
        cfg = self.cfg
        # リンク中は In 1-2 を1つの項目にまとめる（構成が変わったときだけ作り直す）
        key = (cfg.is_linked, tuple(i.name for i in cfg.inputs))
        if key != self._mute_menu_key:
            self._mute_menu_key = key
            if self.mute_menu._menu is not None:
                self.mute_menu.clear()
            for i, inp in enumerate(cfg.inputs):
                if cfg.is_linked and i == 1:
                    continue
                title = f"{inp.name} / {cfg.inputs[1].name}（Link）" if cfg.is_linked and i == 0 else inp.name
                self.mute_menu.add(rumps.MenuItem(title, callback=self._mute_callback(i)))
        for item, i in zip(self.mute_menu.values(),
                           [i for i in range(len(cfg.inputs)) if not (cfg.is_linked and i == 1)]):
            item.state = cfg.inputs[i].mute
        for mode, label in MODE_LABELS.items():
            self.mode_menu[label].state = cfg.output_mode == mode
        self.tone_item.state = self.test_tone == TONE_BOTH
        self.lr_item.state = self.test_tone == TONE_LR
        self.login_item.state = login_item.is_installed()

    # ---- エンジンの開始・停止 ----

    def start_engine(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.engine = Engine(self.cfg, test_tone=self.test_tone, log=self._on_engine_log)
        self.thread = threading.Thread(target=self.engine.run, daemon=True)
        self.thread.start()
        self.toggle_item.title = "停止"

    def stop_engine(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.thread = None
        self.toggle_item.title = "開始"

    def restart_engine(self) -> None:
        self.stop_engine()
        self.start_engine()

    def set_test_tone(self, mode) -> None:
        """テストトーンを切り替える（None で止める / "both" / "lr"）。"""
        self.test_tone = tone_mode(mode)
        self._refresh_menu()
        if self.window_ctl is not None:
            self.window_ctl.sync_from_config()
        self.restart_engine()

    def _on_engine_log(self, message: str) -> None:
        # Engine のスレッドから呼ばれる。画面は触らず、記録と通知の予約だけする
        log.info(message)
        self.last_message = message
        if message in (MIC_PERMISSION_MESSAGE, OPEN_HANG_MESSAGE):
            self._alerts.append(message)

    def _notify(self, message: str) -> None:
        log.info(message)
        self._alerts.append(message)

    # ---- 設定ウィンドウ ----

    def _open_window_once(self, timer) -> None:
        timer.stop()
        self.on_open_window(None)

    def on_open_window(self, _) -> None:
        if self.window_ctl is None:
            from .window import MixerWindowController
            self.window_ctl = MixerWindowController.alloc().initWithHost_(self)
        self.window_ctl.show()

    def rebuild_window(self) -> None:
        """設定ウィンドウを作り直す（入力の数が変わったときなど）。"""
        reopen = self.window_ctl is not None and self.window_ctl.window.isVisible()
        if self.window_ctl is not None:
            self.window_ctl.window.close()
            self.window_ctl = None
        if reopen:
            self.on_open_window(None)

    def _sync_window(self) -> None:
        if self.window_ctl is not None:
            self.window_ctl.sync_from_config()

    # ---- メニューの操作 ----

    def on_tick(self, _) -> None:
        state = self.engine.state if self.engine and self.thread and self.thread.is_alive() else "停止"
        self._check_source()
        if self.source_missing:
            # インターフェースが外れているときは、メニューバーの印を点滅させて目立たせる
            self._blink = not self._blink
            self.title = "VMIX ⚠" if self._blink else "VMIX   "
            label = "インターフェース未接続"
        else:
            self.title = f"VMIX {STATE_MARKS.get(state, '')}"
            label = "接続中" if state == "動作中" else state
            if self.test_tone and state == "動作中":
                label += " · " + ("L/R Test" if self.test_tone == TONE_LR else "Test Tone")
        self.status_item.title = f"状態: {label}"
        self.message_item.title = _short(self.last_message)
        self._save_if_dirty()
        while self._alerts:
            rumps.alert(title=paths.APP_NAME, message=self._alerts.pop(0))

    def _check_source(self) -> None:
        """SSL 12 が見つからなくなったら、1回だけアラートを出す。

        抜けている間は何度も出さない。つながったら印を戻し、次に抜けたらまた出す。
        """
        # 判定は Engine のスレッドで行っている（ここで Core Audio に問い合わせると、画面ごと止まることがある）
        engine = self.engine
        present = not (engine is not None and engine.source_missing)
        self.source_missing = not present
        if present:
            if self._source_alerted:
                log.info(f"オーディオインターフェース（{self.cfg.source_device}）が接続されました")
            self._source_alerted = False
        elif not self._source_alerted:
            self._source_alerted = True
            message = (f"オーディオインターフェース（{self.cfg.source_device}）が接続されていません。\n"
                       "USB ケーブルと電源を確認してください。つながると自動で再開します。")
            log.info(message.replace("\n", " "))
            self._alerts.append(message)

    def on_toggle(self, _) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.stop_engine()
        else:
            self.start_engine()

    def _mute_callback(self, index: int):
        def callback(sender):
            self.cfg.inputs[index].mute = not self.cfg.inputs[index].mute
            self.settings_changed()
            self._sync_window()
        return callback

    def _mode_callback(self, mode: str):
        def callback(_):
            self.cfg.output_mode = mode
            self.settings_changed()
            self._sync_window()
        return callback

    def on_test_tone(self, _) -> None:
        self.set_test_tone(None if self.test_tone == TONE_BOTH else TONE_BOTH)

    def on_lr_test(self, _) -> None:
        self.set_test_tone(None if self.test_tone == TONE_LR else TONE_LR)

    def create_aggregate(self) -> None:
        self.on_create_aggregate(None)

    def on_create_aggregate(self, _) -> None:
        try:
            dev = devices.create_aggregate_for(self.cfg)
        except (devices.DeviceError, RuntimeError) as e:
            rumps.alert(title=paths.APP_NAME, message=f"機器セットを作れませんでした。\n{e}")
            return
        log.info(f"機器セット「{dev.name}」を作成しました")
        rumps.alert(title=paths.APP_NAME,
                    message=f"機器セット「{dev.name}」を作成しました。\n"
                            f"クロックソース: {self.cfg.source_device}\n"
                            f"ドリフト補正: {self.cfg.sink_device} で ON")
        self.restart_engine()

    def on_open_config(self, _) -> None:
        self._save_if_dirty()
        # テキストエディットで開く
        subprocess.run(["/usr/bin/open", "-t", str(self.config_path)])

    def on_reload(self, _) -> None:
        self.cfg = self._load_config()
        self._dirty = False
        self._refresh_menu()
        # 入力の数が変わることもあるので、ウィンドウは作り直す
        self.restart_engine()
        self.rebuild_window()

    def on_open_log(self, _) -> None:
        subprocess.run(["/usr/bin/open", "-a", "Console", str(paths.LOG_PATH)])

    def on_login_item(self, _) -> None:
        if login_item.is_installed():
            login_item.uninstall()
        else:
            app = paths.app_bundle_path()
            if app is None:
                rumps.alert(title=paths.APP_NAME,
                            message="ログイン時の起動は、アプリ版（BH VMIX.app）から設定してください。")
                return
            login_item.install(app)
        self._refresh_menu()

    def on_quit(self, _) -> None:
        # 実際の後片付けは、終了の直前に shutdown() で行う（⌘Q や Dock の「終了」と同じ流れ）
        rumps.quit_application()

    def shutdown(self) -> None:
        """終了の直前に呼ばれる：設定を保存して音を止める。"""
        self._save_if_dirty()
        self.stop_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=paths.APP_NAME)
    parser.add_argument("--test-tone", action="store_true", help="テストトーンで起動する")
    parser.add_argument("--test-tone-lr", action="store_true", help="L/R 交互のテストトーンで起動する")
    parser.add_argument("--hidden", action="store_true", help="設定ウィンドウを開かずに起動する（ログイン時用）")
    parser.add_argument("--install-login-item", action="store_true", help="ログイン時の起動を登録して終了")
    parser.add_argument("--uninstall-login-item", action="store_true", help="ログイン時の起動を解除して終了")
    # Finder から起動されたときに macOS が付ける引数などは無視する
    args, _ = parser.parse_known_args(argv)

    if args.install_login_item:
        app = paths.app_bundle_path()
        if app is None:
            print("BH VMIX.app の中から実行してください", file=sys.stderr)
            return 1
        login_item.install(app)
        print(f"登録しました: {login_item.PLIST_PATH}")
        return 0
    if args.uninstall_login_item:
        login_item.uninstall()
        print("解除しました")
        return 0

    _setup_logging()
    log.info(f"{paths.APP_NAME} を起動します（設定: {paths.APP_CONFIG_PATH}）")
    tone = TONE_LR if args.test_tone_lr else (TONE_BOTH if args.test_tone else None)
    MixerApp(test_tone=tone, show_window=not args.hidden).run()
    return 0
