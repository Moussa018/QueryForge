"""API HTTP QueryForge."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.nlp.generator import SQLGenerator
from app.pipeline import PipelineError, QueryForgePipeline
from app.security.guard import SQLValidationError, validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("queryforge")

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    # Filet de securite au niveau de la session : meme un chemin de code qui
    # oublierait readonly_transaction hérite du mode lecture seule.
    connect_args={"options": "-c default_transaction_read_only=on"},
)

app = FastAPI(
    title="QueryForge",
    description="Moteur de requetes SQL genere depuis le langage naturel.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Le generateur exige une cle API. On tolere son absence au demarrage pour que
# /health et /schema restent utilisables sans cle -- pratique en CI.
try:
    _generator = SQLGenerator(settings)
    pipeline: QueryForgePipeline | None = QueryForgePipeline(engine, settings, _generator)
    _startup_error: str | None = None
except Exception as err:
    logger.error("Generateur indisponible : %s", err)
    pipeline = None
    _startup_error = str(err)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    dry_run: bool = Field(
        default=False,
        description="Genere, valide et estime la requete sans l'executer.",
    )


class ValidateRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)


def _require_pipeline() -> QueryForgePipeline:
    if pipeline is None:
        raise HTTPException(status_code=503, detail=_startup_error or "Pipeline indisponible.")
    return pipeline


@app.get("/health")
def health() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as err:
        logger.warning("Health check base echoue : %s", err)
        db_ok = False

    return {
        "status": "ok" if db_ok and pipeline is not None else "degraded",
        "database": db_ok,
        "llm": pipeline is not None,
        "model": settings.llm_model,
    }


@app.get("/schema")
def get_schema(refresh: bool = False) -> dict:
    """Schema tel que le modele le voit. Utile pour diagnostiquer une mauvaise generation."""
    introspector = _require_pipeline().introspector
    schema = introspector.get_schema(force_refresh=refresh)
    return {
        "dialect": schema.dialect,
        "captured_at": schema.captured_at,
        "tables": [
            {
                "name": t.qualified_name,
                "approx_rows": t.approx_rows,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type,
                        "nullable": c.nullable,
                        "primary_key": c.primary_key,
                        "sample_values": c.sample_values,
                    }
                    for c in t.columns
                ],
                "foreign_keys": [
                    {"columns": fk.columns, "target": fk.target_table, "target_columns": fk.target_columns}
                    for fk in t.foreign_keys
                ],
            }
            for t in schema.tables
        ],
        "prompt_representation": schema.render_for_prompt(),
    }


@app.post("/ask")
def ask(request: AskRequest) -> dict:
    """Question en francais -> SQL genere, valide, optimise, execute."""
    try:
        outcome = _require_pipeline().run(request.question, dry_run=request.dry_run)
    except PipelineError as err:
        raise HTTPException(
            status_code=422,
            detail={"message": str(err), "stage": err.stage, "attempts": err.attempts},
        ) from err
    except HTTPException:
        raise
    except Exception as err:
        logger.exception("Echec inattendu du pipeline")
        raise HTTPException(status_code=500, detail="Erreur interne lors du traitement.") from err

    return outcome.to_dict()


@app.post("/validate")
def validate_sql(request: ValidateRequest) -> dict:
    """Passe du SQL ecrit a la main dans le validateur, sans l'executer."""
    schema = _require_pipeline().introspector.get_schema()
    try:
        report = validate(request.sql, known_tables=schema.table_names())
    except SQLValidationError as err:
        return {"valid": False, "reason": str(err)}
    return {"valid": True, "tables": sorted(report.referenced_tables)}
