"""Traduction langage naturel -> SQL via GPT-4o (LangChain).

Le schema est injecte dans le prompt a chaque appel : c'est ce qui permet de
brancher QueryForge sur n'importe quelle base sans rien recoder.

La sortie est structuree (pas de parsing de markdown a la main) et l'explication
en francais est produite dans le *meme* appel que le SQL. Un second appel
"explique ce SQL" produirait une explication plausible mais deconnectee de
l'intention reelle du modele -- et couterait le double.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.config import Settings
from app.schema.introspector import DatabaseSchema

SYSTEM_PROMPT = """\
Tu es un expert SQL PostgreSQL. Tu traduis des questions metier en francais en \
une requete SQL unique, correcte et performante.

REGLES ABSOLUES
1. Lecture seule. Uniquement SELECT ou WITH ... SELECT. Jamais INSERT, UPDATE, \
DELETE, DROP, ALTER, CREATE, GRANT, TRUNCATE, ni CTE modifiante.
2. Une seule instruction. Aucun point-virgule de separation, aucun SQL empile.
3. N'utilise que les tables et colonnes presentes dans le schema ci-dessous. \
N'invente jamais un nom. Si la question demande une donnee absente du schema, \
mets `feasible` a false et explique ce qui manque.
4. Interdiction d'interroger pg_catalog, information_schema ou toute table pg_*.
5. Ajoute toujours un LIMIT explicite, sauf si la requete est une agregation \
qui ne renvoie qu'une poignee de lignes.

QUALITE
- Utilise les cles etrangeres du schema pour joindre : ne devine pas les liens.
- Respecte la casse exacte des valeurs d'exemple donnees pour chaque colonne.
- Pour "ce mois", "cette annee", utilise des expressions relatives \
(date_trunc('month', CURRENT_DATE)) et non des dates en dur.
- Prefere les jointures explicites (JOIN ... ON) aux jointures implicites.
- Pour une condition d'absence ("sans avoir ouvert d'email"), prefere \
NOT EXISTS a NOT IN : NOT IN renvoie zero ligne si la sous-requete contient \
un NULL.

EXPLICATION
Redige `explanation` en francais, pour un analyste qui ne lit pas le SQL. \
Decris ce que la requete calcule et les regles metier appliquees, pas la \
syntaxe. 2 a 4 phrases. Ne commence pas par "Cette requete".
"""

USER_PROMPT = """\
Schema de la base ({dialect}) :

{schema}

Question de l'analyste :
{question}
{feedback}
"""


class GeneratedQuery(BaseModel):
    """Sortie structuree attendue du modele."""

    feasible: bool = Field(
        description="False si la question ne peut pas etre repondue avec ce schema."
    )
    sql: str = Field(default="", description="La requete SQL. Vide si feasible=false.")
    explanation: str = Field(
        description="Explication en francais de ce que fait la requete, ou de ce qui manque."
    )
    chart_hint: str = Field(
        default="none",
        description="Visualisation adaptee : 'bar', 'line', 'pie', 'none'.",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Interpretations faites face a une question ambigue.",
    )


@dataclass
class GenerationContext:
    """Retour d'erreur d'une tentative precedente, pour l'auto-correction."""

    failed_sql: str
    error: str


class SQLGenerator:
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY absent : impossible de generer du SQL. "
                "Renseignez-le dans backend/.env"
            )
        llm = ChatOpenAI(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            api_key=settings.openai_api_key,
            timeout=60,
            max_retries=2,
        )
        self._chain = (
            ChatPromptTemplate.from_messages(
                [("system", SYSTEM_PROMPT), ("human", USER_PROMPT)]
            )
            | llm.with_structured_output(GeneratedQuery)
        )

    def generate(
        self,
        question: str,
        schema: DatabaseSchema,
        retry_context: GenerationContext | None = None,
    ) -> GeneratedQuery:
        feedback = ""
        if retry_context is not None:
            feedback = (
                "\nTa tentative precedente a ete REFUSEE.\n"
                f"SQL propose :\n{retry_context.failed_sql}\n"
                f"Motif du refus : {retry_context.error}\n"
                "Corrige la requete en tenant compte de ce motif."
            )

        return self._chain.invoke(
            {
                "schema": schema.render_for_prompt(),
                "dialect": schema.dialect,
                "question": question,
                "feedback": feedback,
            }
        )
