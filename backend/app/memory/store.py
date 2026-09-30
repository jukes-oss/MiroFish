"""SQLite memory for one local graph.

This file is not the OASIS simulation database and not the tweet run ledger.
Nodes, edges, and episodes use this module's records, not Zep SDK types.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .segment import SEGMENT_VERSION, search_tokens, segment, segmented_text

SCHEMA_VERSION = "1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS graphs (
    graph_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT,
    schema_version TEXT NOT NULL,
    segment_version TEXT NOT NULL,
    ontology_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES graphs(graph_id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES graphs(graph_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    repair_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES graphs(graph_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    normalized_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    source_document_id TEXT,
    source_chunk_id TEXT,
    span_start INTEGER,
    span_end INTEGER,
    span_text TEXT,
    created_at TEXT NOT NULL,
    valid_at TEXT,
    invalid_at TEXT,
    schema_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES graphs(graph_id) ON DELETE CASCADE,
    source_node_id TEXT,
    target_node_id TEXT,
    name TEXT NOT NULL,
    fact TEXT NOT NULL,
    source_name TEXT,
    target_name TEXT,
    source_document_id TEXT,
    source_chunk_id TEXT,
    span_start INTEGER,
    span_end INTEGER,
    span_text TEXT,
    created_at TEXT NOT NULL,
    valid_at TEXT,
    invalid_at TEXT,
    schema_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES graphs(graph_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    source_document_id TEXT,
    source_chunk_id TEXT,
    span_start INTEGER,
    span_end INTEGER,
    span_text TEXT,
    created_at TEXT NOT NULL,
    valid_at TEXT,
    invalid_at TEXT,
    schema_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_jobs (
    job_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES graphs(graph_id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    raw_text TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_segments (
    graph_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    token_folded TEXT NOT NULL,
    original_token TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    segment_version TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    record_kind UNINDEXED,
    record_id UNINDEXED,
    graph_id UNINDEXED,
    segmented,
    segment_version UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 0'
);
"""


