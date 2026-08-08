# QueryForge

Moteur de requêtes SQL généré depuis le langage naturel, branché sur une vraie
base de données.

Un analyste écrit *« montre-moi les clients qui ont commandé plus de 3 fois ce
mois sans ouvrir aucun email »*. QueryForge découvre le schéma, génère le SQL,
l'explique en français, le valide, le borne, puis l'exécute en lecture seule et
renvoie un tableau et un graphique.

**Stack** : Python · GPT-4o · LangChain · SQLAlchemy · PostgreSQL · React

---

## Le pipeline

```
question en français
        │
        ▼
 [1] introspection du schéma          SQLAlchemy inspect + pg_class
        │                             (cache TTL 5 min)
        ▼
 [2] génération SQL                   GPT-4o, sortie structurée
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
 [5] EXPLAIN                          refus si trop coûteux
        │
        ▼
 [6] exécution                        transaction READ ONLY + timeout
        │
        ▼
 tableau + graphique + explication
```

L'étape 3 est la plus utile en pratique : quand le modèle invente une colonne,
le motif exact du refus lui est renvoyé et il se corrige au tour suivant. Rien
d'invalide n'atteint jamais la base.

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
ligne — le pire des échecs, parce qu'il est silencieux. Les colonnes à forte
cardinalité (`email`, `full_name`) sont détectées et exclues de
l'échantillonnage pour ne pas gonfler le prompt.

Brancher QueryForge sur une autre base ne demande aucune modification de code :
seulement un `DATABASE_URL`.

## Sécurité : trois barrières indépendantes

Chacune serait suffisante en théorie. Elles sont cumulées parce qu'une chaîne
produite par un LLM ne mérite aucune confiance.

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
- `EXPLAIN` (sans `ANALYZE`, donc sans exécution) avant tout accès réel, avec
  refus au-delà d'un budget de coût ;
- `statement_timeout` et `lock_timeout` côté serveur ;
- signalement des motifs coûteux : produit cartésien, `ORDER BY` sans `LIMIT`,
  `COUNT(*)`.

> Le coût retenu est le **maximum de l'arbre de plan**, pas celui du nœud racine.
> Sous un `LIMIT`, Postgres ne compte à la racine que les lignes remontées : s'en
> tenir à celle-ci laisserait le `LIMIT` que nous injectons nous-mêmes désamorcer
> notre propre plafond. Un produit cartésien sur 18 000 lignes remonte ainsi à
> 450 213 au lieu de 12.

---

## Lancer le projet

### Prérequis

- Python 3.11+
- Node 18+
- PostgreSQL 14+ (ou Docker)
- Une clé API OpenAI **avec des crédits**

### 1. Base de données

**Option A — PostgreSQL déjà installé** (aucun conteneur, plus léger)

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

Vérification :

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","database":true,"llm":true,"model":"gpt-4o"}
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
| `POST` | `/validate` | passe du SQL écrit à la main dans le validateur, sans l'exécuter |

`POST /ask` accepte `{"question": "...", "dry_run": true}` : la requête est
générée, validée et estimée, mais **pas exécutée**. Utile pour inspecter le SQL
avant de le lancer sur une base de production.

## Tests

```bash
cd backend
python -m pytest tests/ -q
```

La suite couvre le validateur (requêtes légitimes, écritures directes,
contournements connus) et le bornage. Les cas d'attaque de
`tests/test_guard.py` correspondent chacun à une façon documentée de tromper un
filtre naïf.

## Configuration

Tout est surchargeable par variable d'environnement (voir `backend/.env.example`).

| Variable | Défaut | Rôle |
|---|---|---|
| `DATABASE_URL` | — | **doit pointer un rôle en lecture seule** |
| `OPENAI_API_KEY` | — | clé OpenAI |
| `LLM_MODEL` | `gpt-4o` | modèle de génération |
| `STATEMENT_TIMEOUT_MS` | `10000` | timeout dur côté Postgres |
| `DEFAULT_ROW_LIMIT` | `500` | `LIMIT` injecté quand il manque |
| `MAX_ROW_LIMIT` | `5000` | plafond absolu |
| `MAX_EXPLAIN_COST` | `1000000` | budget de coût `EXPLAIN` |
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
