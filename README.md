# after_round

after_round 是一个面向技术面试的本地优先面试教练项目。它把单场面试归纳总结、跨场次记忆分析和基于薄弱点的模拟面试串成闭环，但每个步骤都可以独立运行。

## 项目特点

- **单场复盘标准化**：把录音、转录文本或零散回忆整理成三份产物：完整对话、问答整理、面试总评。
- **结构化长期记忆**：从单场总评中抽取问题、薄弱项、准备动作和成长信号，写入本地 SQLite。
- **LLM 教练报告**：把 SQLite 聚合出的事实交给大模型生成自然语言跨场分析，结论仍以结构化记忆为事实源。
- **历史证据驱动模拟面试**：从 SQLite 选择历史薄弱点并绑定原始证据，再由 LLM 改写成面试官问题、追问和 rubric。
- **真实面试和模拟面试分离**：模拟结果可写回记忆库，但默认不进入真实面试趋势统计。
- **Local-first**：Markdown、SQLite 和本地导出是核心数据层；飞书只是归档和展示层，不是核心记忆数据库。
- **可选语音输入**：模拟面试支持命令行文本交互，也支持“每题录音后转写”。
- **skill和脚本配合**：单场归纳仍主要由 Codex `after-round` skill 驱动，项目脚本负责转录、归档、记忆、报告和 mock 链路。


## 目录结构

```text
after_round/
├── outputs/
│   ├── 完整对话/
│   ├── 问答整理/
│   ├── 面评总结/
│   ├── 飞书归档状态/
│   └── 飞书归档结果/
├── config/
│   └── feishu_config.json
├── data/
│   ├── after_round_memory.sqlite
│   └── exports/
│       ├── memory-reports/
│       └── mock-reports/
├── scripts/
│   ├── archive_after_round_to_feishu.py
│   ├── memory_ingest.py
│   ├── memory_llm_report.py
│   ├── mock_plan.py
│   ├── mock_interview.py
│   ├── mock_evaluate.py
│   └── transcribe_after_round.py
├── skills-src/after-round/
└── docs/
```

## 前置条件

### 基础环境

- Python 3.10+。
- macOS / Linux shell 环境。
- `curl`：飞书归档脚本会调用。
- `ffmpeg`：只有音视频转录或模拟面试语音输入时需要。

项目里大部分记忆和报告脚本只依赖 Python 标准库。音视频转录依赖需要单独安装。

### OpenAI API Key

归纳总结不需要 OpenAI API Key。单场面试归纳、飞书归档、记忆抽取都可以在没有 `OPENAI_API_KEY` 的情况下完成。

以下功能需要 `OPENAI_API_KEY`：

- 生成跨场 LLM 报告。
- 生成 mock interview plan 时的面试官问题改写。
- mock interview 结束后的 LLM 评估。

在项目根目录创建 `.env`：

```bash
OPENAI_API_KEY=你的 OpenAI API Key
AFTER_ROUND_OPENAI_MODEL=gpt-4.1
```

`.env` 已在 `.gitignore` 中，不应提交。

### 飞书凭证

只有归档到飞书文档或多维表格时需要。纯本地 Markdown、SQLite 记忆和 LLM 报告不需要飞书。

飞书配置文件默认放在：

```text
~/.codex/feishu_config.json
```

仓库里的 `config/feishu_config.json` 是公开模板，只包含占位值和字段备注。使用飞书归档前，把它复制到 `~/.codex/feishu_config.json` 后再填写自己的真实凭证，不要把真实凭证提交到仓库。

推荐结构：

```json
{
  "app_id": "你的 App ID",
  "app_secret": "你的 App Secret",
  "interview_bitable_app_token": "",
  "interview_bitable_table_id": "",
  "after_round_folder_token": "",
  "after_round_full_dialogue_folder_token": "",
  "after_round_qa_folder_token": "",
  "after_round_summary_folder_token": ""
}
```

飞书自建应用至少需要权限：

- `docx:document`
- `bitable:app`
- `drive:drive`

OAuth 回调地址配置为：

```text
http://localhost:9998/callback
```

归档脚本支持两种 token 模式：

- `--token-mode user`：默认模式。用浏览器 OAuth 获取个人授权，写入内容归属于授权用户，适合 public 项目的普通使用者。
- `--token-mode tenant`：跳过 OAuth，直接使用应用的 tenant token，适合你自己的自动化归档链路；但使用者必须先创建并配置飞书自建应用。

如果当前环境无法自动打开浏览器，可以让脚本只打印授权链接：

```bash
python3 scripts/archive_after_round_to_feishu.py \
  --token-mode user \
  --no-browser \
  ...
```

如果浏览器授权后 localhost 回调失败，复制浏览器地址栏里的完整回调 URL 重跑：

```bash
python3 scripts/archive_after_round_to_feishu.py \
  --token-mode user \
  --oauth-callback-url "http://localhost:9998/callback?code=..." \
  ...
```