def memory_db_path() -> Path:
    override = os.environ.get("MEMORY_STATE_DB", "").strip()
    if override:
        return Path(override)
    from ..config import Config

    return Path(Config.UPLOAD_FOLDER) / "memory.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: str) -> str:
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_name(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def normalize_type(value: str) -> str:
    return "".join(str(value or "").strip().split()).casefold()


def _optional_time(value) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


class MemoryStore:
    def __init__(self, path: Path | None = None):
        self.path = path or memory_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def create_graph(self, *, name: str, project_id: str | None = None, graph_id: str | None = None) -> str:
        chosen = graph_id or f"mem_{uuid.uuid4().hex[:16]}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO graphs (
                    graph_id, name, project_id, schema_version, segment_version,
                    ontology_json, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (chosen, name, project_id, SCHEMA_VERSION, SEGMENT_VERSION, _now()),
            )
        return chosen

    def has_graph(self, graph_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT graph_id FROM graphs WHERE graph_id = ?",
                (graph_id,),
            ).fetchone()
        return row is not None

    def set_ontology(self, graph_id: str, ontology: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE graphs SET ontology_json = ? WHERE graph_id = ?",
                (json.dumps(ontology, ensure_ascii=False), graph_id),
            )

    def ontology(self, graph_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT ontology_json FROM graphs WHERE graph_id = ?",
                (graph_id,),
            ).fetchone()
        if row is None or not row["ontology_json"]:
            return None
        loaded = json.loads(row["ontology_json"])
        return loaded if isinstance(loaded, dict) else None

    def ingest(
        self,
        graph_id: str,
        text: str,
        *,
        source_name: str = "document",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
    ) -> dict:
        """Store the original text and chunks. Repeating the same text is a no-op."""

        if not self.has_graph(graph_id):
            raise KeyError(graph_id)
        document_id = _digest(graph_id, source_name, text)
        pieces = _chunks(text, chunk_size, chunk_overlap)
        created = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO documents (document_id, graph_id, source_name, text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (document_id, graph_id, source_name, text, created),
            )
            for ordinal, (start, end, piece) in enumerate(pieces):
                chunk_id = _digest(document_id, str(start), str(end), piece)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO chunks (
                        chunk_id, graph_id, document_id, ordinal, text, start_offset,
                        end_offset, status, error, repair_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, 0, ?)
                    """,
                    (chunk_id, graph_id, document_id, ordinal, piece, start, end, created),
                )
                self._index(conn, graph_id, "chunk", chunk_id, piece, start)
                self._append_episode_row(
                    conn,
                    graph_id=graph_id,
                    kind="source",
                    text=piece,
                    source_document_id=document_id,
                    source_chunk_id=chunk_id,
                    span_start=start,
                    span_end=end,
                    span_text=piece,
                    valid_at=None,
                    invalid_at=None,
                )
        return {"document_id": document_id, "chunk_count": len(pieces)}

    def chunks(self, graph_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        query = "SELECT * FROM chunks WHERE graph_id = ?"
        params: list = [graph_id]
        if statuses:
            query += f" AND status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        query += " ORDER BY ordinal, chunk_id"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_chunk(self, chunk_id: str, *, status: str, error: str | None, repair_count: int | None = None) -> None:
        with self.connect() as conn:
            current = conn.execute(
                "SELECT text FROM chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
            if current is None:
                raise KeyError(chunk_id)
            if repair_count is None:
                conn.execute(
                    "UPDATE chunks SET status = ?, error = ? WHERE chunk_id = ?",
                    (status, error, chunk_id),
                )
            else:
                conn.execute(
                    "UPDATE chunks SET status = ?, error = ?, repair_count = ? WHERE chunk_id = ?",
                    (status, error, repair_count, chunk_id),
                )
            stored = conn.execute(
                "SELECT text FROM chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
            if stored["text"] != current["text"]:
                raise RuntimeError("分块原文不能被抽取改写。")

    def add_job(self, graph_id: str, chunk_id: str, *, attempt_no: int, status: str, error: str | None, raw_text: str | None) -> str:
        job_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO extraction_jobs (
                    job_id, graph_id, chunk_id, attempt_no, status, error, raw_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, graph_id, chunk_id, attempt_no, status, error, raw_text, _now()),
            )
        return job_id

    def jobs(self, chunk_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM extraction_jobs WHERE chunk_id = ? ORDER BY attempt_no, created_at",
                (chunk_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def commit_records(self, graph_id: str, chunk: dict, payload: dict) -> dict:
        """Write nodes and edges. Same normalized name and type with a different
        attribute set stays as another candidate. Model merge fields are ignored.
        """

        document_id = chunk["document_id"]
        chunk_id = chunk["chunk_id"]
        base = int(chunk["start_offset"])
        nodes_in = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
        edges_in = payload.get("edges") if isinstance(payload.get("edges"), list) else []
        written_nodes = []
        written_edges = []
        with self.connect() as conn:
            for item in nodes_in:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                entity_type = str(item.get("type") or item.get("entity_type") or "").strip()
                if not name or not entity_type:
                    continue
                attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                attributes = {
                    key: value
                    for key, value in attributes.items()
                    if key not in {"same_as", "merge", "merged_into", "duplicate_of"}
                }
                summary = str(item.get("summary") or "")
                span = _span_from(item.get("span"), chunk["text"], base)
                fingerprint = json.dumps(attributes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                norm_name = normalize_name(name)
                norm_type = normalize_type(entity_type)
                node_id = _digest(graph_id, norm_name, norm_type, fingerprint)
                created = _now()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO nodes (
                        node_id, graph_id, name, entity_type, normalized_name, normalized_type,
                        summary, attributes_json, source_document_id, source_chunk_id,
                        span_start, span_end, span_text, created_at, valid_at, invalid_at,
                        schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id, graph_id, name, entity_type, norm_name, norm_type, summary,
                        json.dumps(attributes, ensure_ascii=False), document_id, chunk_id,
                        span[0], span[1], span[2], created, _optional_time(item.get("valid_at")),
                        _optional_time(item.get("invalid_at")), SCHEMA_VERSION,
                    ),
                )
                self._index(conn, graph_id, "node", node_id, f"{name}\n{summary}", 0)
                written_nodes.append(node_id)
            for item in edges_in:
                if not isinstance(item, dict):
                    continue
                relation = str(item.get("name") or item.get("type") or "").strip()
                fact = str(item.get("fact") or "").strip()
                source_name = str(item.get("source_name") or "").strip()
                target_name = str(item.get("target_name") or "").strip()
                source_type = normalize_type(str(item.get("source_type") or ""))
                target_type = normalize_type(str(item.get("target_type") or ""))
                if not relation or not fact or not source_name or not target_name:
                    continue
                sources = self._candidate_ids(conn, graph_id, source_name, source_type)
                targets = self._candidate_ids(conn, graph_id, target_name, target_type)
                if not sources:
                    sources = [None]
                if not targets:
                    targets = [None]
                span = _span_from(item.get("span"), chunk["text"], base)
                for source_id in sources:
                    for target_id in targets:
                        edge_id = _digest(
                            graph_id,
                            source_id or source_name,
                            target_id or target_name,
                            normalize_name(relation),
                            fact,
                        )
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO edges (
                                edge_id, graph_id, source_node_id, target_node_id, name, fact,
                                source_name, target_name, source_document_id, source_chunk_id,
                                span_start, span_end, span_text, created_at, valid_at, invalid_at,
                                schema_version
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                edge_id, graph_id, source_id, target_id, relation, fact,
                                source_name, target_name, document_id, chunk_id,
                                span[0], span[1], span[2], _now(),
                                _optional_time(item.get("valid_at")),
                                _optional_time(item.get("invalid_at")),
                                SCHEMA_VERSION,
                            ),
                        )
                        self._index(conn, graph_id, "edge", edge_id, fact, 0)
                        written_edges.append(edge_id)
        return {"nodes": written_nodes, "edges": written_edges}

    def _candidate_ids(self, conn, graph_id: str, name: str, entity_type: str) -> list[str]:
        rows = conn.execute(
            """
            SELECT node_id FROM nodes
            WHERE graph_id = ? AND normalized_name = ? AND normalized_type = ?
            ORDER BY node_id
            """,
            (graph_id, normalize_name(name), entity_type),
        ).fetchall()
        return [row["node_id"] for row in rows]

    def read_nodes(self, graph_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE graph_id = ? ORDER BY name, node_id",
                (graph_id,),
            ).fetchall()
        return [_public_node(row) for row in rows]

    def read_edges(self, graph_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM edges WHERE graph_id = ? ORDER BY edge_id",
                (graph_id,),
            ).fetchall()
        return [_public_edge(row) for row in rows]

    def append_episode(
        self,
        graph_id: str,
        *,
        kind: str,
        text: str,
        source_document_id: str | None = None,
        source_chunk_id: str | None = None,
        span_start: int | None = None,
        span_end: int | None = None,
        span_text: str | None = None,
        valid_at: str | None = None,
        invalid_at: str | None = None,
    ) -> str:
        if not self.has_graph(graph_id):
            raise KeyError(graph_id)
        with self.connect() as conn:
            return self._append_episode_row(
                conn,
                graph_id=graph_id,
                kind=kind,
                text=text,
                source_document_id=source_document_id,
                source_chunk_id=source_chunk_id,
                span_start=span_start,
                span_end=span_end,
                span_text=span_text,
                valid_at=_optional_time(valid_at),
                invalid_at=_optional_time(invalid_at),
            )

    def _append_episode_row(self, conn, **fields) -> str:
        episode_id = _digest(
            fields["graph_id"],
            fields["kind"],
            fields["text"],
            str(fields.get("span_start")),
            str(fields.get("span_end")),
            fields.get("valid_at") or "",
            fields.get("invalid_at") or "",
        )
        before = conn.execute(
            "SELECT created_at, text FROM episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT OR IGNORE INTO episodes (
                episode_id, graph_id, kind, text, source_document_id, source_chunk_id,
                span_start, span_end, span_text, created_at, valid_at, invalid_at,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id, fields["graph_id"], fields["kind"], fields["text"],
                fields.get("source_document_id"), fields.get("source_chunk_id"),
                fields.get("span_start"), fields.get("span_end"), fields.get("span_text"),
                _now(), fields.get("valid_at"), fields.get("invalid_at"), SCHEMA_VERSION,
            ),
        )
        if before is not None:
            after = conn.execute(
                "SELECT created_at, text FROM episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if after["text"] != before["text"] or after["created_at"] != before["created_at"]:
                raise RuntimeError("事件只追加，不能改写已有记录。")
        self._index(conn, fields["graph_id"], "episode", episode_id, fields["text"], fields.get("span_start") or 0)
        return episode_id

    def read_episodes(self, graph_id: str, kind: str | None = None) -> list[dict]:
        query = "SELECT * FROM episodes WHERE graph_id = ?"
        params: list = [graph_id]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at, episode_id"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def search(self, graph_id: str, query: str, *, limit: int = 20) -> list[dict]:
        unique = search_tokens(query)
        if not unique:
            return []
        match = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in unique)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT record_kind, record_id FROM search_fts
                WHERE search_fts MATCH ? AND graph_id = ? AND segment_version = ?
                LIMIT ?
                """,
                (match, graph_id, SEGMENT_VERSION, limit),
            ).fetchall()
            hits = []
            for row in rows:
                original = self._original(conn, row["record_kind"], row["record_id"])
                if original is None:
                    continue
                offsets = conn.execute(
                    """
                    SELECT token_folded, original_token, start_offset, end_offset
                    FROM search_segments
                    WHERE graph_id = ? AND record_kind = ? AND record_id = ?
                      AND segment_version = ? AND token_folded IN ({})
                    ORDER BY start_offset
                    """.format(",".join("?" for _ in unique)),
                    (graph_id, row["record_kind"], row["record_id"], SEGMENT_VERSION, *unique),
                ).fetchall()
                hits.append({
                    "record_kind": row["record_kind"],
                    "record_id": row["record_id"],
                    "text": original,
                    "offsets": [
                        {
                            "token": item["original_token"],
                            "folded": item["token_folded"],
                            "start": item["start_offset"],
                            "end": item["end_offset"],
                        }
                        for item in offsets
                    ],
                    "segment_version": SEGMENT_VERSION,
                })
        return hits

    def _original(self, conn, kind: str, record_id: str) -> str | None:
        if kind == "chunk":
            row = conn.execute("SELECT text FROM chunks WHERE chunk_id = ?", (record_id,)).fetchone()
        elif kind == "node":
            row = conn.execute("SELECT name || char(10) || summary AS text FROM nodes WHERE node_id = ?", (record_id,)).fetchone()
        elif kind == "edge":
            row = conn.execute("SELECT fact AS text FROM edges WHERE edge_id = ?", (record_id,)).fetchone()
        elif kind == "episode":
            row = conn.execute("SELECT text FROM episodes WHERE episode_id = ?", (record_id,)).fetchone()
        else:
            return None
        if row is None:
            return None
        return row["text"]

    def _index(self, conn, graph_id: str, kind: str, record_id: str, text: str, origin: int) -> None:
        conn.execute(
            "DELETE FROM search_segments WHERE graph_id = ? AND record_kind = ? AND record_id = ?",
            (graph_id, kind, record_id),
        )
        conn.execute(
            "DELETE FROM search_fts WHERE record_kind = ? AND record_id = ?",
            (kind, record_id),
        )
        tokens = segment(text)
        for token in tokens:
            if not token["folded"]:
                continue
            conn.execute(
                """
                INSERT INTO search_segments (
                    graph_id, record_kind, record_id, token_folded, original_token,
                    start_offset, end_offset, segment_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id, kind, record_id, token["folded"], token["token"],
                    origin + token["start"], origin + token["end"], SEGMENT_VERSION,
                ),
            )
        conn.execute(
            """
            INSERT INTO search_fts (record_kind, record_id, graph_id, segmented, segment_version)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kind, record_id, graph_id, segmented_text(text), SEGMENT_VERSION),
        )

    def delete_graph(self, graph_id: str) -> bool:
        """Delete one graph. Other graphs in this file stay."""

        with self.connect() as conn:
            row = conn.execute("SELECT graph_id FROM graphs WHERE graph_id = ?", (graph_id,)).fetchone()
            if row is None:
                return False
            ids = []
            for kind, column, table in (
                ("chunk", "chunk_id", "chunks"),
                ("node", "node_id", "nodes"),
                ("edge", "edge_id", "edges"),
                ("episode", "episode_id", "episodes"),
            ):
                found = conn.execute(
                    f"SELECT {column} AS record_id FROM {table} WHERE graph_id = ?",
                    (graph_id,),
                ).fetchall()
                ids.extend((kind, item["record_id"]) for item in found)
            for kind, record_id in ids:
                conn.execute(
                    "DELETE FROM search_fts WHERE record_kind = ? AND record_id = ?",
                    (kind, record_id),
                )
            conn.execute("DELETE FROM search_segments WHERE graph_id = ?", (graph_id,))
            conn.execute("DELETE FROM graphs WHERE graph_id = ?", (graph_id,))
        return True

    def export_graph(self, graph_id: str) -> dict:
        if not self.has_graph(graph_id):
            raise KeyError(graph_id)
        with self.connect() as conn:
            graph = dict(conn.execute("SELECT * FROM graphs WHERE graph_id = ?", (graph_id,)).fetchone())
            documents = [dict(row) for row in conn.execute("SELECT * FROM documents WHERE graph_id = ?", (graph_id,))]
            chunks = [dict(row) for row in conn.execute(
                "SELECT * FROM chunks WHERE graph_id = ? ORDER BY ordinal",
                (graph_id,),
            )]
            jobs = [dict(row) for row in conn.execute(
                "SELECT * FROM extraction_jobs WHERE graph_id = ? ORDER BY created_at",
                (graph_id,),
            )]
        nodes = self.read_nodes(graph_id)
        edges = self.read_edges(graph_id)
        episodes = self.read_episodes(graph_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "segment_version": SEGMENT_VERSION,
            "graph": graph,
            "documents": documents,
            "chunks": chunks,
            "nodes": nodes,
            "edges": edges,
            "episodes": episodes,
            "extraction_jobs": jobs,
            "search_equivalence": False,
        }

    def graph_view(self, graph_id: str) -> dict:
        """A JSON shape close to the old graph payload.

        Matching these keys is not the same as Zep search quality.
        """

        nodes = self.read_nodes(graph_id)
        edges = self.read_edges(graph_id)
        names = {item["node_id"]: item["name"] for item in nodes}
        return {
            "graph_id": graph_id,
            "schema_version": SCHEMA_VERSION,
            "segment_version": SEGMENT_VERSION,
            "search_equivalence": False,
            "search_note": (
                "返回的 JSON 形状接近原图谱接口，检索质量并不等价。"
                "本地检索使用版本化分词索引和 FTS5，不是 Zep 的检索。"
                "unicode61 本身不会给中文分词。"
            ),
            "nodes": [
                {
                    "uuid": item["node_id"],
                    "name": item["name"],
                    "labels": [item["entity_type"], "Entity"],
                    "summary": item["summary"],
                    "attributes": item["attributes"],
                    "created_at": item["created_at"],
                    "valid_at": item["valid_at"],
                    "invalid_at": item["invalid_at"],
                    "source_span": item["source_span"],
                    "schema_version": item["schema_version"],
                }
                for item in nodes
            ],
            "edges": [
                {
                    "uuid": item["edge_id"],
                    "name": item["name"],
                    "fact": item["fact"],
                    "fact_type": item["name"],
                    "source_node_uuid": item["source_node_id"],
                    "target_node_uuid": item["target_node_id"],
                    "source_node_name": names.get(item["source_node_id"] or "", item["source_name"]),
                    "target_node_name": names.get(item["target_node_id"] or "", item["target_name"]),
                    "created_at": item["created_at"],
                    "valid_at": item["valid_at"],
                    "invalid_at": item["invalid_at"],
                    "source_span": item["source_span"],
                    "schema_version": item["schema_version"],
                }
                for item in edges
            ],
            "node_count": len(nodes),
            "edge_count": len(edges),
        }


