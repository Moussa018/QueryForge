"""API HTTP QueryForge : enquetes sur une base PostgreSQL."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.investigation.agent import InvestigationAgent, Limits, LLMReasoner
from app.investigation.tools import InvestigationTools
from app.pipeline import QueryForgePipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("queryforge")

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    # Filet de securite au niveau de la session : meme un chemin de code qui
    # oublierait readonly_transaction herite du mode lecture seule.
    connect_args={"options": "-c default_transaction_read_only=on"},
)
pipeline = QueryForgePipeline(engine, settings)

app = FastAPI(
    title="QueryForge",
    description="Agent d'enquete sur une base PostgreSQL : hypotheses, requetes validees, rapport chiffre.",
    version="1.0.0",
)


class InvestigateRequest(BaseModel):
    question: str = Field(min_length=5, max_length=2000)


@app.get("/health")
def health() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as err:
        logger.warning("Health check base echoue : %s", err)
        db_ok = False

    llm_ok = bool(settings.openai_api_key)
    return {
        "status": "ok" if db_ok and llm_ok else "degraded",
        "database": db_ok,
        "llm": llm_ok,
        "model": settings.llm_model,
    }


@app.post("/investigate")
def investigate(request: InvestigateRequest) -> dict:
    """Enquete sur une question ouverte : renvoie le deroule des etapes puis le rapport."""
    try:
        agent = InvestigationAgent(
            reasoner=LLMReasoner(settings),
            tools=InvestigationTools(pipeline.introspector.get_schema, pipeline.run_sql),
            schema_provider=pipeline.introspector.get_schema,
            limits=Limits.from_settings(settings),
        )
    except Exception as err:
        raise HTTPException(status_code=503, detail=str(err)) from err

    try:
        events = list(agent.run(request.question))
    except Exception as err:
        logger.exception("Enquete interrompue")
        raise HTTPException(status_code=500, detail="Erreur interne pendant l'enquete.") from err

    report = next((e for e in events if e["event"] == "report"), None)
    if report is None:
        error = next((e for e in events if e["event"] == "error"), None)
        message = error["message"] if error else "Enquete sans resultat."
        raise HTTPException(status_code=422, detail={"message": message, "stage": "investigation"})

    steps = [e for e in events if e["event"] not in ("report", "done")]
    return {"steps": steps, "report": report}
