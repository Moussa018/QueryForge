"""Outils de l'agent d'enquete : metriques, comparaisons, ventilations.

Le modele ne fournit jamais de SQL brut ici : il decrit une *metrique*
(table, agregat, colonne, colonne de date) et le code construit la requete.
Chaque identifiant est verifie contre le schema reel puis cite ; la requete
construite passe ensuite dans `pipeline.run_sql` (AST, bornage,
READ ONLY) comme n'importe quelle autre.

Les variations, pourcentages et contributions sont calcules ici, en Python :
le modele interprete des chiffres, il ne les calcule pas.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable

from app.pipeline import SqlRun
from app.schema.introspector import DatabaseSchema, Table

_AGGREGATES = {"sum", "avg", "count", "count_distinct"}
_MAX_BREAKDOWN_ROWS = 25


class ToolError(Exception):
    """Argument invalide ou requete refusee. Le message est renvoye a l'agent."""


@dataclass(frozen=True)
class Metric:
    table: str
    aggregate: str  # sum | avg | count | count_distinct
    date_column: str
    column: str | None = None  # None autorise pour count
    filters: tuple[tuple[str, str], ...] = ()  # (colonne, valeur) en egalite

    def label(self) -> str:
        target = self.column or "*"
        base = f"{self.aggregate}({self.table}.{target})"
        if self.filters:
            base += " [" + ", ".join(f"{c}={v}" for c, v in self.filters) + "]"
        return base


@dataclass
class ToolOutput:
    """Resultat d'un outil : donnees calculees par le code + SQL pour l'annexe."""

    tool: str
    summary: dict[str, Any]
    sql: str
    notes: list[str] = field(default_factory=list)


SqlRunner = Callable[[str], SqlRun]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _find_table(schema: DatabaseSchema, name: str) -> Table:
    wanted = name.lower()
    for table in schema.tables:
        if wanted in (table.name.lower(), table.qualified_name.lower()):
            return table
    raise ToolError(f"Table inconnue : {name!r}.")


def _check_column(table: Table, column: str) -> str:
    for col in table.columns:
        if col.name.lower() == column.lower():
            return col.name
    raise ToolError(f"Colonne inconnue : {table.name}.{column}.")


