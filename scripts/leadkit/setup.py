"""环境准备与自检：安装上游 MediaCrawler、打补丁、体检。

跨系统原则：只用 git / uv / node 这类各平台都有的命令，不写任何 shell 专有语法；
路径全部走 pathlib。每一步失败都给出「下一步该敲什么」，方便人或 agent 直接照做。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guard import Ledger
from .logger import get_logger
from .paths import PATCHES_DIR, PLATFORMS_DIR, Workspace
from .profile import ProfileError, load_platform_map, load_profile
from .scoring import Scorer

LOG = get_logger("setup")

UPSTREAM = "https://github.com/NanmiCoder/MediaCrawler.git"
# 补丁是针对这个提交验证过的。换更新的版本，补丁可能打不上（会明确报错，不会静默失败）。
PINNED_REF = "380b426"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    required: bool = True  # False = 缺了只是警告


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    LOG.debug("执行 %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def patch_files(with_raw_identity: bool) -> list[Path]:
    """默认只打防封号补丁；明文昵称/用户 ID 的补丁是可选的，需要显式开启。"""
    files = sorted(PATCHES_DIR.glob("*.patch"))
    if with_raw_identity:
        files += sorted((PATCHES_DIR / "optional").glob("*.patch"))
    return files


def apply_patches(mc_dir: Path, with_raw_identity: bool = False) -> list[tuple[str, str]]:
    """幂等地打补丁。返回 [(补丁名, applied|already|failed:原因)]。"""
    results = []
    for p in patch_files(with_raw_identity):
        if _run(["git", "apply", "--check", "--reverse", str(p)], mc_dir).returncode == 0:
            LOG.info("补丁已存在，跳过 %s", p.name)
            results.append((p.name, "already"))
            continue
        chk = _run(["git", "apply", "--check", str(p)], mc_dir)
        if chk.returncode != 0:
            LOG.error("补丁无法应用 %s: %s", p.name, chk.stderr.strip()[:200])
            results.append((p.name, f"failed:{chk.stderr.strip()[:200]}"))
            continue
        _run(["git", "apply", str(p)], mc_dir)
        LOG.info("已应用补丁 %s", p.name)
        results.append((p.name, "applied"))
    return results


def setup(ws: Workspace, ref: str = PINNED_REF, mc_dir: Path | None = None, skip_sync: bool = False,
          with_raw_identity: bool = False) -> int:
    """安装流程。返回进程退出码（0 成功）。"""
    ws.ensure()
    target = mc_dir or ws.vendor
    for tool in ("git",):
        if not shutil.which(tool):
            LOG.error("找不到 %s，请先安装", tool)
            return 2
    if mc_dir is None:
        if (target / "main.py").exists():
            LOG.info("已存在 %s，跳过克隆", target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            LOG.info("克隆上游 MediaCrawler → %s", target)
            r = _run(["git", "clone", "--quiet", UPSTREAM, str(target)])
            if r.returncode != 0:
                LOG.error("克隆失败: %s", r.stderr.strip()[:300])
                return 3
            r = _run(["git", "checkout", "--quiet", ref], target)
            if r.returncode != 0:
                LOG.error("切换到 %s 失败: %s", ref, r.stderr.strip()[:300])
                return 3
    elif not (target / "main.py").exists():
        LOG.error("%s 不像 MediaCrawler 目录（没有 main.py）", target)
        return 2

    results = apply_patches(target, with_raw_identity)
    failed = [n for n, s in results if s.startswith("failed")]
    if failed:
        LOG.error("补丁失败：%s。上游版本可能不匹配，试试 --ref %s", failed, PINNED_REF)
        return 4

    if skip_sync:
        LOG.warning("已跳过依赖安装（--skip-sync）。采集前请在 %s 运行 uv sync", target)
    elif shutil.which("uv"):
        LOG.info("安装依赖：uv sync（首次较慢）")
        r = _run(["uv", "sync"], target, timeout=1800)
        if r.returncode != 0:
            LOG.error("uv sync 失败: %s", r.stderr.strip()[-300:])
            return 5
    else:
        LOG.error("没有 uv。安装方式见 https://docs.astral.sh/uv/ ，然后重跑 leadctl setup")
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
    add("uv", bool(shutil.which("uv")), shutil.which("uv") or "", "安装 uv：https://docs.astral.sh/uv/")
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
        add("今日采集用量", True, f"{use['sessions']} 次 / {use['notes']} 篇 / {use['comments']} 条评论", required=False)
    except ProfileError as exc:
        add(f"profile {profile_name}", False, str(exc), "leadctl profiles 查看可用 profile")

    has_mc = (mc / "main.py").exists()
    add("MediaCrawler 已安装", has_mc, str(mc), "运行 leadctl setup", required=False)
    if has_mc:
        add("MediaCrawler 依赖", (mc / ".venv").exists(), "", f"在 {mc} 运行 uv sync", required=False)
        for pf in sorted(PLATFORMS_DIR.glob("*.toml")):
            pm = load_platform_map(pf.stem)
            patch = pm.get("patch")
            if not patch:
                continue
            f = mc / patch["file"]
            ok = f.exists() and patch["marker"] in f.read_text(encoding="utf-8", errors="ignore")
            add(f"{pm['platform']['label']}搜索截断补丁", ok, patch["marker"], "运行 leadctl setup 重新打补丁", required=False)
    return checks
