"""Postgres storage: cached model answers, the batches submitted in batch mode, and
extracted documents with their hand corrections."""

import json
import os
from typing import Any, Self

import psycopg2


class Database:
    """The Postgres database at DATABASE_URL.

        with Database() as database:
            database.put_answer(key, model, answer_json)
            database.get_answer(key)   # -> the answer as a dict, or None

    Entering connects and creates the tables if needed; leaving closes the connection.
    """

    def __enter__(self) -> Self:
        self.conn = psycopg2.connect(os.environ["DATABASE_URL"])
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS answers (
                    key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    answer JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    key TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    tokens JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # One row per PDF (by its SHA-256): only the pages sent to the model, so
            # they can be shown with their set boxes, the extracted result, and the
            # hand-corrected result with the paths of the fields that were changed.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    pdf_hash TEXT PRIMARY KEY,
                    filename TEXT,
                    page_numbers INTEGER[] NOT NULL,
                    pages_pdf BYTEA NOT NULL,
                    result JSONB NOT NULL,
                    corrected JSONB,
                    changes JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    corrected_at TIMESTAMPTZ
                )
                """
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.conn.close()

    def get_answer(self, key: str) -> Any | None:
        with self.conn, self.conn.cursor() as cur:
            cur.execute("SELECT answer FROM answers WHERE key = %s", (key,))
            row = cur.fetchone()
        return row[0] if row else None

    def put_answer(self, key: str, model: str, answer_json: str) -> None:
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO answers (key, model, answer) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (key, model, answer_json),
            )

    def get_batch(self, key: str) -> tuple[str, dict[str, int] | None] | None:
        """(batch id, tokens or None while uncollected), or None if never submitted."""
        with self.conn, self.conn.cursor() as cur:
            cur.execute("SELECT batch_id, tokens FROM batches WHERE key = %s", (key,))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def add_batch(self, key: str, batch_id: str) -> None:
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO batches (key, batch_id) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET batch_id = EXCLUDED.batch_id, "
                "tokens = NULL",
                (key, batch_id),
            )

    def finish_batch(self, key: str, tokens: dict[str, int]) -> None:
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "UPDATE batches SET tokens = %s WHERE key = %s",
                (json.dumps(tokens), key),
            )

    def put_document(
        self,
        pdf_hash: str,
        filename: str | None,
        page_numbers: list[int],
        pages_pdf: bytes,
        result_json: str,
    ) -> None:
        """Store a new extraction; a stored correction is kept."""
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (pdf_hash, filename, page_numbers, pages_pdf, "
                "result) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (pdf_hash) DO UPDATE "
                "SET filename = EXCLUDED.filename, page_numbers = EXCLUDED.page_numbers, "
                "pages_pdf = EXCLUDED.pages_pdf, result = EXCLUDED.result",
                (
                    pdf_hash,
                    filename,
                    page_numbers,
                    psycopg2.Binary(pages_pdf),
                    result_json,
                ),
            )

    def get_result(self, pdf_hash: str) -> tuple[Any, Any | None] | None:
        """(extracted result, corrected result or None), or None if not stored."""
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "SELECT result, corrected FROM documents WHERE pdf_hash = %s",
                (pdf_hash,),
            )
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def get_pages(self, pdf_hash: str) -> tuple[list[int], bytes] | None:
        """(the original page numbers, a PDF of just those pages), or None."""
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "SELECT page_numbers, pages_pdf FROM documents WHERE pdf_hash = %s",
                (pdf_hash,),
            )
            row = cur.fetchone()
        return (row[0], bytes(row[1])) if row else None

    def put_correction(
        self, pdf_hash: str, corrected_json: str, changes: list[str]
    ) -> None:
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET corrected = %s, changes = %s, corrected_at = now() "
                "WHERE pdf_hash = %s",
                (corrected_json, json.dumps(changes), pdf_hash),
            )
