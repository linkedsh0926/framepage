from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app


def test_real_mode_rejects_placeholder_signing_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="FRAMEPAGE_TOKEN_SECRET"):
        Settings(
            mode="real",
            data_dir=tmp_path,
            token_secret="framepage-local-development-secret-change-me",
        )


def test_real_mode_requires_openrouter_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings(
        mode="real",
        data_dir=tmp_path,
        token_secret="unit-test-real-mode-secret-0123456789abcdef",
    )
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        create_app(settings)
