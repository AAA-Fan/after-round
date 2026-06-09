# 模拟面试参考

用于基于历史面试记忆生成模拟面试题、运行命令行文本/录音后转写模拟面试，并把模拟结果评估写回记忆库。

## 触发场景

- 用户说“模拟面试”“mock interview”“针对薄弱点出题”。
- 用户要求用历史面试暴露的问题做追问。
- 用户要求命令行文本交互、录音后转写或模拟结果评估。
- 用户询问 mock 题目生成链路、是否使用 LLM、如何写回记忆。

## 当前 MVP 链路

题目生成采用：

```text
规则选题
→ SQLite 直接绑定历史主证据
→ LLM 改写为面试官问题和追问
```

含义：

- 规则从 `weakness_clusters` / `weakness_instances` 中选择历史薄弱点。
- 证据直接来自 SQLite，因为选题时已经知道“为什么选这道题”。
- LLM 只负责把历史证据改写成自然的面试官问题、追问和评分 rubric。
- 当前 mock plan 不做外部检索扩展。

## 默认命令

生成计划：

```bash
python3 scripts/mock_plan.py --db data/after_round_memory.sqlite --company "模拟公司" --position "推荐算法工程师" --round "模拟二面" --intensity pressure --question-count 5 --output data/exports/mock-reports/latest-plan.md --json-output data/exports/mock-reports/latest-plan.json
```

运行文本模拟：

```bash
python3 scripts/mock_interview.py data/exports/mock-reports/latest-plan.json --transcript-json data/exports/mock-reports/latest-transcript.json --transcript-md data/exports/mock-reports/latest-transcript.md
```

运行录音后转写模拟：

```bash
python3 scripts/mock_interview.py data/exports/mock-reports/latest-plan.json --voice-input --transcript-json data/exports/mock-reports/latest-transcript.json --transcript-md data/exports/mock-reports/latest-transcript.md
```

评估并写回记忆库：

```bash
python3 scripts/mock_evaluate.py --db data/after_round_memory.sqlite --plan-json data/exports/mock-reports/latest-plan.json --transcript-json data/exports/mock-reports/latest-transcript.json --output data/exports/mock-reports/latest-review.md
```

## 语音模式约束

- 当前做“每题录音后转写”。
- `--voice-input` 会调用 ffmpeg 录音，再复用 `scripts/transcribe_after_round.py` 转写。
- 录音、麦克风权限和长时间交互要在启动前提醒用户；不要在用户没准备好时启动交互会话。

## 写回记忆库

模拟面试写回时必须：

- `sessions.source_type = mock`
- `sessions.include_in_trends = 0`
- `result_signal = mock_only`
- 保存 mock plan、transcript、question sources 和 review。

默认不把模拟面试纳入真实面试趋势，除非用户明确要求包含。报告里需要区分“真实面试暴露的问题”和“模拟中复发/改善的问题”。

## 题目选择原则

- 优先选择跨场复发的 weakness cluster。
- 优先覆盖不同薄弱点，而不是把 5 道题都压在同一个 cluster。
- 题目必须绑定历史证据：公司、轮次、原问题、扣分点或更好回答方向。
- 追问应该沿着历史薄弱点加压，而不是泛泛考知识点。
- 如果记忆库数据不足，先提醒用户需要更多真实面试数据，再生成低置信度 mock。

## LLM 使用边界

LLM 用在两个位置：

- `scripts/mock_plan.py`：把规则选出的薄弱点和证据改写成面试官问题、追问、rubric。
- `scripts/mock_evaluate.py`：根据 plan 和 transcript 做回答评估，并把复发/改善写回 SQLite。

LLM 不能新增不存在的历史证据。没有 `OPENAI_API_KEY` 时，不要伪造 mock plan 或评估报告；提示用户设置 key 后重跑。

## 质量原则

- 模拟题要像真实面试官追问，而不是复习提纲。
- 每题都要能解释“为什么问这题”：对应哪个历史薄弱点、来自哪几场面试。
- 评估要指出是否修复了历史问题，而不仅是本题答得好不好。
- 输出要保留可复盘 transcript，避免只有最终评分。
