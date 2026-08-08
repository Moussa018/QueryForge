"""Introspection dynamique du schema.

Le modele ne connait rien de la base a l'avance : on lit le catalogue Postgres a
chaud via SQLAlchemy, on en tire une representation compacte (DDL + relations +
valeurs d'exemple) et on la met en cache avec un TTL court.

La representation est volontairement dense : chaque token depense ici est un
token en moins pour le raisonnement du modele.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import Engine, inspect, text

from app.config import Settings

# Types pour lesquels echantillonner des valeurs distinctes a du sens : le modele
# a besoin de savoir que status vaut 'shipped'/'pending' et pas 'SHIPPED'/'PEND'.
_SAMPLEABLE_TYPES = ("VARCHAR", "TEXT", "CHAR", "ENUM", "NAME")
_MAX_DISTINCT_FOR_SAMPLING = 50


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    primary_key: bool = False
    comment: str | None = None
    sample_values: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    columns: list[str]
    target_table: str
    target_columns: list[str]


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey]
    comment: str | None = None
    approx_rows: int | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class DatabaseSchema:
    tables: list[Table]
    dialect: str
    captured_at: float

    def render_for_prompt(self) -> str:
        """Rend le schema en pseudo-DDL compact destine au prompt du LLM."""
        blocks: list[str] = []
        for table in self.tables:
            header = f"TABLE {table.qualified_name}"
            if table.approx_rows is not None:
                header += f"  -- ~{table.approx_rows:,} lignes".replace(",", " ")
            if table.comment:
                header += f"  -- {table.comment}"
            lines = [header + " ("]

            for col in table.columns:
                parts = [f"  {col.name} {col.type}"]
                if col.primary_key:
                    parts.append("PK")
                if not col.nullable:
                    parts.append("NOT NULL")
                line = " ".join(parts)

                annotations: list[str] = []
                if col.comment:
                    annotations.append(col.comment)
                if col.sample_values:
                    rendered = ", ".join(col.sample_values)
                    annotations.append(f"valeurs: {rendered}")
                if annotations:
                    line += "  -- " + " | ".join(annotations)
                lines.append(line + ",")

            lines.append(")")
            for fk in table.foreign_keys:
                src = ", ".join(fk.columns)
                dst = ", ".join(fk.target_columns)
                lines.append(f"  FK ({src}) -> {fk.target_table}({dst})")
            blocks.append("\n".join(lines))

        return "\n\n".join(blocks)

    def table_names(self) -> set[str]:
        """Noms qualifies et non qualifies, pour la validation de securite."""
        names: set[str] = set()
        for table in self.tables:
            names.add(table.qualified_name.lower())
            names.add(table.name.lower())
        return names


class SchemaIntrospector:
    """Lit le schema reel et le met en cache. Thread-safe."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._cache: DatabaseSchema | None = None
        self._lock = threading.Lock()

    def get_schema(self, *, force_refresh: bool = False) -> DatabaseSchema:
        with self._lock:
            if not force_refresh and self._cache is not None:
                age = time.time() - self._cache.captured_at
                if age < self._settings.schema_cache_ttl_seconds:
                    return self._cache
            self._cache = self._introspect()
            return self._cache

    # -- interne ------------------------------------------------------------

    def _introspect(self) -> DatabaseSchema:
        inspector = inspect(self._engine)
        row_counts = self._approximate_row_counts()
        tables: list[Table] = []

        for schema_name in self._settings.included_schemas:
            if schema_name not in inspector.get_schema_names():
                continue

            for table_name in inspector.get_table_names(schema=schema_name):
                pk = inspector.get_pk_constraint(table_name, schema=schema_name)
                pk_columns = set(pk.get("constrained_columns") or [])

                columns: list[Column] = []
                for raw in inspector.get_columns(table_name, schema=schema_name):
                    column = Column(
                        name=raw["name"],
                        type=str(raw["type"]),
                        nullable=bool(raw.get("nullable", True)),
                        primary_key=raw["name"] in pk_columns,
                        comment=raw.get("comment"),
                    )
                    columns.append(column)

                foreign_keys = [
                    ForeignKey(
                        columns=list(fk["constrained_columns"]),
                        target_table=(
                            f"{fk.get('referred_schema') or schema_name}.{fk['referred_table']}"
                        ),
                        target_columns=list(fk["referred_columns"]),
                    )
                    for fk in inspector.get_foreign_keys(table_name, schema=schema_name)
                ]

                table = Table(
                    schema=schema_name,
                    name=table_name,
                    columns=columns,
                    foreign_keys=foreign_keys,
                    comment=inspector.get_table_comment(table_name, schema=schema_name).get("text"),
                    approx_rows=row_counts.get(f"{schema_name}.{table_name}"),
                )
                self._attach_sample_values(table)
                tables.append(table)

        return DatabaseSchema(
            tables=tables,
            dialect=self._engine.dialect.name,
            captured_at=time.time(),
        )

    def _approximate_row_counts(self) -> dict[str, int]:
        """Compte approximatif via pg_class : pas de COUNT(*) sur des tables enormes."""
        query = text(
            """
            SELECT n.nspname AS schema_name,
                   c.relname  AS table_name,
                   c.reltuples::bigint AS approx_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = ANY(:schemas)
            """
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    query, {"schemas": list(self._settings.included_schemas)}
                ).fetchall()
        except Exception:
            # Sur un dialecte non-Postgres ou sans droit de lecture du catalogue,
            # l'absence de statistiques n'est pas bloquante.
            return {}
        return {f"{r.schema_name}.{r.table_name}": max(int(r.approx_rows), 0) for r in rows}

    def _attach_sample_values(self, table: Table) -> None:
        """Echantillonne les colonnes texte a faible cardinalite.

        Sans ca, le modele invente des litteraux ('Shipped' au lieu de 'shipped')
        et la requete renvoie zero ligne sans erreur -- le pire des echecs.
        """
        limit = self._settings.sample_values_per_column
        if limit <= 0:
            return

        for column in table.columns:
            if column.primary_key:
                continue
            if not any(t in column.type.upper() for t in _SAMPLEABLE_TYPES):
                continue

            query = text(
                f"""
                SELECT val FROM (
                    SELECT DISTINCT "{column.name}"::text AS val
                    FROM "{table.schema}"."{table.name}"
                    WHERE "{column.name}" IS NOT NULL
                    LIMIT :probe
                ) s LIMIT :probe
                """
            )
            try:
                with self._engine.connect() as conn:
                    conn.execute(text(f"SET LOCAL statement_timeout = {2000}"))
                    values = [r.val for r in conn.execute(query, {"probe": _MAX_DISTINCT_FOR_SAMPLING})]
            except Exception:
                continue

            # Colonne a forte cardinalite (email, adresse...) : les exemples
            # n'apprennent rien au modele et polluent le prompt.
            if len(values) >= _MAX_DISTINCT_FOR_SAMPLING or not values:
                continue
            if len(values) > limit:
                continue

            column.sample_values = [v[:40] for v in sorted(values)]
