"""アプリのアイコン（assets/BHVMIX.icns）を作る。

灰色の角丸の四角に、「VMIX」（V はアクセントカラーの赤、MIX は白）を中央に、小さな「BH」を上に描く。
使い方: .venv/bin/python scripts/make_icon.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (NSBezierPath, NSBitmapImageFileTypePNG, NSBitmapImageRep, NSCalibratedRGBColorSpace,
                    NSColor, NSColorSpace, NSFont, NSFontAttributeName, NSFontWeightBlack, NSFontWeightBold,
                    NSForegroundColorAttributeName, NSGraphicsContext, NSKernAttributeName,
                    NSMakeRect, NSMutableAttributedString)

PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKGROUND = "#48484A"   # 灰色
ACCENT = "#A01E23"       # 深い赤（アクセントカラー）
OUT = PROJECT_DIR / "assets" / "BHVMIX.icns"
# iconset に必要な画像（ファイル名, 一辺のピクセル数）
SIZES = [(f"icon_{s}x{s}{suffix}.png", s * scale)
         for s in (16, 32, 128, 256, 512) for suffix, scale in (("", 1), ("@2x", 2))]


def rgb(hex_code):
    r, g, b = (int(hex_code[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


def draw(size: int, path: Path) -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    # 色がずれないよう、画像の色の基準を sRGB にする
    rep = rep.bitmapImageRepByRetaggingWithColorSpace_(NSColorSpace.sRGBColorSpace())
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    # macOS のアイコンと同じく、周りに少し余白を取った灰色の角丸の四角
    inset = size * 0.1
    body = size - inset * 2
    rect = NSMakeRect(inset, inset, body, body)
    rgb(BACKGROUND).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, body * 0.225, body * 0.225).fill()

    # 「VMIX」（V だけアクセントカラーの赤、MIX は白）をアイコンの真ん中に置く
    font = NSFont.systemFontOfSize_weight_(body * 0.27, NSFontWeightBlack)
    text = NSMutableAttributedString.alloc().initWithString_attributes_(
        "VMIX", {NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.whiteColor(),
                 NSKernAttributeName: -body * 0.005})
    text.addAttribute_value_range_(NSForegroundColorAttributeName, rgb(ACCENT), (0, 1))
    w = text.size().width
    # 大文字だけなので、文字の見た目の高さ（capHeight）の中心を、アイコンの中心に合わせる
    # drawAtPoint の y は行の下端なので、ベースラインとの差（descender、負の値）を足す
    center = size / 2
    baseline = center - font.capHeight() / 2
    text.drawAtPoint_((center - w / 2, baseline + font.descender()))

    # 小さな「BH」を上に添える
    small_font = NSFont.systemFontOfSize_weight_(body * 0.11, NSFontWeightBold)
    small = NSMutableAttributedString.alloc().initWithString_attributes_(
        "BH", {NSFontAttributeName: small_font,
               NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(1.0, 0.6)})
    sw = small.size().width
    small_baseline = baseline + font.capHeight() + body * 0.07
    small.drawAtPoint_((center - sw / 2, small_baseline + small_font.descender()))

    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(str(path), True)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "BHVMIX.iconset"
        iconset.mkdir()
        for name, px in SIZES:
            draw(px, iconset / name)
        OUT.parent.mkdir(exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
        if len(sys.argv) > 1:
            # 確認用に 512px の画像も書き出す
            draw(512, Path(sys.argv[1]))
    print(f"できました: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
