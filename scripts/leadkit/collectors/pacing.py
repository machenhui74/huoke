"""采集节奏：把固定的请求间隔换成「有长有短、偶尔停久一点」的随机间隔。

为什么要有：上游每次请求后都是 `asyncio.sleep(固定 3 秒)`。固定间隔本身就是最典型的机器特征，
而且单次会话很短（十几篇笔记几分钟就跑完）。这里不增加任何请求，只是让节奏更接近人在读帖时的停顿。

设计约束（和防封号红线一致）：
  1. 只会比下限更慢，永远不会更快：最小值 = 请求间隔下限（base），不会低于红线 §1 的 3 秒。
  2. 不改变请求的内容、数量和顺序，只改请求之间的等待时间。
  3. 只依赖标准库：本文件既被 leadkit 导入，也会被 mc_runner.py 在 MediaCrawler 自己的 Python 环境里
     按文件路径直接加载（那里没有 leadkit 包）。

分布参考了 xiaohongshu-mcp 的 humanize 模块：用对数正态分布（多数间隔偏短、少数偏长，右侧有长尾）
加上下限截断，而不是均匀随机。均匀随机的间隔分布太「平」，反而不自然。
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable

# 默认值：中位数约 base 的 2.5 倍（base=3 时约 7.5 秒），截断在 base~5×base，
# 约 15% 的间隔后面再追加一次 20~60 秒的长停顿（相当于「看了一会儿、走神了」）。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "median_factor": 2.5,        # 中位间隔 = base × 该值
    "sigma": 0.5,                # 对数正态的离散程度；越大越参差
    "cap_factor": 5.0,           # 单次常规间隔上限 = base × 该值
    "long_pause_prob": 0.15,     # 每次间隔之后追加长停顿的概率
    "long_pause_sec": [20, 60],  # 长停顿的范围（秒）
}


def normalize(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """用默认值补全 profile 里写的配置。"""
    out = dict(DEFAULTS)
    out.update(cfg or {})
    return out


def validate(cfg: dict[str, Any], base: float) -> list[str]:
    """校验配置，返回问题列表（空 = 通过）。关闭节奏随机化时不校验其余项。"""
    if not cfg.get("enabled", True):
        return []
    bad: list[str] = []
    num = lambda k: isinstance(cfg.get(k), (int, float)) and not isinstance(cfg.get(k), bool)
    for key in ("median_factor", "sigma", "cap_factor", "long_pause_prob"):
        if not num(key):
            bad.append(f"pacing.{key} 必须是数字")
    if bad:
        return bad
    if cfg["median_factor"] < 1:
        bad.append("pacing.median_factor 不能小于 1（节奏只能比下限更慢）")
    if not 0 <= cfg["sigma"] <= 1.5:
        bad.append("pacing.sigma 应在 0~1.5 之间")
    if cfg["cap_factor"] < cfg["median_factor"]:
        bad.append("pacing.cap_factor 不能小于 median_factor")
    if not 0 <= cfg["long_pause_prob"] <= 0.5:
        bad.append("pacing.long_pause_prob 应在 0~0.5 之间")
    lp = cfg.get("long_pause_sec")
    if not (isinstance(lp, (list, tuple)) and len(lp) == 2 and all(isinstance(v, (int, float)) for v in lp)):
        bad.append("pacing.long_pause_sec 必须是 [最小秒, 最大秒]")
    else:
        if lp[0] < base:
            bad.append(f"pacing.long_pause_sec 的最小值 {lp[0]} 低于请求间隔下限 {base}")
        if lp[1] < lp[0]:
            bad.append("pacing.long_pause_sec 的最大值不能小于最小值")
        if lp[1] > 300:
            bad.append("pacing.long_pause_sec 的最大值不应超过 300 秒（过长会让会话卡住）")
    return bad


class Pacer:
    """间隔采样器。每次 next() 返回 (等待秒数, 是否为长停顿)。"""

    def __init__(self, base: float, cfg: dict[str, Any] | None = None, rng: random.Random | None = None):
        self.base = float(base)
        self.cfg = normalize(cfg)
        self.rng = rng or random.Random()

    def next(self) -> tuple[float, bool]:
        c = self.cfg
        median = self.base * c["median_factor"]
        lo, hi = self.base, self.base * c["cap_factor"]
        # 对数正态：exp(N(ln 中位数, sigma))。越界就重抽而不是直接截断——直接截断会让
        # 大量间隔恰好堆在上下限的同一个值上，形成不自然的尖峰；重抽 8 次仍越界才兜底截断。
        for _ in range(8):
            d = math.exp(math.log(median) + c["sigma"] * self.rng.gauss(0, 1))
            if lo <= d <= hi:
                break
        d = min(max(d, lo), hi)
        long_pause = self.rng.random() < c["long_pause_prob"]
        if long_pause:
            lo, hi = c["long_pause_sec"]
            d += self.rng.uniform(lo, hi)
        return d, long_pause

    def expected(self, samples: int = 4000) -> float:
        """平均每次间隔多少秒。用固定种子蒙特卡洛估算，只用于预检时给人看「大概要多久」。"""
        probe = Pacer(self.base, self.cfg, random.Random(20260101))
        return sum(probe.next()[0] for _ in range(samples)) / samples


def estimate_minutes(est_notes: int, n_keywords: int, base: float, cfg: dict[str, Any] | None) -> float:
    """粗估整次会话时长（分钟）。

    经验口径：每篇笔记约 2 次间隔（详情 + 评论翻页），每个关键词另有搜索和翻页的几次间隔。
    不含登录扫码和请求本身的耗时，所以是「下限偏乐观」的粗估。
    """
    sleeps = est_notes * 2 + n_keywords * 2 + 5
    cfg = normalize(cfg)
    mean = Pacer(base, cfg).expected() if cfg["enabled"] else float(base)
    return sleeps * mean / 60


def install(asyncio_mod: Any, base: float, cfg: dict[str, Any] | None,
            emit: Callable[[str], None] = print, rng: random.Random | None = None) -> Pacer | None:
    """替换 asyncio.sleep：只拦截「等于请求间隔 base」的那种调用，其余等待（登录轮询等）原样放行。

    上游所有节流都是 `await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)` 或其别名 crawl_interval，
    运行时值恰好等于 base。拦截后改用 Pacer 的采样值（>= base）。极个别无关的 sleep(3) 也被换成更长的
    等待，无害。运行时才打补丁，上游源码保持原样，升级不受影响。
    """
    cfg = normalize(cfg)
    if not cfg["enabled"]:
        return None
    pacer = Pacer(base, cfg, rng)
    original = asyncio_mod.sleep

    async def paced_sleep(delay, result=None):
        if delay == base:
            delay, was_long = pacer.next()
            if was_long:  # 只记长停顿：每次间隔都打日志会淹没真正有用的输出
                emit(f"LEADKIT_PACING long_pause {delay:.1f}s")
        return await original(delay, result)

    asyncio_mod.sleep = paced_sleep
    return pacer
