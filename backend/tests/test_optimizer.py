"""Tests du bornage de requetes."""

import pytest

from app.config import Settings
from app.optimizer.optimizer import optimize
from app.security.guard import validate


@pytest.fixture
def settings():
    return Settings(default_row_limit=500, max_row_limit=5000, openai_api_key="test")


def _run(sql: str, settings: Settings):
    return optimize(validate(sql).statement, settings)


def test_injecte_un_limit_absent(settings):
    result = _run("SELECT * FROM customers", settings)
    assert "LIMIT 500" in result.sql
    assert result.limit_applied == 500


def test_preserve_un_limit_raisonnable(settings):
    result = _run("SELECT * FROM customers LIMIT 20", settings)
    assert result.limit_applied == 20
    assert "LIMIT 20" in result.sql


def test_rabote_un_limit_excessif(settings):
    """Le modele ne doit pas pouvoir ramener 1 million de lignes dans le navigateur."""
    result = _run("SELECT * FROM customers LIMIT 1000000", settings)
    assert result.limit_applied == 5000
    assert "LIMIT 5000" in result.sql
    assert "1000000" not in result.sql
    assert any("rabote" in note for note in result.notes)


def test_limit_sur_union_englobe_les_deux_branches(settings):
    result = _run("SELECT id FROM customers UNION SELECT customer_id FROM orders", settings)
    assert result.sql.rstrip().endswith("LIMIT 500")


def test_signale_le_produit_cartesien(settings):
    result = _run("SELECT * FROM customers, orders", settings)
    assert any("cartesien" in note for note in result.notes)


def test_signale_count_star(settings):
    result = _run("SELECT COUNT(*) FROM orders", settings)
    assert any("COUNT(*)" in note for note in result.notes)


def test_le_sql_optimise_reste_valide(settings):
    """La reecriture ne doit jamais produire du SQL que le validateur refuserait."""
    sql = (
        "WITH r AS (SELECT * FROM orders WHERE status = 'paid') "
        "SELECT customer_id, COUNT(*) FROM r GROUP BY customer_id"
    )
    result = _run(sql, settings)
    validate(result.sql)  # ne doit pas lever


# -- garde-fou de cout ------------------------------------------------------

def test_cout_lit_le_noeud_le_plus_cher_pas_la_racine():
    """Sous un LIMIT, la racine sous-estime : c'est l'arbre entier qui compte.

    Sans ca, le LIMIT que nous injectons nous-memes desamorcerait le plafond.
    """
    from app.optimizer.optimizer import _max_plan_cost

    plan = {
        "Node Type": "Limit",
        "Total Cost": 12.5,
        "Plans": [
            {
                "Node Type": "Nested Loop",
                "Total Cost": 450213.0,
                "Plans": [{"Node Type": "Seq Scan", "Total Cost": 310.0}],
            },
        ],
    }
    assert _max_plan_cost(plan) == 450213.0


def test_message_de_cout_conserve_sa_ponctuation(settings):
    """Le formatage des milliers ne doit pas manger la virgule de la phrase."""
    from app.optimizer.optimizer import QueryTooExpensive, explain

    class FakeResult:
        def scalar(self):
            return [{"Plan": {"Node Type": "Limit", "Total Cost": 9_999_999.0}}]

    class FakeConn:
        def execute(self, _):
            return FakeResult()

    strict = settings.model_copy(update={"max_explain_cost": 10.0})
    with pytest.raises(QueryTooExpensive) as err:
        explain(FakeConn(), "SELECT 1", strict)

    message = str(err.value)
    # La virgule de la phrase survit au formatage des milliers...
    assert "(filtre de date, perimetre plus etroit)" in message
    # ...et le separateur est bien l'espace fine insecable, pas une virgule.
    assert "9 999 999" in message
    assert "9,999,999" not in message
