"""設定ウィンドウ（pyobjc で macOS 標準の AppKit を使う）。

部品の位置と大きさはすべて数値で決める（自動で改行・伸縮しない）。
座標は左上を原点にしたいので、上下を反転した FlippedView の上に部品を並べる。
メーターと ON/OFF ボタンは、大きさと色を確実にそろえるため自分で描く。
"""

from __future__ import annotations

import time
from pathlib import Path

import objc
from AppKit import (
    NSAlert, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSApp,
    NSBackingStoreBuffered,
    NSBezierPath, NSBitmapImageFileTypePNG, NSColor, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSLineBreakByClipping, NSMakeRect, NSModalResponseOK,
    NSMutableAttributedString, NSOpenPanel, NSPopUpButton, NSSavePanel,
    NSRunLoop, NSRunLoopCommonModes, NSSegmentedControl, NSSegmentSwitchTrackingSelectOne,
    NSSlider, NSString, NSTextAlignmentCenter, NSTextAlignmentLeft, NSTextAlignmentRight,
    NSTextField, NSTimer, NSView, NSWindow, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSObject

from . import presets
from .config import ConfigError
from .dsp import TEST_TONE_DB, TONE_BOTH, TONE_LR, lin_to_db

# ---- 色（深い赤をアクセントカラーにする） ----


def _rgb(hex_code: str, alpha: float = 1.0) -> NSColor:
    r, g, b = (int(hex_code[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


ACCENT = _rgb("#A01E23")
ACCENT_TEXT_DARK = _rgb("#F09595")      # ダークモードで文字に使う明るめの赤
# 深い赤の反対色（色相を180°回した補色）。ロゴの「V」だけに使う遊び心の差し色
COMPLEMENT = _rgb("#1EA09B")
COMPLEMENT_DARK = _rgb("#5DCAC4")       # ダークモードで少し明るくしたもの
METER_GREEN = _rgb("#639922")
METER_YELLOW = _rgb("#EF9F27")
WHITE = NSColor.whiteColor()

# ---- 寸法（px） ----
PAD = 16
TOP_H = 46
FOOT_H = 52
INFO_H = 52        # 通話アプリの設定を案内する帯
PRESET_H = 46      # プリセットの帯
LABEL_W = 60
CH_W = 116
MASTER_W = 168
BTN_W, BTN_H = 76, 26
FOOT_BTN_W, FOOT_BTN_H = 150, 28
METER_W = 10
FADER_H = 170
# 行の高さ（見出し・Limiter・Pan・Fader・Mute）
ROWS = [("", 46), ("Limiter", 92), ("Pan", 52), ("Fader", 208), ("", 46)]

METER_FLOOR_DB = -60.0
METER_FALL_DB_PER_SEC = 20.0
GR_RANGE_DB = 20.0
FPS = 30.0


def _is_dark(view) -> bool:
    name = view.effectiveAppearance().bestMatchFromAppearancesWithNames_(
        [NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
    return name == NSAppearanceNameDarkAqua


def _accent_text(view) -> NSColor:
    return ACCENT_TEXT_DARK if _is_dark(view) else ACCENT


def _draw_text(text: str, rect, font, color, align: str = "center") -> None:
    """rect の中に1行の文字を描く（縦は中央）。"""
    attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color}
    s = NSString.stringWithString_(text)
    size = s.sizeWithAttributes_(attrs)
    (x, y), (w, h) = rect
    if align == "center":
        x += (w - size.width) / 2
    elif align == "right":
        x += w - size.width
    s.drawAtPoint_withAttributes_((x, y + (h - size.height) / 2), attrs)


def _label(text: str, frame, size: float = 11, bold: bool = False,
           secondary: bool = True, align=NSTextAlignmentCenter) -> NSTextField:
    """1行のラベル（折り返さない）。"""
    field = NSTextField.labelWithString_(text)
    field.setFrame_(frame)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    field.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    field.setAlignment_(align)
    field.setLineBreakMode_(NSLineBreakByClipping)
    return field


# ---- 自分で描く部品 ----


class FlippedView(NSView):
    """左上が原点になるビュー（上から順に部品を並べやすくする）。"""

    def isFlipped(self):
        return True


class BackgroundView(FlippedView):
    """背景：上下の帯、アクセントの線、行の区切り線、Master の前の縦線を描く。"""

    def initWithFrame_(self, frame):
        self = objc.super(BackgroundView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.row_lines = []
        self.master_x = 0
        self.grid_top = 0
        self.grid_bottom = 0
        return self

    def drawRect_(self, rect):
        (_, _), (w, h) = self.bounds()
        NSColor.controlBackgroundColor().setFill()
        NSBezierPath.fillRect_(self.bounds())
        NSColor.windowBackgroundColor().setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, w, TOP_H))
        NSBezierPath.fillRect_(NSMakeRect(0, h - FOOT_H - INFO_H, w, FOOT_H + INFO_H))
        ACCENT.setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, TOP_H - 2, w, 2))
        NSColor.separatorColor().setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, h - FOOT_H - INFO_H, w, 0.5))
        NSBezierPath.fillRect_(NSMakeRect(PAD, h - FOOT_H - INFO_H - PRESET_H, w - PAD * 2, 0.5))
        NSBezierPath.fillRect_(NSMakeRect(PAD, h - FOOT_H, w - PAD * 2, 0.5))
        for y in self.row_lines:
            NSBezierPath.fillRect_(NSMakeRect(PAD, y, w - PAD * 2, 0.5))
        NSBezierPath.fillRect_(NSMakeRect(self.master_x, self.grid_top, 0.5,
                                          self.grid_bottom - self.grid_top))


