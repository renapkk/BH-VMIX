"""BH VMIX.app の入口（py2app がこのファイルを起動する）。"""

import sys

from ssl_mixer.app import main

sys.exit(main())
