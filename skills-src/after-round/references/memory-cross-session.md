# 记忆和跨场次总结参考

用于把单场面试总评写入 SQLite，并输出跨场次的共性问题、动态变化和成长判断。

## 触发场景

- 用户要求“记忆”“抽取记忆”“复盘记忆”“更新 SQL / SQLite”。
- 用户要求“跨场次总结”“共性问题”“是否成长”“长期分析”。
- 用户要求生成跨场 LLM 报告。
- 用户询问记忆抽取策略、覆盖策略或 SQLite 数据结构。

## 当前实现

Phase 1 已实现本地 SQLite 记忆化。跨场报告保留 LLM 加工版：

- `scripts/memory_ingest.py`：从 `outputs/面评总结/*-面试总评.md` 抽取场次、问题、薄弱项、准备动作和成长信号，写入 `data/after_round_memory.sqlite`。
- `scripts/memory_llm_report.py`：把 SQLite analysis pack 交给模型做自然语言教练分析。

默认命令：

```bash
python3 scripts/memory_ingest.py --db data/after_round_memory.sqlite --campaign "2026 推荐算法求职" outputs/面评总结
python3 scripts/memory_llm_report.py --db data/after_round_memory.sqlite --output data/exports/memory-reports/latest-llm.md
```

归纳总结、记忆抽取不需要 `OPENAI_API_KEY`。生成跨场 LLM 报告需要 `OPENAI_API_KEY`；没有 key 时不能伪造模型报告。

## 抽取范围

- `questions` 来自单场总评里的 `## 面试问题总览`。
- `weakness_instances` 和 `action_items` 只来自 `## 回答不够好的问题清单`。
- `growth_signals` 来自总结中能反映改善、复发、稳定薄弱的结构化信息。

如果用户修改了总评文档里的“回答不够好的问题清单”，重新执行 `memory_ingest.py` 会按当前 Markdown 更新该场记忆。

## 覆盖策略

同一个 `source_path` 对应同一场面试。重复 ingest 时：

- 复用原来的 `session_id`。
- 新增一条 `ingestion_runs` 记录，保留抽取运行历史。
- 删除该场旧的 `questions`、`action_items`、`weakness_instances`、`growth_signals`。
- 从当前 Markdown 重新插入该场记忆。
- 重新计算受影响的 weakness cluster。

结论：源文件路径不变时是“覆盖同一场记忆”，不是新增一场面试。文件复制到新路径或元数据变成另一场时，才可能新增 session。

## 共性问题判断

- 跨场次“共性问题”必须覆盖至少两场不同面试。
- 同一场里同类问题被追问多次，只能算该场证据，不能误判为跨场次复发。
- 报告中要同时看出现频率、覆盖场次数、最近是否仍出现、是否有改善证据。
- 模拟面试默认不进入真实趋势：`source_type = mock` 且 `include_in_trends = 0`。

## LLM 报告策略

- 不做外部检索扩展。
- 把 SQLite analysis pack 交给 LLM，让它生成更像教练的自然语言分析。
- 输出必须受结构化数据约束，不能新增不存在的面试证据。
- 每个跨场结论都要能回到具体公司、轮次、原问题或扣分点。

## 数据核查常用点

- 查看公司名是否规范：查 `sessions.company` 的 distinct 值。
- 查看轮次是否规范：查 `sessions.round_name` 的 distinct 值。
- 查看记忆抽取是否覆盖：按 `source_path` 找 `source_documents`、`sessions`、最近 `ingestion_runs`。
- 查看某场弱点：按 `session_id` 查询 `weakness_instances`、`action_items`、`questions`。

## 质量原则

- 把“SQLite 里的事实”和“LLM 生成的表达”分开。
- 趋势判断要谨慎：数据少时写“暂时看起来”，不要写成确定结论。
- 用户要求评价报告质量时，重点比较证据密度、可执行性、是否幻觉、是否遗漏关键薄弱点。
