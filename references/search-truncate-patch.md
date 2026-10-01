# 搜索页截断补丁说明

## 根因

`media_platform/xhs/core.py` 的 `search()` 在开头把 `CRAWLER_MAX_NOTES_COUNT` **强制抬到页大小 20**：

```python
xhs_limit_count = 20
if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count:
    config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count
```

因此配置写成 `5` 也会被改成 `20`，整页搜索结果全部进入 `get_note_detail_async_task`，日志里会出现约 20 次 `Begin get note detail`。创作者路径（`client.get_all_notes_by_creator`）本来就有 `remaining` 截断，搜索路径缺失同等保护。

## 改动

| 文件 | 逻辑 |
|------|------|
| `media_platform/xhs/help.py` | 新增纯函数 `truncate_search_note_items`：过滤 `rec_query`/`hot_query` 后按剩余配额切片（注释标明「防封号：搜索页截断」） |
| `media_platform/xhs/core.py` `search()` | ① 不再把配置强制抬到 20；② 用局部 `max_notes_count` + 每词 `crawled_note_count`；③ 进入详情/评论前调用截断；④ while 条件改为 `crawled_note_count < max_notes_count`（每词不超过上限，与 creator 路径一致） |

未改架构，未加代理/绕过，未动评论上限与其它平台。

## 日志验收（真实采数前请自行批准）

配置 `CRAWLER_MAX_NOTES_COUNT = 5` 后跑搜索，在日志中统计：

```bash
# 建议验收命令（请人工批准后再跑；本补丁流程未执行真实采数）
# cd 到 MediaCrawler 根目录后：
# uv run main.py
# 然后：
grep -c "Begin get note detail" logs/*.log   # 或你的实际日志路径
# 每个关键词对应的 Begin get note detail 次数应 ≤ 5
# 同时应能看到 Truncate search page to N notes (crawled=..., max=5, ...)
```

期望：每个关键词的 `Begin get note detail` **≤ `CRAWLER_MAX_NOTES_COUNT`**；不应再出现「配置 5 却拉 20 帖详情」。

## 静态/单测（不联网）

```bash
uv run pytest tests/test_xhs_search_truncate.py -q
```


## 抖音（2026-09-30）

`media_platform/douyin/core.py` 的 `search()` 原先在开头把 `CRAWLER_MAX_NOTES_COUNT` **强制抬到页大小 10**（`dy_limit_count`）。配置为 5 时会被改成 10，整页结果全部入库并拉评论。

本次与小红书同一方式处理，**仅改抖音搜索路径**：

| 文件 | 逻辑 |
|------|------|
| `media_platform/douyin/help.py` | 新增纯函数 `truncate_search_aweme_items`：抽出 `aweme_info`（或合集首条）后按剩余配额切片 |
| `media_platform/douyin/core.py` `search()` | ① 不再把配置强制抬到 10；② 用局部 `max_notes_count` + 每词 `crawled_aweme_count`；③ 进入存储/评论前调用截断；④ while 条件改为 `crawled_aweme_count < max_notes_count` |

页大小 10 仍是接口 offset 步长，不是条数下限。未改小红书代码，未开子评论、代理、滑块或私信。

**尚未做直播验证。** 静态验收：

```bash
uv run pytest tests/test_douyin_search_truncate.py -q
```

期望：配置 `CRAWLER_MAX_NOTES_COUNT = 5` 时，每个关键词进入存储/评论的作品数 ≤ 5。真实跑数后的日志验收不在本说明中宣称已完成。
