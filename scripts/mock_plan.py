#!/usr/bin/env python3
"""Generate a rule-selected mock interview plan from after_round memory."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.memory_ingest import stable_id
from scripts.memory_llm_report import DEFAULT_MODEL, STATUS_LABELS, OpenAIConfigError, OpenAIResponsesClient, fetch_clusters
from scripts.memory_store import connect, initialize_database


DEFAULT_DB_PATH = Path("data/after_round_memory.sqlite")
DEFAULT_OUTPUT = Path("data/exports/mock-reports/latest-plan.md")


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.S)
    if fence_match:
        stripped = fence_match.group(1)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("LLM response JSON must be an object")
    return payload


def fetch_cluster_sources(conn, cluster: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          wi.instance_id,
          wi.cluster_id,
          wi.dimension,
          wi.topic,
          wi.severity,
          wi.issue_summary,
          wi.evidence,
          wi.suggested_fix,
          q.question_id,
          q.stage,
          q.original_question,
          q.interviewer_intent,
          q.answer_summary,
          q.better_answer,
          s.company,
          s.position,
          s.round,
          s.session_date
        FROM weakness_instances wi
        JOIN sessions s ON s.session_id = wi.session_id
        LEFT JOIN questions q ON q.question_id = wi.question_id
        WHERE wi.cluster_id = ?
          AND s.include_in_trends = 1
        ORDER BY wi.severity DESC, COALESCE(s.session_date, '') DESC, wi.created_at DESC, wi.instance_id
        LIMIT ?
        """,
        (cluster["cluster_id"], limit),
    ).fetchall()
    sources: list[dict[str, Any]] = []
    occurrence_count = int(cluster["occurrence_count"])
    session_count = int(cluster["session_count"])
    for index, row in enumerate(rows):
        generation_mode = "replay" if index == 0 else "extension"
        source = {
            "cluster_id": row["cluster_id"],
            "source_instance_id": row["instance_id"],
            "source_question_id": row["question_id"],
            "generation_mode": generation_mode,
            "dimension": row["dimension"],
            "topic": row["topic"],
            "stage": row["stage"] or row["dimension"],
            "severity": int(row["severity"]),
            "issue_summary": row["issue_summary"],
            "evidence": row["evidence"],
            "suggested_fix": row["suggested_fix"],
            "original_question": row["original_question"] or row["issue_summary"],
            "interviewer_intent": row["interviewer_intent"],
            "answer_summary": row["answer_summary"],
            "better_answer": row["better_answer"],
            "company": row["company"],
            "position": row["position"],
            "round": row["round"],
            "session_date": row["session_date"],
            "rationale": (
                f"{row['topic']} 在真实面试中出现 {occurrence_count} 次，覆盖 {session_count} 场，"
                f"本条来自 {row['company']} {row['round']}，严重度 {row['severity']}。"
            ),
        }
        sources.append(source)
    return sources


