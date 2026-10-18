# QueryForge

Un agent qui enquête sur une base PostgreSQL. Tu poses une question ouverte
(« pourquoi les ventes ont baissé en septembre ? »), il teste des hypothèses avec
des requêtes en lecture seule, puis rédige un rapport chiffré.

**Stack** : Python · FastAPI · LangChain (Groq) · SQLAlchemy · sqlglot · PostgreSQL · Docker

## Comment ça marche

1. Un planificateur propose 3 à 5 hypothèses.
2. À chaque étape, un outil mesure quelque chose : `compare_periods` (deux
   périodes) ou `breakdown` (par catégorie).
3. Un analyste interprète le résultat, met à jour les hypothèses et choisit la suite.
4. Un rédacteur produit le rapport, avec les requêtes en annexe.

Le modèle ne fournit jamais de SQL : il décrit ce qu'il veut mesurer, le code
écrit la requête, la vérifie et l'exécute. Les chiffres sont calculés par le code.

## Sécurité

Trois barrières indépendantes protègent la base :

- le **garde AST** refuse tout sauf une lecture (`SELECT`) ;
- une **transaction en lecture seule** avec un timeout ;
- un **rôle Postgres** qui n'a que le droit `SELECT`.

Un `LIMIT` est ajouté (500) et plafonné (5 000).

## Lancer

```bash
docker compose up -d db          # base de démo
cd backend
cp .env.example .env             # renseigner OPENAI_API_KEY (clé Groq)
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

```bash
curl -X POST http://127.0.0.1:8000/investigate \
  -H "Content-Type: application/json" \
  -d '{"question": "Pourquoi le chiffre d affaires a baissé en septembre ?"}'
```

La réponse contient `steps` (le déroulé) et `report` (le rapport).

## Limites

- La fiabilité de l'agent n'est pas encore mesurée.
- Le code n'a pas été exécuté sur la machine de développement : lancer
  `python -m pytest tests/ -q` avant de s'y fier.
- Pas d'authentification.