class DotView(NSView):
    def drawRect_(self, rect):
        ACCENT.setFill()
        NSBezierPath.bezierPathWithOvalInRect_(self.bounds()).fill()


# 状態表示の色（ライト用の文字色, ダーク用の文字色）
STATUS_COLORS = {
    "ok": ("#3B6D11", "#97C459"),       # 接続中：緑
    "alert": ("#A01E23", "#F09595"),    # インターフェース未接続：赤（点滅）
    "wait": ("#854F0B", "#EF9F27"),     # 接続待ち：黄
    "idle": ("#5F5E5A", "#B4B2A9"),     # 起動中・停止：灰
}
BLINK_SEC = 0.5


class StatusPillView(NSView):
    """「● 接続中」などの状態表示。状態ごとに色を変え、未接続のときは点滅させる。"""

    def initWithFrame_(self, frame):
        self = objc.super(StatusPillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.text = "● 起動中"
        self.kind = "idle"
        self.lit = True          # 点滅中の「強く光っている」側か
        return self

    def setText_kind_(self, text, kind):
        if text != self.text or kind != self.kind:
            self.text = text
            self.kind = kind
            self.lit = True
            # 文字の長さに合わせて幅を変える
            attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(12)}
            width = NSString.stringWithString_(text).sizeWithAttributes_(attrs).width + 24
            self.setFrameSize_((width, self.frame().size.height))
            self.setNeedsDisplay_(True)

    def blink(self):
        """未接続のときだけ、強い赤と薄い赤を交互に切り替える。"""
        if self.kind == "alert":
            self.lit = not self.lit
            self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        dark = _is_dark(self)
        color = _rgb(STATUS_COLORS[self.kind][1 if dark else 0])
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 6, 6)
        if self.kind == "alert" and self.lit:
            # 点滅の「強い」側：赤で塗りつぶして白い文字
            ACCENT.setFill()
            path.fill()
            text_color = WHITE
        else:
            color.colorWithAlphaComponent_(0.18 if dark else 0.13).setFill()
            path.fill()
            text_color = color
        _draw_text(self.text, self.bounds(), NSFont.boldSystemFontOfSize_(12), text_color)


class InfoCardView(NSView):
    """通話アプリの設定を目立たせるための、薄い赤の枠付きカード。"""

    def drawRect_(self, rect):
        dark = _is_dark(self)
        inner = NSMakeRect(0.5, 0.5, self.bounds().size.width - 1, self.bounds().size.height - 1)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inner, 8, 8)
        _rgb("#E24B4A" if dark else "#A01E23", 0.14 if dark else 0.07).setFill()
        path.fill()
        _rgb("#E24B4A" if dark else "#A01E23", 0.55 if dark else 0.45).setStroke()
        path.setLineWidth_(1.0)
        path.stroke()


class ToggleButton(NSView):
    """大きさをそろえた ON/OFF ボタン。ON のときは深い赤で塗りつぶし、文字を白にする。

    momentary=True にすると押したときだけ反応する普通のボタンになる。
    """

    def initWithFrame_(self, frame):
        self = objc.super(ToggleButton, self).initWithFrame_(frame)
        if self is None:
            return None
        self.title = ""
        self.state = False
        self.momentary = False
        self.pressed = False
        self.callback = None
        self.font_size = 11
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        # ウィンドウが手前に無くても1回のクリックで押せるようにする
        return True

    def setOn_(self, on):
        if bool(on) != self.state:
            self.state = bool(on)
            self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
        self.pressed = True
        self.setNeedsDisplay_(True)

    def mouseUp_(self, event):
        self.pressed = False
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        inside = 0 <= point.x <= self.bounds().size.width and 0 <= point.y <= self.bounds().size.height
        if inside:
            if not self.momentary:
                self.state = not self.state
            if self.callback is not None:
                self.callback(self.state)
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        inner = NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inner, 6, 6)
        on = self.state and not self.momentary
        if on or (self.momentary and self.pressed):
            ACCENT.setFill()
            path.fill()
            color = WHITE
        else:
            fill = NSColor.controlColor()
            if self.pressed:
                fill = NSColor.selectedControlColor()
            fill.setFill()
            path.fill()
            NSColor.tertiaryLabelColor().setStroke()
            path.setLineWidth_(0.5)
            path.stroke()
            color = NSColor.labelColor()
        _draw_text(self.title, bounds, NSFont.systemFontOfSize_(self.font_size), color)


