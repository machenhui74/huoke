"""采集后端注册表。新增后端：实现 Collector，在 BACKENDS 里登记名字。"""
from __future__ import annotations

from .base import CollectResult, Collector
from .mediacrawler import MediaCrawlerCollector

BACKENDS: dict[str, type[Collector]] = {
    "mediacrawler": MediaCrawlerCollector,
}

__all__ = ["BACKENDS", "CollectResult", "Collector", "MediaCrawlerCollector"]
