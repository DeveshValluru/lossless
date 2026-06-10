from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Google Cloud / Gemini. We support both auth paths:
    # * Vertex AI (GOOGLE_CLOUD_PROJECT + ADC) — the canonical "Google Cloud" path.
    # * AI Studio API key (GOOGLE_API_KEY) — easiest for local dev / demos.
    # If only one is set we use whichever is present; if both, prefer Vertex.
    google_genai_use_vertexai: bool = Field(default=False)
    google_cloud_project: str = Field(default="")
    google_cloud_location: str = Field(default="us-central1")
    google_api_key: str = Field(default="")
    gemini_model: str = Field(default="gemini-3-pro")
    gemini_fallback_model: str = Field(default="gemini-2.5-flash")

    # Dynatrace
    dt_environment: str = Field(default="")
    dt_platform_token: str = Field(default="")

    # App
    app_port: int = Field(default=8080)
    demo_mode: bool = Field(default=True)

    @property
    def dynatrace_configured(self) -> bool:
        return bool(self.dt_environment and self.dt_platform_token)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.google_api_key) or bool(self.google_cloud_project)

    @property
    def gemini_auth_mode(self) -> str:
        # Prefer Vertex when explicitly requested AND a project is set;
        # otherwise prefer API key if present; otherwise fall back to Vertex.
        if self.google_genai_use_vertexai and self.google_cloud_project:
            return "vertex"
        if self.google_api_key:
            return "api_key"
        if self.google_cloud_project:
            return "vertex"
        return "none"


@lru_cache
def get_settings() -> Settings:
    return Settings()