class MeterView(NSView):
    """縦のピークメーター。-60〜0 dBFS を 緑（〜-12）→ 黄（〜-3）→ 赤 で描く。"""

    def initWithFrame_(self, frame):
        self = objc.super(MeterView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.level_db = METER_FLOOR_DB
        return self

    def setLevel_(self, db):
        if abs(db - self.level_db) > 0.05:
            self.level_db = db
            self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        (_, _), (w, h) = self.bounds()
        NSColor.quaternaryLabelColor().setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 3, 3).fill()

        def y_of(db):
            # 下が 0（-60dB）、上が h（0dB）。このビューは反転していないので下が原点
            return h * (min(max(db, METER_FLOOR_DB), 0.0) - METER_FLOOR_DB) / -METER_FLOOR_DB

        top = y_of(self.level_db)
        for lo, hi, color in ((METER_FLOOR_DB, -12.0, METER_GREEN),
                              (-12.0, -3.0, METER_YELLOW), (-3.0, 0.0, ACCENT)):
            y0, y1 = y_of(lo), min(y_of(hi), top)
            if y1 > y0:
                color.setFill()
                NSBezierPath.fillRect_(NSMakeRect(0, y0, w, y1 - y0))


class GainReductionView(NSView):
    """リミッターの減衰量を横棒で表示する（左から右へ伸びる。0〜-20dB）。"""

    def initWithFrame_(self, frame):
        self = objc.super(GainReductionView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.reduction_db = 0.0
        return self

    def setReduction_(self, db):
        if abs(db - self.reduction_db) > 0.05:
            self.reduction_db = db
            self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        (_, _), (w, h) = self.bounds()
        NSColor.quaternaryLabelColor().setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 2, 2).fill()
        amount = min(max(-self.reduction_db / GR_RANGE_DB, 0.0), 1.0)
        if amount > 0:
            ACCENT.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, w * amount, h), 2, 2).fill()


# ---- 表示用の文字 ----


def pan_text(pan: float, prefix: str = "") -> str:
    """Pro Tools 風のパン表示（C / L 50 / R 50）。"""
    amount = round(abs(pan) * 100)
    if amount == 0:
        return f"{prefix}C"
    return f"{prefix}{'L' if pan < 0 else 'R'} {amount}"


def db_text(db: float) -> str:
    return f"{db:+.1f} dB" if db > 0 else f"{db:.1f} dB"


# ---- ウィンドウ本体 ----


