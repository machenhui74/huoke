"""测试公共：把 scripts/ 加进 sys.path，并提供内置 profile 的加载。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from leadkit.profile import load_profile  # noqa: E402

PROFILE = "education_taizhou"


def profile():
    return load_profile(PROFILE)
