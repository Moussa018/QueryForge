"""Tests des outils d'enquete et de la boucle de l'agent (sans LLM, sans base)."""

import datetime as dt

import pytest

from app.executor.runner import QueryResult
from app.investigation.agent import InvestigationAgent, Limits
from app.investigation.state import (
    Action,
    Analysis,
    Finding,
    Hypothesis,
    HypothesisUpdate,
    Plan,
    ReportDraft,
)
from app.investigation.tools import (
    InvestigationTools,
    Metric,
    ToolError,
    build_breakdown_sql,
    build_compare_sql,
    contributions,
    variation,
)
from app.pipeline import SqlRun
from app.schema.introspector import Column, DatabaseSchema, ForeignKey, Table
from app.security.guard import SQLValidationError, validate


def _col(name, type_="TEXT"):
    return Column(name=name, type=type_, nullable=False)


SCHEMA = DatabaseSchema(
    tables=[
        Table("public", "customers", [_col("id", "INTEGER"), _col("country")], []),
        Table(
            "public",
            "orders",
            [
                _col("id", "INTEGER"),
                _col("customer_id", "INTEGER"),
                _col("status"),
                _col("total_amount", "NUMERIC"),
                _col("created_at", "TIMESTAMPTZ"),
            ],
            [ForeignKey(["customer_id"], "public.customers", ["id"])],
        ),
    ],
    dialect="postgresql",
    captured_at=0.0,
)
KNOWN = SCHEMA.table_names()
P1 = (dt.date(2025, 8, 1), dt.date(2025, 9, 1))
P2 = (dt.date(2025, 9, 1), dt.date(2025, 10, 1))
REVENUE = Metric("orders", "sum", "created_at", column="total_amount")


# -- construction SQL ---------------------------------------------------------

def test_compare_sql_passe_le_validateur():
    sql = build_compare_sql(SCHEMA, REVENUE, P1, P2)
    validate(sql, known_tables=KNOWN)
    assert "DATE '2025-09-01'" in sql


def test_breakdown_avec_jointure_fk_passe_le_validateur():
    sql = build_breakdown_sql(SCHEMA, REVENUE, "customers.country", P1, P2)
    report = validate(sql, known_tables=KNOWN)
    assert report.referenced_tables == {"orders", "customers"}


def test_count_sans_colonne():
    sql = build_compare_sql(SCHEMA, Metric("orders", "count", "created_at"), P1, P2)
    assert "COUNT(*) FILTER" in sql
    validate(sql, known_tables=KNOWN)


def test_filtre_egalite_echappe_les_quotes():
    metric = Metric("orders", "count", "created_at", filters=(("status", "x'; DROP TABLE orders;--"),))
    sql = build_compare_sql(SCHEMA, metric, P1, P2)
    validate(sql, known_tables=KNOWN)  # une seule instruction, la quote est doublee
    assert "x''; DROP TABLE orders;--" in sql


@pytest.mark.parametrize(
    "metric",
    [
        Metric("inconnue", "sum", "created_at", column="total_amount"),
        Metric("orders", "sum", "created_at", column="nope"),
        Metric("orders", "sum", "nope", column="total_amount"),
        Metric("orders", "median", "created_at", column="total_amount"),
        Metric("orders", "sum", "created_at"),  # sum sans colonne
        Metric("orders", "count", "created_at", filters=(('x"; DROP', "v"),)),
    ],
)
def test_metriques_invalides_refusees(metric):
    with pytest.raises(ToolError):
        build_compare_sql(SCHEMA, metric, P1, P2)


def test_dimension_sans_cle_etrangere_refusee():
    with pytest.raises(ToolError):
        build_breakdown_sql(SCHEMA, REVENUE, "products.category", P1, P2)


# -- calculs ----------------------------------------------------------------------

def test_variation():
    v = variation(200.0, 150.0)
    assert v["delta"] == -50.0
    assert v["delta_pct"] == -25.0
    assert variation(0.0, 10.0)["delta_pct"] is None


def test_contributions_triees_et_parts():
    rows = [
        {"dimension": "A", "p1": 100, "p2": 40},
        {"dimension": "B", "p1": 100, "p2": 90},
        {"dimension": "C", "p1": 10, "p2": None},
    ]
    ranked = contributions(rows)
    assert [r["dimension"] for r in ranked] == ["A", "B", "C"]
    # variation totale = -60 -10 -10 = -80 ; A y contribue pour 75 %
    assert ranked[0]["share_of_change"] == pytest.approx(75.0)


