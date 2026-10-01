"""外部命令的定位与进程管理（跨平台）。

为什么不能只用 shutil.which("uv")：
uv 的安装器把自己装在 ~/.local/bin（Windows 是 %USERPROFILE%\\.local\\bin），并改「用户级 PATH」。
但 Git Bash、agent 的子 shell、已经开着的终端都不一定继承这个 PATH——
结果是 uv 明明装好了，工具却报「没装」，还让人去重装。所以这里先查 PATH，再查常见安装位。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger

LOG = get_logger("tools")

ENV_UV = "LEADKIT_UV"      # 显式指定 uv 可执行文件的完整路径（与 LEADKIT_MC_DIR 同风格）
IS_WIN = os.name == "nt"


@dataclass(frozen=True)
class Found:
    """找到的可执行文件。source: env（环境变量指定）/ path（在 PATH 上）/ fallback（常见安装位，不在 PATH）。"""

    path: str
    source: str

    @property
    def off_path(self) -> bool:
        return self.source == "fallback"


def _exe(name: str) -> str:
    return f"{name}.exe" if IS_WIN else name


def uv_candidates() -> list[Path]:
    """uv 官方安装器和常见包管理器的落点。"""
    home = Path.home()
    cands = [home / ".local" / "bin" / _exe("uv"), home / ".cargo" / "bin" / _exe("uv")]
    local = os.environ.get("LOCALAPPDATA")  # 仅 Windows 有
    if local:
        cands += [Path(local) / "Programs" / "uv" / _exe("uv"), Path(local) / "uv" / "bin" / _exe("uv")]
    return cands


def find_uv() -> Found | None:
    """查找 uv：环境变量 > PATH > 常见安装位。都没有返回 None。"""
    env = os.environ.get(ENV_UV)
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return Found(str(p), "env")
        LOG.warning("%s=%s 指向的文件不存在，改用自动查找", ENV_UV, env)
    hit = shutil.which("uv")
    if hit:
        return Found(hit, "path")
    for p in uv_candidates():
        if p.is_file():
            LOG.debug("uv 不在 PATH，但在 %s 找到", p)
            return Found(str(p), "fallback")
    return None


def kill_tree(proc: subprocess.Popen) -> None:
    """强制结束进程及其子进程。

    Windows 的 terminate() 只杀当前进程，不杀子进程（爬虫拉起的 Chrome 会残留），
    所以用 taskkill /T 连子树一起杀；POSIX 直接 kill。
    """
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()
