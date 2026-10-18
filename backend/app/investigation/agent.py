"""Boucle d'enquete : plan -> execute -> analyse -> (boucle) -> rapport.

    question ouverte
          |
     [Planificateur]  hypotheses + premiere etape
          |
     +--> [Outil]      compare_periods / breakdown / run_sql (pipeline valide)
     |        |
     |    [Analyste]   interprete, met a jour les hypotheses, choisit la suite
     +--------+        (borne par les garde-fous)
          |
     [Redacteur]       rapport ; le SQL d'annexe vient de l'etat, pas du modele

Le raisonnement (`Reasoner`) est separe de la mecanique (budgets, dedoublonnage,
dispatch des outils) : la boucle se teste avec un raisonneur scripte, sans LLM.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq

from app.config import Settings
from app.investigation.state import (
    Action,
    Analysis,
    InvestigationState,
    Observation,
    Plan,
    ReportDraft,
)
from app.investigation.tools import InvestigationTools, Metric, ToolError, ToolOutput
from app.schema.introspector import DatabaseSchema

logger = logging.getLogger(__name__)

Event = dict[str, Any]


class Reasoner(Protocol):
    def plan(self, question: str, schema: DatabaseSchema, today: dt.date) -> Plan: ...

    def analyze(
        self, state: InvestigationState, schema: DatabaseSchema, today: dt.date, steps_left: int
    ) -> Analysis: ...

    def write_report(self, state: InvestigationState, stop_reason: str) -> ReportDraft: ...


# -- raisonneur LLM -----------------------------------------------------------

_SYSTEM = """\
Tu es un analyste de donnees rigoureux. Tu enquetes sur une question metier \
ouverte en testant des hypotheses avec des outils qui interrogent une base \
PostgreSQL en lecture seule.

