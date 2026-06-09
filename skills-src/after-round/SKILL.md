---
name: after-round
description: Use when handling after_round interview debrief work: single interview transcription/review/archive, cross-interview memory analysis, recurring weakness/growth reports, or mock interviews based on past interview weaknesses.
---

# after_round

after_round 是面试教练项目的总入口。先判断用户意图，只读取对应 reference；不要一次性加载所有细节。

## 路由

按用户当前请求选择一个或多个模块：

| 用户意图 | 触发词或信号 | 读取 |
| --- | --- | --- |
| 单场归纳 | 面试复盘、整理这场面试、转录、完整对话、问答整理、面试总评、本地 Markdown、飞书归档、多维表格 | [references/single-round-summary.md](references/single-round-summary.md) |
| 记忆和跨场总结 | 记忆、跨场次、共性问题、是否成长、长期分析、LLM 报告、SQLite memory | [references/memory-cross-session.md](references/memory-cross-session.md) |
| 模拟面试 | 模拟面试、mock interview、针对薄弱点出题、录音后转写、模拟结果写回记忆库 | [references/mock-interview.md](references/mock-interview.md) |
| 转录、飞书和归档实现细节 | 音视频转录、ffmpeg、mlx-whisper、faster-whisper、飞书 OAuth、飞书文档块、多维表格 API、追加复盘追问 | [references/transcription-feishu-archive.md](references/transcription-feishu-archive.md) |

如果请求跨模块，按依赖顺序读取：单场归纳 -> 记忆和跨场总结 -> 模拟面试。例：用户说“整理这场面试并更新记忆”，先读单场归纳，再读记忆。

## 共享原则

- 保持证据链：所有判断都要能追溯到原始转录、单场总评、SQLite 记录或模拟面试 transcript。
- 不编造：材料缺失、转录不清、简历缺失、API 失败时明确说明限制。
- 不丢数据：飞书、LLM 或网络失败时，优先保留本地 Markdown / SQLite 的可恢复结果。
- 不混淆真实和模拟：真实面试进入趋势分析；模拟面试写回时默认 `source_type = mock` 且 `include_in_trends = 0`。
- 变更用户已有文件前先理解当前状态；不要覆盖、删除或回滚用户未明确要求改动的内容。

## 常用输出位置

- 完整对话：`outputs/完整对话/{公司}-{岗位}-{轮次}-完整对话.md`
- 问答整理：`outputs/问答整理/{公司}-{岗位}-{轮次}-问答整理.md`
- 面试总评：`outputs/面评总结/{公司}-{岗位}-{轮次}-面试总评.md`
- 飞书归档状态：`outputs/飞书归档状态/`
- 飞书归档结果：`outputs/飞书归档结果/`
- 记忆库：`data/after_round_memory.sqlite`
- 跨场报告：`data/exports/memory-reports/`
- 模拟面试产物：`data/exports/mock-reports/`

## 执行习惯

- 读取项目文件时优先使用 `rg` / `rg --files`。
- 转录和飞书配置细节不要凭记忆执行，读取 `transcription-feishu-archive.md`。
- 需要 OpenAI 的跨场 LLM 报告或模拟题改写时，先确认 `.env` 或环境变量里有 `OPENAI_API_KEY`；没有 key 不伪造模型输出。
- 命令执行失败时，先保留已有中间产物，再说明失败点和可重跑命令。
