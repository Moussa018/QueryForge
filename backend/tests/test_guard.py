"""Tests du validateur de securite.

Chaque cas refuse correspond a une facon connue de contourner un filtre naif
par expression reguliere.
"""

import pytest

from app.security.guard import SQLValidationError, validate

KNOWN = {"customers", "orders", "email_events", "public.customers", "public.orders"}


# -- requetes legitimes -----------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers LIMIT 10",
        "SELECT c.id, COUNT(o.id) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        "WITH recents AS (SELECT * FROM orders WHERE created_at > now() - interval '30 days') "
        "SELECT customer_id, COUNT(*) FROM recents GROUP BY customer_id",
        "SELECT id FROM customers UNION SELECT customer_id FROM orders",
        "SELECT * FROM customers c WHERE NOT EXISTS "
        "(SELECT 1 FROM email_events e WHERE e.customer_id = c.id AND e.opened_at IS NOT NULL)",
    ],
)
def test_accepte_les_lectures(sql):
    report = validate(sql, known_tables=KNOWN)
    assert report.statement is not None


def test_alias_de_cte_non_confondu_avec_une_table():
    """Un alias de CTE ne doit pas declencher 'table inconnue'."""
    sql = "WITH truc AS (SELECT * FROM orders) SELECT * FROM truc"
    report = validate(sql, known_tables=KNOWN)
    assert "truc" not in report.referenced_tables
    assert "orders" in report.referenced_tables


# -- ecritures directes -----------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM customers",
        "DROP TABLE customers",
        "UPDATE customers SET email = 'x'",
        "INSERT INTO customers (email) VALUES ('x')",
        "TRUNCATE customers",
        "ALTER TABLE customers ADD COLUMN x int",
        "CREATE TABLE evil (id int)",
        "GRANT ALL ON customers TO PUBLIC",
    ],
)
def test_refuse_les_ecritures(sql):
    with pytest.raises(SQLValidationError):
        validate(sql, known_tables=KNOWN)


# -- contournements ---------------------------------------------------------

def test_refuse_les_instructions_empilees():
    with pytest.raises(SQLValidationError, match="seule instruction"):
        validate("SELECT 1; DROP TABLE customers", known_tables=KNOWN)


def test_refuse_la_cte_modifiante():
    """Racine SELECT, mais la CTE supprime la table -- le piege classique."""
    sql = "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone"
    with pytest.raises(SQLValidationError):
        validate(sql, known_tables=KNOWN)


def test_refuse_le_dml_dans_une_sous_requete():
    sql = "SELECT * FROM (WITH x AS (DELETE FROM orders RETURNING id) SELECT * FROM x) t"
    with pytest.raises(SQLValidationError):
        validate(sql, known_tables=KNOWN)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_sleep(60)",
        "SELECT * FROM customers WHERE id = 1 AND pg_sleep(10) IS NULL",
        "SELECT dblink('host=evil.com', 'SELECT 1')",
        "SELECT lo_import('/etc/shadow')",
    ],
)
def test_refuse_les_fonctions_dangereuses(sql):
    with pytest.raises(SQLValidationError, match="[Ff]onction interdite"):
        validate(sql, known_tables=None)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM information_schema.tables",
        "SELECT rolname, rolpassword FROM pg_authid",
        "SELECT * FROM pg_shadow",
    ],
)
def test_refuse_les_tables_systeme(sql):
    with pytest.raises(SQLValidationError, match="refuse"):
        validate(sql, known_tables=None)


def test_refuse_une_table_hors_schema():
    """Defense contre l'hallucination : une table inventee ne doit pas partir en base."""
    with pytest.raises(SQLValidationError, match="[Tt]able inconnue"):
        validate("SELECT * FROM table_qui_nexiste_pas", known_tables=KNOWN)


def test_refuse_le_sql_invalide():
    with pytest.raises(SQLValidationError):
        validate("SELEC * FRM customers", known_tables=KNOWN)


def test_refuse_la_requete_vide():
    with pytest.raises(SQLValidationError):
        validate("   ", known_tables=KNOWN)


def test_les_commentaires_ne_masquent_pas_le_dml():
    """Un filtre regex sur '^SELECT' laisserait passer ceci."""
    sql = "/* SELECT */ DELETE FROM customers"
    with pytest.raises(SQLValidationError):
        validate(sql, known_tables=KNOWN)