def _chunks(text: str, chunk_size: int, overlap: int) -> list[tuple[int, int, str]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须是正整数。")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须满足 0 <= overlap < chunk_size。")
    if text == "":
        return [(0, 0, "")]
    pieces = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + chunk_size)
        pieces.append((start, end, text[start:end]))
        if end == length:
            break
        start = end - overlap
    return pieces


def _span_from(value, chunk_text: str, base: int) -> tuple[int | None, int | None, str | None]:
    if not isinstance(value, dict):
        return None, None, None
    start = value.get("start")
    end = value.get("end")
    snippet = value.get("text")
    if not isinstance(start, int) or not isinstance(end, int):
        return None, None, None
    if not (0 <= start < end <= len(chunk_text)):
        return None, None, None
    if chunk_text[start:end] != snippet:
        return None, None, None
    return base + start, base + end, snippet


def _public_node(row) -> dict:
    return {
        "node_id": row["node_id"],
        "graph_id": row["graph_id"],
        "name": row["name"],
        "entity_type": row["entity_type"],
        "normalized_name": row["normalized_name"],
        "normalized_type": row["normalized_type"],
        "summary": row["summary"],
        "attributes": json.loads(row["attributes_json"] or "{}"),
        "source_span": {
            "document_id": row["source_document_id"],
            "chunk_id": row["source_chunk_id"],
            "start": row["span_start"],
            "end": row["span_end"],
            "text": row["span_text"],
        },
        "created_at": row["created_at"],
        "valid_at": row["valid_at"],
        "invalid_at": row["invalid_at"],
        "schema_version": row["schema_version"],
    }


def _public_edge(row) -> dict:
    return {
        "edge_id": row["edge_id"],
        "graph_id": row["graph_id"],
        "source_node_id": row["source_node_id"],
        "target_node_id": row["target_node_id"],
        "name": row["name"],
        "fact": row["fact"],
        "source_name": row["source_name"],
        "target_name": row["target_name"],
        "source_span": {
            "document_id": row["source_document_id"],
            "chunk_id": row["source_chunk_id"],
            "start": row["span_start"],
            "end": row["span_end"],
            "text": row["span_text"],
        },
        "created_at": row["created_at"],
        "valid_at": row["valid_at"],
        "invalid_at": row["invalid_at"],
        "schema_version": row["schema_version"],
    }
