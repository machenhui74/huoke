"""统一日志模块。

项目是 Python，没有 Winston；这里用标准库 logging 做同样的事：
- 所有模块通过 get_logger(name) 取 logger，不自己配 handler；
- 控制台走 stderr，stdout 留给机器可读的结果（方便别的 agent 解析）；
- 文件日志按大小轮转，落在工作区 logs/ 下。

级别约定：
- DEBUG    逐条评论的打分细节
- INFO     流程节点（开始/结束/产物路径）
- WARNING  护栏触发、降级、需要人注意
- ERROR    步骤失败，但进程还能继续或可重试
- CRITICAL 风控信号，采集被强制中止
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT_NAME = "leadkit"
_FMT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
_configured = False


def get_logger(name: str) -> logging.Logger:
    """取子 logger，名字统一挂在 leadkit. 下，便于整体调级别。"""
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def setup_logging(log_dir: Path | None = None, verbose: bool = False, quiet: bool = False) -> None:
    """初始化根 logger，重复调用只生效一次。

    :param log_dir: 文件日志目录；None 时只输出到控制台
    :param verbose: True 时控制台也输出 DEBUG
    :param quiet: True 时控制台只输出 WARNING 及以上
    """
    global _configured
    if _configured:
        return
    root = logging.getLogger(ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO)
    console.setFormatter(logging.Formatter(_FMT, "%H:%M:%S"))
    root.addHandler(console)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "leadkit.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)  # 文件里永远留全量，事后排查用
            file_handler.setFormatter(logging.Formatter(_FMT))
            root.addHandler(file_handler)
        except OSError as exc:  # 只读目录等：降级为仅控制台，不阻断主流程
            root.warning("文件日志不可用，仅输出到控制台: %s", exc)
    _configured = True
