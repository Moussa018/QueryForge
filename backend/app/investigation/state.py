"""Etat d'une enquete : hypotheses, observations, budgets.

L'etat est la memoire de l'agent. Il evite de refaire deux fois la meme
requete et alimente le rapport final.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

HYPOTHESIS_STATUSES = ("pending", "confirmed", "rejected", "inconclusive")


class Action(BaseModel):
    """Prochaine etape demandee par le modele. Les champs inutiles restent vides."""

    tool: str = Field(
        description="compare_periods | breakdown | run_sql | finish"
    )
    rationale: str = Field(default="", description="Pourquoi cette etape, en une phrase.")
    hypothesis_id: str = Field(default="", description="Hypothese testee par cette etape.")
    # metrique
    table: str = ""
    aggregate: str = Field(default="", description="sum | avg | count | count_distinct")
    column: str = Field(default="", description="Colonne agregee ; vide pour count(*).")
    date_column: str = ""
    filters: dict[str, str] = Field(
        default_factory=dict, description="Egalites colonne -> valeur appliquees a la table."
    )
    dimension: str = Field(
        default="", description="Pour breakdown : colonne, ou table_liee.colonne (cle etrangere directe)."
    )
    # periodes, bornes ISO, fin exclue
    p1_start: str = ""
    p1_end: str = ""
    p2_start: str = ""
    p2_end: str = ""
    # run_sql
    sql: str = ""

    def key(self) -> str:
        """Identite de l'action, pour detecter une requete deja faite."""
        payload = self.model_dump(exclude={"rationale", "hypothesis_id"})
        return json.dumps(payload, sort_keys=True)


class HypothesisUpdate(BaseModel):
    hypothesis_id: str
    status: str = Field(description="pending | confirmed | rejected | inconclusive")
    evidence: str = Field(default="", description="Chiffres precis tires des observations.")


class Hypothesis(BaseModel):
    id: str
    statement: str


class Plan(BaseModel):
    """Sortie du planificateur."""

    hypotheses: list[Hypothesis] = Field(description="3 a 5 hypotheses testables.")
    first_action: Action


class Analysis(BaseModel):
    """Sortie de l'analyste apres chaque observation."""

    interpretation: str = Field(description="Ce que montre le dernier resultat, chiffres a l'appui.")
    updates: list[HypothesisUpdate] = Field(default_factory=list)
    next_action: Action = Field(description="Prochaine etape, ou tool='finish' si assez d'elements.")


class Finding(BaseModel):
    statement: str
    evidence: str = Field(description="Chiffres issus des observations, jamais inventes.")


class ReportDraft(BaseModel):
    """Partie redigee par le modele ; le SQL d'annexe est ajoute par le code."""

    conclusion: str = Field(description="La cause principale en une phrase, ou l'absence de cause nette.")
    findings: list[Finding] = Field(description="3 a 5 constats chiffres.")
    unverified: list[str] = Field(
        default_factory=list, description="Ce qui n'a pas pu etre verifie, limites, correlation vs causalite."
    )


@dataclass
class Observation:
    step: int
    action: Action
    summary: dict[str, Any] | None = None
    sql: str | None = None
    error: str | None = None

    def render(self, max_chars: int = 3500) -> str:
        head = f"[etape {self.step}] {self.action.tool} -- {self.action.rationale}"
        if self.error:
            return f"{head}\nERREUR : {self.error}"
        body = json.dumps(self.summary, ensure_ascii=False, default=str)
        if len(body) > max_chars:
            body = body[:max_chars] + " ...[tronque]"
        return f"{head}\n{body}"


@dataclass
class InvestigationState:
    question: str
    hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    consecutive_errors: int = 0
    stalled_steps: int = 0

    def set_hypotheses(self, hypotheses: list[Hypothesis]) -> None:
        for h in hypotheses:
            self.hypotheses[h.id] = h
            self.status[h.id] = "pending"

    def apply_updates(self, updates: list[HypothesisUpdate]) -> bool:
        """Applique les mises a jour ; renvoie True si au moins un statut a change."""
        changed = False
        for u in updates:
            if u.hypothesis_id not in self.hypotheses or u.status not in HYPOTHESIS_STATUSES:
                continue
            if self.status[u.hypothesis_id] != u.status:
                changed = True
            self.status[u.hypothesis_id] = u.status
            if u.evidence:
                self.evidence[u.hypothesis_id] = u.evidence
        return changed

    def render_hypotheses(self) -> str:
        lines = []
        for hid, h in self.hypotheses.items():
            line = f"- {hid} [{self.status[hid]}] {h.statement}"
            if hid in self.evidence:
                line += f" -- {self.evidence[hid]}"
            lines.append(line)
        return "\n".join(lines) or "(aucune)"

    def render_observations(self) -> str:
        return "\n\n".join(o.render() for o in self.observations) or "(aucune)"
