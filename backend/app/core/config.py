from __future__ import annotations

from pathlib import Path

from platformdirs import user_data_path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MANGA_TRANSLATOR_",
        extra="ignore",
    )

    app_name: str = "漫画智能翻译"
    version: str = "0.9.0"
    data_dir: Path = Field(
        default_factory=lambda: user_data_path("MangaTranslator", appauthor=False)
    )
    log_level: str = "INFO"
    import_concurrency: int = Field(default=1, ge=1, le=4)
    import_page_concurrency: int = Field(default=2, ge=1, le=4)
    upload_concurrency: int = Field(default=2, ge=1, le=4)
    ocr_concurrency: int = Field(default=2, ge=1, le=4)
    llm_concurrency: int = Field(default=3, ge=1, le=16)
    llm_max_retries: int = Field(default=3, ge=0, le=5)
    llm_retry_base_seconds: float = Field(default=1.0, ge=0.1, le=10.0)
    thumbnail_size: int = Field(default=220, ge=100, le=600)
    preview_size: int = Field(default=1600, ge=800, le=3000)
    upload_session_ttl_hours: int = Field(default=24, ge=1, le=168)
    max_archive_entries: int = Field(default=10_000, ge=1)
    max_import_bytes: int = Field(default=20 * 1024**3, ge=1024**2)
    max_compression_ratio: float = Field(default=200.0, ge=1)
    max_model_bytes: int = Field(default=2 * 1024**3, ge=1024**2)
    launcher_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    launcher_heartbeat_interval_seconds: int = Field(default=10, ge=1, le=60)
    launcher_heartbeat_timeout_seconds: int = Field(default=60, ge=10, le=300)
    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[3])

    @property
    def database_path(self) -> Path:
        return self.data_dir / "data" / "app.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "workspace" / "projects"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def models_dir(self) -> Path:
        return self.project_root / "models"

    @property
    def ocr_models_dir(self) -> Path:
        return self.models_dir / "ocr"

    @property
    def inpainting_models_dir(self) -> Path:
        return self.models_dir / "inpainting"

    @property
    def yolo_models_dir(self) -> Path:
        return self.models_dir / "yolo"

    @property
    def fonts_dir(self) -> Path:
        return self.data_dir / "fonts"

    @property
    def frontend_dist(self) -> Path:
        return self.project_root / "frontend" / "dist"

    def ensure_directories(self) -> None:
        for path in (
            self.database_path.parent,
            self.projects_dir,
            self.staging_dir,
            self.logs_dir,
            self.models_dir,
            self.ocr_models_dir,
            self.inpainting_models_dir,
            self.yolo_models_dir,
            self.fonts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