OUTILS (champ `tool` d'une action)
- compare_periods : compare une metrique sur deux periodes. Champs : table, \
aggregate (sum|avg|count|count_distinct), column (vide pour count(*)), \
date_column, filters (optionnel), p1_start/p1_end, p2_start/p2_end.
- breakdown : meme metrique ventilee par `dimension` (colonne de la table, ou \
table_liee.colonne si une cle etrangere directe existe), avec la part de chaque \
segment dans la variation. Memes champs + dimension.
- run_sql : requete SELECT libre, en dernier recours. Champ sql.
- finish : tu as assez d'elements.

Dates ISO AAAA-MM-JJ, fin EXCLUE (septembre = 2025-09-01 a 2025-10-01). \
p1 = periode de reference, p2 = periode examinee.

METHODE
- Commence par verifier que le fait est reel (compare_periods).
- Decompose : CA = nombre de commandes x panier moyen. Lequel a bouge ?
- Segmente (categorie, pays, statut, segment client) avec breakdown.
- Teste les hypotheses une a une ; ecarte celles que les chiffres contredisent.

HONNETETE
- Tous les chiffres viennent des observations. N'en invente aucun.
- Distingue correlation et causalite. Signale les petits echantillons.
- Si les donnees ne montrent pas de cause nette, dis-le. N'invente pas de cause.
- Utilise uniquement les tables et colonnes du schema.
"""

_PLAN_HUMAN = """\
Aujourd'hui : {today}

Schema :
{schema}

Question : {question}

Propose 3 a 5 hypotheses testables (ids h1, h2, ...) et la premiere etape.
"""

_ANALYZE_HUMAN = """\
Aujourd'hui : {today}

Schema :
{schema}

Question : {question}

Hypotheses :
{hypotheses}

Observations jusqu'ici :
{observations}

Etapes restantes : {steps_left}.
Interprete la derniere observation, mets a jour les hypotheses concernees \
(confirmed / rejected / inconclusive) avec des chiffres, puis choisis la \
prochaine etape. Ne refais pas une etape deja faite. Termine avec \
tool='finish' des que la cause est etablie ou qu'aucune piste ne progresse.
"""

_REPORT_HUMAN = """\
Question : {question}

Hypotheses et statuts :
{hypotheses}

Observations :
{observations}

Motif d'arret : {stop_reason}

Redige le rapport : conclusion en une phrase, 3 a 5 constats chiffres (chiffres \
copies des observations), et ce qui n'a pas pu etre verifie. Ne conclus pas \
plus fort que les donnees ne le permettent.
"""


class LLMReasoner:
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY absent : impossible de lancer une enquete.")
        llm = ChatGroq(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            api_key=settings.openai_api_key,
            timeout=60,
            max_retries=2,
        )

        def chain(human: str, schema: type) -> Any:
            prompt = ChatPromptTemplate.from_messages([("system", _SYSTEM), ("human", human)])
            return prompt | llm.with_structured_output(schema)

        self._plan = chain(_PLAN_HUMAN, Plan)
        self._analyze = chain(_ANALYZE_HUMAN, Analysis)
        self._report = chain(_REPORT_HUMAN, ReportDraft)

    def plan(self, question: str, schema: DatabaseSchema, today: dt.date) -> Plan:
        return self._plan.invoke(
            {"question": question, "schema": schema.render_for_prompt(), "today": today.isoformat()}
        )

    def analyze(
        self, state: InvestigationState, schema: DatabaseSchema, today: dt.date, steps_left: int
    ) -> Analysis:
        return self._analyze.invoke(
            {
                "question": state.question,
                "schema": schema.render_for_prompt(),
                "today": today.isoformat(),
                "hypotheses": state.render_hypotheses(),
                "observations": state.render_observations(),
                "steps_left": steps_left,
            }
        )

    def write_report(self, state: InvestigationState, stop_reason: str) -> ReportDraft:
        return self._report.invoke(
            {
                "question": state.question,
                "hypotheses": state.render_hypotheses(),
                "observations": state.render_observations(),
                "stop_reason": stop_reason,
            }
        )


# -- agent ---------------------------------------------------------------------

@dataclass
class Limits:
    max_steps: int
    stall_limit: int

    @classmethod
    def from_settings(cls, s: Settings) -> "Limits":
        return cls(s.investigation_max_steps, s.investigation_stall_limit)


class InvestigationAgent:
    def __init__(
        self,
        reasoner: Reasoner,
        tools: InvestigationTools,
        schema_provider,
        limits: Limits,
        today: dt.date | None = None,
    ) -> None:
        self._reasoner = reasoner
        self._tools = tools
        self._schema_provider = schema_provider
        self._limits = limits
        self._today = today or dt.date.today()

    def run(self, question: str) -> Iterator[Event]:
        """Genere les evenements de l'enquete, un par etape (consomme par l'API)."""
        state = InvestigationState(question=question)
        schema = self._schema_provider()
        stop_reason = "enquete terminee"

        try:
            plan = self._reasoner.plan(question, schema, self._today)
        except Exception as err:
            logger.exception("Planification echouee")
            yield {"event": "error", "message": f"Planification impossible ({type(err).__name__})."}
            return

        state.set_hypotheses(plan.hypotheses)
        yield {
            "event": "plan",
            "hypotheses": [h.model_dump() for h in plan.hypotheses],
        }

        action = plan.first_action
        step = 0
        while True:
            if action.tool == "finish":
                break
            if step >= self._limits.max_steps:
                stop_reason = f"budget d'etapes atteint ({self._limits.max_steps})"
                break

            step += 1
            yield {"event": "step", "step": step, "tool": action.tool, "rationale": action.rationale}

            observation = self._execute(step, action, state)
            state.observations.append(observation)
            yield {
                "event": "observation",
                "step": step,
                "tool": action.tool,
                "summary": observation.summary,
                "error": observation.error,
                "sql": observation.sql,
            }

            if observation.error:
                state.consecutive_errors += 1
                if state.consecutive_errors >= 2:
                    stop_reason = "deux echecs consecutifs d'outil"
                    break
            else:
                state.consecutive_errors = 0

            try:
                analysis = self._reasoner.analyze(
                    state, schema, self._today, self._limits.max_steps - step
                )
            except Exception as err:
                logger.exception("Analyse echouee")
                stop_reason = f"analyse interrompue ({type(err).__name__})"
                break

            changed = state.apply_updates(analysis.updates)
            state.stalled_steps = 0 if changed else state.stalled_steps + 1
            yield {
                "event": "analysis",
                "step": step,
                "interpretation": analysis.interpretation,
                "hypotheses": {
                    hid: {"status": state.status[hid], "evidence": state.evidence.get(hid, "")}
                    for hid in state.hypotheses
                },
            }

            if state.stalled_steps >= self._limits.stall_limit:
                stop_reason = "aucune hypothese ne progresse"
                break
            action = analysis.next_action

        yield from self._report(state, stop_reason)

    # -- internes ---------------------------------------------------------------

    def _execute(self, step: int, action: Action, state: InvestigationState) -> Observation:
        key = action.key()
        if key in state.seen:
            return Observation(
                step, action, error="Etape deja executee : choisissez une autre analyse ou terminez."
            )
        state.seen.add(key)

        try:
            output = self._dispatch(action)
        except ToolError as err:
            return Observation(step, action, error=str(err))
        except Exception as err:
            logger.exception("Outil %s en echec", action.tool)
            return Observation(step, action, error=f"Echec inattendu ({type(err).__name__}).")

        return Observation(
            step, action, summary=output.summary, sql=output.sql
        )

    def _dispatch(self, action: Action) -> ToolOutput:
        if action.tool == "run_sql":
            if not action.sql.strip():
                raise ToolError("run_sql exige un champ sql.")
            return self._tools.run_sql(action.sql)

        if action.tool not in ("compare_periods", "breakdown"):
            raise ToolError(f"Outil inconnu : {action.tool!r}.")

        if not (action.table and action.aggregate and action.date_column):
            raise ToolError("Metrique incomplete : table, aggregate et date_column sont requis.")
        metric = Metric(
            table=action.table,
            aggregate=action.aggregate,
            date_column=action.date_column,
            column=action.column or None,
            filters=tuple(sorted(action.filters.items())),
        )
        p1 = (action.p1_start, action.p1_end)
        p2 = (action.p2_start, action.p2_end)

        if action.tool == "compare_periods":
            return self._tools.compare_periods(metric, p1, p2)
        if not action.dimension:
            raise ToolError("breakdown exige une dimension.")
        return self._tools.breakdown(metric, action.dimension, p1, p2)

    def _report(self, state: InvestigationState, stop_reason: str) -> Iterator[Event]:
        if not any(o.summary for o in state.observations):
            yield {
                "event": "error",
                "message": f"Aucun resultat exploitable ({stop_reason}). Reformulez la question.",
            }
            return
        try:
            draft = self._reasoner.write_report(state, stop_reason)
        except Exception as err:
            logger.exception("Redaction du rapport echouee")
            yield {"event": "error", "message": f"Rapport impossible ({type(err).__name__})."}
            return

        yield {
            "event": "report",
            "conclusion": draft.conclusion,
            "findings": [f.model_dump() for f in draft.findings],
            "unverified": draft.unverified,
            "hypotheses": [
                {
                    "id": hid,
                    "statement": h.statement,
                    "status": state.status[hid],
                    "evidence": state.evidence.get(hid, ""),
                }
                for hid, h in state.hypotheses.items()
            ],
            # Annexe tiree de l'etat : le modele ne peut pas la falsifier.
            "queries": [
                {"step": o.step, "tool": o.action.tool, "sql": o.sql}
                for o in state.observations
                if o.sql
            ],
            "stop_reason": stop_reason,
            "steps": len(state.observations),
        }
        yield {"event": "done"}
