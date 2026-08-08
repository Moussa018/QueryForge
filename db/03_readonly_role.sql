-- Troisieme barriere de securite : le role Postgres lui-meme.
--
-- L'application se connecte avec ce compte. Meme si l'analyse d'AST et la
-- transaction READ ONLY etaient toutes deux contournees, ce role n'a
-- physiquement pas le droit d'ecrire.

CREATE ROLE queryforge_ro WITH LOGIN PASSWORD 'readonly';

-- Aucun droit implicite : on part de zero et on n'ajoute que SELECT.
REVOKE ALL ON SCHEMA public FROM queryforge_ro;
GRANT CONNECT ON DATABASE queryforge TO queryforge_ro;
GRANT USAGE ON SCHEMA public TO queryforge_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO queryforge_ro;

-- Les tables creees plus tard sont couvertes automatiquement.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO queryforge_ro;

-- Interdit la creation d'objets (une table temporaire est encore une ecriture).
REVOKE CREATE ON SCHEMA public FROM queryforge_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM queryforge_ro;

-- Ceinture et bretelles : toute transaction de ce role demarre en lecture seule.
ALTER ROLE queryforge_ro SET default_transaction_read_only = on;
ALTER ROLE queryforge_ro SET statement_timeout = '15s';
