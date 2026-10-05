# lead-intake · 公开评论获客意向筛选

从小红书、抖音等平台的**公开评论**里，找出"可能是目标客户"的人，并告诉你：他是不是目标客户、在问什么、意向强不强。

```
扩词 → 受限采集 → 打分 → 人工池 → （你）人工抽检、触达
```

**它只做判断。** 不会评论、不会关注、不会私信、不会绕过任何风控。触达永远由人决定。

可以当命令行工具直接用，也可以装成 skill 让 Cursor / Codex / Claude Code 等 agent 替你操作。

---

## 一、5 分钟上手

### 1. 准备环境

| 需要 | 说明 |
|---|---|
| Python ≥ 3.11 | 本工具零第三方依赖 |
| git、[uv](https://docs.astral.sh/uv/) | 安装采集后端用 |
| Chrome 浏览器 | 采集时用它登录并读取页面 |
| Node.js ≥ 16 | **只有采抖音才需要** |

macOS / Linux / Windows 都可以。Windows 请用 PowerShell 或 Git Bash，下面命令里的 `python` 在 macOS 上可能要写成 `python3`。

### 2. 安装采集后端（只需一次）

```bash
cd lead-intake
python scripts/leadctl.py setup
python scripts/leadctl.py doctor      # 全是 ✔ 就绪；有 ✘ 会告诉你怎么修
```

`setup` 会把上游 MediaCrawler 克隆到 `~/.leadkit/vendor/`，打上 3 个防封号补丁，并装好依赖。
已经有自己的 MediaCrawler？用 `setup --mc-dir 你的目录`，只打补丁不克隆。

### 3. 不采集，先试打分（30 秒体验）

```bash
python scripts/leadctl.py score --input examples/sample_comments.csv --text-col 评论 --keyword-col 搜索词
```

得到 `~/.leadkit/exports/sample_comments.scored.csv`，每条评论都有「分级」（高相关 / 待复核 / 低意向归档 / 已排除）、「问题类型」、「强度」。所有导出（Excel 和 CSV）表头与取值都是中文。

### 4. 完整流程：从搜词到线索池

```bash
# ① 品类 + 地区 → 搜索词清单（10~18 条，写入 ~/.leadkit/keywords/）
python scripts/leadctl.py keywords --category 托管班 --place 台州椒江

# ①' 推荐：把词存进待采队列（--add 可加你自己想到的词，排在最前面；附近区的备选词不会入队）
python scripts/leadctl.py keywords --category 托管班 --place 台州椒江 --add "椒江托管班哪家靠谱" --enqueue

# ② 先预检（不会真的访问平台）：自动从队列取下一批（≤10 个词，配额不够就少取），并显示节奏和预计时长
python scripts/leadctl.py collect --platform xhs --next

# ③ 预检通过后，真正采集并自动入库（会弹出浏览器，用手机 App 扫码登录）
python scripts/leadctl.py collect --platform xhs --next --yes --ingest

# ④ 看结果；队列里还有词的话，隔天（或至少 30 分钟后）重复 ②③
python scripts/leadctl.py pool stats
python scripts/leadctl.py queue list
```

单次最多 10 个词、每天最多 2 次会话、每天总量有上限，所以词多的时候要**分批、分天**采完。队列替你记着「采到哪了」：采成功的词标为已采，失败或被风控打断的词留在队列里下次再来。

不想用队列，也可以手动指定（最多 10 个）：`collect --platform xhs --keywords "词1,词2,词3" --yes`。

**采集节奏**：请求间隔不是固定 3 秒，而是随机的（中位约 7.5 秒、最长 15 秒），约每 7 次间隔后还会插入一次 20~60 秒的长停顿，一次会话大约 10 分钟。这只会让节奏更慢，不会增加请求数；预检时会显示预计时长。可在 profile 的 `[collect.pacing]` 调整。

人工池在 `~/.leadkit/exports/<profile>_pool.xlsx`（同目录还有同内容的 `.csv`）。**高相关（`ready`）排在最前、整行标红**，其后是待复核，层内按意向分从高到低。想让待复核也标红，在画像里加：

```toml
[export]
highlight_status = ["ready", "needs_review"]
```

> 第 ③ 步需要人在电脑前扫码并看着。出现滑块或验证码时**直接关掉**，工具会自动终止并冷却，不要尝试手动重试或绕过。

---

## 二、命令速查

所有命令都支持：`--workdir 目录`、`--profile 名字`、`-v` 详细日志、`-q` 安静、`--json` 机器可读输出。

| 命令 | 作用 | 会访问网络/平台吗 |
|---|---|---|
| `doctor` | 环境体检，告诉你缺什么 | 否 |
| `setup` | 安装 MediaCrawler 并打补丁 | 只访问 GitHub |
| `profiles` | 列出可用的行业/地区配置 | 否 |
| `check` | 跑配置里的自检用例（改词表后必跑） | 否 |
| `keywords --category X --place Y [--add "词,词"] [--enqueue]` | 扩写搜索词清单；`--add` 补自己的词，`--enqueue` 存入待采队列 | 否 |
| `queue add\|list\|skip\|retry` | 管理待采队列（`add --words "a,b"` 或 `--from-proposal 清单文件`） | 否 |
| `collect --platform P --keywords "a,b,c"` 或 `--next` | 受限采集；`--next` 从队列取词；**不加 `--yes` 只做预检** | 加 `--yes` 才会 |
| `ingest --input 批次/目录/CSV` | 打分 → 入库 → 导出 | 否 |
| `score --input a.csv --text-col 列名` | 给任意 CSV 打分，不入库 | 否 |
| `pool rescore` | 改了规则后，对库里所有线索重打分 | 否 |
| `pool export [--status ready]` | 重新导出人工池 | 否 |
| `pool stats` | 池子概况 | 否 |
| `pool mark --platform-id xhs --comment-id ID --reach approved` | 人工标记触达状态 | 否 |
| `pool purge [--yes]` | 清理过期数据（默认只预演） | 否 |

**`collect` 常用参数**

| 参数 | 说明 |
|---|---|
| `--platform` | `xhs` 小红书、`dy` 抖音（已实测）；`ks` `bili` `wb`（未实测，需额外放行） |
| `--keywords` | 逗号分隔，最多 10 个 |
| `--next` | 从待采队列取下一批词（与 `--keywords` 二选一）。按今日剩余配额决定取几个，最多 10 个 |
| `--notes` / `--comments` | 每词笔记数 / 单帖评论数。不写则自动取安全值（评论数按当日剩余配额反推） |
| `--yes` | 确认执行。不加就是预检 |
| `--ingest` | 采集成功后自动入库打分 |

**`ingest --input` 可以是**：采集批次目录、批次 ID、`latest`（最近一次）、或任意 CSV。
手上只有一份 CSV（不是 MediaCrawler 采的）：`ingest --input a.csv --text-col 评论列名 [--keyword-col ...]`。
想把以前 MediaCrawler 采的数据导进来：`ingest --input 旧的/data/xhs/csv --platform xhs`。

---

## 三、怎么看结果

| 字段 | 含义 |
|---|---|
| `status = ready` | 高意向（≥70 分）。**仍须人工抽检**通过后才能触达 |
| `status = needs_review` | 中意向（40–69 分），进人工池由你判断 |
| `status = excluded` | 低分归档，或广告/引流/同行互推 |
| `problem` | 他在问什么：求推荐 / 问价 / 纠结 / 报名 / 适龄咨询 / 投诉避雷 … |
| `strength` | 高 / 中 / 低 |
| `parent_likely` | 评论者本人像不像目标客户（1 = 像） |
| `tags` | 命中的词和加分项，用来理解为什么是这个分 |

特殊需求（如感统、多动）命中后分数封顶 65，**永远只进人工复核，不会是 ready**。

### 数据放在哪里

默认 `~/.leadkit/`（`--workdir` 或环境变量 `LEADKIT_HOME` 可改）。**skill 目录里不会出现任何数据。**

```text
~/.leadkit/
├── vendor/MediaCrawler/    上游爬虫（含你的浏览器登录态，别外传）
├── raw/<批次>/             每次采集的原始 CSV、日志、manifest.json（当时的参数和版本）
├── db/<profile>.sqlite     线索库
├── exports/                ✅ 脱敏导出（无昵称），可以给别人
├── internal/               ⛔ 含昵称的对照表，仅限内部
├── state/runs.jsonl        采集台账（日配额、冷却、风控记录）
├── keywords/               扩词清单
└── logs/leadkit.log        全量日志，排错看这里
```

---

## 四、安全护栏（代码强制，不是建议）

这些由程序检查，参数不合规会**直接拒绝执行**，不是只给个警告：

| 项目 | 限制 |
|---|---|
| 每次关键词数 | ≤ 10 |
| 每词笔记数 / 单帖评论数 | ≤ 5 / ≤ 10（评论数默认按当日剩余配额自动取；满额即 10 词 × 5 篇 × 10 条 = 50 篇 / 500 条） |
| 并发 / 请求间隔 | 1 / ≥ 3 秒 |
| 二级评论、代理、媒体下载 | 必须关闭，没有放宽的口子 |
| 登录方式 | 只允许扫码（手机验证码登录会触发上游的"自动过滑块"，属于绕过风控） |
| 每日采集次数 / 间隔 | ≤ 2 次 / 间隔 ≥ 30 分钟 |
| 每日估算用量 | 笔记详情 ≤ 50，评论 ≤ 500 |
| 上游补丁 | 必须已生效，否则拒绝（见下） |

**为什么要补丁**：上游 MediaCrawler 会把"每词笔记数"强行抬到整页大小（小红书 20、抖音 10）。配置写 5，实际会拉 20。补丁修掉这个行为；每次采集前还会检查补丁是否在。

**采集过程中的实时监控**：逐行盯爬虫日志，出现下面任一情况立刻终止进程并写入台账：
- 某个关键词实际拉取的笔记数超过上限
- 出现验证码、滑块、限流、登录失效等信号 → 冷却 2 小时；**当天第 2 次直接封存**

放宽限额需要显式加 `--allow-exceed-limits`，会留痕，且只放宽数量类限制，代理/二级评论/登录方式永远不放行。
完整红线和与代码的对照见 [references/safety-redlines.md](references/safety-redlines.md)。

**代码管不了的，靠你自己遵守**：用隔离的小号而不是主号；扫码时有人值守；ready 线索人工抽检后再触达；原始数据不外传。

---

## 五、换行业、换城市

所有业务规则都在 `profiles/*.toml`，代码里没有任何行业词或地名。

```bash
cp profiles/_template.toml profiles/我的行业_我的城市.toml    # 模板是「家装·苏州」的示例
# 编辑：词表、[geo] 地名、问题归类、限额…（文件里每段都有注释）
python scripts/leadctl.py check --profile profiles/我的行业_我的城市.toml
python scripts/leadctl.py keywords --profile 我的行业_我的城市 --category 装修 --place 苏州姑苏
```

要点：

- **`[[cases]]` 自检用例**：把"人工确认过的句子和结论"写进去。以后每次改词表，`ingest` 和 `pool rescore` 都会先跑一遍，不通过就拒绝执行，防止改一处坏一片。
- 不同 profile 用不同的库文件，数据互不混。
- profile 的限额只能**收紧**，写得比代码硬上限松也会被压回去。
- 改规则后让旧数据生效：`pool rescore`（已人工标记触达的线索不会被改分）。
- 个人 profile 可以放在工作区 `~/.leadkit/profiles/`，不用动 skill 目录，优先级更高。

---

## 六、装成 skill，让别的 agent 用

把整个 `lead-intake/` 目录放到对应位置即可，目录名保持 `lead-intake`：

| Agent | 个人目录 | 项目目录 |
|---|---|---|
| Cursor | `~/.cursor/skills/lead-intake/` | `<项目>/.cursor/skills/lead-intake/` |
| Codex 等 | `~/.agents/skills/lead-intake/` | `<项目>/.agents/skills/lead-intake/` |
| Claude Code | `~/.claude/skills/lead-intake/` | `<项目>/.claude/skills/lead-intake/` |

```bash
# 推荐用软链接，改动只维护一份（Windows 用复制，或管理员 PowerShell 的 New-Item -ItemType SymbolicLink）
ln -s "$(pwd)" ~/.cursor/skills/lead-intake
```

放好后对 agent 说"帮我筛一下椒江托管班的家长需求"之类的话即可。`SKILL.md` 里已写明流程和**必须停下来问你的节点**：采集前要你同意、挑关键词、扫码、遇到验证码。

换到另一台电脑：复制目录，运行 `doctor` → `setup` 即可。如果要沿用旧电脑的数据，复制整个 `~/.leadkit/db/` 和 `exports/`；**不要复制 `vendor/` 里的登录态**，新机器重新扫码。

---

## 七、常见问题

| 现象 | 原因与处理 |
|---|---|
| `拒绝执行：关键词数=11 超过上限 10` | 一次最多 10 个词，其余入队分批采 |
| `拒绝执行：今日评论已用 X，再采 Y 会超过 50` | 当日配额不够。减少 `--comments`，或明天再采 |
| `距上次采集不足 30 分钟` | 冷却中，等一等。别绕过 |
| `搜索截断补丁未生效` | 运行 `setup` 重新打补丁；若报补丁打不上，说明上游版本不匹配，`setup --ref 380b426` |
| `没有搜索截断补丁、也未实测` | 该平台（快手/B站/微博）没验证过。确需采集加 `--allow-unverified-platform`，运行时监控仍然有效 |
| 采集中途被终止，台账里有 `block` | 触发了风控信号。冷却 2 小时后再说，当天第 2 次会封存 |
| `profile 自检失败` | 刚改的词表让某条人工确认过的句子判错了。`leadctl check` 会列出是哪条 |
| `doctor` 报找不到 uv / node | 按提示安装；node 只有抖音才需要 |
| 扫码窗口没出现 | 确认装了 Chrome；非默认路径时在 profile 的 `[collect.overrides]` 里设 `CUSTOM_BROWSER_PATH` |
| Windows 中文乱码 | 用 PowerShell 7 或 `chcp 65001`；工具自身已强制 UTF-8 输出 |
| Windows：uv 已装但提示「不在 PATH」 | 工具会自动在 `~/.local/bin`、`%LOCALAPPDATA%\Programs\uv` 等常见位置找并直接使用，无需处理；找不到时设环境变量 `LEADKIT_UV=uv.exe 的完整路径` |
| Windows：`uv sync` 报 `WinError 183` | uv 构建缓存残留，与包本身无关。按提示运行 `uv cache clean <包名>`，再重跑 `leadctl setup` |
| Windows：提示找不到时区 | `pip install tzdata`（中国大陆各时区缺库时会自动等价回落，其他时区必须装） |
| Windows：中途终止后 Chrome 残留 | 工具会用 `taskkill /T /F` 结束整棵进程树；若仍有残留，到任务管理器结束 chrome 即可 |
| 任何未预期错误 | 看 `~/.leadkit/logs/leadkit.log`，或加 `-v` 重跑 |

---

## 八、开发者

```text
lead-intake/
├── SKILL.md                    给 agent 看的入口（流程、硬规则、何时停下问人）
├── README.md                   本文件
├── scripts/
│   ├── leadctl.py              命令行入口
│   └── leadkit/
│       ├── cli.py              子命令与参数
│       ├── profile.py          读取并校验 profile（TOML）
│       ├── scoring.py          打分引擎（与行业无关）
│       ├── keywords.py         搜索词扩写
│       ├── normalize.py        平台 CSV → 统一评论记录
│       ├── guard.py            护栏：限额、台账、补丁校验、运行时监控
│       ├── pool.py             SQLite 入库 / 重打分 / 导出 / 清理
│       ├── setup.py            安装 / 打补丁 / doctor
│       ├── logger.py           统一日志
│       ├── paths.py            skill 目录 与 工作区 的分离
│       └── collectors/         采集后端（可替换）
├── profiles/                   行业+地区配置  （education_taizhou、_template）
├── platforms/                  平台字段映射    （xhs、dy、ks、bili、wb）
├── patches/mediacrawler/       上游补丁（0001/0002 防封号；optional/ 可选）
├── references/                 规则、红线、触达话术原文
├── examples/                   示例 CSV
└── tests/                      python -m unittest discover -s tests
```

**加一个平台**：复制 `platforms/xhs.toml`，改列名即可（`[contents]` 和 `[comments]` 把上游 CSV 列名映射到统一字段）。想让它能被运行时监控，补上 `[monitor]` 的三条日志正则；想让护栏校验补丁，补上 `[patch]`。

**加一个采集后端**：在 `collectors/` 里继承 `Collector`，实现 `preflight / describe / run`，在 `collectors/__init__.py` 的 `BACKENDS` 登记。参数护栏对所有后端统一生效，后端里不要自己放宽。

**可选补丁 `0003` / `0004`**：让 MediaCrawler 输出明文昵称（上游默认是中间打码的，如 `小*`）。`0003` 管小红书（同时存用户 ID），`0004` 管抖音评论（**只放开昵称**，不存原始 uid）。涉及个人信息，默认不打，需要 `setup --with-raw-identity` 显式开启；最终表格要显示完整用户名就必须开。已采过的旧数据不会回填，需要重新采集。

**可选补丁 `0005` + 分层地域过滤**：平台评论的 IP 属地只到省（台州人显示「浙江」），任何单一信号都分不出台州。`setup --with-ip-province` 打上 `0005` 后，评论 CSV 多一列 `ip_province`（只存省级文字，不存其他个人信息）；profile 的 `[geo_filter]` 把评论正文地名、笔记标题/话题标签、昵称、IP 省级、自述外地叠加成地域状态，再按配置保持 / 封顶复核 / 排除。默认关闭；没有 IP 列也能用（只靠文本信号）。旧线索重新 `ingest` 同一批原始数据即可补上笔记上下文。

**测试**：`python -m unittest discover -s tests`。覆盖打分回归、护栏限额、台账配额、运行时监控（含误报防护）、入库幂等、导出脱敏。

---

## 九、许可与合规

- 上游 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 采用 **NON-COMMERCIAL LEARNING LICENSE 1.1**：禁止商业用途，禁止大规模采集或干扰平台运营。本项目**不分发**它，由你自行安装，合规责任在使用者。
- 用于商业获客前，请先确认上游授权、目标平台的服务条款，以及个人信息保护相关法规。
- 本工具只处理公开内容，并通过硬上限、冷却和运行时监控尽量降低对平台的影响，但这不构成任何合规保证。
