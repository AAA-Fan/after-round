#!/usr/bin/env python3
"""Ingest after_round interview summaries into the local memory database."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import re
import sys
import uuid
from pathlib import Path
from typing import Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.memory_store import SCHEMA_VERSION, connect, initialize_database


DEFAULT_DB_PATH = Path("data/after_round_memory.sqlite")


@dataclasses.dataclass(frozen=True)
class IngestResult:
    session_id: str
    campaign_id: str
    company: str
    position: str
    round: str
    questions_inserted: int
    weaknesses_inserted: int


@dataclasses.dataclass(frozen=True)
class ParsedQuestion:
    index: int
    stage: str
    text: str


@dataclasses.dataclass(frozen=True)
class ParsedWeakness:
    title: str
    original_question: str
    issue: str
    penalty: str
    better_answer: str
    review_suggestion: str
    dimension: str
    topic: str
    normalized_label: str
    severity: int


def stable_id(prefix: str, *parts: object) -> str:
    raw = "\n".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def normalize_label_token(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", lowered)
    lowered = re.sub(r"_+", "_", lowered).strip("_")
    return lowered[:80] or "unknown"


def parse_metadata(summary_text: str, source_path: str | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {
        "company": "",
        "position": "",
        "round": "",
        "session_date": None,
        "duration_minutes": None,
    }
    field_map = {
        "公司": "company",
        "岗位": "position",
        "轮次": "round",
        "日期": "session_date",
    }
    for line in summary_text.splitlines():
        match = re.match(r"^\s*[-*]\s*([^：:]+)[：:]\s*(.+?)\s*$", line)
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        if key in field_map:
            metadata[field_map[key]] = value
        elif key == "面试时长":
            duration_match = re.search(r"(\d+)", value)
            if duration_match:
                metadata["duration_minutes"] = int(duration_match.group(1))

    if not metadata["company"] or not metadata["position"] or not metadata["round"]:
        title_match = re.search(r"^#\s+(.+?)\s+(.+?)\s+(.+?)\s+-\s+面试总评", summary_text, re.M)
        if title_match:
            metadata["company"] = metadata["company"] or title_match.group(1)
            metadata["position"] = metadata["position"] or title_match.group(2)
            metadata["round"] = metadata["round"] or title_match.group(3)

    if not metadata["company"] and source_path:
        metadata["company"] = Path(source_path).stem.split("-")[0]

    metadata["company"] = str(metadata["company"] or "未知公司").strip()
    metadata["position"] = str(metadata["position"] or "未知岗位").strip()
    metadata["round"] = str(metadata["round"] or "未知轮次").strip()
    return metadata


def extract_section(markdown: str, heading: str) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    match = re.search(pattern, markdown, re.M)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^##\s+", markdown[start:], re.M)
    end = start + next_match.start() if next_match else len(markdown)
    return markdown[start:end].strip()


STAGE_MAP = {
    "项目自我介绍": "self_intro",
    "项目细节追问": "project_deep_dive",
    "基础知识和八股": "fundamentals",
    "做题": "coding",
    "手撕代码": "coding",
    "系统设计": "system_design",
    "反问": "reverse_questions",
    "求职动机": "motivation",
    "其他": "other",
}


def normalize_stage(stage_title: str) -> str:
    for keyword, value in STAGE_MAP.items():
        if keyword in stage_title:
            return value
    return "other"


def parse_questions(summary_text: str) -> list[ParsedQuestion]:
    section = extract_section(summary_text, "面试问题总览")
    if not section:
        return []

    questions: list[ParsedQuestion] = []
    current_stage = "other"
    for line in section.splitlines():
        stage_match = re.match(r"^###\s+(.+?)\s*$", line)
        if stage_match:
            current_stage = normalize_stage(stage_match.group(1))
            continue
        question_match = re.match(r"^\s*(\d+)[.、]\s+(.+?)\s*$", line)
        if not question_match:
            continue
        text = normalize_space(question_match.group(2))
        if text:
            questions.append(ParsedQuestion(int(question_match.group(1)), current_stage, text))
    return questions


def parse_bullets(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in block.splitlines():
        match = re.match(r"^\s*[-*]\s*([^：:]+)[：:]\s*(.*?)\s*$", line)
        if match:
            current_key = match.group(1).strip()
            fields[current_key] = match.group(2).strip()
        elif current_key and line.strip():
            fields[current_key] = f"{fields[current_key]} {line.strip()}".strip()
    return fields


def classify_weakness(text: str) -> tuple[str, str, str]:
    lower = text.lower()
    if "target attention" in lower or "attention 缓存" in lower:
        return "project_depth", "target attention 缓存", "project_depth.target_attention_cache"
    if "rq-vae" in lower or "vq-vae" in lower or "码本" in text or "codebook" in lower:
        return "project_depth", "RQ-VAE 码本机制", "project_depth.rq_vae"
    if "onerec" in lower or "tiger" in lower or "生成式召回" in text or "semantic id" in lower:
        return "project_depth", "OneRec 生成式召回", "project_depth.onerec"
    if "qwen-vl" in lower or "llava" in lower or "多模态" in text or "clip" in lower or "端到端" in text:
        return "project_depth", "多模态内容理解", "project_depth.multimodal"
    if "moe" in lower or "expert" in lower or "routing" in lower or "路由" in text:
        return "project_depth", "MoE routing", "project_depth.moe_routing"
    if "反问" in text:
        return "reverse_questions", "反问质量", "reverse_questions.reverse_questions"
    if "自我不足" in text or "优势和不足" in text or "自我认知" in text:
        return "motivation", "自我认知表达", "motivation.self_assessment"
    if (
        "离职" in text
        or "求职动机" in text
        or "薪资" in text
        or "岗位匹配" in text
        or "转大模型" in text
        or "换工作" in text
        or "职业规划" in text
        or "稳定性风险" in text
    ):
        return "motivation", "求职动机和岗位匹配", "motivation.career_motivation"
    if "手撕" in text or "代码" in text or "语法" in text:
        return "coding", "编码稳定性", "coding.coding_stability"
    if (
        "指标" in text
        or "口径" in text
        or "收益" in text
        or "广告主价值" in text
        or "商业化机制" in text
        or "自然流量" in text
        or "成交" in text
        or "guardrail" in lower
    ):
        return "metrics_and_impact", "指标口径和收益归因", "metrics_and_impact.metrics"
    if "attention" in lower or "flashattention" in lower or "kv cache" in lower:
        return "fundamentals", "Attention 基础机制", "fundamentals.attention"
    if "贡献" in text or "owner" in lower or "边界" in text or "项目介绍" in text or "主线" in text:
        return "project_storyline", "项目主线和职责边界", "project_storyline.ownership"
    fallback = normalize_label_token(text[:40])
    return "communication", text[:30] or "表达结构", f"communication.{fallback}"


def estimate_severity(text: str) -> int:
    severity = 3
    if re.search(r"最弱|高风险|最大风险|严重|核心卖点|直接扣分|不能只说", text):
        severity += 1
    if re.search(r"没有|不够|不足|不清|不稳|扣分|质疑|风险", text):
        severity += 1
    if re.search(r"比之前清楚|有所改善|这次能说出|仍不够完整", text):
        severity -= 1
    return max(1, min(5, severity))


def parse_weaknesses(summary_text: str) -> list[ParsedWeakness]:
    section = extract_section(summary_text, "回答不够好的问题清单")
    if not section:
        return []

    blocks = re.split(r"(?=^###\s+\d+[.、]\s+)", section, flags=re.M)
    weaknesses: list[ParsedWeakness] = []
    for block in blocks:
        title_match = re.match(r"^###\s+\d+[.、]\s+(.+?)\s*$", block.strip(), re.M)
        if not title_match:
            continue
        title = normalize_space(title_match.group(1))
        fields = parse_bullets(block)
        original_question = fields.get("原问题", "")
        issue = fields.get("原回答的问题", "")
        penalty = fields.get("为什么会扣分", "")
        better_answer = fields.get("更好的回答方向", "")
        review_suggestion = fields.get("复习建议", "") or fields.get("练习建议", "")
        classification_text = "\n".join([title, original_question, issue, penalty])
        scoring_text = "\n".join([classification_text, better_answer, review_suggestion])
        dimension, topic, normalized_label = classify_weakness(classification_text)
        weaknesses.append(
            ParsedWeakness(
                title=title,
                original_question=normalize_space(original_question),
                issue=normalize_space(issue),
                penalty=normalize_space(penalty),
                better_answer=normalize_space(better_answer),
                review_suggestion=normalize_space(review_suggestion),
                dimension=dimension,
                topic=topic,
                normalized_label=normalized_label,
                severity=estimate_severity(scoring_text),
            )
        )
    return weaknesses


def ensure_campaign(conn, campaign_name: str, target_position: str | None) -> str:
    campaign_id = stable_id("camp", campaign_name)
    conn.execute(
        """
        INSERT INTO campaigns (campaign_id, name, target_position)
        VALUES (?, ?, ?)
        ON CONFLICT(campaign_id) DO UPDATE SET
          target_position = COALESCE(excluded.target_position, campaigns.target_position),
          updated_at = CURRENT_TIMESTAMP
        """,
        (campaign_id, campaign_name, target_position),
    )
    return campaign_id


def find_matching_question_id(conn, session_id: str, weakness: ParsedWeakness) -> str | None:
    if not weakness.original_question:
        return None
    rows = conn.execute(
        "SELECT question_id, original_question FROM questions WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    normalized_weakness_question = normalize_space(weakness.original_question)
    for row in rows:
        question = normalize_space(row["original_question"])
        if question and (question in normalized_weakness_question or normalized_weakness_question in question):
            return row["question_id"]
    return None


def upsert_weakness_cluster(conn, campaign_id: str, session_id: str, session_date: str | None, weakness: ParsedWeakness) -> tuple[str, str, str | None, int | None]:
    existing = conn.execute(
        """
        SELECT cluster_id, status FROM weakness_clusters
        WHERE campaign_id = ? AND normalized_label = ?
        """,
        (campaign_id, weakness.normalized_label),
    ).fetchone()
    if not existing:
        cluster_id = stable_id("clu", campaign_id, weakness.normalized_label)
        conn.execute(
            """
            INSERT INTO weakness_clusters (
              cluster_id, campaign_id, dimension, topic, normalized_label,
              description, status, first_seen_session_id, first_seen_at,
              last_seen_session_id, last_seen_at, recurrence_count,
              max_severity, avg_severity, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 0, NULL, NULL, 1.0)
            """,
            (
                cluster_id,
                campaign_id,
                weakness.dimension,
                weakness.topic,
                weakness.normalized_label,
                weakness.title,
                session_id,
                session_date,
                session_id,
                session_date,
            ),
        )
        return cluster_id, "new", None, None

    cluster_id = existing["cluster_id"]
    previous = conn.execute(
        """
        SELECT instance_id, severity FROM weakness_instances
        WHERE cluster_id = ?
        ORDER BY created_at DESC, instance_id DESC
        LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()
    previous_instance_id = previous["instance_id"] if previous else None
    previous_severity = int(previous["severity"]) if previous else None
    if previous_severity is None:
        signal_type = "new"
    elif weakness.severity < previous_severity:
        signal_type = "improved"
    elif weakness.severity > previous_severity:
        signal_type = "regressed"
    else:
        signal_type = "recurred"
    return cluster_id, signal_type, previous_instance_id, previous_severity


