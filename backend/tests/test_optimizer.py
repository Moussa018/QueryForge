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
