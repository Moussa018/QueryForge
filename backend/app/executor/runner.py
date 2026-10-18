"""Execution de la requete validee et mise en forme des resultats."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

from app.config import Settings


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    duration_ms: float
    notes: list[str] = field(default_factory=list)


def execute(conn: Connection, sql: str, settings: Settings) -> QueryResult:
    import time

    started = time.perf_counter()
    result = conn.execute(text(sql))
    columns = list(result.keys())

    # On lit une ligne de plus que le plafond : si elle existe, le resultat a ete
    # tronque et le rapport doit le dire plutot que de laisser croire a un total.
    fetched = result.fetchmany(settings.max_row_limit + 1)
    truncated = len(fetched) > settings.max_row_limit
    if truncated:
        fetched = fetched[: settings.max_row_limit]

    rows = [{col: _to_jsonable(value) for col, value in zip(columns, row)} for row in fetched]
    duration_ms = (time.perf_counter() - started) * 1000

    notes: list[str] = []
    if truncated:
        notes.append(
            f"Resultat tronque a {settings.max_row_limit} lignes."
        )

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        duration_ms=duration_ms,
        notes=notes,
    )


def _to_jsonable(value: Any) -> Any:
    """Convertit les types Postgres en equivalents serialisables en JSON.

    Decimal -> float est une perte de precision assumee : ces valeurs partent
    vers un JSON lu par un client, qui n'a de toute facon que des float64.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} octets>"
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return str(value)
