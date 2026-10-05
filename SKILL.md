---
name: lead-intake
description: >-
  Find potential customers from public social-media comments (Xiaohongshu, Douyin and others):
  expand search keywords, run a rate-limited public-comment crawl, score each comment's purchase
  intent, and produce a de-identified human-review pool. Use when the user wants lead generation
  from public comments, parent/customer demand mining, 获客, 线索, 评论意向打分, 搜词扩写, 采集小红书/抖音评论,
  or asks to adapt this to a new industry or city. Never sends comments or DMs.
---

# lead-intake（公开评论获客意向筛选）

只做**判断**：不评论、不关注、不私信、不绕风控。产出是一份给人看的线索池，触达由人决定。

## 开始前（必读）

1. 所有命令在本 skill 目录下运行：`python scripts/leadctl.py <命令>`（Python ≥ 3.11，零第三方依赖）。
2. 先跑 `python scripts/leadctl.py doctor`，把 ✘ 的项解决掉再往下。
3. 数据不进 skill 目录：默认写到 `~/.leadkit/`（`--workdir` 或 `$LEADKIT_HOME` 可改）。
4. 结果在 stdout，日志在 stderr；加 `--json` 得到可解析的 JSON。退出码：`0` 成功，`1` 失败，`2` 护栏拒绝/参数错误。

## 硬规则（违反任何一条都要停下问用户）

- **`collect` 会真的访问平台，必须先得到用户明确同意再加 `--yes`。** 不加 `--yes` 只做预检，随便跑。
- **绝不**使用 `--allow-exceed-limits`、`--allow-unverified-platform`、`setup --with-raw-identity`，除非用户明确要求并说明已获书面批准。
- 出现二维码：告诉用户用 App 扫码，你不要操作。出现滑块/验证码：**立即停止**，不要尝试任何自动处理，告诉用户。
- `collect` 被拒绝时，把 `✘` 的原因原样转告用户，不要通过改 profile、改参数、改代码去绕过。
- 含昵称的文件（`internal/` 下）不要贴给用户以外的人、不要写进仓库；`exports/` 下的是脱敏版，才可外传。
- 一次采集的关键词 ≤ 10。你可以用 `keywords --add` 补充联想出的词并 `--enqueue` 入队，但**采哪几个词要让用户看到并同意**：先跑 `collect --next`（预检）把本次取到的词、节奏和预计时长告诉用户，得到同意后才加 `--yes`。不要预检和执行连着一口气跑完。

## 标准流程

```
setup(一次) → keywords --enqueue → collect --next(预检→用户同意→--yes) → [隔天重复，直到队列采完] → ingest → 人工池 → (用户)人工抽检/触达
```

| 步骤 | 命令 | 说明 |
|---|---|---|
| 1 安装 | `setup` | 克隆上游 MediaCrawler 并打防封号补丁，装依赖。只需一次。已有 MediaCrawler 目录用 `--mc-dir` |
| 2 扩词 | `keywords --category 托管班 --place 台州椒江 --add "你联想的词,词" --enqueue` | 模板出 10~18 条，`--add` 补你想到的长尾词（排前面），`--enqueue` 存入待采队列；附近区备选词不入队 |
| 3 预检 | `collect --platform xhs --next` | dry-run：显示本次取哪几个词（按今日剩余配额，≤10）、节奏、预计时长；被拒绝则按原因处理。**把结果告诉用户** |
| 4 采集 | 同上加 `--yes --ingest` | 用户同意后执行；需要用户在场扫码；成功的词自动标为已采，失败/被风控打断的词留在队列。队列还有词就隔天（或 ≥30 分钟后）回到第 3 步 |
| 队列 | `queue list` / `queue add --words "a,b"` / `queue skip` / `queue retry` | 查看进度、手动加词、跳过、恢复「卡住」的词（连续失败 3 次不再被取用） |
| 5 入库 | `ingest --input latest` | 已采好的批次（或 CSV 路径）→ 打分 → 入库 → 导出 |
| 6 查看 | `pool stats` / 读 **`internal/<profile>_pool.xlsx`**（中文表头；高相关在前并标红；含用户名所以在 `internal/`，不在 `exports/`）。`exports/` 下是不含用户名的中文 CSV | 汇报：ready / 待复核数量、问题分布 |

没有用 MediaCrawler、只有一份 CSV 时：`score --input a.csv --text-col 评论列名`（不入库）或 `ingest --input a.csv --text-col 评论列名`（入库）。

## 理解输出

- `status`：`ready`（高意向，**仍需人工抽检才能触达**）/ `needs_review`（进人工池）/ `excluded`（低分归档或广告）。
- `problem`：求推荐 / 问价 / 纠结 / 报名 / 适龄咨询 / 投诉避雷 … ；`strength`：高 / 中 / 低；`parent_likely`：是否像目标客户本人。
- 特殊需求词（profile 的 `special`）命中后分数封顶，只会进复核，永远不会是 ready。
- 汇报时给数量和代表性原文，不要自己改分或改状态；要改判断规则就改 profile（见下）。

## 改规则 / 换行业 / 换城市

- 规则都在 `profiles/*.toml`（词表、阈值、地名、问题归类、自检用例）。代码里没有任何行业词。
- 新行业：复制 `profiles/_template.toml` → 改词表和 `[geo]` → `leadctl check --profile 路径` 通过 → 之后用 `--profile 名字`。
- 改完词表必须跑 `leadctl check`，且把人工确认过的句子补进 `[[cases]]`；自检失败时 `ingest`/`pool rescore` 会拒绝执行。
- 改规则后对旧数据生效：`pool rescore`（已人工标记触达的线索不会被改分）。

## 护栏（代码强制，不是建议）

每次采集前自动检查，任一不满足就拒绝：关键词 ≤10、每词 ≤5 篇、单帖评论 ≤10（默认按当日剩余配额自动算）、并发 1、间隔 ≥3s、禁二级评论/代理/媒体、只许扫码登录、上游补丁已生效、日会话 ≤2 且间隔 ≥30 分钟、日笔记 ≤50、日评论 ≤500。上限在 2026-10-02 按使用者指示放宽过一次（原为 3/5/5/30/50），出现风控信号时应建议用户退回旧档位。
采集节奏默认是随机间隔（中位约 7.5s、偶尔 20~60s 长停顿，一次会话约 10 分钟），只会比 3s 下限更慢，不增加请求数；不要为了「快」去关掉它。
采集中盯日志：实际拉取超限或出现风控信号会立刻终止进程并写台账（冷却 2 小时，当天第 2 次封存）。若出现「账号疑似存在风险」类提示，整号停用观察 ≥24 小时（把 profile 的 `block_cooldown_hours` 调到 24 以上）。红线里各数字的依据等级、平台协议与个人信息的合规边界（含「抖音来源线索触达前须先确认书面许可」）详见 `references/safety-redlines.md`。

## 更多资料（按需读取，不要一次全读）

- 完整使用说明和排错：`README.md`
- 打分规则文字版：`references/intent-rules.md`；触达话术（人工）：`references/outreach.md`
- 红线原文与代码对照：`references/safety-redlines.md`；补丁原理：`references/search-truncate-patch.md`
- 接新平台（`platforms/*.toml`）、新采集后端（`scripts/leadkit/collectors/`）：见 README「开发者」一节
- 测试：`python -m unittest discover -s tests`

## 许可注意

上游 MediaCrawler 采用非商业学习许可（NCAL 1.1），本 skill 不分发它，由使用者自行安装并对合规负责。用于商业获客前请先确认授权和平台条款，并告知用户。
