#!/usr/bin/env python3
"""leadctl 启动脚本：无需安装，直接 `python scripts/leadctl.py <命令>`。

把本目录加进 sys.path，使 leadkit 包可被导入；这样 skill 被整体复制到任何位置都能跑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leadkit.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