也可以只复制 `code` 参数：

```bash
python3 scripts/archive_after_round_to_feishu.py \
  --token-mode user \
  --oauth-code "..." \
  ...
```

### 音视频转录依赖

只有处理 `.m4a`、`.mp3`、`.wav`、`.mp4` 或 mock 语音输入时需要。

```bash
python3 -m venv ~/.codex/venvs/after-round
. ~/.codex/venvs/after-round/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install faster-whisper
```

并安装 `ffmpeg`：

```bash
brew install ffmpeg
```

## 各步骤独立性总览

| 步骤 | 是否可独立运行 | 输入 | 输出 | 必需前置条件 | 可选前置条件 |
| --- | --- | --- | --- | --- | --- |
| 1. 单场面试归纳 | 可以 | 音频、视频、转录文本、PDF、Markdown 或回忆 | `outputs/完整对话`、`outputs/问答整理`、`outputs/面评总结` | 面试材料 | 音视频转录依赖、飞书凭证 |
| 2. 飞书归档 | 可以 | 已有三份 Markdown 或已有飞书文档 ID | 飞书文档、多维表格记录、本地归档状态 | 飞书凭证 | 已配置文件夹 token |
| 3. 记忆抽取 | 可以 | `outputs/面评总结` 中的总评 Markdown | `data/after_round_memory.sqlite` | 面试总评 Markdown | 无 |
| 4. 生成跨场 LLM 报告 | 可以 | SQLite 记忆库 | Markdown 报告 | SQLite 记忆库、OpenAI API Key | 可包含 mock 记录 |
| 5. 生成模拟面试计划 | 可以 | SQLite 记忆库 | mock plan Markdown/JSON | SQLite 记忆库、OpenAI API Key | 指定公司、岗位、轮次、强度 |
| 6. 执行模拟面试 | 可以 | mock plan JSON | transcript JSON/Markdown | mock plan JSON | 语音输入需要 ffmpeg 和转录依赖 |
| 7. 评估模拟面试并写回 | 可以 | mock plan JSON、transcript JSON | mock review Markdown、SQLite mock session | OpenAI API Key、SQLite 记忆库 | 无 |

## 使用流程

### 1. 单场面试归纳

单场归纳通常通过 Codex 的 `after-round` skill 完成。输入可以是音频、视频、转录文本、PDF、聊天记录或零散回忆。

标准产物是：

```text
outputs/完整对话/{公司}-{岗位}-{轮次}-完整对话.md
outputs/问答整理/{公司}-{岗位}-{轮次}-问答整理.md
outputs/面评总结/{公司}-{岗位}-{轮次}-面试总评.md
```

这一阶段不要求 OpenAI API Key 或飞书凭证。只有两种情况需要额外配置：

- 输入是音视频：需要 `ffmpeg` 和转录依赖。
- 要同步到飞书：需要飞书凭证。

### 2. 飞书归档

如果已经有三份 Markdown，可以单独执行飞书归档：

```bash
python3 scripts/archive_after_round_to_feishu.py \
  --company "B公司" \
  --position "推荐算法工程师" \
  --round-name "一面" \
  --date "2026-06-01" \
  --full outputs/完整对话/B公司-推荐算法工程师-一面-完整对话.md \
  --qa outputs/问答整理/B公司-推荐算法工程师-一面-问答整理.md \
  --summary outputs/面评总结/B公司-推荐算法工程师-一面-面试总评.md
```

前置条件：

- `~/.codex/feishu_config.json` 中配置了 `app_id` 和 `app_secret`。
- 飞书应用有 `docx:document`、`bitable:app`、`drive:drive` 权限。
- 默认 `--token-mode user` 会走 OAuth 授权；无法自动打开浏览器时加 `--no-browser`，回调失败时用 `--oauth-callback-url` 或 `--oauth-code` 重跑。
- 如需跳过 OAuth，可加 `--token-mode tenant` 使用 tenant token。

### 3. 记忆抽取

从已有面试总评中抽取结构化记忆：

```bash
python3 scripts/memory_ingest.py \
  --db data/after_round_memory.sqlite \
  --campaign "2026 推荐算法求职" \
  outputs/面评总结
```

这一阶段不需要 OpenAI API Key，不需要飞书凭证。

抽取范围：

- `questions` 来自 `## 面试问题总览`。
- `weakness_instances` 和 `action_items` 只来自 `## 回答不够好的问题清单`。

重复执行时，同一个源 Markdown 路径会覆盖同一场面试的旧记忆，并新增一条 ingestion run 记录；不会把同一场重复插成多场。

### 4. 生成跨场 LLM 报告

把 SQLite 聚合结果交给 LLM，可以分析在这个过程中共性的错误，以及捕捉成长的动态变化：

