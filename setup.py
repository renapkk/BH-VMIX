"""py2app で BH VMIX.app を作るための設定。ビルドは scripts/build_app.sh から行う。"""

from setuptools import setup

APP = ["app_main.py"]
OPTIONS = {
    "argv_emulation": False,
    "packages": ["ssl_mixer", "numpy", "sounddevice", "_sounddevice_data", "cffi", "rumps"],
    "resources": ["config.example.toml"],
    "iconfile": "assets/BHVMIX.icns",
    "plist": {
        "CFBundleName": "BH VMIX",
        "CFBundleDisplayName": "BH VMIX",
        "CFBundleIdentifier": "com.koichi.sslmixer",
        "CFBundleShortVersionString": "0.4.0",
        "CFBundleVersion": "0.4.0",
        # Dock と ⌘Tab に出る、ふつうのアプリにする（メニューバーのアイコンも出る）
        "LSUIElement": False,
        # 同じアプリを二重に起動しない
        "LSMultipleInstancesProhibited": True,
        # マイク権限のダイアログに表示される説明（これが無いと録音できない）
        "NSMicrophoneUsageDescription":
            "SSL 12 の入力をミックスして BlackHole 2ch に送るためにマイク（オーディオ入力）を使います。",
        "LSMinimumSystemVersion": "13.0",
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
)
