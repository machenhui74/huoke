"""关键词待采队列：把扩出来的十几二十个词存下来，分天、分批采完。

为什么需要：单次词数、每天会话数都有上限（见 guard.py），清单里的十几二十个词一次采不完。
与其让人记着「上次采到哪个词了」，不如落成队列：扩词时入队，之后每次 `collect --next` 自动取下一批，
采成功才标记完成；失败或被风控打断的词留在队列里，下次接着来。

队列按 profile 分文件存放（品类/地区不同，词表自然不同），平台作为条目字段。
只存关键词和状态，不含任何评论数据，没有隐私问题。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .logger import get_logger

LOG = get_logger("queue")

# 一个词累计「真失败」达到这个次数就暂停取用，避免同一个词反复撞墙。需要人看一眼再 retry。
MAX_ATTEMPTS = 3
MAX_WORD_LEN = 30


def parse_words(raw: str) -> list[str]:
    """逗号分隔的词。顺手去掉从清单里整行复制过来的 `# 注释`。"""
    out = []
    for part in raw.replace("，", ",").split(","):
        word = part.split("#")[0].strip()
        if word:
            out.append(word)
    return out


def parse_proposal(path: Path) -> list[str]:
    """读取 `leadctl keywords` 写出的清单文件。

    跳过 # 开头的说明行，也跳过标了「附近区」的备选词（清单里明确写了「本次不要采集」）。
    """
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "附近区" in line:
            continue
        word = line.split("#")[0].strip()
        if word:
            words.append(word)
    return words


class KeywordQueue:
    """JSON 文件存储。量很小（几十条），整读整写即可；写入用临时文件 + 替换，避免写一半崩溃。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 存取 ----------
    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return list(data["items"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            bad = self.path.with_suffix(".bad.json")
            self.path.replace(bad)  # 保留坏文件供人查看，不要悄悄丢掉队列
            LOG.error("队列文件损坏（%s），已改名为 %s，当作空队列继续", exc, bad.name)
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---------- 写入 ----------
    def add(self, platform: str, words: list[str], source: str = "manual") -> dict[str, Any]:
        """入队。已存在的词不重复加，并说明原因。返回 {added: [...], skipped: {词: 原因}}。"""
        items = self._load()
        index = {(i["platform"], i["keyword"]): i for i in items}
        added: list[str] = []
        skipped: dict[str, str] = {}
        for w in words:
            if len(w) > MAX_WORD_LEN or "," in w:
                skipped[w] = f"词太长（>{MAX_WORD_LEN}）或含逗号"
            elif (platform, w) in index:
                st = index[(platform, w)]
                skipped[w] = {"pending": "已在队列中", "done": f"已采过（{st.get('updated', '')[:10]}）",
                              "skipped": "已被跳过（queue retry 可恢复）"}.get(st["status"], st["status"])
            else:
                item = {"platform": platform, "keyword": w, "status": "pending", "source": source,
                        "added": self._now(), "updated": self._now(), "attempts": 0, "batch": ""}
                items.append(item)
                index[(platform, w)] = item
                added.append(w)
        if added:
            self._save(items)
        LOG.info("入队 平台=%s 新增 %d 跳过 %d", platform, len(added), len(skipped))
        return {"added": added, "skipped": skipped}

    def _update(self, platform: str, words: list[str], fn) -> list[str]:
        items = self._load()
        hit = []
        for i in items:
            if i["platform"] == platform and i["keyword"] in words:
                fn(i)
                i["updated"] = self._now()
                hit.append(i["keyword"])
        if hit:
            self._save(items)
        return hit

    def mark_done(self, platform: str, words: list[str], batch: str) -> list[str]:
        def f(i):
            i["status"], i["batch"] = "done", batch
        LOG.info("标记完成 %s: %s", batch, words)
        return self._update(platform, words, f)

    def mark_failed(self, platform: str, words: list[str], batch: str, *, count: bool = True) -> list[str]:
        """一次没采成。词留在队列里，下次接着取。

        count=False 用于被风控信号打断或用户中断的情况：那不是这个词的问题，不该算它失败一次。
        """
        def f(i):
            if count:
                i["attempts"] = i.get("attempts", 0) + 1
            i["batch"] = batch
        LOG.warning("采集未成功 %s: %s（计入失败=%s）", batch, words, count)
        return self._update(platform, words, f)

    def skip(self, platform: str, words: list[str]) -> list[str]:
        return self._update(platform, words, lambda i: i.__setitem__("status", "skipped"))

    def retry(self, platform: str, words: list[str] | None = None) -> list[str]:
        """恢复为待采并清零失败次数。不传 words 则恢复该平台所有已跳过/卡住的词。"""
        def f(i):
            i["status"], i["attempts"] = "pending", 0
        items = self._load()
        targets = words or [i["keyword"] for i in items
                            if i["platform"] == platform and (i["status"] == "skipped" or i.get("attempts", 0) >= MAX_ATTEMPTS)]
        return self._update(platform, targets, f)

    # ---------- 读取 ----------
    def items(self, platform: str | None = None) -> list[dict[str, Any]]:
        return [i for i in self._load() if platform is None or i["platform"] == platform]

    def pending(self, platform: str) -> list[dict[str, Any]]:
        """可取用的词，保持入队顺序（先入先采；想让某个词优先，就先入队它）。"""
        return [i for i in self.items(platform) if i["status"] == "pending" and i.get("attempts", 0) < MAX_ATTEMPTS]

    def stuck(self, platform: str) -> list[dict[str, Any]]:
        return [i for i in self.items(platform) if i["status"] == "pending" and i.get("attempts", 0) >= MAX_ATTEMPTS]

    def counts(self, platform: str) -> dict[str, int]:
        its = self.items(platform)
        return {"pending": len(self.pending(platform)), "stuck": len(self.stuck(platform)),
                "done": sum(i["status"] == "done" for i in its), "skipped": sum(i["status"] == "skipped" for i in its)}
