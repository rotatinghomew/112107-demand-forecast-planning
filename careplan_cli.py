#!/usr/bin/env python3
"""隔离模式（python3 -I）下的入口：自举 src 到 sys.path 后转交 careplan.cli。

用法：python3 -I careplan_cli.py <子命令> ...
数据目录作为参数传入，解释器不依赖工作目录，避免从 data/ 目录加载到植入模块。
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))

from careplan.cli import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1:])
