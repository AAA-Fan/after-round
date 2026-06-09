#!/usr/bin/env python3
"""Generate the LLM-enhanced cross-session memory report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.memory_store import connect, initialize_database


DEFAULT_DB_PATH = Path("data/after_round_memory.sqlite")
DEFAULT_OUTPUT = Path("data/exports/memory-reports/latest-llm.md")
DEFAULT_MODEL = os.environ.get("AFTER_ROUND_OPENAI_MODEL", "gpt-4.1")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

STATUS_LABELS = {
    "open": "待修复",
    "recurring": "持续复发",
    "improving": "改善中",
    "resolved": "已解决",
}


def find_env_file(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    candidates = [current] if current.is_dir() else [current.parent]
    candidates.extend(candidates[0].parents)
    for directory in candidates:
        path = directory / ".env"
        if path.exists():
            return path
    return None


def load_env_file(path: str | Path | None = None) -> bool:
    if path is None and os.environ.get("AFTER_ROUND_DISABLE_DOTENV") == "1":
        return False
    env_path = Path(path) if path is not None else find_env_file()
    if env_path is None or not env_path.exists():
        return False
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


class OpenAIConfigError(RuntimeError):
    pass


class OpenAIResponsesClient:
    """Tiny Responses API client using only the Python standard library."""

    def __init__(self, api_key: str | None = None, *, base_url: str = OPENAI_RESPONSES_URL) -> None:
        load_env_file()
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url
        if not self.api_key:
            raise OpenAIConfigError("OPENAI_API_KEY is not set")

    def generate_markdown(self, *, system_prompt: str, user_payload: dict[str, Any], model: str) -> str:
        request_body = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "请基于下面 JSON 生成 Markdown 报告。JSON 是唯一事实来源：\n"
                        f"{json.dumps(user_payload, ensure_ascii=False, indent=2)}"
                    ),
                },
            ],
        }
        response = self._post_json(self.base_url, request_body)
        return extract_response_text(response)

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI API request failed: {exc.reason}") from exc


def extract_response_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    parts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError("OpenAI API response did not contain output text")
    return text


def fetch_clusters(conn, include_mock: bool) -> list[dict[str, object]]:
    source_filter = "" if include_mock else "AND s.include_in_trends = 1"
    rows = conn.execute(
        f"""
        SELECT
          wc.cluster_id,
          wc.dimension,
          wc.topic,
          wc.normalized_label,
          wc.status,
          COUNT(wi.instance_id) AS occurrence_count,
          COUNT(DISTINCT s.session_id) AS session_count,
          MAX(wi.severity) AS max_severity,
          AVG(wi.severity) AS avg_severity,
          GROUP_CONCAT(s.company || ' ' || s.round, '；') AS sessions
        FROM weakness_clusters wc
        JOIN weakness_instances wi ON wi.cluster_id = wc.cluster_id
        JOIN sessions s ON s.session_id = wi.session_id
        WHERE 1 = 1 {source_filter}
        GROUP BY wc.cluster_id
        ORDER BY occurrence_count DESC, max_severity DESC, wc.topic
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_instances(conn, cluster_id: str, include_mock: bool) -> list[dict[str, object]]:
    source_filter = "" if include_mock else "AND s.include_in_trends = 1"
    rows = conn.execute(
        f"""
        SELECT
          wi.instance_id,
          wi.issue_summary,
          wi.severity,
          wi.evidence,
          wi.suggested_fix,
          s.company,
          s.position,
          s.round,
          s.session_date,
          s.source_type
        FROM weakness_instances wi
        JOIN sessions s ON s.session_id = wi.session_id
        WHERE wi.cluster_id = ? {source_filter}
        ORDER BY COALESCE(s.session_date, ''), wi.created_at, wi.instance_id
        """,
        (cluster_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_analysis_pack(db_path: str | Path, *, include_mock: bool = False) -> dict[str, Any]:
    initialize_database(db_path)
    conn = connect(db_path)
    try:
        clusters = fetch_clusters(conn, include_mock)
        session_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE include_in_trends = 1 OR ?",
            (1 if include_mock else 0,),
        ).fetchone()[0]
        common_clusters: list[dict[str, Any]] = []
        all_clusters: list[dict[str, Any]] = []
        for cluster in clusters:
            instances = fetch_instances(conn, str(cluster["cluster_id"]), include_mock)
            distinct_sessions = list(dict.fromkeys(f"{item['company']} {item['round']}" for item in instances))
            cluster_payload = {
                "cluster_id": cluster["cluster_id"],
                "dimension": cluster["dimension"],
                "topic": cluster["topic"],
                "status": cluster["status"],
                "status_label": STATUS_LABELS.get(str(cluster["status"]), str(cluster["status"])),
                "occurrence_count": int(cluster["occurrence_count"]),
                "session_count": int(cluster["session_count"]),
                "max_severity": int(cluster["max_severity"]),
                "avg_severity": round(float(cluster["avg_severity"]), 2),
                "sessions": distinct_sessions,
                "instances": instances,
            }
            all_clusters.append(cluster_payload)
            if int(cluster["session_count"]) >= 2:
                common_clusters.append(cluster_payload)

        return {
            "scope": "all_real_sessions" if not include_mock else "all_sessions_including_mock",
            "session_count": int(session_count),
            "common_clusters": common_clusters,
            "all_clusters": all_clusters,
        }
    finally:
        conn.close()


def build_llm_system_prompt() -> str:
    return """你是一个严厉但务实的程序员面试教练。

只基于用户提供的 JSON 事实生成报告，不要编造没有证据的经历、结论或数字。
报告目标不是复述统计，而是解释：
1. 哪些问题是真正跨场次复发；
2. 哪些问题有改善或没有改善；
3. 为什么这些问题会影响面试评价；
4. 下一场面试前最应该做什么。

输出 Markdown，必须包含：
# LLM 加工跨场次面试分析报告
## 一句话结论
## 共性问题深度分析
## 动态变化和成长判断
## 下场面试准备优先级
## 证据引用
"""


def build_llm_report(
    db_path: str | Path,
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    include_mock: bool = False,
) -> str:
    payload = build_analysis_pack(db_path, include_mock=include_mock)
    payload["report_mode"] = "llm"
    model_client = client or OpenAIResponsesClient()
    return model_client.generate_markdown(
        system_prompt=build_llm_system_prompt(),
        user_payload=payload,
        model=model,
    )


def write_report(path: str | Path, text: str) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text.rstrip() + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 记忆库路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="报告 Markdown 输出路径")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--include-mock", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = build_llm_report(args.db, model=args.model, include_mock=args.include_mock)
    except OpenAIConfigError as exc:
        print(f"OpenAI configuration error: {exc}", file=sys.stderr)
        print("Set OPENAI_API_KEY and rerun this command.", file=sys.stderr)
        return 2
    write_report(args.output, report)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
