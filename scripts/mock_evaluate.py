#!/usr/bin/env python3
"""Evaluate a mock interview transcript and persist mock results to memory."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.memory_ingest import stable_id
from scripts.memory_llm_report import DEFAULT_MODEL, OpenAIConfigError, OpenAIResponsesClient
from scripts.memory_store import connect, initialize_database
from scripts.mock_plan import extract_json_object


DEFAULT_DB_PATH = Path("data/after_round_memory.sqlite")
DEFAULT_REVIEW_OUTPUT = Path("data/exports/mock-reports/latest-review.md")

VALID_OUTCOMES = {"strong", "partial", "weak", "failed", "unknown"}
VALID_SIGNALS = {"new", "recurred", "improved", "regressed", "resolved_candidate"}


def build_evaluator_prompt() -> str:
    return """你是一个严厉但务实的程序员模拟面试复盘教练。

你会收到 mock plan 和 transcript。只基于这些内容判断：
1. 用户是否修复了历史薄弱项；
2. 哪些问题仍然复发；
3. 哪些回答有改善；
4. 下一场真实面试前应该做什么。

必须输出 JSON object，格式如下：
{
  "review_markdown": "# 模拟面试复盘\\n...",
  "question_evaluations": [
    {
      "question_id": "必须原样复制 plan.questions[].question_id",
      "source_instance_id": "对应来源 weakness instance",
      "answer_quality": 1,
      "outcome": "strong|partial|weak|failed|unknown",
      "improvement_signal": "new|recurred|improved|regressed|resolved_candidate",
      "severity": 1,
      "issue_summary": "本题暴露的问题",
      "evidence": "基于用户回答的证据",
      "suggested_fix": "下一步修复动作"
    }
  ]
}
"""


def normalize_evaluation(raw: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    plan_question_ids = {question["question_id"] for question in plan.get("questions", [])}
    evaluations = raw.get("question_evaluations") or []
    if not isinstance(evaluations, list):
        raise ValueError("LLM response question_evaluations must be a list")
    normalized: list[dict[str, Any]] = []
    for item in evaluations:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("question_id") or "")
        if question_id not in plan_question_ids:
            continue
        outcome = str(item.get("outcome") or "unknown")
        if outcome not in VALID_OUTCOMES:
            outcome = "unknown"
        signal = str(item.get("improvement_signal") or "recurred")
        if signal not in VALID_SIGNALS:
            signal = "recurred"
        try:
            quality = int(item.get("answer_quality") or 1)
        except (TypeError, ValueError):
            quality = 1
        try:
            severity = int(item.get("severity") or 3)
        except (TypeError, ValueError):
            severity = 3
        normalized.append(
            {
                "question_id": question_id,
                "source_instance_id": item.get("source_instance_id"),
                "answer_quality": min(5, max(1, quality)),
                "outcome": outcome,
                "improvement_signal": signal,
                "severity": min(5, max(1, severity)),
                "issue_summary": str(item.get("issue_summary") or "模拟面试暴露的问题"),
                "evidence": str(item.get("evidence") or ""),
                "suggested_fix": str(item.get("suggested_fix") or ""),
            }
        )
    return {
        "review_markdown": str(raw.get("review_markdown") or render_fallback_review(normalized)),
        "question_evaluations": normalized,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def render_fallback_review(evaluations: list[dict[str, Any]]) -> str:
    lines = ["# 模拟面试复盘", "", "## 一句话结论", ""]
    if not evaluations:
        lines.append("没有得到可用的逐题评估。")
        return "\n".join(lines) + "\n"
    recurred = sum(1 for item in evaluations if item["improvement_signal"] == "recurred")
    lines.append(f"本次模拟共评估 {len(evaluations)} 道题，其中 {recurred} 道仍有历史问题复发。")
    lines.extend(["", "## 本次问题总览", ""])
    for item in evaluations:
        lines.append(f"- {item['question_id']}：{item['issue_summary']}（{item['outcome']}）")
    return "\n".join(lines).rstrip() + "\n"


def build_mock_review(
    plan: dict[str, Any],
    transcript: dict[str, Any],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    model_client = client or OpenAIResponsesClient()
    payload = {
        "plan": plan,
        "transcript": transcript,
        "evaluation_policy": {
            "compare_to_history": True,
            "write_mock_only": True,
            "real_trends_pollution_allowed": False,
        },
    }
    response = model_client.generate_markdown(
        system_prompt=build_evaluator_prompt(),
        user_payload=payload,
        model=model,
    )
    return normalize_evaluation(extract_json_object(response), plan)


def find_campaign_id(conn, plan: dict[str, Any]) -> str:
    for question in plan.get("questions", []):
        for source in question.get("sources", []):
            cluster_id = source.get("cluster_id")
            if not cluster_id:
                continue
            row = conn.execute(
                "SELECT campaign_id FROM weakness_clusters WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()
            if row:
                return str(row["campaign_id"])
    row = conn.execute("SELECT campaign_id FROM campaigns ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return str(row["campaign_id"])
    campaign_id = stable_id("camp", "default")
    conn.execute(
        """
        INSERT INTO campaigns (campaign_id, name, status)
        VALUES (?, 'default', 'active')
        """,
        (campaign_id,),
    )
    return campaign_id


def group_turn_answers(transcript: dict[str, Any]) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for turn in transcript.get("turns", []):
        question_id = str(turn.get("question_id") or "")
        if not question_id:
            continue
        grouped.setdefault(question_id, []).append(str(turn.get("answer_text") or ""))
    return {question_id: "\n".join(answer for answer in answers if answer) for question_id, answers in grouped.items()}


def index_evaluations(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["question_id"]): item
        for item in review.get("question_evaluations", [])
        if item.get("question_id")
    }


def persist_mock_session(
    db_path: str | Path,
    plan: dict[str, Any],
    transcript: dict[str, Any],
    review: dict[str, Any],
    *,
    report_path: str | Path | None = None,
) -> str:
    initialize_database(db_path)
    conn = connect(db_path)
    try:
        campaign_id = find_campaign_id(conn, plan)
        session_id = f"mock_{uuid.uuid4().hex[:16]}"
        target = plan.get("target") or {}
        conn.execute(
            """
            INSERT INTO sessions (
              session_id, campaign_id, source_type, company, position, round,
              session_date, result_signal, include_in_trends, input_summary
            )
            VALUES (?, ?, 'mock', ?, ?, ?, ?, 'mock_only', 0, ?)
            """,
            (
                session_id,
                campaign_id,
                str(target.get("company") or "模拟公司"),
                str(target.get("position") or "模拟岗位"),
                str(target.get("round") or "模拟面试"),
                date.today().isoformat(),
                str(plan.get("plan_summary") or ""),
            ),
        )
        question_mix = Counter(question.get("generation_mode", "replay") for question in plan.get("questions", []))
        focus_dimensions = [
            question.get("sources", [{}])[0].get("dimension")
            for question in plan.get("questions", [])
            if question.get("sources")
        ]
        source_scope = [
            source.get("source_instance_id")
            for question in plan.get("questions", [])
            for source in question.get("sources", [])
        ]
        conn.execute(
            """
            INSERT INTO mock_session_configs (
              session_id, intensity, focus_dimensions_json, question_mix_json,
              source_scope_json, planner_model, plan_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                str(plan.get("intensity") or "normal"),
                json.dumps(focus_dimensions, ensure_ascii=False),
                json.dumps(dict(question_mix), ensure_ascii=False),
                json.dumps(source_scope, ensure_ascii=False),
                str((plan.get("generation") or {}).get("planner_model") or ""),
                str(plan.get("plan_summary") or ""),
            ),
        )

        answers_by_question = group_turn_answers(transcript)
        evaluations_by_question = index_evaluations(review)
        db_question_ids: dict[str, str] = {}
        for index, question in enumerate(plan.get("questions", []), start=1):
            plan_question_id = str(question["question_id"])
            evaluation = evaluations_by_question.get(plan_question_id, {})
            db_question_id = stable_id("mockqdb", session_id, plan_question_id)
            db_question_ids[plan_question_id] = db_question_id
            source = question.get("sources", [{}])[0]
            conn.execute(
                """
                INSERT INTO questions (
                  question_id, session_id, question_index, stage, topic,
                  normalized_topic, original_question, interviewer_intent,
                  answer_summary, answer_quality, outcome, issue_type,
                  evidence_summary, better_answer
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'mock_evaluation', ?, ?)
                """,
                (
                    db_question_id,
                    session_id,
                    index,
                    str(question.get("stage") or "mock"),
                    str(question.get("topic") or ""),
                    str(question.get("topic") or "").lower(),
                    str(question.get("question") or ""),
                    str(source.get("interviewer_intent") or ""),
                    answers_by_question.get(plan_question_id, ""),
                    evaluation.get("answer_quality"),
                    evaluation.get("outcome") or "unknown",
                    evaluation.get("evidence") or "",
                    evaluation.get("suggested_fix") or question.get("rubric") or "",
                ),
            )
            for source in question.get("sources", []):
                conn.execute(
                    """
                    INSERT INTO mock_question_sources (
                      source_id, mock_question_id, cluster_id, source_instance_id,
                      source_question_id, generation_mode, rationale
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable_id(
                            "mqs",
                            db_question_id,
                            source.get("cluster_id"),
                            source.get("source_instance_id"),
                        ),
                        db_question_id,
                        source.get("cluster_id"),
                        source.get("source_instance_id"),
                        source.get("source_question_id"),
                        question.get("generation_mode") or source.get("generation_mode") or "replay",
                        source.get("rationale"),
                    ),
                )

        for evaluation in review.get("question_evaluations", []):
            plan_question_id = str(evaluation["question_id"])
            db_question_id = db_question_ids.get(plan_question_id)
            plan_question = next(
                (item for item in plan.get("questions", []) if item.get("question_id") == plan_question_id),
                None,
            )
            if not db_question_id or not plan_question:
                continue
            source = plan_question.get("sources", [{}])[0]
            cluster_id = source.get("cluster_id")
            if not cluster_id:
                continue
            current_instance_id = None
            if evaluation["improvement_signal"] in {"new", "recurred", "regressed"}:
                current_instance_id = stable_id("win", session_id, db_question_id, evaluation["issue_summary"])
                conn.execute(
                    """
                    INSERT INTO weakness_instances (
                      instance_id, cluster_id, session_id, question_id, dimension,
                      topic, severity, confidence, issue_summary, evidence,
                      suggested_fix, extraction_source
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, 'mock_evaluation')
                    """,
                    (
                        current_instance_id,
                        cluster_id,
                        session_id,
                        db_question_id,
                        source.get("dimension") or "mock",
                        source.get("topic") or plan_question.get("topic") or "",
                        evaluation["severity"],
                        evaluation["issue_summary"],
                        evaluation["evidence"],
                        evaluation["suggested_fix"],
                    ),
                )
            conn.execute(
                """
                INSERT INTO growth_signals (
                  growth_signal_id, cluster_id, session_id, signal_type,
                  previous_instance_id, current_instance_id, previous_severity,
                  current_severity, evidence, reasoning
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id("sig", session_id, db_question_id, evaluation["improvement_signal"]),
                    cluster_id,
                    session_id,
                    evaluation["improvement_signal"],
                    source.get("source_instance_id"),
                    current_instance_id,
                    source.get("severity"),
                    evaluation["severity"],
                    evaluation["evidence"],
                    evaluation["issue_summary"],
                ),
            )

        output_path = Path(report_path) if report_path is not None else None
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(review["review_markdown"].rstrip() + "\n", encoding="utf-8")
        conn.execute(
            """
            INSERT INTO analysis_reports (
              report_id, campaign_id, scope_json, include_mock, report_type, report_path, summary
            )
            VALUES (?, ?, ?, 1, 'mock_review', ?, ?)
            """,
            (
                stable_id("rep", session_id, "mock_review"),
                campaign_id,
                json.dumps({"session_id": session_id, "plan_id": plan.get("plan_id")}, ensure_ascii=False),
                str(output_path) if output_path is not None else None,
                review["review_markdown"].splitlines()[0] if review.get("review_markdown") else "",
            ),
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--transcript-json", required=True)
    parser.add_argument("--output", default=str(DEFAULT_REVIEW_OUTPUT))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)

    plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
    transcript = json.loads(Path(args.transcript_json).read_text(encoding="utf-8"))
    try:
        review = build_mock_review(plan, transcript, model=args.model)
        session_id = persist_mock_session(args.db, plan, transcript, review, report_path=args.output)
    except (OpenAIConfigError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(args.output)
    print(session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
