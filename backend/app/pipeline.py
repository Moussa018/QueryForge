"""Execution securisee d'une requete SQL : le socle utilise par l'agent d'enquete.

    SQL (construit par les outils de l'agent)
       |
       v
  [1] validation AST contre le schema reel
       |
       v
  [2] bornage (LIMIT injecte ou plafonne)
       |
       v
  [3] execution en transaction READ ONLY, avec timeout
       |
       v
    resultat

Le modele ne fournit jamais de SQL : ce sont les outils de l'agent qui le
construisent, et il passe quand meme par ces trois etapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine

from app.config import Settings
from app.executor.runner import QueryResult, execute
from app.optimizer.optimizer import optimize
from app.schema.introspector import SchemaIntrospector
from app.security.guard import validate
from app.security.readonly import readonly_transaction


@dataclass
class SqlRun:
    sql: str
    result: QueryResult
    notes: list[str] = field(default_factory=list)


class QueryForgePipeline:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._introspector = SchemaIntrospector(engine, settings)

    @property
    def introspector(self) -> SchemaIntrospector:
        return self._introspector

    def run_sql(self, sql: str) -> SqlRun:
        """Valide, borne et execute du SQL en lecture seule.

        Les exceptions (`SQLValidationError`, erreur Postgres) remontent telles
        quelles pour que l'appelant decide s'il corrige ou abandonne.
        """
        schema = self._introspector.get_schema()
        report = validate(sql, known_tables=schema.table_names())
        optimized = optimize(report.statement, self._settings)

        with readonly_transaction(self._engine, self._settings) as conn:
            result = execute(conn, optimized.sql, self._settings)

        return SqlRun(
            sql=optimized.sql,
            result=result,
            notes=optimized.notes + result.notes,
        )


def _clean_db_error(err: Exception) -> str:
    """Reduit une exception SQLAlchemy a la ligne utile de Postgres.

    Le texte brut contient la requete complete et le chemin du driver : trop
    bruyant pour le modele, et a ne pas exposer tel quel.
    """
    message = str(getattr(err, "orig", err))
    first_line = message.strip().splitlines()[0] if message.strip() else "Erreur inconnue."
    return first_line[:300]