class MixerWindowController(NSObject):
    """設定ウィンドウ。host（メニューバーアプリ）の cfg を書き換え、host に反映を頼む。

    host に必要なもの:
      cfg, engine, test_tone,
      settings_changed()（すぐ反映して保存）, restart_engine(), set_test_tone(None / "both" / "lr"),
      create_aggregate()
    """

    def initWithHost_(self, host):
        self = objc.super(MixerWindowController, self).init()
        if self is None:
            return None
        self.host = host
        self.window = None
        self.timer = None
        self.strips = []          # チャンネルごとの部品（メーター更新に使う）
        self.strip_views = []     # 作り直すときに消す部品
        self.master = {}
        self.display_db = {}
        self.last_tick = time.monotonic()
        self.last_blink = self.last_tick
        self._build()
        return self

    # ---- 組み立て ----

    @objc.python_method
    def _content_size(self):
        n = len(self.host.cfg.inputs)
        width = PAD * 2 + LABEL_W + CH_W * n + MASTER_W
        height = TOP_H + 8 + sum(h for _, h in ROWS) + 8 + PRESET_H + INFO_H + FOOT_H
        return width, height

    @objc.python_method
    def _build(self):
        width, height = self._content_size()
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("BH VMIX")
        self.window.setReleasedWhenClosed_(False)
        self.window.center()

        self.root = BackgroundView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        self.window.setContentView_(self.root)
        self._build_top(width)
        self._build_presets(width, height)
        self._build_info(width, height)
        self._build_footer(width, height)
        self._build_strips()

    @objc.python_method
    def _build_top(self, width):
        y = (TOP_H - 2 - BTN_H) / 2
        dot = DotView.alloc().initWithFrame_(NSMakeRect(PAD, (TOP_H - 2 - 10) / 2, 10, 10))
        self.root.addSubview_(dot)
        self.logo = _label("", NSMakeRect(PAD + 16, y + 4, 80, 18), size=13, bold=True,
                           secondary=False, align=NSTextAlignmentLeft)
        self.root.addSubview_(self.logo)
        self._update_logo()
        self.status_pill = StatusPillView.alloc().initWithFrame_(NSMakeRect(PAD + 98, y + 2, 80, 22))
        self.root.addSubview_(self.status_pill)
        self.device_label = _label("", NSMakeRect(PAD + 186, y + 5, 150, 16), align=NSTextAlignmentLeft)
        self.root.addSubview_(self.device_label)

        # 右側: Output と Buffer Size
        x = width - PAD - BTN_W
        self.buffer_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(x, y, BTN_W, BTN_H), False)
        self.buffer_popup.addItemsWithTitles_(["128", "256", "512", "1024"])
        self.buffer_popup.setTarget_(self)
        self.buffer_popup.setAction_("bufferSizeChanged:")
        self.root.addSubview_(self.buffer_popup)
        x -= 78
        self.root.addSubview_(_label("Buffer Size", NSMakeRect(x, y + 5, 72, 16),
                                     align=NSTextAlignmentRight))
        x -= 16 + 128
        self.mode_control = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Stereo", "Mono"], NSSegmentSwitchTrackingSelectOne, self, "outputModeChanged:")
        self.mode_control.setFrame_(NSMakeRect(x, y, 128, BTN_H))
        self.mode_control.setWidth_forSegment_(62, 0)
        self.mode_control.setWidth_forSegment_(62, 1)
        self.mode_control.setSelectedSegmentBezelColor_(ACCENT)
        self.root.addSubview_(self.mode_control)
        x -= 52
        self.root.addSubview_(_label("Output", NSMakeRect(x, y + 5, 46, 16), align=NSTextAlignmentRight))

    @objc.python_method
    def _build_presets(self, width, height):
        """プリセットの帯：Load…（開く）・Save…（保存）と、今のプリセットの名前。"""
        y = height - FOOT_H - INFO_H - PRESET_H + (PRESET_H - BTN_H) / 2
        self.root.addSubview_(_label("Preset", NSMakeRect(PAD, y + 5, LABEL_W - 4, 16),
                                     align=NSTextAlignmentLeft))
        x = PAD + LABEL_W
        for title, callback in (("Load…", self._on_load_preset), ("Save…", self._on_save_preset)):
            button = ToggleButton.alloc().initWithFrame_(NSMakeRect(x, y, BTN_W, BTN_H))
            button.title = title
            button.momentary = True
            button.callback = callback
            self.root.addSubview_(button)
            x += BTN_W + 8
        self.preset_label = _label("", NSMakeRect(x + 8, y + 5, 300, 16), secondary=False,
                                   align=NSTextAlignmentLeft)
        self.root.addSubview_(self.preset_label)
        self.root.addSubview_(_label("変更は自動で保存されます",
                                     NSMakeRect(width - PAD - 170, y + 5, 170, 16),
                                     align=NSTextAlignmentRight))
        self.preset_dir = None   # 前回保存・読み込みしたフォルダ（次の画面をそこから始める）

    @objc.python_method
    def _start_dir(self, panel):
        """保存・開く画面を、前回のフォルダ（最初は「書類」）から始める。"""
        folder = self.preset_dir or str(Path.home() / "Documents")
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(folder))

    @objc.python_method
    def _show_error(self, title, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.runModal()

    @objc.python_method
    def _on_save_preset(self, _):
        """Mac 標準の保存画面で、好きな名前・場所にプリセットを保存する。"""
        panel = NSSavePanel.savePanel()
        panel.setTitle_("プリセットを保存")
        panel.setMessage_("今のミックス設定（Fader・Pan・Mute・Limiter・Link など）を保存します")
        panel.setNameFieldStringValue_("BH VMIX プリセット")
        panel.setAllowedFileTypes_([presets.EXTENSION])
        panel.setCanCreateDirectories_(True)
        self._start_dir(panel)
        NSApp.activateIgnoringOtherApps_(True)
        if panel.runModal() != NSModalResponseOK:
            return
        try:
            path = presets.save_preset(self.host.cfg, Path(panel.URL().path()))
        except OSError as e:
            self._show_error("プリセットを保存できませんでした", str(e))
            return
        self.preset_dir = str(path.parent)
        self.preset_label.setStringValue_(f"保存しました：{path.stem}")

    @objc.python_method
    def _on_load_preset(self, _):
        """Mac 標準の「開く」画面で、プリセットのファイルを選んで読み込む。"""
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("プリセットを開く")
        panel.setMessage_("読み込むと、今のミックス設定は上書きされます")
        panel.setAllowedFileTypes_([presets.EXTENSION])
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        self._start_dir(panel)
        NSApp.activateIgnoringOtherApps_(True)
        if panel.runModal() != NSModalResponseOK:
            return
        path = Path(panel.URL().path())
        try:
            preset = presets.load_preset(path)
        except ConfigError as e:
            self._show_error("プリセットを読み込めませんでした", str(e))
            return
        self.preset_dir = str(path.parent)
        count_changed = presets.apply_preset(self.host.cfg, preset)
        self.host.settings_changed()
        if count_changed:
            # 入力の数が変わったときはウィンドウの大きさも変わるので作り直す
            self.host.rebuild_window()
        else:
            self._build_strips()
            self.preset_label.setStringValue_(f"読み込みました：{path.stem}")

    @objc.python_method
    def _build_info(self, width, height):
        """通話アプリ（Discord・Zoom など）で選ぶデバイスの案内。目立つようにカードで表示する。"""
        top = height - FOOT_H - INFO_H
        card = InfoCardView.alloc().initWithFrame_(NSMakeRect(PAD, top + 8, width - PAD * 2, INFO_H - 14))
        self.root.addSubview_(card)
        cfg = self.host.cfg
        y = top + 8 + (INFO_H - 14 - 20) / 2
        self.root.addSubview_(_label("通話アプリ（Zoom・Discord 等）の設定", NSMakeRect(PAD + 14, y + 2, 232, 16),
                                     size=12, bold=True, secondary=False, align=NSTextAlignmentLeft))
        self.info_devices = []
        x = PAD + 252

        def text_width(text, font):
            return NSString.stringWithString_(text).sizeWithAttributes_({NSFontAttributeName: font}).width

        for role, device in (("マイク（IN）", cfg.sink_device), ("スピーカー（OUT）", cfg.source_device)):
            # 文字の幅を測って、役割 → 機材名 → 次の役割 と詰めて並べる
            role_w = text_width(role, NSFont.systemFontOfSize_(11)) + 4
            self.root.addSubview_(_label(role, NSMakeRect(x, y + 3, role_w, 16), align=NSTextAlignmentLeft))
            x += role_w + 6
            name_w = text_width(device, NSFont.boldSystemFontOfSize_(15)) + 4
            name = _label(device, NSMakeRect(x, y, name_w, 20), size=15, bold=True,
                          secondary=False, align=NSTextAlignmentLeft)
            self.root.addSubview_(name)
            self.info_devices.append(name)
            x += name_w + 36

    @objc.python_method
    def _build_footer(self, width, height):
        y = height - FOOT_H + (FOOT_H - FOOT_BTN_H) / 2
        buttons = [("機器セットを作成", True, lambda _: self.host.create_aggregate()),
                   ("Link In 1-2", False, self._on_link),
                   (f"Test Tone  {TEST_TONE_DB:g} dBFS", False, self._on_test_tone),
                   ("L/R Test", False, self._on_lr_test)]
        x = PAD
        self.footer_buttons = []
        for title, momentary, callback in buttons:
            button = ToggleButton.alloc().initWithFrame_(NSMakeRect(x, y, FOOT_BTN_W, FOOT_BTN_H))
            button.title = title
            button.momentary = momentary
            button.callback = callback
            button.font_size = 12
            self.root.addSubview_(button)
            self.footer_buttons.append(button)
            x += FOOT_BTN_W + 8
        self.link_button = self.footer_buttons[1]
        self.tone_button = self.footer_buttons[2]
        self.lr_button = self.footer_buttons[3]
        if len(self.host.cfg.inputs) < 2:
            self.link_button.setHidden_(True)

    @objc.python_method
    def _row_tops(self):
        tops, y = [], TOP_H + 8
        for _, h in ROWS:
            tops.append(y)
            y += h
        return tops, y

    @objc.python_method
    def _build_strips(self):
        for view in self.strip_views:
            view.removeFromSuperview()
        self.strip_views = []
        self.strips = []
        cfg = self.host.cfg
        tops, bottom = self._row_tops()
        width, _ = self._content_size()

        # 背景の区切り線
        self.root.row_lines = tops[1:]
        self.root.master_x = PAD + LABEL_W + CH_W * len(cfg.inputs)
        self.root.grid_top = tops[1]
        self.root.grid_bottom = bottom
        self.root.setNeedsDisplay_(True)

        for (name, _), top in zip(ROWS, tops):
            if name:
                self._add(_label(name, NSMakeRect(PAD, top + 8, LABEL_W - 4, 16),
                                 align=NSTextAlignmentLeft))

        # チャンネル（リンク中は In 1-2 を2列分の幅で1本にまとめる）
        columns = []
        if cfg.is_linked:
            columns.append(([0, 1], PAD + LABEL_W, CH_W * 2))
            start = 2
        else:
            start = 0
        for i in range(start, len(cfg.inputs)):
            columns.append(([i], PAD + LABEL_W + CH_W * i, CH_W))
        for indexes, x, w in columns:
            self.strips.append(self._build_channel(indexes, x, w, tops))
        self.master = self._build_master(self.root.master_x, MASTER_W, tops)
        self.sync_from_config()

    @objc.python_method
    def _add(self, view):
        self.root.addSubview_(view)
        self.strip_views.append(view)
        return view

    @objc.python_method
    def _toggle(self, title, cx, y, callback):
        button = ToggleButton.alloc().initWithFrame_(NSMakeRect(cx - BTN_W / 2, y, BTN_W, BTN_H))
        button.title = title
        button.callback = callback
        return self._add(button)

    @objc.python_method
    def _small_slider(self, cx, y, lo, hi, action, tag):
        slider = NSSlider.alloc().initWithFrame_(NSMakeRect(cx - BTN_W / 2, y, BTN_W, 18))
        slider.setMinValue_(lo)
        slider.setMaxValue_(hi)
        slider.setControlSize_(1)   # NSControlSizeSmall
        slider.setTrackFillColor_(ACCENT)
        slider.setContinuous_(True)
        slider.setTag_(tag)
        slider.setTarget_(self)
        slider.setAction_(action)
        return self._add(slider)

    @objc.python_method
    def _fader(self, cx, y, n_meters, tag):
        group_w = n_meters * METER_W + (n_meters - 1) * 4 + 6 + 22
        x = cx - group_w / 2
        meters = []
        for _ in range(n_meters):
            meters.append(self._add(MeterView.alloc().initWithFrame_(
                NSMakeRect(x, y, METER_W, FADER_H))))
            x += METER_W + 4
        slider = NSSlider.alloc().initWithFrame_(NSMakeRect(x + 2, y - 3, 22, FADER_H + 6))
        slider.setMinValue_(-60.0)
        slider.setMaxValue_(12.0)
        slider.setTrackFillColor_(ACCENT)
        slider.setContinuous_(True)
        slider.setTag_(tag)
        slider.setTarget_(self)
        slider.setAction_("faderChanged:")
        self._add(slider)
        value = self._add(_label("0.0 dB", NSMakeRect(cx - 40, y + FADER_H + 8, 80, 16),
                                 secondary=False))
        value.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0))
        return meters, slider, value

    @objc.python_method
    def _build_channel(self, indexes, x, w, tops):
        cfg = self.host.cfg
        i = indexes[0]
        linked = len(indexes) == 2
        cx = x + w / 2
        inp = cfg.inputs[i]

        # 見出し: 名前（クリックして書き換えられる）と入力番号
        name = NSTextField.alloc().initWithFrame_(NSMakeRect(x + 6, tops[0] + 4, w - 12, 20))
        name.setStringValue_(" / ".join(cfg.inputs[k].name for k in indexes) if linked else inp.name)
        name.setBezeled_(False)
        name.setDrawsBackground_(False)
        name.setEditable_(not linked)
        name.setSelectable_(not linked)
        name.setAlignment_(NSTextAlignmentCenter)
        name.setFont_(NSFont.boldSystemFontOfSize_(13))
        name.cell().setLineBreakMode_(NSLineBreakByClipping)
        name.cell().setSendsActionOnEndEditing_(True)
        name.setTag_(i)
        name.setTarget_(self)
        name.setAction_("nameChanged:")
        self._add(name)
        sub = self._add(_label("In 1-2 · Linked" if linked else f"In {i + 1}",
                               NSMakeRect(x + 4, tops[0] + 25, w - 8, 15)))

        # Limiter: ON/OFF・スレッショルド・減衰メーター
        top = tops[1] + 8
        limiter = self._toggle("Limiter", cx, top, lambda on, k=i: self._on_channel_limiter(k, on))
        thr = self._small_slider(cx, top + BTN_H + 6, -30.0, 0.0, "thresholdChanged:", i)
        thr_label = self._add(_label("", NSMakeRect(cx - 50, top + BTN_H + 26, 100, 15)))
        gr = self._add(GainReductionView.alloc().initWithFrame_(
            NSMakeRect(cx - BTN_W / 2, top + BTN_H + 45, BTN_W, 5)))

        # Pan（リンク中は Balance）
        top = tops[2] + 8
        pan = self._small_slider(cx, top, -1.0, 1.0, "panChanged:", i)
        pan_label = self._add(_label("", NSMakeRect(cx - 50, top + 20, 100, 15)))

        # Fader とメーター
        meters, fader, fader_label = self._fader(cx, tops[3] + 10, 2 if linked else 1, i)

        # Mute
        mute = self._toggle("Mute", cx, tops[4] + 10, lambda on, k=i: self._on_mute(k, on))

        return {"indexes": indexes, "linked": linked, "sub": sub, "limiter": limiter, "thr": thr,
                "thr_label": thr_label, "gr": gr, "pan": pan, "pan_label": pan_label,
                "meters": meters, "fader": fader, "fader_label": fader_label, "mute": mute}

    @objc.python_method
    def _build_master(self, x, w, tops):
        cx = x + w / 2
        self._add(_label("Master", NSMakeRect(x + 4, tops[0] + 3, w - 8, 18), size=13, bold=True,
                         secondary=False))
        self._add(_label("L / R", NSMakeRect(x + 4, tops[0] + 25, w - 8, 15)))
        top = tops[1] + 8
        limiter = self._toggle("Limiter", cx, top, self._on_master_limiter)
        self._add(_label("-1 dBFS", NSMakeRect(cx - 50, top + BTN_H + 26, 100, 15)))
        gr = self._add(GainReductionView.alloc().initWithFrame_(
            NSMakeRect(cx - BTN_W / 2, top + BTN_H + 45, BTN_W, 5)))
        meters, fader, fader_label = self._fader(cx, tops[3] + 10, 2, 100)
        return {"limiter": limiter, "gr": gr, "meters": meters, "fader": fader,
                "fader_label": fader_label}

    # ---- 設定 → 画面 ----

    @objc.python_method
    def sync_from_config(self):
        """設定の値を画面の部品に反映する（メニュー側で変えたときにも呼ぶ）。"""
        cfg = self.host.cfg
        for strip in self.strips:
            inp = cfg.inputs[strip["indexes"][0]]
            strip["limiter"].setOn_(inp.limiter)
            strip["thr"].setDoubleValue_(inp.limiter_threshold_db)
            strip["thr_label"].setStringValue_(f"Thr {inp.limiter_threshold_db:.1f} dB")
            if strip["linked"]:
                strip["pan"].setDoubleValue_(cfg.link_balance)
                strip["pan_label"].setStringValue_(pan_text(cfg.link_balance, "Bal "))
            else:
                strip["pan"].setDoubleValue_(inp.pan)
                strip["pan_label"].setStringValue_(pan_text(inp.pan))
            strip["fader"].setDoubleValue_(inp.gain_db)
            strip["fader_label"].setStringValue_(db_text(inp.gain_db))
            strip["mute"].setOn_(inp.mute)
        self.master["limiter"].setOn_(cfg.limiter)
        self.master["fader"].setDoubleValue_(cfg.master_gain_db)
        self.master["fader_label"].setStringValue_(db_text(cfg.master_gain_db))
        self.mode_control.setSelectedSegment_(0 if cfg.output_mode == "stereo" else 1)
        self.buffer_popup.selectItemWithTitle_(str(cfg.blocksize))
        self.link_button.setOn_(cfg.is_linked)
        self.tone_button.setOn_(self.host.test_tone == TONE_BOTH)
        self.lr_button.setOn_(self.host.test_tone == TONE_LR)

    # ---- 画面 → 設定 ----

    @objc.python_method
    def _changed(self):
        self.host.settings_changed()

    def faderChanged_(self, sender):
        db = round(sender.doubleValue() * 2) / 2   # 0.5 dB 刻み
        if sender.tag() == 100:
            self.host.cfg.master_gain_db = db
            self.master["fader_label"].setStringValue_(db_text(db))
        else:
            self.host.cfg.inputs[sender.tag()].gain_db = db
            for strip in self.strips:
                if strip["indexes"][0] == sender.tag():
                    strip["fader_label"].setStringValue_(db_text(db))
        self._changed()

    def panChanged_(self, sender):
        value = round(sender.doubleValue() * 100) / 100
        if abs(value) < 0.03:
            value = 0.0   # センター付近は吸い付かせる
        for strip in self.strips:
            if strip["indexes"][0] != sender.tag():
                continue
            if strip["linked"]:
                self.host.cfg.link_balance = value
                strip["pan_label"].setStringValue_(pan_text(value, "Bal "))
            else:
                self.host.cfg.inputs[sender.tag()].pan = value
                strip["pan_label"].setStringValue_(pan_text(value))
        self._changed()

    def thresholdChanged_(self, sender):
        db = round(sender.doubleValue() * 2) / 2
        self.host.cfg.inputs[sender.tag()].limiter_threshold_db = db
        for strip in self.strips:
            if strip["indexes"][0] == sender.tag():
                strip["thr_label"].setStringValue_(f"Thr {db:.1f} dB")
        self._changed()

    def nameChanged_(self, sender):
        text = sender.stringValue().strip()
        inp = self.host.cfg.inputs[sender.tag()]
        if text and text != inp.name:
            inp.name = text
            self._changed()
        else:
            sender.setStringValue_(inp.name)

    def outputModeChanged_(self, sender):
        self.host.cfg.output_mode = "stereo" if sender.selectedSegment() == 0 else "mono"
        self._changed()

    def bufferSizeChanged_(self, sender):
        size = int(sender.titleOfSelectedItem())
        if size != self.host.cfg.blocksize:
            self.host.cfg.blocksize = size
            self._changed()
            # バッファサイズはストリームを開き直さないと変わらない
            self.host.restart_engine()

    @objc.python_method
    def _on_channel_limiter(self, index, on):
        self.host.cfg.inputs[index].limiter = on
        self._changed()

    @objc.python_method
    def _on_mute(self, index, on):
        self.host.cfg.inputs[index].mute = on
        self._changed()

    @objc.python_method
    def _on_master_limiter(self, on):
        self.host.cfg.limiter = on
        self._changed()

    @objc.python_method
    def _on_link(self, on):
        cfg = self.host.cfg
        cfg.link_in_1_2 = on
        if on:
            # リンクした瞬間に In 1 の設定を In 2 にコピーする
            a, b = cfg.inputs[0], cfg.inputs[1]
            b.gain_db, b.mute = a.gain_db, a.mute
            b.limiter, b.limiter_threshold_db = a.limiter, a.limiter_threshold_db
        self._build_strips()
        self._changed()

    @objc.python_method
    def _on_test_tone(self, on):
        self.host.set_test_tone(TONE_BOTH if on else None)

    @objc.python_method
    def _on_lr_test(self, on):
        self.host.set_test_tone(TONE_LR if on else None)

    # ---- 表示の更新（1秒に30回） ----

    @objc.python_method
    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        if self.timer is None:
            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / FPS, self, "tick:", None, True)
            # スライダーを動かしている間も止まらないように登録する
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def tick_(self, timer):
        if not self.window.isVisible():
            return
        now = time.monotonic()
        dt, self.last_tick = now - self.last_tick, now
        if now - self.last_blink >= BLINK_SEC:
            self.last_blink = now
            self.status_pill.blink()
        self._update_status()
        engine = self.host.engine
        mixer = engine.mixer if engine is not None else None
        n_in = len(self.host.cfg.inputs)
        if mixer is not None:
            peaks = list(mixer.peak_in) + list(mixer.peak_out)
            mixer.peak_in.fill(0.0)
            mixer.peak_out.fill(0.0)
            ch_gr = [lin_to_db(float(g)) for g in mixer.channel_limiter.gain]
            master_gr = lin_to_db(float(mixer.limiter.gain)) if mixer.limiter_enabled else 0.0
        else:
            peaks = [0.0] * (n_in + 2)
            ch_gr = [0.0] * n_in
            master_gr = 0.0

        for strip in self.strips:
            for meter, k in zip(strip["meters"], strip["indexes"]):
                meter.setLevel_(self._ballistics(k, peaks[k], dt))
            strip["gr"].setReduction_(ch_gr[strip["indexes"][0]] if strip["limiter"].state else 0.0)
        for j, meter in enumerate(self.master["meters"]):
            meter.setLevel_(self._ballistics(n_in + j, peaks[n_in + j], dt))
        self.master["gr"].setReduction_(master_gr)

    @objc.python_method
    def _ballistics(self, key, peak, dt):
        """上がるときはすぐ、下がるときはゆっくり（20dB/秒）。"""
        db = lin_to_db(peak, METER_FLOOR_DB)
        shown = max(db, self.display_db.get(key, METER_FLOOR_DB) - METER_FALL_DB_PER_SEC * dt)
        self.display_db[key] = shown
        return shown

    @objc.python_method
    def _update_logo(self):
        """ロゴ「BH VMIX」を描く。V だけアクセントカラーの反対色にする。"""
        dark = _is_dark(self.root)
        font = NSFont.boldSystemFontOfSize_(13)
        text = NSMutableAttributedString.alloc().initWithString_attributes_(
            "BH VMIX", {NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.labelColor()})
        text.addAttribute_value_range_(NSForegroundColorAttributeName,
                                       COMPLEMENT_DARK if dark else COMPLEMENT, (3, 1))
        self.logo.setAttributedStringValue_(text)

    @objc.python_method
    def _update_status(self):
        self._update_logo()
        for name in self.info_devices:
            name.setTextColor_(_accent_text(self.root))
        # 「In 1-2 · Linked」はアクセントの赤で表示（ライト/ダークの切り替えに追従する）
        for strip in self.strips:
            if strip["linked"]:
                strip["sub"].setTextColor_(_accent_text(self.root))
        engine = self.host.engine
        state = engine.state if engine is not None else "停止"
        missing = getattr(self.host, "source_missing", False)
        routing = engine.routing if engine is not None and state == "動作中" else None
        if missing:
            text, kind, detail = "● インターフェース未接続", "alert", ""
        elif state == "動作中":
            text, kind = "● 接続中", "ok"
            if self.host.test_tone:
                text += " · " + ("L/R Test" if self.host.test_tone == TONE_LR else "Test Tone")
            detail = f"{routing.device_name} · {routing.samplerate / 1000:g} kHz" if routing else ""
        elif state == "接続待ち":
            text, kind, detail = "● 接続待ち", "wait", self.host.cfg.device
        else:
            text, kind, detail = f"● {state}", "idle", ""
        self.status_pill.setText_kind_(text, kind)
        self.device_label.setStringValue_(detail)
        # デバイス名は状態表示のすぐ右に置く
        pill = self.status_pill.frame()
        label = self.device_label.frame()
        self.device_label.setFrameOrigin_((pill.origin.x + pill.size.width + 8, label.origin.y))

    # ---- 動作確認用 ----

    @objc.python_method
    def snapshot(self, path: str, dark: bool = False) -> None:
        """ウィンドウの中身を PNG 画像に書き出す（見た目の確認用）。"""
        from AppKit import NSAppearance
        appearance = NSAppearance.appearanceNamed_(
            NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua)
        self.window.setAppearance_(appearance)
        self._update_logo()
        self.root.setNeedsDisplay_(True)
        view = self.root
        rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        data.writeToFile_atomically_(path, True)
