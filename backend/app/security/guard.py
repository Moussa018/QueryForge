"""Validation de securite du SQL genere.

Principe : on ne fait *jamais* confiance a une chaine produite par un LLM, et on
ne valide pas par expressions regulieres -- trop faciles a contourner
(commentaires, casse, unicode, espaces exotiques). On parse le SQL en AST avec
sqlglot et on raisonne sur l'arbre.

Trois barrieres independantes, chacune suffisante en theorie, cumulees en
pratique (defense en profondeur) :
  1. ce module -- analyse statique de l'AST ;
  2. `readonly.py` -- transaction READ ONLY + timeout cote serveur ;
  3. le role Postgres lui-meme, qui n'a que le droit SELECT.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# Seuls ces noeuds racine sont acceptes. Tout le reste (INSERT, UPDATE, DELETE,
# DROP, ALTER, CREATE, GRANT, COPY, CALL...) est rejete par construction.
_ALLOWED_ROOT_NODES = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# Fonctions Postgres permettant de lire le disque, d'ouvrir une connexion
# sortante ou d'executer du code. Interdites meme dans un SELECT.
_FORBIDDEN_FUNCTIONS = {
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "dblink_connect",
    "pg_logical_slot_get_changes",
    "query_to_xml",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "set_config",
    "current_setting",
}

# Schemas systeme : contiennent les hashs de mots de passe (pg_authid), la
# configuration, les statistiques d'usage. Aucune question metier n'en a besoin.
_FORBIDDEN_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}
_FORBIDDEN_TABLE_PREFIXES = ("pg_",)


class SQLValidationError(Exception):
    """Le SQL a ete refuse. Le message est sur, il peut remonter a l'UI."""


@dataclass
class ValidationReport:
    statement: exp.Expression
    referenced_tables: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def validate(sql: str, *, known_tables: set[str] | None = None) -> ValidationReport:
    """Valide une requete et renvoie son AST parse.

    Leve `SQLValidationError` au premier probleme. L'AST renvoye est celui qui a
    ete valide : l'appelant doit executer *cet* arbre re-serialise, jamais la
    chaine d'origine, sinon la validation porte sur un texte different de ce qui
    part en base.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("Requete vide.")

    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as err:
        raise SQLValidationError(f"SQL invalide : {err}") from err

    statements = [s for s in statements if s is not None]

    # Empeche l'empilement `SELECT 1; DROP TABLE users`.
    if len(statements) != 1:
        raise SQLValidationError(
            f"Une seule instruction est autorisee ({len(statements)} detectees)."
        )

    statement = statements[0]
    _assert_read_only_root(statement)
    _assert_no_dml_anywhere(statement)
    _assert_no_forbidden_functions(statement)

    tables = _collect_tables(statement)
    _assert_no_system_tables(tables)

    report = ValidationReport(statement=statement, referenced_tables=tables)

    if known_tables is not None:
        unknown = {t for t in tables if t.lower() not in known_tables}
        if unknown:
            raise SQLValidationError(
                "Table inconnue dans le schema : " + ", ".join(sorted(unknown))
            )

    return report


# -- barrieres individuelles ------------------------------------------------


def _assert_read_only_root(statement: exp.Expression) -> None:
    node = statement
    # Une CTE (`WITH x AS (...) SELECT`) enveloppe le SELECT : on regarde ce
    # qu'il y a reellement a la racine.
    if isinstance(node, exp.Subquery):
        node = node.this

    if not isinstance(node, _ALLOWED_ROOT_NODES):
        kind = type(node).__name__.upper()
        raise SQLValidationError(
            f"Seules les requetes de lecture sont autorisees ({kind} refuse)."
        )


def _assert_no_dml_anywhere(statement: exp.Expression) -> None:
    """Attrape le DML imbrique, y compris les CTE modifiantes de Postgres.

    `WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d` a bien un SELECT
    a la racine et supprime pourtant toute la table.
    """
    forbidden = (
        exp.Insert, exp.Update, exp.Delete, exp.Merge,
        exp.Drop, exp.Create, exp.Alter, exp.TruncateTable,
        exp.Grant, exp.Command, exp.Transaction, exp.Commit, exp.Rollback,
    )
    for node in statement.walk():
        if isinstance(node, forbidden):
            kind = type(node).__name__.upper()
            raise SQLValidationError(
                f"Operation d'ecriture detectee dans la requete ({kind})."
            )


def _assert_no_forbidden_functions(statement: exp.Expression) -> None:
    for node in statement.find_all(exp.Anonymous, exp.Func):
        name = _function_name(node)
        if name and name.lower() in _FORBIDDEN_FUNCTIONS:
            raise SQLValidationError(f"Fonction interdite : {name}.")


def _function_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous):
        return str(node.this)
    name = node.sql_name() if hasattr(node, "sql_name") else None
    return name


def _collect_tables(statement: exp.Expression) -> set[str]:
    """Tables reellement lues, hors alias de CTE.

    Un alias de CTE apparait comme une Table dans l'AST ; le compter ferait
    echouer la verification `known_tables` sur du SQL parfaitement legitime.
    """
    cte_aliases = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }

    tables: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = table.name
        if not name or name.lower() in cte_aliases:
            continue
        tables.add(f"{table.db}.{name}" if table.db else name)
    return tables


def _assert_no_system_tables(tables: set[str]) -> None:
    for qualified in tables:
        schema, _, name = qualified.rpartition(".")
        if schema and schema.lower() in _FORBIDDEN_SCHEMAS:
            raise SQLValidationError(f"Acces refuse au schema systeme : {schema}.")
        if name.lower().startswith(_FORBIDDEN_TABLE_PREFIXES):
            raise SQLValidationError(f"Acces refuse a la table systeme : {name}.")