def refresh_cluster_stats(conn, cluster_id: str, latest_session_id: str, latest_date: str | None, signal_type: str) -> None:
    stats = conn.execute(
        """
        SELECT
          COUNT(*) AS count,
          COUNT(DISTINCT session_id) AS session_count,
          MAX(severity) AS max_severity,
          AVG(severity) AS avg_severity
        FROM weakness_instances
        WHERE cluster_id = ?
        """,
        (cluster_id,),
    ).fetchone()
    if int(stats["session_count"]) < 2:
        status = "open"
    else:
        status = {
            "new": "open",
            "recurred": "recurring",
            "improved": "improving",
            "regressed": "recurring",
            "resolved_candidate": "improving",
        }[signal_type]
    conn.execute(
        """
        UPDATE weakness_clusters
        SET recurrence_count = ?,
            max_severity = ?,
            avg_severity = ?,
            status = ?,
            last_seen_session_id = ?,
            last_seen_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE cluster_id = ?
        """,
        (
            int(stats["count"]),
            int(stats["max_severity"]),
            float(stats["avg_severity"]),
            status,
            latest_session_id,
            latest_date,
            cluster_id,
        ),
    )


def recompute_cluster_stats(conn, cluster_id: str) -> None:
    stats = conn.execute(
        """
        SELECT
          COUNT(*) AS count,
          COUNT(DISTINCT session_id) AS session_count,
          MAX(severity) AS max_severity,
          AVG(severity) AS avg_severity
        FROM weakness_instances
        WHERE cluster_id = ?
        """,
        (cluster_id,),
    ).fetchone()
    if int(stats["count"]) == 0:
        conn.execute("DELETE FROM weakness_clusters WHERE cluster_id = ?", (cluster_id,))
        return

    first = conn.execute(
        """
        SELECT wi.severity, wi.session_id, s.session_date
        FROM weakness_instances wi
        JOIN sessions s ON s.session_id = wi.session_id
        WHERE wi.cluster_id = ?
        ORDER BY COALESCE(s.session_date, ''), wi.created_at, wi.instance_id
        LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()
    last = conn.execute(
        """
        SELECT wi.severity, wi.session_id, s.session_date
        FROM weakness_instances wi
        JOIN sessions s ON s.session_id = wi.session_id
        WHERE wi.cluster_id = ?
        ORDER BY COALESCE(s.session_date, '') DESC, wi.created_at DESC, wi.instance_id DESC
        LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()
    if int(stats["session_count"]) < 2:
        status = "open"
    elif int(last["severity"]) < int(first["severity"]):
        status = "improving"
    else:
        status = "recurring"
    conn.execute(
        """
        UPDATE weakness_clusters
        SET recurrence_count = ?,
            max_severity = ?,
            avg_severity = ?,
            status = ?,
            first_seen_session_id = ?,
            first_seen_at = ?,
            last_seen_session_id = ?,
            last_seen_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE cluster_id = ?
        """,
        (
            int(stats["count"]),
            int(stats["max_severity"]),
            float(stats["avg_severity"]),
            status,
            first["session_id"],
            first["session_date"],
            last["session_id"],
            last["session_date"],
            cluster_id,
        ),
    )


def ingest_summary_text(
    db_path: str | Path,
    summary_text: str,
    *,
    source_path: str | None = None,
    campaign_name: str = "default",
    source_type: str = "real",
    include_in_trends: bool | None = None,
) -> IngestResult:
    initialize_database(db_path)
    metadata = parse_metadata(summary_text, source_path)
    questions = parse_questions(summary_text)
    weaknesses = parse_weaknesses(summary_text)
    hash_value = content_hash(summary_text)

    include = (source_type == "real") if include_in_trends is None else include_in_trends

    conn = connect(db_path)
    try:
        campaign_id = ensure_campaign(conn, campaign_name, str(metadata["position"]))
        session_id = stable_id(
            "ses",
            campaign_id,
            source_type,
            metadata["company"],
            metadata["position"],
            metadata["round"],
            metadata["session_date"],
            source_path or hash_value,
        )
        conn.execute(
            """
            INSERT INTO sessions (
              session_id, campaign_id, source_type, company, position, round,
              session_date, duration_minutes, result_signal, include_in_trends, input_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              company = excluded.company,
              position = excluded.position,
              round = excluded.round,
              session_date = excluded.session_date,
              duration_minutes = excluded.duration_minutes,
              include_in_trends = excluded.include_in_trends,
              input_summary = excluded.input_summary,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                session_id,
                campaign_id,
                source_type,
                metadata["company"],
                metadata["position"],
                metadata["round"],
                metadata["session_date"],
                metadata["duration_minutes"],
                1 if include else 0,
                extract_section(summary_text, "一句话结论")[:500],
            ),
        )

        document_id = stable_id("doc", session_id, "summary", source_path or hash_value)
        conn.execute(
            """
            INSERT OR IGNORE INTO source_documents (
              document_id, session_id, doc_type, local_path, content_hash
            )
            VALUES (?, ?, 'summary', ?, ?)
            """,
            (document_id, session_id, source_path, hash_value),
        )

        ingestion_id = f"ing_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """
            INSERT INTO ingestion_runs (
              ingestion_id, session_id, schema_version, extractor_model,
              source_hash, status, finished_at
            )
            VALUES (?, ?, ?, 'deterministic_markdown_v1', ?, 'success', CURRENT_TIMESTAMP)
            """,
            (ingestion_id, session_id, SCHEMA_VERSION, hash_value),
        )

        affected_cluster_ids = [
            row["cluster_id"]
            for row in conn.execute(
                """
                SELECT DISTINCT cluster_id
                FROM weakness_instances
                WHERE session_id = ? AND cluster_id IS NOT NULL
                """,
                (session_id,),
            ).fetchall()
        ]

        conn.execute("DELETE FROM questions WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM action_items WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM weakness_instances WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM growth_signals WHERE session_id = ?", (session_id,))
        for cluster_id in affected_cluster_ids:
            recompute_cluster_stats(conn, cluster_id)

        for question in questions:
            question_id = stable_id("que", session_id, question.index, question.text)
            topic = question.text[:80]
            conn.execute(
                """
                INSERT INTO questions (
                  question_id, session_id, question_index, stage, topic,
                  normalized_topic, original_question, normalized_question,
                  answer_quality, outcome
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 3, 'unknown')
                """,
                (
                    question_id,
                    session_id,
                    question.index,
                    question.stage,
                    topic,
                    normalize_label_token(topic),
                    question.text,
                    normalize_space(question.text),
                ),
            )

        inserted_weaknesses = 0
        for weakness in weaknesses:
            cluster_id, signal_type, previous_instance_id, previous_severity = upsert_weakness_cluster(
                conn,
                campaign_id,
                session_id,
                metadata["session_date"],
                weakness,
            )
            question_id = find_matching_question_id(conn, session_id, weakness)
            instance_id = stable_id("win", session_id, weakness.normalized_label, weakness.title)
            evidence = "\n".join(
                part
                for part in [
                    f"原问题：{weakness.original_question}" if weakness.original_question else "",
                    f"原回答的问题：{weakness.issue}" if weakness.issue else "",
                    f"为什么会扣分：{weakness.penalty}" if weakness.penalty else "",
                ]
                if part
            )
            conn.execute(
                """
                INSERT INTO weakness_instances (
                  instance_id, cluster_id, session_id, question_id, dimension, topic,
                  severity, confidence, issue_summary, evidence, suggested_fix, extraction_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, 'summary_bad_questions')
                """,
                (
                    instance_id,
                    cluster_id,
                    session_id,
                    question_id,
                    weakness.dimension,
                    weakness.topic,
                    weakness.severity,
                    weakness.title,
                    evidence,
                    weakness.better_answer,
                ),
            )
            inserted_weaknesses += 1

            conn.execute(
                """
                INSERT INTO growth_signals (
                  growth_signal_id, cluster_id, session_id, signal_type,
                  previous_instance_id, current_instance_id,
                  previous_severity, current_severity, evidence, reasoning
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id("sig", cluster_id, session_id, instance_id, signal_type),
                    cluster_id,
                    session_id,
                    signal_type,
                    previous_instance_id,
                    instance_id,
                    previous_severity,
                    weakness.severity,
                    weakness.title,
                    "单场薄弱项写入后与同一 cluster 的上一条记录比较。",
                ),
            )

            if weakness.review_suggestion or weakness.better_answer:
                conn.execute(
                    """
                    INSERT INTO action_items (
                      action_item_id, cluster_id, session_id, title, detail, priority
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable_id("act", session_id, weakness.normalized_label, weakness.review_suggestion),
                        cluster_id,
                        session_id,
                        weakness.review_suggestion or weakness.better_answer[:80],
                        weakness.better_answer,
                        "P0" if weakness.severity >= 5 else "P1",
                    ),
                )
            refresh_cluster_stats(conn, cluster_id, session_id, metadata["session_date"], signal_type)

        conn.commit()
    finally:
        conn.close()

    return IngestResult(
        session_id=session_id,
        campaign_id=campaign_id,
        company=str(metadata["company"]),
        position=str(metadata["position"]),
        round=str(metadata["round"]),
        questions_inserted=len(questions),
        weaknesses_inserted=inserted_weaknesses,
    )


def iter_summary_paths(paths: Iterable[str]) -> list[Path]:
    result: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            result.extend(sorted(path.glob("*-面试总评.md")))
        else:
            result.append(path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summaries", nargs="+", help="面试总评 Markdown 文件或目录")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 记忆库路径")
    parser.add_argument("--campaign", default="default", help="求职周期名称")
    parser.add_argument("--source-type", choices=["real", "mock"], default="real")
    parser.add_argument("--include-in-trends", action="store_true", help="mock 默认不参与趋势；传入后强制参与")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    initialize_database(db_path)
    paths = iter_summary_paths(args.summaries)
    if not paths:
        raise SystemExit("没有找到可导入的面试总评 Markdown")

    for path in paths:
        text = path.read_text(encoding="utf-8")
        result = ingest_summary_text(
            db_path,
            text,
            source_path=str(path),
            campaign_name=args.campaign,
            source_type=args.source_type,
            include_in_trends=True if args.include_in_trends else None,
        )
        print(
            f"{result.company} {result.round}: "
            f"{result.questions_inserted} questions, {result.weaknesses_inserted} weaknesses"
        )
    print(f"memory db: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
