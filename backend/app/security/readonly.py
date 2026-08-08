"""Deuxieme barriere : contraintes imposees par le serveur, pas par notre code.

L'analyse d'AST peut avoir un angle mort. Une transaction `READ ONLY` n'en a
pas : Postgres refusera toute ecriture quoi qu'il arrive, y compris via une
fonction qu'on n'aurait pas listee comme interdite.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Connection, Engine, text

from app.config import Settings


@contextmanager
def readonly_transaction(engine: Engine, settings: Settings) -> Iterator[Connection]:
    """Transaction en lecture seule, bornee dans le temps, annulee a la sortie.

    Le ROLLBACK final est systematique : il n'y a rien a valider, et c'est une
    garantie de plus qu'aucun effet de bord ne survit a la requete.
    """
    conn = engine.connect()
    try:
        trans = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            # SET LOCAL : la valeur retombe a la fin de la transaction, donc la
            # connexion rendue au pool n'est pas contaminee.
            conn.execute(text(f"SET LOCAL statement_timeout = {settings.statement_timeout_ms}"))
            # Evite qu'une requete lourde bloque un writer sur un verrou.
            conn.execute(text("SET LOCAL lock_timeout = 3000"))
            # Neutralise un search_path detourne qui ferait resoudre `users` vers
            # une table pieges dans un autre schema.
            conn.execute(text("SET LOCAL search_path = public"))
            yield conn
        finally:
            trans.rollback()
    finally:
        conn.close()
