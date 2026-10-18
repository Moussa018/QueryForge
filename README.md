# QueryForge

Interrogez une vraie base PostgreSQL en français, et laissez un agent enquêter
sur vos questions ouvertes.

QueryForge a deux modes, bâtis sur le même socle de sécurité :

- **`/ask`** : une question précise, une requête SQL. *« Montre-moi les clients
  qui ont commandé plus de 3 fois ce mois sans ouvrir aucun email. »* QueryForge
  découvre le schéma, génère le SQL, l'explique en français, le valide, le borne,
  l'exécute en lecture seule et renvoie un tableau et un graphique.
- **`/investigate`** : une question ouverte. *« Pourquoi les ventes ont baissé en
  septembre ? »* Un agent formule des hypothèses, les teste par une suite de
  requêtes validées, écarte ou confirme chaque piste, puis rédige un rapport
  chiffré avec les requêtes en annexe.

**Stack** : Python · FastAPI · LangChain (Groq) · SQLAlchemy · sqlglot · PostgreSQL · React

---

## Sommaire

1. [Le pipeline `/ask`](#le-pipeline-ask)
2. [Le mode enquête `/investigate`](#le-mode-enquête-investigate)
3. [Le schéma n'est jamais codé en dur](#le-schéma-nest-jamais-codé-en-dur)
4. [Sécurité : trois barrières indépendantes](#sécurité--trois-barrières-indépendantes)
5. [Optimisation et bornage](#optimisation-et-bornage)
6. [Lancer le projet](#lancer-le-projet)
7. [API](#api)
8. [Tests](#tests)
9. [Configuration](#configuration)
10. [Visualisation](#visualisation)
11. [Limites connues](#limites-connues)

---

## Le pipeline `/ask`

```
question en français
        │
        ▼
 [1] introspection du schéma          SQLAlchemy inspect + pg_class
        │                             (cache TTL 5 min)
        ▼
 [2] génération SQL                   LLM, sortie structurée
        │
        ▼
 [3] validation AST ──── refus ──┐    sqlglot
        │                        │
        │            renvoi du motif au modèle (3 tentatives max)
        │                        │
        │◄───────────────────────┘
        ▼
 [4] bornage / optimisation           LIMIT injecté, plafond serveur
        │
        ▼
 [5] exécution                        transaction READ ONLY + timeout
        │
        ▼
 tableau + graphique + explication
```

L'étape 3 est la plus utile en pratique : quand le modèle invente une colonne,
le motif exact du refus lui est renvoyé et il se corrige au tour suivant. Rien
d'invalide n'atteint jamais la base.

## Le mode enquête `/investigate`

Le pipeline ci-dessus devient l'**outil** de l'agent : chaque requête de
l'enquête passe par la même validation AST, le même bornage et la même
exécution en lecture seule (`QueryForgePipeline.run_sql`).

```
question ouverte
      │
      ▼
 [Planificateur]  3 à 5 hypothèses testables + première étape
      │
      ▼
 ┌─► [Outil]      compare_periods · breakdown · run_sql
 │        │
 │        ▼
 │   [Analyste]   interprète, met à jour les hypothèses, choisit la suite
 └────────┘       (boucle bornée par les garde-fous)
      │
      ▼
 [Rédacteur]      conclusion, constats chiffrés, limites, SQL en annexe
```

### Les outils

| Outil | Rôle |
|-------|------|
| `compare_periods` | une métrique sur deux périodes : valeurs, écart, variation en % |
| `breakdown` | la même métrique ventilée par une dimension, avec la part de chaque segment dans la variation totale |
| `run_sql` | SQL libre, en dernier recours, derrière les mêmes barrières |

Pour `compare_periods` et `breakdown`, **le modèle ne fournit pas de SQL**. Il
décrit une métrique (table, agrégat, colonne, colonne de date, filtres
d'égalité) et le code construit la requête : chaque identifiant est vérifié
contre le schéma réel puis cité, chaque valeur échappée. Une dimension peut être
une colonne de la table, ou `table_liée.colonne` via une clé étrangère directe
(jointure automatique).

Les variations, pourcentages et contributions sont **calculés par le code**, pas
par le modèle : il interprète des chiffres, il ne les calcule pas.

### Garde-fous

- 6 étapes maximum par enquête ;
- une étape identique à une étape déjà faite est refusée sans être réexécutée ;
- arrêt après 2 échecs d'outil consécutifs ;
- arrêt si 2 étapes de suite ne font changer aucun statut d'hypothèse ;
- sans aucun résultat exploitable, l'agent renvoie une erreur plutôt qu'un
  rapport inventé.

### Honnêteté du rapport

Le rapport contient la conclusion en une phrase, 3 à 5 constats chiffrés, le
statut de chaque hypothèse (`confirmed`, `rejected`, `inconclusive`, `pending`)
et une section « ce que je n'ai pas pu vérifier ». Le SQL en annexe est repris de
l'état de l'enquête : le modèle ne peut pas le modifier. Le prompt impose de
distinguer corrélation et causalité et de ne pas inventer de cause quand les
données n'en montrent pas.

### Réponse de `/investigate`

`POST /investigate` attend la fin de l'enquête puis renvoie un JSON
`{"steps": [...], "report": {...}}` :

| Champ | Contenu |
|-------|---------|
| `steps` | le déroulé, dans l'ordre : `plan` (hypothèses), puis pour chaque étape `step` (outil et justification), `observation` (résultat chiffré ou erreur, SQL exécuté) et `analysis` (interprétation, statut à jour de chaque hypothèse) |
| `report` | conclusion, constats, limites, hypothèses, requêtes, motif d'arrêt |

Sans aucun résultat exploitable, la route répond `422` avec le message.

```bash
curl -X POST http://127.0.0.1:8000/investigate \
  -H "Content-Type: application/json" \
  -d '{"question": "Pourquoi le chiffre d affaires a baissé en septembre ?"}'
```

## Le schéma n'est jamais codé en dur

L'introspection lit le catalogue Postgres à chaud et produit une représentation
compacte pour le prompt : types, clés primaires, clés étrangères, commentaires,
nombre de lignes approximatif (via `pg_class`, jamais un `COUNT(*)`), et surtout
des **valeurs d'exemple** pour les colonnes texte à faible cardinalité :

```
TABLE public.orders  -- ~6 000 lignes (
  id INTEGER PK NOT NULL,
  customer_id INTEGER NOT NULL,
  status TEXT NOT NULL  -- Etat de la commande | valeurs: cancelled, delivered, paid, pending, shipped,
  total_amount NUMERIC(10, 2) NOT NULL,
  created_at TIMESTAMP NOT NULL,
)
  FK (customer_id) -> public.customers(id)
```

Sans ces valeurs, le modèle écrit `WHERE status = 'Shipped'` au lieu de
`'shipped'` : la requête est valide, ne lève aucune erreur, et renvoie zéro
ligne. Les colonnes à forte cardinalité (`email`, `full_name`) sont détectées et
exclues de l'échantillonnage pour ne pas gonfler le prompt.

Brancher QueryForge sur une autre base ne demande aucune modification de code :
seulement un `DATABASE_URL`.

## Sécurité : trois barrières indépendantes

Chacune serait suffisante en théorie. Elles sont cumulées parce qu'une chaîne
produite par un LLM ne mérite aucune confiance. Les deux modes les partagent.

| # | Barrière | Où | Ce qu'elle arrête |
|---|----------|-----|-------------------|
| 1 | Analyse d'AST | `app/security/guard.py` | DML, instructions empilées, CTE modifiantes, fonctions dangereuses, tables système, tables hallucinées |
| 2 | Transaction `READ ONLY` | `app/security/readonly.py` | toute écriture, y compris par un chemin non prévu |
| 3 | Rôle PostgreSQL | `db/03_readonly_role.sql` | toute écriture, même si l'application est compromise |

La validation porte sur l'**AST**, jamais sur du texte. Un filtre par expression
régulière du type `^SELECT` laisse passer :

```sql
/* SELECT */ DELETE FROM customers                            -- commentaire masquant
SELECT 1; DROP TABLE customers                                -- empilement
WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d    -- CTE modifiante
SELECT * FROM customers WHERE pg_sleep(30) IS NULL            -- déni de service
```

Les quatre sont refusés, ainsi que `pg_read_file`, `dblink`, `lo_import`,
`pg_catalog`, `information_schema` et les tables `pg_*`. Le SQL réellement
exécuté est l'AST validé **re-sérialisé**, jamais la chaîne d'origine : sinon la
validation porterait sur un texte différent de ce qui part en base.

La barrière 2 a été vérifiée en connectant un **superutilisateur** : le `DELETE`
échoue quand même.

## Optimisation et bornage

- `LIMIT` injecté quand il manque (500 par défaut), et raboté à 5 000 même si le
  modèle en demande plus ;
- `statement_timeout` et `lock_timeout` côté serveur ;
- signalement des motifs coûteux : produit cartésien, `ORDER BY` sans `LIMIT`,
  `COUNT(*)`.

> Il n'y a pas d'estimation de coût (`EXPLAIN`) avant l'exécution : une requête
> lourde mais bornée par le `LIMIT` n'est arrêtée que par le `statement_timeout`
> (10 s par défaut), après avoir consommé du CPU côté Postgres.

---

## Lancer le projet

### Prérequis

- Python 3.11+
- Node 18+
- PostgreSQL 14+ (ou Docker)
- Une clé API du fournisseur LLM, **avec des crédits** (le code utilise Groq via
  `langchain-groq`)

### 1. Base de données

**Option A — PostgreSQL déjà installé**

```bash
# adapter le port : 5432 par défaut, 5433 sur certaines installations Windows
export PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres

psql -c "CREATE DATABASE queryforge;"
psql -d queryforge -f db/01_schema.sql
psql -d queryforge -f db/02_seed.sql
psql -d queryforge -f db/03_readonly_role.sql
```

Sous Windows, `psql` se trouve dans
`C:\Program Files\PostgreSQL\<version>\bin\psql.exe`.

**Option B — Docker**

```bash
docker compose up -d db
```

Les trois fichiers `db/*.sql` sont joués automatiquement au premier démarrage.

### 2. Backend

```bash
cd backend
cp .env.example .env        # puis renseigner OPENAI_API_KEY et DATABASE_URL
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

> Le nom de variable `OPENAI_API_KEY` est historique : il contient la clé du
> fournisseur configuré (Groq actuellement). Le code importe `langchain_groq` ;
> si l'installation échoue sur cet import, `pip install langchain-groq`.

Vérification :

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","database":true,"llm":true,"model":"llama-3.1-8b-instant"}
```

`"status":"degraded"` signale que la base ou la clé API manque — le détail est
dans la réponse.

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Ouvrir **http://localhost:5173**. Vite relaie `/api/*` vers le backend, donc
aucune question de CORS en développement.

---

## API

| Méthode | Route | Rôle |
|---------|-------|------|
| `GET` | `/health` | état de la base et du modèle |
| `GET` | `/schema?refresh=true` | schéma tel que le modèle le voit — à consulter en premier quand une génération déraille |
| `POST` | `/ask` | question en français → SQL généré, validé, borné, exécuté |
| `POST` | `/investigate` | question ouverte → enquête puis rapport (JSON) |
| `POST` | `/validate` | passe du SQL écrit à la main dans le validateur, sans l'exécuter |

`POST /ask` accepte `{"question": "...", "dry_run": true}` : la requête est
générée, validée et estimée, mais **pas exécutée**. Utile pour inspecter le SQL
avant de le lancer sur une base de production.

## Structure

```
backend/app/
├── main.py              API FastAPI
├── pipeline.py          orchestration /ask + run_sql (réutilisé par l'agent)
├── nlp/                 génération SQL (LLM, sortie structurée)
├── schema/              introspection et cache
├── security/            validation AST, transaction READ ONLY
├── optimizer/           LIMIT, détection de motifs coûteux
├── executor/            exécution et sérialisation des résultats
└── investigation/       agent d'enquête
    ├── agent.py         boucle plan → outil → analyse → rapport
    ├── tools.py         compare_periods, breakdown, run_sql
    └── state.py         hypothèses, observations, budgets
db/                      schéma de démo, données, rôle lecture seule
frontend/                React + Vite
```

## Tests

```bash
cd backend
python -m pytest tests/ -q
```

- `tests/test_guard.py` : requêtes légitimes, écritures directes, contournements
  connus. Chaque cas d'attaque correspond à une façon documentée de tromper un
  filtre naïf.
- `tests/test_optimizer.py` : bornage du `LIMIT`.
- `tests/test_investigation.py` : construction SQL des outils (validée par le
  garde AST, injection par valeur de filtre refusée), calculs de variation et de
  contribution, et boucle de l'agent avec un raisonneur scripté (rapport
  nominal, étape en double, échecs consécutifs, stagnation, budget d'étapes). Ces
  tests n'appellent ni LLM ni base.

## Configuration

Tout est surchargeable par variable d'environnement (voir `backend/.env.example`).

| Variable | Défaut | Rôle |
|---|---|---|
| `DATABASE_URL` | — | **doit pointer un rôle en lecture seule** |
| `OPENAI_API_KEY` | — | clé du fournisseur LLM |
| `LLM_MODEL` | `llama-3.1-8b-instant` | modèle de génération |
| `STATEMENT_TIMEOUT_MS` | `10000` | timeout dur côté Postgres |
| `DEFAULT_ROW_LIMIT` | `500` | `LIMIT` injecté quand il manque |
| `MAX_ROW_LIMIT` | `5000` | plafond absolu |
| `INVESTIGATION_MAX_STEPS` | `6` | étapes maximum par enquête |
| `INVESTIGATION_STALL_LIMIT` | `2` | étapes consécutives sans progrès avant arrêt |
| `SCHEMA_CACHE_TTL_SECONDS` | `300` | durée du cache de schéma |
| `INCLUDED_SCHEMAS` | `["public"]` | schémas exposés au modèle |

## Visualisation

Le type de graphique est choisi à partir de la **forme des données** (colonne
temporelle → courbe, colonne catégorielle → barres), l'indication du modèle ne
servant que de départage : il se trompe bien plus souvent sur le graphique que
sur le SQL. Au-delà de 30 catégories, aucun graphique n'est affiché — le tableau
reste la source complète.

La palette est validée pour le daltonisme sur les paires adjacentes, en thème
clair comme en thème sombre, et le tableau accompagne toujours le graphique :
l'identité d'une série ne repose jamais sur la couleur seule. Un `chart_hint`
`pie` est volontairement rendu en barres — comparer des longueurs sur une base
commune est plus fiable que comparer des angles.

## Limites connues

- Le `LIMIT` non littéral (paramétré) n'est pas borné à la réécriture ; le
  timeout et le troncage des résultats prennent le relais.
- Le cache de schéma a un TTL de 5 minutes : une migration récente peut ne pas
  être visible immédiatement (`GET /schema?refresh=true` pour forcer).
- L'échantillonnage de valeurs interroge la base à chaque rafraîchissement du
  cache ; sur une base à très nombreuses tables, augmenter le TTL.
- **Enquêtes** : l'affichage dans le frontend n'est pas encore branché (l'API
  est utilisable directement), et la qualité des enquêtes dépend fortement
  du modèle. Un petit modèle remplit moins bien la structure d'action ; la
  fiabilité se mesure sur des scénarios où la cause est plantée dans les
  données, ce jeu d'évaluation reste à écrire.
- Les outils d'enquête ne supportent que des filtres d'égalité et des
  dimensions à un saut de clé étrangère ; le reste passe par `run_sql`.
