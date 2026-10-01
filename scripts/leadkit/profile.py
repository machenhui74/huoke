"""Profile 加载与校验。

Profile = 一个 TOML 文件，描述「某个行业 + 某个地区」的全部业务规则：
词表、打分阈值、地理词、问题归类、扩词模板、采集限额、自检用例。
代码里不再有任何行业词或地名——换行业、换城市只需要新写一个 profile。
TOML 用标准库 tomllib 读取（Python >= 3.11），不引入第三方依赖。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from .logger import get_logger
from .paths import PLATFORMS_DIR, PROFILES_DIR, Workspace

LOG = get_logger("profile")

# 每个 profile 必须提供的段与关键字段；缺了直接报错并指明位置，别让 agent 猜。
REQUIRED = {
    "meta": ["name"],
    "scoring": ["ready_threshold", "review_threshold", "high_base", "mid_base", "low_base", "weak_base"],
    "words": ["high", "mid", "low", "parent"],
    "geo": ["core", "main", "city"],
}


class ProfileError(Exception):
    """profile 缺失或格式不对。"""


# 这些时区当前都是固定 UTC+8、没有夏令时：缺时区库时用固定偏移结果完全等价。
_FIXED_UTC8 = {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "PRC", "Asia/Hong_Kong", "Asia/Macau", "Asia/Taipei", "Asia/Singapore"}


@lru_cache(maxsize=None)
def get_tz(name: str) -> tzinfo:
    """取时区。

    Windows 默认不带时区库（需要 pip install tzdata）。分两种情况：
    - 无夏令时的 +8 时区：回落到固定偏移，结果一致，只在 DEBUG 日志里提一句；
    - 其他时区：回落会让日配额和冷却算错几个小时，宁可报错也不静默算错。
    lru_cache：同一个名字只解析一次，避免每次调用都重复刷日志。
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError / ImportError
        if name in _FIXED_UTC8:
            LOG.debug("系统没有时区库，%s 使用固定 UTC+8（等价）。pip install tzdata 可消除此回落", name)
            return timezone(timedelta(hours=8), name)
        raise ProfileError(f"本机找不到时区「{name}」（Windows 默认不带时区库）。请运行 pip install tzdata 后重试") from exc


@dataclass
class Profile:
    """已校验的 profile。各段保持 dict，使用方按需取值，缺省值写在取值处。"""

    path: Path
    data: dict[str, Any]
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.data["meta"]["name"]

    def section(self, key: str) -> dict[str, Any]:
        return self.data.get(key, {})

    @property
    def tz(self) -> tzinfo:
        return get_tz(self.section("meta").get("timezone", "Asia/Shanghai"))

    @property
    def words(self) -> dict[str, Any]:
        return self.section("words")

    def wordlist(self, key: str) -> list[str]:
        """取词表；以 @ 开头的引用会展开成同 profile 里的另一张词表。"""
        out: list[str] = []
        for w in self.words.get(key, []):
            if isinstance(w, str) and w.startswith("@"):
                out.extend(self.words.get(w[1:], []))
            else:
                out.append(w)
        return out


def _find(name_or_path: str, workspace: Workspace | None) -> Path:
    """按「显式路径 > 工作区自定义 > skill 内置」的顺序找 profile。"""
    p = Path(name_or_path).expanduser()
    if p.suffix == ".toml" and p.exists():
        return p
    candidates = []
    if workspace is not None:
        candidates.append(workspace.root / "profiles" / f"{name_or_path}.toml")
    candidates.append(PROFILES_DIR / f"{name_or_path}.toml")
    for c in candidates:
        if c.exists():
            return c
    tried = ", ".join(str(c) for c in candidates)
    raise ProfileError(f"找不到 profile「{name_or_path}」。已查找: {tried}。可用: {', '.join(list_profiles(workspace))}")


def list_profiles(workspace: Workspace | None = None) -> list[str]:
    """内置 + 工作区自定义的 profile 名（下划线开头的是模板，不列出）。"""
    dirs = [PROFILES_DIR] + ([workspace.root / "profiles"] if workspace is not None else [])
    return sorted({p.stem for d in dirs for p in d.glob("*.toml") if not p.stem.startswith("_")})


def load_profile(name_or_path: str, workspace: Workspace | None = None) -> Profile:
    """读取并校验 profile。"""
    path = _find(name_or_path, workspace)
    LOG.info("加载 profile %s", path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{path} 不是合法 TOML: {exc}") from exc
    for sec, keys in REQUIRED.items():
        if sec not in data:
            raise ProfileError(f"{path} 缺少 [{sec}] 段")
        for k in keys:
            if k not in data[sec]:
                raise ProfileError(f"{path} 的 [{sec}] 缺少字段 {k}")
    prof = Profile(path=path, data=data)
    LOG.debug("profile %s 校验通过: %d 条高意向词, %d 条自检用例",
              prof.name, len(prof.words.get("high", [])), len(data.get("cases", [])))
    return prof


def load_platform_map(platform: str) -> dict[str, Any]:
    """读取平台字段映射（MediaCrawler CSV 列名 → 统一字段）。"""
    path = PLATFORMS_DIR / f"{platform}.toml"
    if not path.exists():
        have = sorted(p.stem for p in PLATFORMS_DIR.glob("*.toml"))
        raise ProfileError(f"没有平台映射 {path.name}。已支持: {', '.join(have)}")
    return tomllib.loads(path.read_text(encoding="utf-8"))