def _parse_date(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as err:
        raise ToolError(f"{label} doit etre une date AAAA-MM-JJ (recu {value!r}).") from err


def _period(start: str, end: str, label: str) -> tuple[dt.date, dt.date]:
    s, e = _parse_date(start, f"{label}.debut"), _parse_date(end, f"{label}.fin")
    if e <= s:
        raise ToolError(f"{label} : la fin (exclue) doit etre posterieure au debut.")
    return s, e


def _aggregate_sql(metric: Metric, table: Table, alias: str) -> str:
    if metric.aggregate not in _AGGREGATES:
        raise ToolError(f"Agregat inconnu : {metric.aggregate!r} (attendu : {sorted(_AGGREGATES)}).")
    if metric.aggregate == "count" and metric.column is None:
        return f"COUNT(*) FILTER (WHERE {{cond}}) AS {alias}"
    if metric.column is None:
        raise ToolError(f"L'agregat {metric.aggregate} exige une colonne.")
    col = f"{_quote(table.name)}.{_quote(_check_column(table, metric.column))}"
    func = {
        "sum": f"SUM({col})",
        "avg": f"AVG({col})",
        "count": f"COUNT({col})",
        "count_distinct": f"COUNT(DISTINCT {col})",
    }[metric.aggregate]
    # SUM/AVG FILTER ne vaut que pour la periode : on reporte la condition dans FILTER.
    return f"{func} FILTER (WHERE {{cond}}) AS {alias}"


def _period_cond(metric: Metric, table: Table, period: tuple[dt.date, dt.date]) -> str:
    date_col = f"{_quote(table.name)}.{_quote(_check_column(table, metric.date_column))}"
    return f"{date_col} >= DATE '{period[0].isoformat()}' AND {date_col} < DATE '{period[1].isoformat()}'"


def _where_filters(metric: Metric, table: Table) -> list[str]:
    clauses = []
    for column, value in metric.filters:
        col = f"{_quote(table.name)}.{_quote(_check_column(table, column))}"
        clauses.append(f"{col}::text = {_literal(str(value))}")
    return clauses


def build_compare_sql(
    schema: DatabaseSchema,
    metric: Metric,
    p1: tuple[dt.date, dt.date],
    p2: tuple[dt.date, dt.date],
) -> str:
    table = _find_table(schema, metric.table)
    c1, c2 = _period_cond(metric, table, p1), _period_cond(metric, table, p2)
    select = [
        _aggregate_sql(metric, table, "p1").replace("{cond}", c1),
        _aggregate_sql(metric, table, "p2").replace("{cond}", c2),
    ]
    where = [f"(({c1}) OR ({c2}))"] + _where_filters(metric, table)
    return f"SELECT {', '.join(select)} FROM {_quote(table.name)} WHERE {' AND '.join(where)}"


def _resolve_dimension(schema: DatabaseSchema, base: Table, dimension: str) -> tuple[str, str]:
    """Renvoie (expression SQL, clause JOIN eventuelle).

    `colonne` -> colonne de la table de base ; `table.colonne` -> colonne d'une
    table atteinte par une cle etrangere directe depuis la table de base.
    """
    if "." not in dimension:
        col = _check_column(base, dimension)
        return f"{_quote(base.name)}.{_quote(col)}", ""

    target_name, column = dimension.rsplit(".", 1)
    target = _find_table(schema, target_name)
    if target.name == base.name:
        col = _check_column(base, column)
        return f"{_quote(base.name)}.{_quote(col)}", ""

    for fk in base.foreign_keys:
        if fk.target_table.lower() in (target.qualified_name.lower(), target.name.lower()):
            col = _check_column(target, column)
            on = " AND ".join(
                f"{_quote(base.name)}.{_quote(src)} = {_quote(target.name)}.{_quote(dst)}"
                for src, dst in zip(fk.columns, fk.target_columns)
            )
            return (
                f"{_quote(target.name)}.{_quote(col)}",
                f"JOIN {_quote(target.name)} ON {on}",
            )
    raise ToolError(
        f"Aucune cle etrangere de {base.name} vers {target.name} : "
        "choisissez une dimension de la table elle-meme ou d'une table directement liee."
    )


def build_breakdown_sql(
    schema: DatabaseSchema,
    metric: Metric,
    dimension: str,
    p1: tuple[dt.date, dt.date],
    p2: tuple[dt.date, dt.date],
) -> str:
    table = _find_table(schema, metric.table)
    dim_expr, join = _resolve_dimension(schema, table, dimension)
    c1, c2 = _period_cond(metric, table, p1), _period_cond(metric, table, p2)
    select = [
        f"{dim_expr}::text AS dimension",
        _aggregate_sql(metric, table, "p1").replace("{cond}", c1),
        _aggregate_sql(metric, table, "p2").replace("{cond}", c2),
    ]
    where = [f"(({c1}) OR ({c2}))"] + _where_filters(metric, table)
    # LIMIT : le tri par contribution se fait en Python, mais on borne le volume.
    return (
        f"SELECT {', '.join(select)} FROM {_quote(table.name)} {join} "
        f"WHERE {' AND '.join(where)} GROUP BY 1 LIMIT 500"
    )


# -- calculs deterministes ----------------------------------------------------

def _num(value: Any) -> float:
    return float(value) if value is not None else 0.0


def variation(before: float, after: float) -> dict[str, float | None]:
    delta = after - before
    pct = (delta / before * 100.0) if before else None
    return {"p1": before, "p2": after, "delta": delta, "delta_pct": pct}


def contributions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Part de chaque segment dans la variation totale, triee par ampleur.

    `share_of_change` peut depasser 100 % ou etre negative quand des segments
    evoluent en sens oppose : c'est voulu, c'est precisement l'information utile.
    """
    enriched = []
    for row in rows:
        v = variation(_num(row.get("p1")), _num(row.get("p2")))
        enriched.append({"dimension": row.get("dimension"), **v})
    total_delta = sum(r["delta"] for r in enriched)
    for r in enriched:
        r["share_of_change"] = (r["delta"] / total_delta * 100.0) if total_delta else None
    enriched.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return enriched


# -- outils exposes a l'agent ---------------------------------------------------

class InvestigationTools:
    def __init__(self, schema_provider: Callable[[], DatabaseSchema], run_sql: SqlRunner) -> None:
        self._schema = schema_provider
        self._run_sql = run_sql

    def _run(self, sql: str) -> SqlRun:
        try:
            return self._run_sql(sql)
        except ToolError:
            raise
        except Exception as err:
            from app.pipeline import _clean_db_error
            from app.security.guard import SQLValidationError

            if isinstance(err, SQLValidationError):
                raise ToolError(str(err)) from err
            raise ToolError(_clean_db_error(err)) from err

    def compare_periods(
        self, metric: Metric, p1: tuple[str, str], p2: tuple[str, str]
    ) -> ToolOutput:
        period1, period2 = _period(*p1, "p1"), _period(*p2, "p2")
        sql = build_compare_sql(self._schema(), metric, period1, period2)
        run = self._run(sql)
        row = run.result.rows[0] if run.result.rows else {}
        summary = {
            "metric": metric.label(),
            "p1_range": list(p1),
            "p2_range": list(p2),
            **variation(_num(row.get("p1")), _num(row.get("p2"))),
        }
        return ToolOutput("compare_periods", summary, run.sql, run.notes)

    def breakdown(
        self, metric: Metric, dimension: str, p1: tuple[str, str], p2: tuple[str, str]
    ) -> ToolOutput:
        period1, period2 = _period(*p1, "p1"), _period(*p2, "p2")
        sql = build_breakdown_sql(self._schema(), metric, dimension, period1, period2)
        run = self._run(sql)
        ranked = contributions(run.result.rows)
        total = variation(
            sum(r["p1"] for r in ranked),
            sum(r["p2"] for r in ranked),
        )
        summary = {
            "metric": metric.label(),
            "dimension": dimension,
            "p1_range": list(p1),
            "p2_range": list(p2),
            "total": total,
            "segments": ranked[:_MAX_BREAKDOWN_ROWS],
            "segments_omitted": max(0, len(ranked) - _MAX_BREAKDOWN_ROWS),
        }
        return ToolOutput("breakdown", summary, run.sql, run.notes)
