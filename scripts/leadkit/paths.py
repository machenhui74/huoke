"""路径与工作区。

两类目录严格分开：
- SKILL_ROOT：skill 本体（代码、profile、补丁），可以被整体复制/分发，运行时只读；
- Workspace：运行数据（上游爬虫、原始采集、数据库、导出、登录态、日志），
  默认 ~/.leadkit，可用 --workdir 或环境变量 LEADKIT_HOME 改。
  这样 skill 目录里永远不会混进昵称、登录态或数据库。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger

LOG = get_logger("paths")

# scripts/leadkit/paths.py -> skill 根目录
SKILL_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = SKILL_ROOT / "profiles"
PLATFORMS_DIR = SKILL_ROOT / "platforms"
PATCHES_DIR = SKILL_ROOT / "patches" / "mediacrawler"
ENV_HOME = "LEADKIT_HOME"


@dataclass(frozen=True)
class Workspace:
    """一个工作区 = 一个根目录 + 固定的子目录布局。"""

    root: Path

    @classmethod
    def resolve(cls, workdir: str | None = None) -> "Workspace":
        """优先级：命令行参数 > 环境变量 > ~/.leadkit。"""
        raw = workdir or os.environ.get(ENV_HOME) or str(Path.home() / ".leadkit")
        root = Path(raw).expanduser().resolve()
        LOG.debug("工作区解析为 %s", root)
        return cls(root)

    @property
    def vendor(self) -> Path:        # 上游 MediaCrawler 的 clone 位置
        return self.root / "vendor" / "MediaCrawler"

    @property
    def raw(self) -> Path:           # 每次采集一个批次目录，原始 CSV 不进 skill
        return self.root / "raw"

    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def exports(self) -> Path:       # 可外传的脱敏导出（无昵称）
        return self.root / "exports"

    @property
    def internal(self) -> Path:      # 含昵称的内部对照表，禁止外传
        return self.root / "internal"

    @property
    def state(self) -> Path:         # 采集台账（日配额、冷却、封控记录）
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def db_path(self, profile_name: str) -> Path:
        """每个 profile 一个库，不同行业/地区的线索互不污染。"""
        return self.db_dir / f"{profile_name}.sqlite"

    def ensure(self) -> "Workspace":
        for d in (self.raw, self.db_dir, self.exports, self.internal, self.state, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        # 数据目录里放一个 .gitignore，万一用户把工作区放进了仓库也不会被提交
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        LOG.debug("工作区已就绪 %s", self.root)
        return self
