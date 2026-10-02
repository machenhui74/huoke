"""测试公共：把 scripts/ 加进 sys.path，并提供内置 profile 的加载。"""
import os
import sys
from pathlib import Path

# 测试必须与使用者的环境隔离：LEADKIT_HOME / LEADKIT_PROFILE / LEADKIT_MC_DIR 若残留在 shell 里，
# 会让 CLI 类测试悄悄读写真实工作区、换掉画像，导致莫名其妙的失败甚至污染真实数据。
for _k in ("LEADKIT_HOME", "LEADKIT_PROFILE", "LEADKIT_MC_DIR"):
    os.environ.pop(_k, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from leadkit.profile import load_profile  # noqa: E402

PROFILE = "education_taizhou"


def profile():
    return load_profile(PROFILE)
