from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    mode: str = "demo"
    data_dir: Path = Path(".framepage-data")
    database_path: Path | None = None
    token_secret: str = "framepage-local-development-secret-change-me"
    cors_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )
    max_video_seconds: int = 120 * 60
    max_download_bytes: int = 2 * 1024 * 1024 * 1024
    artifact_ttl_hours: int = 24
    signed_url_seconds: int = 300
    max_request_bytes: int = 1024 * 1024
    rate_limit_per_minute: int = 180
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    def __post_init__(self) -> None:
        self.mode = self.mode.lower()
        if self.mode not in {"demo", "test", "real"}:
            raise ValueError("FRAMEPAGE_MODE must be demo, test, or real")
        weak_secret_markers = ("change-me", "replace-with", "development-secret")
        if self.mode == "real" and (
            len(self.token_secret) < 32
            or any(marker in self.token_secret.lower() for marker in weak_secret_markers)
        ):
            raise ValueError(
                "FRAMEPAGE_TOKEN_SECRET must be a new random value of at least 32 characters in real mode"
            )
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        if self.database_path is None:
            self.database_path = self.data_dir / "framepage.sqlite3"
        else:
            self.database_path = Path(self.database_path).expanduser().resolve()

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("FRAMEPAGE_DATA_DIR", ".framepage-data"))
        db_value = os.getenv("FRAMEPAGE_DATABASE_PATH")
        return cls(
            mode=os.getenv("FRAMEPAGE_MODE", "demo"),
            data_dir=data_dir,
            database_path=Path(db_value) if db_value else None,
            token_secret=os.getenv(
                "FRAMEPAGE_TOKEN_SECRET",
                "framepage-local-development-secret-change-me",
            ),
            cors_origins=_csv(
                os.getenv(
                    "FRAMEPAGE_CORS_ORIGINS",
                    "http://localhost:3000,http://localhost:5173",
                )
            ),
            max_video_seconds=int(os.getenv("FRAMEPAGE_MAX_VIDEO_SECONDS", "7200")),
            max_download_bytes=int(
                os.getenv("FRAMEPAGE_MAX_DOWNLOAD_BYTES", str(2 * 1024**3))
            ),
            artifact_ttl_hours=int(os.getenv("FRAMEPAGE_ARTIFACT_TTL_HOURS", "24")),
            signed_url_seconds=int(os.getenv("FRAMEPAGE_SIGNED_URL_SECONDS", "300")),
            max_request_bytes=int(os.getenv("FRAMEPAGE_MAX_REQUEST_BYTES", str(1024**2))),
            rate_limit_per_minute=int(os.getenv("FRAMEPAGE_RATE_LIMIT", "180")),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
        )

    @property
    def openrouter_api_key(self) -> str | None:
        # The key is deliberately never accepted from an API model or config file.
        return os.getenv("OPENROUTER_API_KEY")