```bash
python3 scripts/memory_llm_report.py \
  --db data/after_round_memory.sqlite \
  --output data/exports/memory-reports/latest-llm.md
```

前置条件：

- 已有 SQLite 记忆库。
- `.env` 或环境变量中有 `OPENAI_API_KEY`。

### 5. 生成模拟面试计划

基于历史薄弱点生成模拟面试题：

```bash
python3 scripts/mock_plan.py \
  --db data/after_round_memory.sqlite \
  --company "模拟公司" \
  --position "推荐算法工程师" \
  --round "模拟二面" \
  --intensity pressure \
  --question-count 5 \
  --output data/exports/mock-reports/latest-plan.md \
  --json-output data/exports/mock-reports/latest-plan.json
```

前置条件：

- 已有 SQLite 记忆库。
- `.env` 或环境变量中有 `OPENAI_API_KEY`。

当前策略：

```text
选择历史薄弱点
→ 直接绑定历史主证据
→ LLM 改写为面试官问题和追问
```

### 6. 执行模拟面试

文本模式：

```bash
python3 scripts/mock_interview.py \
  data/exports/mock-reports/latest-plan.json \
  --transcript-json data/exports/mock-reports/latest-transcript.json \
  --transcript-md data/exports/mock-reports/latest-transcript.md
```

文本模式只需要 mock plan JSON，不需要 OpenAI API Key。

录音后转写模式：

```bash
python3 scripts/mock_interview.py \
  data/exports/mock-reports/latest-plan.json \
  --voice-input \
  --transcript-json data/exports/mock-reports/latest-transcript.json \
  --transcript-md data/exports/mock-reports/latest-transcript.md
```

语音模式前置条件：

- `ffmpeg` 可用。
- 已安装转录依赖。
- 当前终端有麦克风权限。

### 7. 评估模拟面试并写回记忆

```bash
python3 scripts/mock_evaluate.py \
  --db data/after_round_memory.sqlite \
  --plan-json data/exports/mock-reports/latest-plan.json \
  --transcript-json data/exports/mock-reports/latest-transcript.json \
  --output data/exports/mock-reports/latest-review.md
```

前置条件：

- 已有 mock plan JSON。
- 已有 mock transcript JSON。
- 已有 SQLite 记忆库。
- `.env` 或环境变量中有 `OPENAI_API_KEY`。

写回规则：

- `source_type = mock`
- `include_in_trends = 0`
- `result_signal = mock_only`

因此模拟面试默认不会污染真实面试趋势。需要把模拟面试纳入报告时，报告命令显式加 `--include-mock`。

## 常用命令组合

### 本地记忆抽取

```bash
python3 scripts/memory_ingest.py --db data/after_round_memory.sqlite --campaign "2026 推荐算法求职" outputs/面评总结
```

不需要 Feishu，不需要 OpenAI。

### 生成跨场 LLM 报告

```bash
python3 scripts/memory_llm_report.py --db data/after_round_memory.sqlite --output data/exports/memory-reports/latest-llm.md
```

需要 OpenAI API Key。

### 完整 mock interview MVP

```bash
python3 scripts/mock_plan.py --db data/after_round_memory.sqlite --company "模拟公司" --position "推荐算法工程师" --round "模拟二面" --intensity pressure --question-count 5 --output data/exports/mock-reports/latest-plan.md --json-output data/exports/mock-reports/latest-plan.json
python3 scripts/mock_interview.py data/exports/mock-reports/latest-plan.json --transcript-json data/exports/mock-reports/latest-transcript.json --transcript-md data/exports/mock-reports/latest-transcript.md
python3 scripts/mock_evaluate.py --db data/after_round_memory.sqlite --plan-json data/exports/mock-reports/latest-plan.json --transcript-json data/exports/mock-reports/latest-transcript.json --output data/exports/mock-reports/latest-review.md
```

## 数据安全

- 不要提交 `.env`。
- 不要提交 `~/.codex/feishu_config.json` 或 token 文件。
- 面试录音、完整对话、问答整理和总评通常包含个人隐私，提交前需要确认是否脱敏。
- SQLite 记忆库里包含面试问题、公司、轮次和薄弱点，也应按敏感数据处理。

## 开发验证

运行测试：

```bash
python3 -Wd -m unittest discover -s tests
```

检查脚本语法：

```bash
python3 -m py_compile \
  scripts/memory_store.py \
  scripts/memory_ingest.py \
  scripts/memory_llm_report.py \
  scripts/mock_plan.py \
  scripts/mock_interview.py \
  scripts/mock_evaluate.py \
  scripts/archive_after_round_to_feishu.py
```

## 当前边界

- 单场归纳仍主要由 Codex `after-round` skill 驱动，项目脚本负责转录、归档、记忆、报告和 mock 链路。
- mock interview 的语音模式是“录音后转写”。
- 飞书是归档和展示层；核心数据以本地 Markdown 和 SQLite 为准。
