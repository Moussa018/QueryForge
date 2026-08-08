"""Optimisation et bornage de la requete avant execution.

Deux roles distincts :
  - *borner* : garantir qu'aucune requete ne peut ramener 10 millions de lignes
    dans le navigateur, meme si le modele a oublie le LIMIT ;
  - *estimer* : passer par EXPLAIN pour refuser en amont ce qui couterait trop
    cher, plutot que de le decouvrir au bout du timeout.

Les reecritures se font sur l'AST deja valide, jamais sur la chaine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Connection, text
from sqlglot import exp

from app.config import Settings


@dataclass
class OptimizationResult:
    sql: str
    notes: list[str]
    limit_applied: int | None
    estimated_cost: float | None = None
    estimated_rows: int | None = None


class QueryTooExpensive(Exception):
    """EXPLAIN estime la requete au-dela du budget autorise."""


def optimize(statement: exp.Expression, settings: Settings) -> OptimizationResult:
    """Applique les reecritures de bornage et renvoie le SQL final."""
    notes: list[str] = []
    node = statement.copy()

    limit_applied = _enforce_limit(node, settings, notes)
    _warn_on_unbounded_patterns(node, notes)

    return OptimizationResult(
        sql=node.sql(dialect="postgres", pretty=True),
        notes=notes,
        limit_applied=limit_applied,
    )


def _enforce_limit(node: exp.Expression, settings: Settings, notes: list[str]) -> int | None:
    """Injecte ou rabote le LIMIT de la requete la plus externe."""
    # Sur une UNION, le LIMIT doit envelopper l'ensemble, pas une seule branche.
    if not isinstance(node, exp.Select):
        current = node.args.get("limit")
        if current is None:
            node.set("limit", exp.Limit(expression=exp.Literal.number(settings.default_row_limit)))
            notes.append(f"LIMIT {settings.default_row_limit} ajoute (aucune limite fournie).")
            return settings.default_row_limit
        return _clamp_existing_limit(current, settings, notes)

    existing = node.args.get("limit")
    if existing is None:
        node.set("limit", exp.Limit(expression=exp.Literal.number(settings.default_row_limit)))
        notes.append(f"LIMIT {settings.default_row_limit} ajoute (aucune limite fournie).")
        return settings.default_row_limit

    return _clamp_existing_limit(existing, settings, notes)


def _clamp_existing_limit(limit_node: exp.Expression, settings: Settings, notes: list[str]) -> int | None:
    value_node = limit_node.expression if isinstance(limit_node, exp.Limit) else limit_node
    try:
        value = int(value_node.name)
    except (ValueError, AttributeError):
        # LIMIT parametre ou expression : on ne sait pas evaluer, le timeout et
        # le troncage cote executeur prennent le relais.
        notes.append("LIMIT non litteral : bornage delegue au troncage des resultats.")
        return None

    if value > settings.max_row_limit:
        value_node.set("this", str(settings.max_row_limit))
        notes.append(
            f"LIMIT {value} rabote a {settings.max_row_limit} (plafond serveur)."
        )
        return settings.max_row_limit
    return value


def _warn_on_unbounded_patterns(node: exp.Expression, notes: list[str]) -> None:
    """Signale les motifs couteux. Informatif : on n'empeche rien ici."""
    for select in node.find_all(exp.Select):
        # sqlglot range la virgule de `FROM a, b` dans `joins`, comme un JOIN
        # sans condition. Le signal n'est donc pas l'absence de joins mais
        # l'absence de ON/USING sur l'un d'eux.
        unconditioned = [
            join
            for join in (select.args.get("joins") or [])
            if not join.args.get("on") and not join.args.get("using")
        ]
        if unconditioned and not select.args.get("where"):
            notes.append(
                "Produit cartesien probable : tables jointes sans condition ni WHERE."
            )
            break

    if isinstance(node, exp.Select):
        order = node.args.get("order")
        if order and not node.args.get("limit"):
            notes.append("ORDER BY sans LIMIT : tri de l'ensemble du resultat.")

    for func in node.find_all(exp.Count):
        if isinstance(func.this, exp.Star):
            notes.append("COUNT(*) : peut declencher un parcours complet de table.")
            break


def explain(conn: Connection, sql: str, settings: Settings) -> tuple[float, int, dict]:
    """Estime cout et volume via EXPLAIN, sans executer la requete.

    EXPLAIN seul ne lance pas la requete (contrairement a EXPLAIN ANALYZE), il
    est donc sur a appeler sur du SQL deja valide.
    """
    result = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"))
    raw = result.scalar()
    plan_wrapper = json.loads(raw) if isinstance(raw, str) else raw
    plan = plan_wrapper[0]["Plan"]

    # Le cout du noeud racine ne suffit pas : sous un LIMIT, Postgres n'y compte
    # que les lignes reellement remontees. Un produit cartesien borne a 500
    # lignes affiche donc un cout derisoire alors que le travail sous-jacent est
    # enorme. Comme c'est nous qui injectons ce LIMIT, s'en tenir a la racine
    # reviendrait a desamorcer notre propre garde-fou : on retient le cout
    # maximal de l'arbre, qui reflete le travail reel.
    cost = _max_plan_cost(plan)
    rows = int(plan.get("Plan Rows", 0))

    if cost > settings.max_explain_cost:
        raise QueryTooExpensive(
            f"Cout estime {_fr_number(cost)} au-dela du plafond "
            f"{_fr_number(settings.max_explain_cost)}. "
            "Affinez la question (filtre de date, perimetre plus etroit)."
        )

    return cost, rows, plan


def _fr_number(value: float) -> str:
    """Formate un nombre a la francaise.

    Le separateur est l'espace fine insecable U+202F, ecrite en echappement :
    saisie telle quelle, elle est indiscernable d'une espace ordinaire dans le
    source et provoque des comparaisons de chaines qui echouent sans raison
    visible. Le formatage est isole ici pour ne pas toucher a la ponctuation de
    la phrase qui l'entoure.
    """
    return f"{value:,.0f}".replace(",", " ")


def _max_plan_cost(plan: dict) -> float:
    """Cout le plus eleve de l'arbre de plan, noeuds imbriques compris."""
    cost = float(plan.get("Total Cost", 0.0))
    for child in plan.get("Plans", []) or []:
        cost = max(cost, _max_plan_cost(child))
    return cost
