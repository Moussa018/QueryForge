from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration QueryForge. Tout est surchargeable par variable d'environnement."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Base de donnees ---------------------------------------------------
    # Compte applicatif en LECTURE SEULE. Ne jamais pointer un superuser ici.
    database_url: str = "postgresql+psycopg2://queryforge_ro:readonly@127.0.0.1:5432/queryforge"

    # --- LLM ---------------------------------------------------------------
    openai_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.0

    # --- Garde-fous d'execution -------------------------------------------
    # Timeout dur cote Postgres, en millisecondes.
    statement_timeout_ms: int = 10_000
    # LIMIT injecte quand la requete generee n'en a pas.
    default_row_limit: int = 500
    # Plafond absolu : meme un LIMIT explicite du LLM est rabote a cette valeur.
    max_row_limit: int = 5_000
    # Cout estime par EXPLAIN au-dela duquel on refuse d'executer.
    max_explain_cost: float = 1_000_000.0

    # --- Introspection -----------------------------------------------------
    schema_cache_ttl_seconds: int = 300
    # Schemas Postgres exposes au modele. Les catalogues systeme sont exclus.
    included_schemas: tuple[str, ...] = ("public",)
    # Nombre de valeurs distinctes echantillonnees par colonne texte basse cardinalite.
    sample_values_per_column: int = 8

    cors_origins: tuple[str, ...] = ("http://localhost:5173",)


@lru_cache
def get_settings() -> Settings:
    return Settings()
