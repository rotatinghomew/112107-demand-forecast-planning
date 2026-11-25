"""测试夹具：把 src 加入 sys.path 并提供数据装载。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from careplan.sources import load_bundle  # noqa: E402
from careplan.timeutils import parse_date  # noqa: E402


class CarePlanTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT / "data", ROOT / "data-contract.json")
        cls.root = ROOT

    @staticmethod
    def date(value):
        return parse_date(value)
