"""Runtime configuration loaded from `.env`.

Fail-fast: every variable is required (or has an explicit default). Missing or
malformed values raise on import via Pydantic validation. There is no silent
fallback.

The `.env` file and SQLite database file are resolved relative to the project
root (computed from this file's location), not the process cwd. This keeps
behavior identical whether you launch `uvicorn` from the project root, from
the parent directory, or via a tool that spawns from elsewhere.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# `config.py` lives at <root>/src/portfolio_tracker/config.py — three parents up.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
ENV_FILE: Path = PROJECT_ROOT / ".env"


class PlaidEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="forbid",
    )

    plaid_client_id: str = Field(alias="PLAID_CLIENT_ID", min_length=1)
    plaid_secret: str = Field(alias="PLAID_SECRET", min_length=1)
    plaid_env: PlaidEnvironment = Field(alias="PLAID_ENV")
    plaid_products: str = Field(alias="PLAID_PRODUCTS", default="investments")
    plaid_country_codes: str = Field(alias="PLAID_COUNTRY_CODES", default="US")

    fernet_key: str = Field(alias="FERNET_KEY", min_length=1)

    # SnapTrade — optional. If unset, SnapTrade routes return 503 cleanly
    # and the rest of the app keeps working with Plaid only.
    snaptrade_client_id: str | None = Field(alias="SNAPTRADE_CLIENT_ID", default=None)
    snaptrade_consumer_key: str | None = Field(alias="SNAPTRADE_CONSUMER_KEY", default=None)

    # Optional. Directory containing the out-of-repo `claude_cli.py` wrapper
    # used by the CIO advisor (routes Claude calls through the user's Pro/Max
    # subscription, not the metered API). If unset, the advisor's LLM features
    # raise a clear ImportError when invoked; the rest of the app is unaffected.
    claude_cli_dir: str | None = Field(alias="CLAUDE_CLI_DIR", default=None)

    database_url: str = Field(alias="DATABASE_URL", default="sqlite:///./portfolio.db")

    cors_origins: str = Field(alias="CORS_ORIGINS", default="http://localhost:5173")

    # Optional companion project: earnings-summary. When present, the Holdings
    # and Trade Analysis surfaces enrich each ticker with next-earnings date,
    # thesis breach status, and a link to the latest research brief. When
    # the DB / output dir don't exist, enrichment is silently skipped.
    #
    # Default resolves to `../earnings-summary/data/portfolio.db` relative
    # to PROJECT_ROOT, which is where the sibling project lives by
    # convention.
    earnings_summary_db_path: str = Field(
        alias="EARNINGS_SUMMARY_DB_PATH",
        default="../earnings-summary/data/portfolio.db",
    )
    earnings_summary_output_dir: str = Field(
        alias="EARNINGS_SUMMARY_OUTPUT_DIR",
        default="../earnings-summary/output",
    )

    # Recipient for the scheduled monthly-brief email (jobs.email_brief).
    # PII — lives in `.env` only, never committed. If unset, the email job
    # refuses to send with a clear error; the rest of the app is unaffected.
    brief_email_recipient: str | None = Field(alias="BRIEF_EMAIL_RECIPIENT", default=None)

    # Google OAuth artifacts for Gmail send. Defaults resolve to the repo root
    # (both gitignored). Reuse the same credentials.json as other Google-API
    # projects on this machine; the token is minted per-project per-scope by
    # `python -m portfolio_tracker.jobs.email_brief --authorize`.
    google_credentials_path: str = Field(
        alias="GOOGLE_CREDENTIALS_PATH", default="credentials.json"
    )
    google_token_path: str = Field(alias="GOOGLE_TOKEN_PATH", default="token.json")

    @property
    def resolved_earnings_summary_db_path(self) -> Path:
        p = Path(self.earnings_summary_db_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve()

    @property
    def resolved_earnings_summary_output_dir(self) -> Path:
        p = Path(self.earnings_summary_output_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve()

    @property
    def resolved_google_credentials_path(self) -> Path:
        p = Path(self.google_credentials_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve()

    @property
    def resolved_google_token_path(self) -> Path:
        p = Path(self.google_token_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve()

    @field_validator("plaid_products", "plaid_country_codes", "cors_origins")
    @classmethod
    def _strip_whitespace(cls, v: str) -> str:
        return v.strip()

    @property
    def plaid_products_list(self) -> list[str]:
        return [p.strip() for p in self.plaid_products.split(",") if p.strip()]

    @property
    def plaid_country_codes_list(self) -> list[str]:
        return [c.strip() for c in self.plaid_country_codes.split(",") if c.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_database_url(self) -> str:
        """Return `database_url` with relative SQLite paths anchored to PROJECT_ROOT.

        Without this, launching uvicorn from a different cwd would create a
        second `portfolio.db` file in that cwd — the schema migration was
        applied to the right one but the app would silently use a fresh empty
        copy. Anchoring relative paths makes the resolution deterministic.
        """
        url = self.database_url
        prefix = "sqlite:///"
        if not url.startswith(prefix):
            return url
        path_part = url[len(prefix) :]
        if path_part.startswith("./") or path_part.startswith(".\\"):
            absolute = (PROJECT_ROOT / path_part[2:]).as_posix()
            return f"{prefix}{absolute}"
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
