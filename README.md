# QueryForge

Interrogez une base PostgreSQL en français, et laissez un agent enquêter sur vos questions ouvertes.

**Stack** : Python · FastAPI · LangChain (Groq) · SQLAlchemy · sqlglot · PostgreSQL · React

## Deux modes, un même socle de sécurité

- **`/ask`** : une question précise, une requête SQL. *« Clients ayant commandé plus de 3 fois ce mois »* → SQL généré, expliqué, validé, borné, exécuté en lecture seule, avec tableau et graphique.
- **`/investigate`** : une question ouverte. *« Pourquoi les ventes ont baissé en septembre ? »* → un agent formule des hypothèses, les teste par des requêtes validées, puis rédige un rapport chiffré avec les requêtes en annexe.

## Pipeline `/ask`

```
question → introspection du schéma → génération SQL (LLM)
         → validation AST (sqlglot) ─ refus → motif renvoyé au modèle (3 essais max)
         → LIMIT injecté → EXPLAIN (refus si trop coûteux)
         → exécution READ ONLY + timeout → tableau + graphique + explication
```

Le schéma n'est jamais codé en dur : il est lu à chaud dans le catalogue PostgreSQL (types, clés, commentaires, valeurs d'exemple des colonnes à faible cardinalité). Changer de base ne demande qu'un `DATABASE_URL`.

## Mode `/investigate`

Le pipeline `/ask` devient l'outil de l'agent : chaque requête passe par la même validation et la même exécution en lecture seule.

```
question ouverte → planificateur (hypothèses)
                 → boucle : outil → analyste (met à jour les hypothèses)
                 → rédacteur (conclusion, constats chiffrés, limites, SQL)
```

- **Outils** : `compare_periods` (écart entre deux périodes), `breakdown` (contribution de chaque segment), `run_sql` (dernier recours). Pour les deux premiers, le code construit le SQL et calcule les variations ; le modèle ne fait qu'interpréter.
- **Garde-fous** : 6 étapes max, coût `EXPLAIN` cumulé plafonné, étapes en double refusées, arrêt après 2 échecs ou 2 étapes sans progrès. Sans résultat exploitable, une erreur est renvoyée plutôt qu'un rapport inventé.
- **Rapport** : conclusion, 3 à 5 constats chiffrés, statut de chaque hypothèse, section « ce que je n'ai pas pu vérifier ».
- **Flux SSE** : `plan`, `step`, `observation`, `analysis`, `report`, `done`.

```bash
curl -N -X POST http://127.0.0.1:8000/investigate \
  -H "Content-Type: application/json" \
  -d '{"question": "Pourquoi le chiffre d affaires a baissé en septembre ?"}'
```

## Sécurité : trois barrières indépendantes

| # | Barrière | Arrête |
|---|----------|--------|
| 1 | Analyse d'AST (`security/guard.py`) | DML, instructions empilées, CTE modifiantes, fonctions dangereuses, tables système |
| 2 | Transaction `READ ONLY` | toute écriture, même par un chemin non prévu |
| 3 | Rôle PostgreSQL en lecture seule | toute écriture, même si l'application est compromise |

La validation porte sur l'AST, jamais sur du texte, et le SQL exécuté est l'AST validé re-sérialisé. Côté bornage : `LIMIT` injecté (500 par défaut, 5 000 max), `EXPLAIN` avant tout accès, `statement_timeout` côté serveur.

## Lancer le projet

**Prérequis** : Python 3.11+, Node 18+, PostgreSQL 14+ (ou Docker), une clé API Groq.

```bash
# 1. Base de données (Docker : les scripts db/*.sql sont joués automatiquement)
docker compose up -d db

# 2. Backend
cd backend
cp .env.example .env     # renseigner OPENAI_API_KEY (clé Groq) et DATABASE_URL
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000

# 3. Frontend
cd ../frontend
npm install && npm run dev      # http://localhost:5173
```

Sans Docker : `psql -d queryforge -f db/01_schema.sql`, puis `02_seed.sql` et `03_readonly_role.sql`.

Vérification : `curl http://127.0.0.1:8000/health`

## API

| Méthode | Route | Rôle |
|---------|-------|------|
| `GET` | `/health` | état de la base et du modèle |
| `GET` | `/schema?refresh=true` | schéma tel que le modèle le voit |
| `POST` | `/ask` | question → SQL validé et exécuté (`dry_run: true` pour ne pas exécuter) |
| `POST` | `/investigate` | question ouverte → enquête en streaming (SSE) |
| `POST` | `/validate` | valide du SQL manuel sans l'exécuter |

## Configuration (variables d'environnement)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `DATABASE_URL` | n/a | **doit pointer vers un rôle en lecture seule** |
| `OPENAI_API_KEY` | n/a | clé du fournisseur LLM (nom historique, Groq actuellement) |
| `LLM_MODEL` | `llama-3.1-8b-instant` | modèle de génération |
| `STATEMENT_TIMEOUT_MS` | `10000` | timeout côté PostgreSQL |
| `DEFAULT_ROW_LIMIT` / `MAX_ROW_LIMIT` | `500` / `5000` | bornage des résultats |
| `MAX_EXPLAIN_COST` | `1000000` | budget de coût par requête |
| `INVESTIGATION_MAX_STEPS` | `6` | étapes max par enquête |
| `INVESTIGATION_MAX_TOTAL_COST` | `5000000` | coût cumulé max par enquête |

## Tests

```bash
cd backend && python -m pytest tests/ -q
```

Couvrent le validateur (contournements connus), le bornage, les outils d'enquête et la boucle de l'agent avec un raisonneur scripté (aucun appel LLM ni base).

## Limites connues

- Un `LIMIT` paramétré n'est pas borné à la réécriture (le timeout prend le relais).
- Cache de schéma de 5 minutes (`/schema?refresh=true` pour forcer).
- Enquêtes : affichage frontend pas encore branché (l'API SSE fonctionne), qualité dépendante du modèle, et jeu d'évaluation à écrire. Outils limités aux filtres d'égalité et à un saut de clé étrangère ; le reste passe par `run_sql`.
