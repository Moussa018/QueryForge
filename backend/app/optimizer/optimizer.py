"""Optimisation et bornage de la requete avant execution.

Garantit qu'aucune requete ne peut ramener 10 millions de lignes dans le
navigateur, meme si le modele a oublie le LIMIT. Le timeout cote serveur
(statement_timeout) borne la duree des requetes lourdes.

Les reecritures se font sur l'AST deja valide, jamais sur la chaine.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from app.config import Settings


@dataclass
class OptimizationResult:
    sql: str
    notes: list[str]
    limit_applied: int | None


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

