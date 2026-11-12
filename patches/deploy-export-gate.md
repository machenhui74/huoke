# 导出硬门禁部署说明

## 概述

本次更新为 `education_taizhou` profile 添加了导出硬门禁功能。评论必须同时满足以下三项条件才能进入外发交付表（exports/）：

1. **地域本人自述**：评论者在评论正文中明确自述在台州（含所属区县）
   - 仅出现地名不够，必须有「我在/坐标/人在」等自述句式
   - 搜词、帖子标题、作者位置、昵称、浙江 IP 都不能证明评论者在台州
   - 「台州有吗」不能当作本人在台州
   - 台州/泰州混淆会被检测并拒绝

2. **评论时效**：commented_at 在最近 30 天内（Asia/Shanghai 时区）
   - 未来时间、解析失败、未知时间一律不通过

3. **服务需求**：明确主动询问当地课程/机构/报名/试听等服务需求
   - 不推断儿童诊断、健康
   - 不挖联系方式

不满足门禁的线索仍保留在内部库和复核表，只是不进入外发交付表。

## 部署步骤

### 1. 在本地 Mac 上拉取代码

```bash
cd /Users/ama/cangku/Grok\ bot/获客自动化/lead-intake
git fetch origin
git checkout main
git pull origin main
```

### 2. 验证部署

```bash
# 检查 profile 是否包含导出门禁配置
grep -A5 '\[export_gate\]' profiles/education_taizhou.toml

# 运行测试
python -m pytest tests/test_export_gate.py -v
python -m pytest tests/test_export.py -v
```

### 3. 测试导出

```bash
# 导出现有线索，验证门禁过滤
python scripts/leadctl.py pool export --profile education_taizhou

# 查看输出：
# - 内部表 (internal/) 应包含所有线索
# - 外发表 (exports/) 只包含通过门禁的线索
# - 0 条通过门禁时，外发表为空（这是正确行为）
```

## daily-run.sh 无需修改

导出硬门禁在 `pool.py` 的 `export()` 函数中自动应用。`daily-run.sh` 调用的 `leadctl ingest/collect` 命令最终会调用 `pool.export()`，门禁会自动生效。

因此，只需要拉取最新代码，无需修改 `daily-run.sh`。

## 验证清单

部署后请验证以下场景：

- [ ] 评论者明确自述在台州 + 近期 + 有服务需求 → **进入外发表**
- [ ] 仅出现台州地名但无自述 → **不进入外发表**
- [ ] 浙江 IP 但无本人自述 → **不进入外发表**
- [ ] 外省/境外 IP 与台州自述冲突 → **不进入外发表**
- [ ] 含「泰州」但无「台州」→ **不进入外发表**（泰州是江苏的）
- [ ] 2019 年/超过 30 天的旧评论 → **不进入外发表**
- [ ] 评论时间为空/解析失败 → **不进入外发表**
- [ ] 无服务需求询问 → **不进入外发表**
- [ ] 内部复核表（internal/）→ **仍包含所有线索 + 门禁结果列**
- [ ] 0 条通过门禁时 → **外发表为空，不复制未核实表**

## 回滚

如需回滚，将 `profiles/education_taizhou.toml` 中的 `[export_gate].enabled` 改为 `false`：

```toml
[export_gate]
enabled = false  # 临时禁用门禁
```

或者回滚到之前的 commit。
