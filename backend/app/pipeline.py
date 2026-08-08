"""Orchestration : question en francais -> resultat tabulaire.

    question
       |
       v
  [1] introspection du schema (cache TTL)
       |
       v
  [2] generation SQL (GPT-4o, sortie structuree)
       |
       v
  [3] validation AST  --refus--> [2] avec le motif du refus (boucle bornee)
       |
       v
  [4] optimisation / bornage (LIMIT, plafond)
       |
       v
  [5] EXPLAIN : refus si trop couteux
       |
       v
  [6] execution en transaction READ ONLY
       |
       v
    resultat + explication + graphique suggere

L'etape 3 renvoie au modele le motif exact du refus. C'est ce qui rattrape le
cas frequent d'une colonne halluciné : le modele corrige tout seul au 2e tour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from app.config import Settings
from app.executor.runner import QueryResult, execute
from app.nlp.generator import GenerationContext, GeneratedQuery, SQLGenerator
from app.optimizer.optimizer import QueryTooExpensive, explain, optimize
from app.schema.introspector import SchemaIntrospector
from app.security.guard import SQLValidationError, validate
from app.security.readonly import readonly_transaction

logger = logging.getLogger(__name__)

MAX_GENERATION_ATTEMPTS = 3


class PipelineError(Exception):
    """Echec destine a l'utilisateur : le message est sur a afficher."""

    def __init__(self, message: str, *, stage: str, attempts: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.stage = stage
        self.attempts = attempts or []


@dataclass
class PipelineOutcome:
    question: str
    sql: str
    explanation: str
    chart_hint: str
    assumptions: list[str]
    result: QueryResult
    optimizer_notes: list[str] = field(default_factory=list)
    estimated_cost: float | None = None
    estimated_rows: int | None = None
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "sql": self.sql,
            "explanation": self.explanation,
            "chart_hint": self.chart_hint,
            "assumptions": self.assumptions,
            "columns": self.result.columns,
            "column_types": self.result.column_types,
            "rows": self.result.rows,
            "row_count": self.result.row_count,
            "truncated": self.result.truncated,
            "duration_ms": round(self.result.duration_ms, 1),
            "notes": self.optimizer_notes + self.result.notes,
            "estimated_cost": self.estimated_cost,
            "estimated_rows": self.estimated_rows,
            "attempts": self.attempts,
        }


class QueryForgePipeline:
    def __init__(self, engine: Engine, settings: Settings, generator: SQLGenerator) -> None:
        self._engine = engine
        self._settings = settings
        self._generator = generator
        self._introspector = SchemaIntrospector(engine, settings)

    @property
    def introspector(self) -> SchemaIntrospector:
        return self._introspector

    def run(self, question: str, *, dry_run: bool = False) -> PipelineOutcome:
        schema = self._introspector.get_schema()
        known_tables = schema.table_names()

        retry_context: GenerationContext | None = None
        attempt_log: list[dict[str, str]] = []

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            try:
                generated: GeneratedQuery = self._generator.generate(question, schema, retry_context)
            except Exception as err:
                # Panne du fournisseur (quota, cle invalide, coupure reseau) :
                # inutile de reessayer, et l'utilisateur doit voir la cause
                # reelle plutot qu'une "erreur interne" opaque.
                raise PipelineError(_describe_llm_failure(err), stage="llm") from err

            if not generated.feasible:
                raise PipelineError(generated.explanation, stage="generation")

            try:
                report = validate(generated.sql, known_tables=known_tables)
            except SQLValidationError as err:
                logger.warning("SQL refuse (tentative %s) : %s", attempt, err)
                attempt_log.append({"sql": generated.sql, "error": str(err)})
                retry_context = GenerationContext(failed_sql=generated.sql, error=str(err))
                continue

            optimized = optimize(report.statement, self._settings)

            try:
                return self._execute(question, generated, optimized, dry_run=dry_run, attempts=attempt)
            except QueryTooExpensive as err:
                # Pas une erreur de syntaxe : le modele doit reduire le perimetre,
                # pas corriger un nom de colonne.
                raise PipelineError(str(err), stage="explain") from err
            except Exception as err:  # erreur SQL renvoyee par Postgres
                message = _clean_db_error(err)
                logger.warning("Execution echouee (tentative %s) : %s", attempt, message)
                attempt_log.append({"sql": optimized.sql, "error": message})
                retry_context = GenerationContext(failed_sql=optimized.sql, error=message)

        raise PipelineError(
            f"Aucune requete valide apres {MAX_GENERATION_ATTEMPTS} tentatives. "
            "Reformulez la question ou precisez les tables concernees.",
            stage="validation",
            attempts=attempt_log,
        )

    def _execute(
        self,
        question: str,
        generated: GeneratedQuery,
        optimized,
        *,
        dry_run: bool,
        attempts: int,
    ) -> PipelineOutcome:
        with readonly_transaction(self._engine, self._settings) as conn:
            cost, est_rows, _plan = explain(conn, optimized.sql, self._settings)

            if dry_run:
                result = QueryResult(
                    columns=[], column_types={}, rows=[], row_count=0,
                    truncated=False, duration_ms=0.0,
                    notes=["Mode simulation : la requete n'a pas ete executee."],
                )
            else:
                result = execute(conn, optimized.sql, self._settings)

        return PipelineOutcome(
            question=question,
            sql=optimized.sql,
            explanation=generated.explanation,
            chart_hint=generated.chart_hint,
            assumptions=generated.assumptions,
            result=result,
            optimizer_notes=optimized.notes,
            estimated_cost=cost,
            estimated_rows=est_rows,
            attempts=attempts,
        )


def _describe_llm_failure(err: Exception) -> str:
    """Traduit une panne du fournisseur LLM en message actionnable."""
    name = type(err).__name__
    text = str(err)

    if "insufficient_quota" in text or "credit_balance_exhausted" in text:
        return (
            "Le compte OpenAI n'a plus de credits : la generation est impossible. "
            "Rechargez le compte sur platform.openai.com, ou renseignez une autre "
            "cle dans backend/.env."
        )
    if name == "RateLimitError" or "rate_limit" in text:
        return "Limite de debit OpenAI atteinte. Reessayez dans quelques secondes."
    if name == "AuthenticationError" or "invalid_api_key" in text:
        return "Cle OPENAI_API_KEY invalide ou revoquee. Verifiez backend/.env."
    if name in ("APITimeoutError", "APIConnectionError"):
        return "L'API OpenAI est injoignable (reseau ou delai depasse). Reessayez."
    return f"Le service de generation a echoue ({name})."


def _clean_db_error(err: Exception) -> str:
    """Reduit une exception SQLAlchemy a la ligne utile de Postgres.

    Le texte brut contient la requete complete et le chemin du driver : trop
    bruyant pour le modele, et a ne pas exposer tel quel dans l'UI.
    """
    message = str(getattr(err, "orig", err))
    first_line = message.strip().splitlines()[0] if message.strip() else "Erreur inconnue."
    return first_line[:300]