# -- boucle de l'agent --------------------------------------------------------------

def _run(rows):
    return SqlRun(
        sql="SELECT 1",
        result=QueryResult(["p1", "p2"], {}, rows, len(rows), False, 1.0),
    )


COMPARE = dict(
    tool="compare_periods", table="orders", aggregate="sum", column="total_amount",
    date_column="created_at", p1_start="2025-08-01", p1_end="2025-09-01",
    p2_start="2025-09-01", p2_end="2025-10-01",
)


class ScriptedReasoner:
    def __init__(self, analyses):
        self._analyses = list(analyses)
        self.report_calls = 0

    def plan(self, question, schema, today):
        return Plan(
            hypotheses=[Hypothesis(id="h1", statement="Baisse reelle")],
            first_action=Action(**COMPARE),
        )

    def analyze(self, state, schema, today, steps_left):
        return self._analyses.pop(0)

    def write_report(self, state, stop_reason):
        self.report_calls += 1
        return ReportDraft(
            conclusion="ok", findings=[Finding(statement="s", evidence="e")], unverified=[]
        )


def _agent(reasoner, run_sql, **limits):
    cfg = dict(max_steps=6, stall_limit=2) | limits
    tools = InvestigationTools(lambda: SCHEMA, run_sql)
    return InvestigationAgent(reasoner, tools, lambda: SCHEMA, Limits(**cfg), today=dt.date(2025, 10, 17))


def _events(agent):
    return list(agent.run("pourquoi les ventes baissent ?"))


def test_enquete_complete_produit_un_rapport_avec_annexe_sql():
    analysis = Analysis(
        interpretation="-25 %",
        updates=[HypothesisUpdate(hypothesis_id="h1", status="confirmed", evidence="-25 %")],
        next_action=Action(tool="finish"),
    )
    agent = _agent(ScriptedReasoner([analysis]), lambda sql: _run([{"p1": 200, "p2": 150}]))
    events = _events(agent)

    kinds = [e["event"] for e in events]
    assert kinds == ["plan", "step", "observation", "analysis", "report", "done"]
    report = events[-2]
    assert report["hypotheses"][0]["status"] == "confirmed"
    assert report["queries"][0]["sql"] == "SELECT 1"
    assert events[2]["summary"]["delta_pct"] == -25.0


def test_requete_en_double_refusee_sans_reexecuter():
    calls = []

    def run_sql(sql):
        calls.append(sql)
        return _run([{"p1": 200, "p2": 150}])

    again = Analysis(interpretation="", updates=[], next_action=Action(**COMPARE))
    finish = Analysis(interpretation="", updates=[], next_action=Action(tool="finish"))
    # la 2e etape repete la 1re (erreur), puis l'agent termine ; stall_limit eleve
    agent = _agent(ScriptedReasoner([again, finish]), run_sql, stall_limit=10)
    events = _events(agent)

    assert len(calls) == 1
    assert events[5]["error"].startswith("Etape deja executee")


def test_arret_apres_deux_echecs_consecutifs():
    def run_sql(sql):
        raise SQLValidationError("colonne inconnue")

    a = Analysis(interpretation="", updates=[], next_action=Action(**{**COMPARE, "aggregate": "avg"}))
    agent = _agent(ScriptedReasoner([a, a]), run_sql, stall_limit=10)
    events = _events(agent)

    # aucun resultat exploitable -> pas de rapport invente
    assert events[-1]["event"] == "error"


def test_arret_sur_stagnation():
    nothing = Analysis(
        interpretation="", updates=[], next_action=Action(**{**COMPARE, "aggregate": "avg"})
    )
    other = Analysis(
        interpretation="", updates=[], next_action=Action(**{**COMPARE, "aggregate": "count_distinct"})
    )
    agent = _agent(
        ScriptedReasoner([nothing, other]), lambda sql: _run([{"p1": 1, "p2": 1}]), stall_limit=2
    )
    events = _events(agent)
    report = events[-2]
    assert report["stop_reason"] == "aucune hypothese ne progresse"
    assert report["steps"] == 2


def test_budget_d_etapes():
    go_on = Analysis(
        interpretation="", updates=[], next_action=Action(**{**COMPARE, "aggregate": "avg"})
    )
    agent = _agent(
        ScriptedReasoner([go_on]), lambda sql: _run([{"p1": 1, "p2": 1}]),
        max_steps=1, stall_limit=10,
    )
    report = _events(agent)[-2]
    assert report["stop_reason"] == "budget d'etapes atteint (1)"
    assert report["steps"] == 1
