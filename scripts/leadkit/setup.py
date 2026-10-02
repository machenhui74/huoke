"""环境准备与自检：安装上游 MediaCrawler、打补丁、体检。

跨系统原则：只用 git / uv / node 这类各平台都有的命令，不写任何 shell 专有语法；
路径全部走 pathlib。每一步失败都给出「下一步该敲什么」，方便人或 agent 直接照做。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .guard import Ledger, check_patch
from .logger import get_logger
from .paths import PATCHES_DIR, PLATFORMS_DIR, Workspace
from .profile import ProfileError, load_platform_map, load_profile
from .scoring import Scorer
from .tools import ENV_UV, Found, find_uv, kill_tree

LOG = get_logger("setup")

UPSTREAM = "https://github.com/NanmiCoder/MediaCrawler.git"
# 补丁是针对这个提交验证过的。换更新的版本，补丁可能打不上（会明确报错，不会静默失败）。
PINNED_REF = "380b426"
UV_HELP = (f"安装 uv：https://docs.astral.sh/uv/ 。如果已经装了但不在 PATH，"
           f"设置环境变量 {ENV_UV}=uv 可执行文件的完整路径")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    required: bool = True  # False = 缺了只是警告


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    """静默执行并捕获输出。用于很快、不需要给人看进度的命令。"""
    LOG.debug("执行 %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _stream(cmd: list[str], cwd: Path | None = None, timeout: int = 1800, keep: int = 60) -> tuple[int, str]:
    """执行并把输出实时转到 stderr，同时留下最后 keep 行，失败时用来给出针对性提示。

    用于 uv sync 这种耗时长的命令：静默捕获会让人以为卡死了。
    """
    LOG.debug("执行(实时输出) %s (cwd=%s)", " ".join(cmd), cwd)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    timer = threading.Timer(timeout, lambda: kill_tree(proc))  # 看门狗，防止无限挂起
    timer.start()
    tail: deque[str] = deque(maxlen=keep)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stderr.write(line)
            tail.append(line)
        return proc.wait(), "".join(tail)
    finally:
        timer.cancel()


def sync_failure_hint(tail: str) -> str:
    """识别 uv sync 的已知失败并给出可操作的修复建议；认不出来返回空串。

    Windows 上 WinError 183（文件已存在）几乎总是 uv 的构建缓存残留，
    和包本身无关，清掉对应包的缓存重跑即可。
    """
    if "WinError 183" in tail or "os error 183" in tail:
        m = re.search(r"([A-Za-z0-9_.\-]+?)-\d[\w.]*\.dist-info", tail)
        pkg = m.group(1) if m else "<出错的包名>"
        return f"这是 uv 构建缓存残留（Windows 常见），不是包本身的问题。先运行：uv cache clean {pkg}，再重跑 leadctl setup"
    return ""


# 可选补丁按用途分组：0003/0004 = 明文昵称和用户 ID；0005 = 评论者 IP 省级属地（地域过滤用）。两组互相独立
IP_PATCH_PREFIX = "0005"


def patch_files(with_raw_identity: bool, with_ip_province: bool = False) -> list[Path]:
    """默认只打防封号补丁；明文昵称/用户 ID、IP 省级属地的补丁是可选的，需要显式开启。"""
    files = sorted(PATCHES_DIR.glob("*.patch"))
    for p in sorted((PATCHES_DIR / "optional").glob("*.patch")):
        if p.name.startswith(IP_PATCH_PREFIX):
            if with_ip_province:
                files.append(p)
        elif with_raw_identity:
            files.append(p)
    return files


# --ignore-whitespace：Windows 上 git 可能把上游文件检出成 CRLF，而补丁是 LF，
# 不加这个开关上下文行会对不上。只放宽空白差异，不影响实际改动内容。
_APPLY = ["git", "apply", "--ignore-whitespace"]


def apply_patches(mc_dir: Path, with_raw_identity: bool = False, with_ip_province: bool = False) -> list[tuple[str, str]]:
    """幂等地打补丁。返回 [(补丁名, applied|already|failed:原因)]。"""
    results = []
    for p in patch_files(with_raw_identity, with_ip_province):
        if _run(_APPLY + ["--check", "--reverse", str(p)], mc_dir).returncode == 0:
            LOG.info("补丁已存在，跳过 %s", p.name)
            results.append((p.name, "already"))
            continue
        chk = _run(_APPLY + ["--check", str(p)], mc_dir)
        if chk.returncode != 0:
            LOG.error("补丁无法应用 %s: %s", p.name, chk.stderr.strip()[:200])
            results.append((p.name, f"failed:{chk.stderr.strip()[:200]}"))
            continue
        _run(_APPLY + [str(p)], mc_dir)
        LOG.info("已应用补丁 %s", p.name)
        results.append((p.name, "applied"))
    return results


def setup(ws: Workspace, ref: str = PINNED_REF, mc_dir: Path | None = None, skip_sync: bool = False,
          with_raw_identity: bool = False, with_ip_province: bool = False) -> int:
    """安装流程。返回进程退出码（0 成功）。"""
    ws.ensure()
    target = mc_dir or ws.vendor
    if not shutil.which("git"):
        LOG.error("找不到 git，请先安装")
        return 2

    # 先确认 uv，再动网络：缺 uv 时别让人白等一轮克隆
    uv: Found | None = find_uv()
    if not skip_sync:
        if uv is None:
            LOG.error("没有找到 uv，尚未做任何改动。%s ；也可以加 --skip-sync 先只克隆和打补丁", UV_HELP)
            return 5
        if uv.off_path:
            LOG.warning("uv 不在 PATH 上，已在 %s 找到并直接使用", uv.path)

    if mc_dir is None:
        if (target / "main.py").exists():
            LOG.info("已存在 %s，跳过克隆", target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            LOG.info("克隆上游 MediaCrawler（约 40MB）→ %s", target)
            # 不捕获输出：让 git 自己的进度条直接显示在终端
            r = subprocess.run(["git", "clone", "--progress", UPSTREAM, str(target)], stdout=subprocess.DEVNULL)
            if r.returncode != 0:
                LOG.error("克隆失败（见上方 git 输出）。检查网络，或稍后重试")
                return 3
            r = _run(["git", "checkout", "--quiet", ref], target)
            if r.returncode != 0:
                LOG.error("切换到 %s 失败: %s", ref, r.stderr.strip()[:300])
                return 3
    elif not (target / "main.py").exists():
        LOG.error("%s 不像 MediaCrawler 目录（没有 main.py）", target)
        return 2

    results = apply_patches(target, with_raw_identity, with_ip_province)
    failed = [n for n, s in results if s.startswith("failed")]
    if failed:
        LOG.error("补丁失败：%s。上游版本可能不匹配，试试 --ref %s", failed, PINNED_REF)
        return 4

    if skip_sync:
        LOG.warning("已跳过依赖安装（--skip-sync）。采集前请在 %s 运行 uv sync", target)
    else:
        LOG.info("安装依赖：uv sync（首次较慢，下面是实时输出）")
        rc, tail = _stream([uv.path, "sync"], target)
        if rc != 0:
            hint = sync_failure_hint(tail)
            LOG.error("uv sync 失败（退出码 %s）。%s", rc, hint or "完整输出见上方")
            return 5
    LOG.info("setup 完成。下一步：leadctl doctor 检查环境")
    return 0


def doctor(ws: Workspace, profile_name: str, mc_dir: Path | None = None) -> list[Check]:
    """逐项体检，返回检查结果列表。"""
    mc = mc_dir or ws.vendor
    checks: list[Check] = []
    add = lambda *a, **k: checks.append(Check(*a, **k))

    v = sys.version_info
    add("Python >= 3.11", v >= (3, 11), f"当前 {v.major}.{v.minor}.{v.micro}", "安装 Python 3.11+")
    add("git", bool(shutil.which("git")), shutil.which("git") or "", "安装 git")
    uv = find_uv()
    if uv is None:
        add("uv", False, "", UV_HELP)
    else:
        # 已安装但不在 PATH：算通过（工具会直接用绝对路径），但要明说，免得人去重装
        add("uv", True, uv.path + ("（不在 PATH 上，工具会直接使用它）" if uv.off_path else ""))
    add("node（抖音签名需要）", bool(shutil.which("node")), shutil.which("node") or "", "只采小红书可忽略；采抖音需安装 Node.js >= 16", required=False)

    try:
        ws.ensure()
        add("工作区可写", True, str(ws.root))
    except OSError as exc:
        add("工作区可写", False, str(exc), "用 --workdir 指定可写目录")

    try:
        prof = load_profile(profile_name, ws)
        bad = Scorer(prof).self_check()
        add(f"profile {prof.name}", True, f"{prof.path.name}")
        add("profile 自检用例", not bad, f"{len(prof.data.get('cases', []))} 条，失败 {len(bad)}", "改词表后用例未通过：运行 leadctl check 看详情")
        use = Ledger(ws.state, prof.tz).usage_today()
        add("今日采集用量（各平台合计；额度按平台分别计算）", True, f"{use['sessions']} 次 / {use['notes']} 篇 / {use['comments']} 条评论", required=False)
    except ProfileError as exc:
        add(f"profile {profile_name}", False, str(exc), "leadctl profiles 查看可用 profile；时区报错时 pip install tzdata")

    has_mc = (mc / "main.py").exists()
    add("MediaCrawler 已安装", has_mc, str(mc), "运行 leadctl setup", required=False)
    if has_mc:
        add("MediaCrawler 依赖", (mc / ".venv").exists(), "", f"在 {mc} 运行 uv sync", required=False)
        for pf in sorted(PLATFORMS_DIR.glob("*.toml")):
            pm = load_platform_map(pf.stem)
            patch = pm.get("patch")
            if not patch:
                continue
            # 与采集预检共用 check_patch：它会同时检查 help.py 里的函数和 core.py 里的调用，
            # 只看一个文件的话，补丁没接上时 doctor 仍显示通过，会误导人。
            problems = check_patch(mc, pm)
            add(f"{pm['platform']['label']}搜索截断补丁", not problems, patch["marker"] if not problems else problems[0],
                "运行 leadctl setup 重新打补丁", required=False)
    return checks