def select_mock_sources(
    db_path: str | Path,
    *,
    question_count: int = 5,
    focus_topics: list[str] | None = None,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    conn = connect(db_path)
    try:
        clusters = fetch_clusters(conn, include_mock=False)
        if focus_topics:
            needles = [item.lower() for item in focus_topics]
            clusters = [
                cluster
                for cluster in clusters
                if any(needle in str(cluster["topic"]).lower() for needle in needles)
            ]
        recurring = [cluster for cluster in clusters if int(cluster["session_count"]) >= 2]
        eligible = recurring or clusters
        source_groups: list[list[dict[str, Any]]] = []
        for cluster in eligible:
            sources = fetch_cluster_sources(conn, cluster, limit=question_count)
            if sources:
                source_groups.append(sources)
        selected: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        for sources in source_groups:
            if len(selected) >= question_count:
                break
            source = sources[0]
            selected.append(source)
            used_ids.add(source["source_instance_id"])
        source_index = 1
        while len(selected) < question_count:
            progressed = False
            for sources in source_groups:
                if len(selected) >= question_count:
                    break
                if len(sources) <= source_index:
                    continue
                source = sources[source_index]
                if source["source_instance_id"] in used_ids:
                    continue
                selected.append(source)
                used_ids.add(source["source_instance_id"])
                progressed = True
            if not progressed:
                break
            source_index += 1
        if not selected:
            raise ValueError("记忆库里还没有可用于模拟面试的薄弱项")
        return selected[:question_count]
    finally:
        conn.close()


def build_question_writer_prompt() -> str:
    return """你是一个严厉但务实的技术面试官。

你会收到由规则从 SQLite 选出的历史薄弱项和主证据。不要新增来源 ID，不要引入外部检索或外部知识。
你的任务只是把每条 selected_sources 改写成自然、真实、有压力但不过度攻击的面试问题。

必须输出 JSON object，格式如下：
{
  "plan_summary": "一句话说明本次模拟策略",
  "questions": [
    {
      "source_instance_id": "必须原样复制 selected_sources 中的 source_instance_id",
      "question": "主问题",
      "follow_ups": ["追问1", "追问2"],
      "rubric": "评分标准"
    }
  ]
}
"""


def build_writer_payload(
    *,
    target_company: str,
    position: str,
    round_name: str,
    intensity: str,
    selected_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "target": {
            "company": target_company,
            "position": position,
            "round": round_name,
        },
        "intensity": intensity,
        "generation_policy": {
            "selector": "rules",
            "evidence_source": "sqlite",
            "external_retrieval_enabled": False,
            "allowed_edits": ["question", "follow_ups", "rubric", "plan_summary"],
        },
        "selected_sources": selected_sources,
    }


def rewrite_questions_with_llm(
    *,
    selected_sources: list[dict[str, Any]],
    target_company: str,
    position: str,
    round_name: str,
    intensity: str,
    client: Any,
    model: str,
) -> dict[str, Any]:
    payload = build_writer_payload(
        target_company=target_company,
        position=position,
        round_name=round_name,
        intensity=intensity,
        selected_sources=selected_sources,
    )
    response = client.generate_markdown(
        system_prompt=build_question_writer_prompt(),
        user_payload=payload,
        model=model,
    )
    parsed = extract_json_object(response)
    questions = parsed.get("questions")
    if not isinstance(questions, list):
        raise ValueError("LLM response JSON must contain questions list")
    by_instance = {
        str(item.get("source_instance_id")): item
        for item in questions
        if isinstance(item, dict) and item.get("source_instance_id")
    }
    return {
        "payload": payload,
        "plan_summary": str(parsed.get("plan_summary") or "基于历史复发薄弱项生成模拟面试。"),
        "questions_by_instance": by_instance,
    }


def build_mock_plan(
    db_path: str | Path,
    *,
    target_company: str,
    position: str,
    round_name: str,
    intensity: str = "normal",
    question_count: int = 5,
    focus_topics: list[str] | None = None,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    if intensity not in {"normal", "strict", "pressure"}:
        raise ValueError("intensity must be one of: normal, strict, pressure")
    selected_sources = select_mock_sources(
        db_path,
        question_count=question_count,
        focus_topics=focus_topics,
    )
    model_client = client or OpenAIResponsesClient()
    llm_result = rewrite_questions_with_llm(
        selected_sources=selected_sources,
        target_company=target_company,
        position=position,
        round_name=round_name,
        intensity=intensity,
        client=model_client,
        model=model,
    )
    plan_id = stable_id(
        "mockplan",
        target_company,
        position,
        round_name,
        intensity,
        "|".join(source["source_instance_id"] for source in selected_sources),
    )
    questions: list[dict[str, Any]] = []
    for index, source in enumerate(selected_sources, start=1):
        rewritten = llm_result["questions_by_instance"].get(source["source_instance_id"], {})
        follow_ups = rewritten.get("follow_ups") or []
        if not isinstance(follow_ups, list):
            follow_ups = [str(follow_ups)]
        question_text = str(rewritten.get("question") or source["original_question"])
        questions.append(
            {
                "question_id": stable_id("mockq", plan_id, index, source["source_instance_id"]),
                "question_index": index,
                "stage": source["stage"],
                "topic": source["topic"],
                "question": question_text,
                "follow_ups": [str(item) for item in follow_ups[:2]],
                "rubric": str(rewritten.get("rubric") or source["suggested_fix"] or ""),
                "generation_mode": source["generation_mode"],
                "sources": [source],
            }
        )
    return {
        "plan_id": plan_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target": {
            "company": target_company,
            "position": position,
            "round": round_name,
        },
        "intensity": intensity,
        "plan_summary": llm_result["plan_summary"],
        "generation": {
            "mode": "rules_sqlite_llm",
            "selector": "rules",
            "evidence_source": "sqlite",
            "external_retrieval_enabled": False,
            "planner_model": model,
        },
        "selected_sources": selected_sources,
        "questions": questions,
    }


def render_mock_plan_markdown(plan: dict[str, Any]) -> str:
    target = plan["target"]
    lines = [
        "# 模拟面试计划",
        "",
        "## 目标",
        "",
        f"- 公司：{target['company']}",
        f"- 岗位：{target['position']}",
        f"- 轮次：{target['round']}",
        f"- 强度：{plan['intensity']}",
        "",
        "## 本次重点薄弱项",
        "",
    ]
    seen_topics: set[str] = set()
    for source in plan["selected_sources"]:
        topic = str(source["topic"])
        if topic in seen_topics:
            continue
        seen_topics.add(topic)
        lines.append(
            f"- **{topic}**：{source['rationale']} 状态："
            f"{STATUS_LABELS.get(str(source.get('status', 'open')), '待修复')}"
        )
    lines.extend(["", "## 时间分配", "", f"- 共 {len(plan['questions'])} 道主问题；每题 1-2 个追问。", ""])
    lines.extend(["## 题目列表", ""])
    for question in plan["questions"]:
        source = question["sources"][0]
        lines.extend(
            [
                f"### {question['question_index']}. {question['topic']}",
                "",
                f"- 主问题：{question['question']}",
                f"- 题型：{question['generation_mode']}",
                f"- 历史来源：{source['company']} {source['round']}，原问题：{source['original_question']}",
                f"- 历史扣分点：{source['issue_summary']}（严重度 {source['severity']}）",
                "- 追问：",
            ]
        )
        for follow_up in question["follow_ups"]:
            lines.append(f"  - {follow_up}")
        lines.extend(["", "## 评分标准" if question["question_index"] == 1 else ""])
        if question["question_index"] == 1:
            lines.append("")
        lines.append(f"- **{question['topic']}**：{question['rubric']}")
        lines.append("")
    return "\n".join(line for line in lines if line is not None).rstrip() + "\n"


def write_plan_outputs(plan: dict[str, Any], output_path: str | Path, json_output_path: str | Path | None = None) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_mock_plan_markdown(plan), encoding="utf-8")
    if json_output_path is not None:
        json_output = Path(json_output_path)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--company", default="模拟公司")
    parser.add_argument("--position", default="推荐算法工程师")
    parser.add_argument("--round", default="模拟二面")
    parser.add_argument("--intensity", choices=["normal", "strict", "pressure"], default="normal")
    parser.add_argument("--question-count", type=int, default=5)
    parser.add_argument("--focus-topic", action="append", default=[])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)

    try:
        plan = build_mock_plan(
            args.db,
            target_company=args.company,
            position=args.position,
            round_name=args.round,
            intensity=args.intensity,
            question_count=args.question_count,
            focus_topics=args.focus_topic,
            model=args.model,
        )
    except (OpenAIConfigError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    write_plan_outputs(plan, args.output, args.json_output)
    print(args.output)
    if args.json_output:
        print(args.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
