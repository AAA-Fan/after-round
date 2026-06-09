#!/usr/bin/env python3
"""SQLite storage helpers for after_round memory."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = "memory_phase1_v1"


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database(db_path: str | Path) -> None:
    """Create the Phase 1 memory schema if it does not exist."""

    conn = connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns (
              campaign_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              target_position TEXT,
              target_industry TEXT,
              start_date TEXT,
              end_date TEXT,
              status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'closed', 'archived')),
              notes TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
              session_id TEXT PRIMARY KEY,
              campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
              source_type TEXT NOT NULL DEFAULT 'real'
                CHECK (source_type IN ('real', 'mock')),
              company TEXT NOT NULL,
              position TEXT NOT NULL,
              round TEXT NOT NULL,
              session_date TEXT,
              duration_minutes INTEGER,
              result_signal TEXT NOT NULL DEFAULT 'unknown',
              include_in_trends INTEGER NOT NULL DEFAULT 1
                CHECK (include_in_trends IN (0, 1)),
              input_summary TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS source_documents (
              document_id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
              doc_type TEXT NOT NULL,
              local_path TEXT,
              feishu_url TEXT,
              content_hash TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ingestion_runs (
              ingestion_id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
              schema_version TEXT NOT NULL,
              extractor_model TEXT NOT NULL,
              source_hash TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'partial')),
              error_message TEXT,
              started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS questions (
              question_id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
              question_index INTEGER NOT NULL,
              stage TEXT NOT NULL,
              topic TEXT,
              normalized_topic TEXT,
              original_question TEXT NOT NULL,
              normalized_question TEXT,
              interviewer_intent TEXT,
              answer_summary TEXT,
              answer_quality INTEGER CHECK (answer_quality BETWEEN 1 AND 5),
              outcome TEXT CHECK (outcome IN ('strong', 'partial', 'weak', 'failed', 'unknown')),
              issue_type TEXT,
              evidence_summary TEXT,
              better_answer TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS weakness_clusters (
              cluster_id TEXT PRIMARY KEY,
              campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
              dimension TEXT NOT NULL,
              topic TEXT NOT NULL,
              normalized_label TEXT NOT NULL,
              description TEXT,
              status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'recurring', 'improving', 'resolved')),
              first_seen_session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
              first_seen_at TEXT,
              last_seen_session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
              last_seen_at TEXT,
              recurrence_count INTEGER NOT NULL DEFAULT 0,
              max_severity INTEGER,
              avg_severity REAL,
              confidence REAL NOT NULL DEFAULT 1.0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (campaign_id, normalized_label)
            );

            CREATE TABLE IF NOT EXISTS weakness_instances (
              instance_id TEXT PRIMARY KEY,
              cluster_id TEXT REFERENCES weakness_clusters(cluster_id) ON DELETE SET NULL,
              session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
              question_id TEXT REFERENCES questions(question_id) ON DELETE SET NULL,
              dimension TEXT NOT NULL,
              topic TEXT NOT NULL,
              severity INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 5),
              confidence REAL NOT NULL DEFAULT 1.0,
              issue_summary TEXT NOT NULL,
              evidence TEXT,
              suggested_fix TEXT,
              extraction_source TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS action_items (
              action_item_id TEXT PRIMARY KEY,
              cluster_id TEXT REFERENCES weakness_clusters(cluster_id) ON DELETE CASCADE,
              session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              detail TEXT,
              priority TEXT NOT NULL DEFAULT 'P1'
                CHECK (priority IN ('P0', 'P1', 'P2')),
              status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'done', 'skipped')),
              due_date TEXT,
              completed_at TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS growth_signals (
              growth_signal_id TEXT PRIMARY KEY,
              cluster_id TEXT NOT NULL REFERENCES weakness_clusters(cluster_id) ON DELETE CASCADE,
              session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
              signal_type TEXT NOT NULL
                CHECK (signal_type IN ('new', 'recurred', 'improved', 'regressed', 'resolved_candidate')),
              previous_instance_id TEXT REFERENCES weakness_instances(instance_id) ON DELETE SET NULL,
              current_instance_id TEXT REFERENCES weakness_instances(instance_id) ON DELETE SET NULL,
              previous_severity INTEGER,
              current_severity INTEGER,
              evidence TEXT,
              reasoning TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS analysis_reports (
              report_id TEXT PRIMARY KEY,
              campaign_id TEXT REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
              scope_json TEXT,
              include_mock INTEGER NOT NULL DEFAULT 0 CHECK (include_mock IN (0, 1)),
              report_type TEXT NOT NULL,
              report_path TEXT,
              summary TEXT,
              generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS mock_session_configs (
              session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
              intensity TEXT NOT NULL DEFAULT 'normal'
                CHECK (intensity IN ('normal', 'strict', 'pressure')),
              focus_dimensions_json TEXT,
              question_mix_json TEXT,
              source_scope_json TEXT,
              planner_model TEXT,
              plan_summary TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS mock_question_sources (
              source_id TEXT PRIMARY KEY,
              mock_question_id TEXT NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
              cluster_id TEXT REFERENCES weakness_clusters(cluster_id) ON DELETE SET NULL,
              source_instance_id TEXT REFERENCES weakness_instances(instance_id) ON DELETE SET NULL,
              source_question_id TEXT REFERENCES questions(question_id) ON DELETE SET NULL,
              generation_mode TEXT NOT NULL
                CHECK (generation_mode IN ('replay', 'extension', 'transfer')),
              rationale TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_campaign_date
            ON sessions (campaign_id, session_date);

            CREATE INDEX IF NOT EXISTS idx_sessions_source_type
            ON sessions (campaign_id, source_type, include_in_trends);

            CREATE INDEX IF NOT EXISTS idx_questions_session_stage
            ON questions (session_id, stage, question_index);

            CREATE INDEX IF NOT EXISTS idx_questions_topic
            ON questions (normalized_topic);

            CREATE INDEX IF NOT EXISTS idx_clusters_campaign_dimension
            ON weakness_clusters (campaign_id, dimension, status);

            CREATE INDEX IF NOT EXISTS idx_instances_cluster_session
            ON weakness_instances (cluster_id, session_id);

            CREATE INDEX IF NOT EXISTS idx_instances_session_dimension
            ON weakness_instances (session_id, dimension, severity);

            CREATE INDEX IF NOT EXISTS idx_growth_cluster_time
            ON growth_signals (cluster_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_action_items_status
            ON action_items (cluster_id, status, priority);

            CREATE INDEX IF NOT EXISTS idx_mock_sources_question
            ON mock_question_sources (mock_question_id);

            CREATE INDEX IF NOT EXISTS idx_mock_sources_cluster
            ON mock_question_sources (cluster_id, source_instance_id);
            """
        )
        conn.commit()
    finally:
        conn.close()
