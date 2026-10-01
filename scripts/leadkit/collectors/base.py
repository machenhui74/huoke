"""采集后端接口。

后端只需要做一件事：按 CollectPlan 采集公开评论，把产物写进 batch_dir，
并返回 CollectResult。后面的标准化、打分、入库完全不关心是谁采的。
想接别的爬虫或官方 API：继承 Collector 实现 run()，在 __init__.py 注册即可。
参数护栏（guard.py）对所有后端统一生效，后端不需要、也不应该自己放宽。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..guard import CollectPlan


@dataclass
class CollectResult:
    """一次采集的结果。status 取值：ok / aborted:block / aborted:overfetch / failed / interrupted。"""

    batch_id: str
    batch_dir: Path
    status: str
    returncode: int | None = None
    notes: int = 0
    comments: int = 0
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Collector(ABC):
    """采集后端抽象。"""

    name: str = "base"

    @abstractmethod
    def preflight(self, plan: CollectPlan, platform_map: dict[str, Any], **opts: Any) -> list[str]:
        """开跑前检查，返回问题列表（空 = 可以跑）。不允许有副作用。"""

    @abstractmethod
    def describe(self, plan: CollectPlan, batch_dir: Path, platform_map: dict[str, Any] | None = None) -> str:
        """给人看的执行计划（dry-run 时展示）。"""

    @abstractmethod
    def run(self, plan: CollectPlan, platform_map: dict[str, Any], batch_id: str, batch_dir: Path) -> CollectResult:
        """真正执行采集。调用方保证已经通过 preflight。"""
