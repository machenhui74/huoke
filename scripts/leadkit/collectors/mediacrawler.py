"""MediaCrawler 采集后端。

MediaCrawler 不随 skill 分发（体积大、带登录态、且为 NCAL 非商业许可），
由 `leadctl setup` 克隆到工作区并打补丁。本模块只负责：
  - 开跑前的统一检查（guard）
  - 拼命令、起子进程、逐行喂给 RunMonitor
  - 触发红线就终止进程，并把结果写进台账

首次运行需要人工扫码；出现滑块/验证码一律停手，绝不尝试自动处理。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..guard import (
    CollectPlan, Ledger, RunMonitor, check_patch, effective_limits, validate_overrides, validate_plan,
)
from ..logger import get_logger
from ..paths import Workspace
from ..profile import Profile
from .base import CollectResult, Collector

LOG = get_logger("collect.mediacrawler")
RUNNER = Path(__file__).with_name("mc_runner.py")


def _git_rev(mc_dir: Path) -> str:
    """记录上游版本进 manifest，便于复现。没有 git 时返回 unknown。"""
    try:
        out = subprocess.run(["git", "-C", str(mc_dir), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class MediaCrawlerCollector(Collector):
    name = "mediacrawler"

    def __init__(self, workspace: Workspace, profile: Profile, mc_dir: Path | None = None):
        self.ws = workspace
        self.profile = profile
        self.mc_dir = mc_dir or workspace.vendor
        self.ledger = Ledger(workspace.state, profile.tz)

    # ---------- 开跑前 ----------
    def limits(self, allow_exceed: bool) -> dict[str, int]:
        return effective_limits(self.profile.section("limits"), allow_exceed)

    def overrides(self, plan: CollectPlan) -> dict[str, Any]:
        """运行时覆盖：profile 白名单项 + 由计划决定的限速。"""
        ov = dict(self.profile.data.get("collect", {}).get("overrides", {}))
        ov["CRAWLER_MAX_SLEEP_SEC"] = plan.sleep_sec   # 命令行没有这个参数，只能运行时覆盖
        ov["ENABLE_GET_WORDCLOUD"] = False
        return ov

    def preflight(self, plan: CollectPlan, platform_map: dict[str, Any], *, allow_exceed: bool = False,
                  allow_unverified: bool = False, **_: Any) -> list[str]:
        """参数 → profile 覆盖项 → 补丁 → 运行环境 → 台账，任何一项不过都拒绝。"""
        limits = self.limits(allow_exceed)
        bad = validate_plan(plan, limits, allow_exceed)
        bad += validate_overrides(self.profile.data.get("collect", {}).get("overrides", {}))
        bad += check_patch(self.mc_dir, platform_map, allow_unverified)
        if not (shutil.which("uv") or self._venv_python()):
            bad.append("找不到 uv，也没有 MediaCrawler/.venv。先运行 leadctl setup")
        bad += self.ledger.check(plan, limits)
        LOG.info("预检完成：%d 项问题", len(bad))
        return bad

    def _venv_python(self) -> Path | None:
        for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
            p = self.mc_dir / rel
            if p.exists():
                return p
        return None

    # ---------- 命令 ----------
    def build_command(self, plan: CollectPlan, platform_map: dict[str, Any], batch_dir: Path) -> list[str]:
        """所有限额都显式传参，不依赖上游 base_config 里的默认值。"""
        py = ["uv", "run", "python"] if shutil.which("uv") else [str(self._venv_python()), ]
        yn = lambda b: "yes" if b else "no"
        return py + [
            str(RUNNER),
            "--platform", platform_map["platform"]["mc_platform"],
            "--lt", plan.login,
            "--type", "search",
            "--start", "1",
            "--keywords", ",".join(plan.keywords),
            "--get_comment", "yes",
            "--get_sub_comment", yn(plan.sub_comments),
            "--get_media", yn(plan.media),
            "--headless", yn(plan.headless),
            "--save_data_option", "csv",
            "--save_data_path", str(batch_dir),
            "--crawler_max_notes_count", str(plan.notes_per_keyword),
            "--max_comments_count_singlenotes", str(plan.comments_per_note),
            "--max_concurrency_num", str(plan.concurrency),
            "--enable_ip_proxy", yn(plan.proxy),
        ]

    def describe(self, plan: CollectPlan, batch_dir: Path, platform_map: dict[str, Any] | None = None) -> str:
        cmd = self.build_command(plan, platform_map, batch_dir) if platform_map else ["(平台映射未提供)"]
        return (
            f"平台: {plan.platform}   关键词({len(plan.keywords)}): {', '.join(plan.keywords)}\n"
            f"每词笔记 ≤{plan.notes_per_keyword}，单帖评论 ≤{plan.comments_per_note}，并发 {plan.concurrency}，间隔 ≥{plan.sleep_sec}s\n"
            f"预计最多 {plan.est_notes} 篇笔记 / {plan.est_comments} 条评论\n"
            f"产物目录: {batch_dir}\n"
            f"命令(cwd={self.mc_dir}):\n  {' '.join(cmd)}"
        )

    # ---------- 执行 ----------
    def run(self, plan: CollectPlan, platform_map: dict[str, Any], batch_id: str, batch_dir: Path) -> CollectResult:
        batch_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.build_command(plan, platform_map, batch_dir)
        ov = self.overrides(plan)
        monitor = RunMonitor.from_platform(plan, platform_map)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "LEADKIT_MC_OVERRIDES": json.dumps(ov)}

        # manifest：事后能说清「当时用什么参数、什么版本采的」
        (batch_dir / "manifest.json").write_text(json.dumps({
            "batch_id": batch_id, "platform": plan.platform, "keywords": plan.keywords,
            "notes_per_keyword": plan.notes_per_keyword, "comments_per_note": plan.comments_per_note,
            "concurrency": plan.concurrency, "sleep_sec": plan.sleep_sec, "overrides": ov,
            "mediacrawler_rev": _git_rev(self.mc_dir), "profile": self.profile.name,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        self.ledger.append(event="start", batch=batch_id, platform=plan.platform, keywords=plan.keywords,
                           est_notes=plan.est_notes, est_comments=plan.est_comments)
        LOG.info("开始采集 %s：%s", batch_id, " ".join(cmd))
        LOG.warning("如出现二维码请用对应 App 扫码；出现滑块/验证码请直接关闭，不要尝试绕过")

        status, reason, rc = "failed", "", None
        proc = subprocess.Popen(cmd, cwd=self.mc_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        try:
            with (batch_dir / "crawler.log").open("w", encoding="utf-8") as logf:
                assert proc.stdout is not None
                for line in proc.stdout:
                    logf.write(line)
                    sys.stderr.write(line)  # stdout 留给 leadctl 的机器可读输出
                    if (why := monitor.feed(line.rstrip("\n"))) and not reason:
                        reason = why
                        LOG.critical("终止采集进程: %s", why)
                        self._stop(proc)
                        break
            rc = proc.wait(timeout=30) if proc.poll() is None else proc.returncode
            if monitor.abort_kind:
                status = f"aborted:{monitor.abort_kind}"
            else:
                status = "ok" if rc == 0 else "failed"
                reason = reason or ("" if rc == 0 else f"进程退出码 {rc}，查看 {batch_dir / 'crawler.log'}")
        except KeyboardInterrupt:
            self._stop(proc)
            status, reason = "interrupted", "用户中断"
            LOG.warning("采集被用户中断")
        except subprocess.TimeoutExpired:
            self._stop(proc)
            status, reason = "failed", "进程收尾超时，已强制结束"
        finally:
            if proc.poll() is None:
                self._stop(proc)

        # 风控信号单独记 block 事件：触发冷却，当天两次直接封存
        if status == "aborted:block":
            self.ledger.append(event="block", batch=batch_id, platform=plan.platform, reason=reason)
        self.ledger.append(event="end", batch=batch_id, status=status, returncode=rc,
                           actual_notes=monitor.notes_total, actual_comments=monitor.comments_total)
        level = LOG.info if status == "ok" else LOG.error
        level("采集结束 %s status=%s 笔记=%d 评论=%d %s", batch_id, status, monitor.notes_total,
              monitor.comments_total, reason)
        return CollectResult(batch_id, batch_dir, status, rc, monitor.notes_total, monitor.comments_total, reason)

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        """先礼后兵：SIGTERM 让上游有机会关浏览器，15 秒后还不退就强杀。"""
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            LOG.warning("进程 15 秒内未退出，强制结束")
            proc.kill()
