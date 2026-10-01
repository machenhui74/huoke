"""把采集产物统一成同一种评论记录。

两个入口：
- load_mediacrawler(): 读 MediaCrawler 的 contents + comments CSV，按平台映射表转换；
- load_generic_csv(): 读任意 CSV，列名由调用方指定（让没有用 MediaCrawler 的人也能接入）。

统一字段：platform, comment_id, post_id, post_url, post_title, search_keyword,
          text, nickname, user_hash, created_at, like_count, parent_comment_id

隐私：帖子 URL 一律去掉 ? 之后的部分（xsec_token 等会话令牌都在 query 里）。
"""
from __future__ import annotations

import csv
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from .logger import get_logger

LOG = get_logger("normalize")

FIELDS = [
    "platform", "comment_id", "post_id", "post_url", "post_title", "search_keyword",
    "text", "nickname", "user_hash", "created_at", "like_count", "parent_comment_id",
]


def clean_url(url: str) -> str:
    """去掉 query，防止 xsec_token 之类的会话令牌进入库和导出。"""
    base = (url or "").split("?")[0].strip()
    if "token" in base.lower():  # 令牌不在 query 里的极端情况：宁可拒绝也不入库
        raise ValueError(f"URL 路径里含 token，拒绝处理: {base[:60]}")
    return base


def when(raw: str, tz: tzinfo) -> str:
    """毫秒/秒级时间戳转本地时间；已经是文本的原样返回。"""
    text = (raw or "").strip()
    if not text.isdigit():
        return text
    value = int(text)
    if value > 10_000_000_000:  # 毫秒
        value /= 1000
    return datetime.fromtimestamp(value, tz).strftime("%Y-%m-%d %H:%M")


def read_csv(path: Path) -> list[dict[str, str]]:
    # utf-8-sig：MediaCrawler 写的 CSV 带 BOM
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def find_csvs(root: Path, kind: str) -> list[Path]:
    """在目录里找 *_{kind}_*.csv（kind = contents | comments）；也接受直接传单个文件。"""
    if root.is_file():
        return [root] if kind in root.name else []
    return sorted(root.rglob(f"*_{kind}_*.csv"))


def load_mediacrawler(
    source: Path, platform_map: dict[str, Any], tz: tzinfo, platform: str | None = None,
) -> list[dict[str, str]]:
    """读取一个批次目录（或一对文件），返回去重后的统一评论记录。"""
    platform = platform or platform_map["platform"]["name"]
    cm, cmm = platform_map["contents"], platform_map["comments"]
    content_files = find_csvs(source, "contents") if source.is_dir() else find_csvs(source.parent, "contents")
    comment_files = find_csvs(source, "comments")
    LOG.info("读取 %s：内容文件 %d 个，评论文件 %d 个", source, len(content_files), len(comment_files))
    if not comment_files:
        raise FileNotFoundError(f"{source} 下没有找到 *_comments_*.csv")

    # 帖子信息：同一个帖子可能因多次采集重复，取第一条
    posts: dict[str, dict[str, str]] = {}
    for f in content_files:
        for row in read_csv(f):
            pid = row.get(cm["post_id"], "")
            if pid and pid not in posts:
                posts[pid] = {
                    "post_url": clean_url(row.get(cm["post_url"], "")),
                    "post_title": row.get(cm.get("title", ""), "") or "",
                    "search_keyword": row.get(cm.get("keyword", ""), "") or "",
                }
    LOG.info("帖子 %d 条", len(posts))

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    dup = orphan = 0
    for f in comment_files:
        for row in read_csv(f):
            cid = row.get(cmm["comment_id"], "")
            if not cid or cid in seen:
                dup += 1
                continue
            seen.add(cid)
            pid = row.get(cmm["post_id"], "")
            post = posts.get(pid)
            if post is None:
                orphan += 1  # 评论找不到帖子：保留评论，帖子信息留空，由人工判断
                post = {"post_url": "", "post_title": "", "search_keyword": ""}
            out.append({
                "platform": platform,
                "comment_id": cid,
                "post_id": pid,
                **post,
                "text": row.get(cmm["text"], "") or "",
                "nickname": row.get(cmm.get("nickname", ""), "") or "",
                "user_hash": row.get(cmm.get("user_hash", ""), "") or "",
                "created_at": when(row.get(cmm.get("created_at", ""), ""), tz),
                "like_count": row.get(cmm.get("like_count", ""), "") or "",
                "parent_comment_id": row.get(cmm.get("parent_comment_id", ""), "") or "",
            })
    if dup:
        LOG.info("跳过重复/无 ID 评论 %d 条", dup)
    if orphan:
        LOG.warning("%d 条评论找不到对应帖子（帖子 URL/搜索词为空）", orphan)
    LOG.info("标准化完成：%d 条评论", len(out))
    return out


def load_generic_csv(
    path: Path, *, text_col: str, id_col: str = "", keyword_col: str = "", url_col: str = "",
    nickname_col: str = "", platform: str = "csv",
) -> list[dict[str, str]]:
    """读任意 CSV。只有评论文本列是必须的，其余缺省时用行号或空值。"""
    rows = read_csv(path)
    if rows and text_col not in rows[0]:
        raise KeyError(f"{path.name} 里没有列「{text_col}」。现有列: {', '.join(rows[0].keys())}")
    out = []
    for i, row in enumerate(rows, 1):
        out.append({
            "platform": platform,
            "comment_id": row.get(id_col, "") if id_col else f"row{i}",
            "post_id": "",
            "post_url": clean_url(row.get(url_col, "")) if url_col else "",
            "post_title": "",
            "search_keyword": row.get(keyword_col, "") if keyword_col else "",
            "text": row.get(text_col, "") or "",
            "nickname": row.get(nickname_col, "") if nickname_col else "",
            "user_hash": "", "created_at": "", "like_count": "", "parent_comment_id": "",
        })
    LOG.info("通用 CSV %s：%d 行", path.name, len(out))
    return out
