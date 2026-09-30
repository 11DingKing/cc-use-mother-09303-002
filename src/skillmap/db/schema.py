"""SQLite 连接与建表脚本。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jurisdictions (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standards (
    id                 INTEGER PRIMARY KEY,
    jurisdiction_id    INTEGER NOT NULL REFERENCES jurisdictions(id),
    code               TEXT NOT NULL,
    title              TEXT NOT NULL,
    version_no         TEXT NOT NULL,
    status             TEXT NOT NULL,
    publisher          TEXT NOT NULL,
    effective_at       TEXT,
    published_at       TEXT,
    superseded_by_id   INTEGER REFERENCES standards(id),
    content_hash       TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (jurisdiction_id, code, version_no)
);

CREATE TABLE IF NOT EXISTS units (
    id           INTEGER PRIMARY KEY,
    standard_id  INTEGER NOT NULL REFERENCES standards(id),
    code         TEXT NOT NULL,
    title        TEXT NOT NULL,
    level_label  TEXT NOT NULL,
    scope        TEXT NOT NULL,
    ordinal      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (standard_id, code)
);

CREATE TABLE IF NOT EXISTS evidence_requirements (
    id          INTEGER PRIMARY KEY,
    unit_id     INTEGER NOT NULL REFERENCES units(id),
    code        TEXT NOT NULL,
    title       TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    requirement TEXT NOT NULL,
    ordinal     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (unit_id, code)
);

CREATE TABLE IF NOT EXISTS prerequisites (
    id                   INTEGER PRIMARY KEY,
    unit_id              INTEGER NOT NULL REFERENCES units(id),
    prerequisite_unit_id INTEGER NOT NULL REFERENCES units(id),
    relation_kind        TEXT NOT NULL,
    UNIQUE (unit_id, prerequisite_unit_id)
);

CREATE TABLE IF NOT EXISTS proposals (
    id                     INTEGER PRIMARY KEY,
    proposal_no            TEXT NOT NULL UNIQUE,
    title                  TEXT NOT NULL,
    source_standard_id     INTEGER NOT NULL REFERENCES standards(id),
    target_standard_id     INTEGER NOT NULL REFERENCES standards(id),
    status                 TEXT NOT NULL,
    creator                TEXT NOT NULL,
    based_on_publish_id    INTEGER REFERENCES publishes(id),
    revision_no            INTEGER NOT NULL DEFAULT 1,
    decision_seq           INTEGER NOT NULL DEFAULT 0,
    snapshot_hash          TEXT,
    created_at             TEXT NOT NULL,
    submitted_at           TEXT,
    current_publish_id     INTEGER REFERENCES publishes(id)
);

CREATE TABLE IF NOT EXISTS proposal_revisions (
    id            INTEGER PRIMARY KEY,
    proposal_id   INTEGER NOT NULL REFERENCES proposals(id),
    revision_no   INTEGER NOT NULL,
    snapshot_hash TEXT NOT NULL,
    change_note   TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (proposal_id, revision_no)
);

CREATE TABLE IF NOT EXISTS line_items (
    id               INTEGER PRIMARY KEY,
    proposal_id      INTEGER NOT NULL REFERENCES proposals(id),
    revision_no      INTEGER NOT NULL,
    source_unit_id   INTEGER NOT NULL REFERENCES units(id),
    target_unit_id   INTEGER REFERENCES units(id),
    decision         TEXT NOT NULL,
    rationale        TEXT NOT NULL DEFAULT '',
    ordinal          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (proposal_id, revision_no, source_unit_id)
);

CREATE TABLE IF NOT EXISTS item_gaps (
    id                 INTEGER PRIMARY KEY,
    line_item_id       INTEGER NOT NULL REFERENCES line_items(id),
    source_evidence_id INTEGER REFERENCES evidence_requirements(id),
    target_evidence_id INTEGER REFERENCES evidence_requirements(id),
    gap_kind           TEXT NOT NULL,
    description        TEXT NOT NULL,
    ordinal            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signoffs (
    id          INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    revision_no INTEGER NOT NULL,
    party       TEXT NOT NULL,
    signer      TEXT NOT NULL,
    signed_at   TEXT NOT NULL,
    UNIQUE (proposal_id, revision_no, party)
);

CREATE TABLE IF NOT EXISTS proposal_events (
    id           INTEGER PRIMARY KEY,
    proposal_id  INTEGER NOT NULL REFERENCES proposals(id),
    seq          INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    at           TEXT NOT NULL,
    UNIQUE (proposal_id, seq)
);

CREATE TABLE IF NOT EXISTS publishes (
    id              INTEGER PRIMARY KEY,
    publish_no      TEXT NOT NULL UNIQUE,
    proposal_id     INTEGER NOT NULL REFERENCES proposals(id),
    revision_no     INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'MAPPING',
    source_standard_id INTEGER NOT NULL REFERENCES standards(id),
    target_standard_id INTEGER NOT NULL REFERENCES standards(id),
    mapping_hash    TEXT NOT NULL,
    prev_publish_id INTEGER REFERENCES publishes(id),
    published_by    TEXT NOT NULL,
    released_at     TEXT NOT NULL,
    superseded_at   TEXT
);

CREATE TABLE IF NOT EXISTS publish_lines (
    id             INTEGER PRIMARY KEY,
    publish_id     INTEGER NOT NULL REFERENCES publishes(id),
    source_unit_id INTEGER NOT NULL REFERENCES units(id),
    target_unit_id INTEGER REFERENCES units(id),
    decision       TEXT NOT NULL,
    rationale      TEXT NOT NULL DEFAULT '',
    ordinal        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS publish_line_gaps (
    id                 INTEGER PRIMARY KEY,
    publish_line_id    INTEGER NOT NULL REFERENCES publish_lines(id),
    source_evidence_id INTEGER REFERENCES evidence_requirements(id),
    target_evidence_id INTEGER REFERENCES evidence_requirements(id),
    gap_kind           TEXT NOT NULL,
    description        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    id                  INTEGER PRIMARY KEY,
    certificate_no      TEXT NOT NULL UNIQUE,
    holder              TEXT NOT NULL,
    source_standard_id  INTEGER NOT NULL REFERENCES standards(id),
    grade_label         TEXT NOT NULL,
    publish_id          INTEGER NOT NULL REFERENCES publishes(id),
    mapping_hash_at_issue TEXT NOT NULL,
    issuer              TEXT NOT NULL,
    issued_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_standard ON units(standard_id);
CREATE INDEX IF NOT EXISTS idx_evidence_unit ON evidence_requirements(unit_id);
CREATE INDEX IF NOT EXISTS idx_prereq_unit ON prerequisites(unit_id);
CREATE INDEX IF NOT EXISTS idx_lines_proposal_rev ON line_items(proposal_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_publish_lines ON publish_lines(publish_id);
CREATE INDEX IF NOT EXISTS idx_publishes_pair ON publishes(source_standard_id, target_standard_id, kind);
CREATE INDEX IF NOT EXISTS idx_signoffs ON signoffs(proposal_id, revision_no);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False：HTTP 层为多线程，写操作由 SkillMapApp 的写锁串行化
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
