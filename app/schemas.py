from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectRequest(StrictModel):
    youtube_url: str = Field(min_length=1, max_length=2048)
    youtube_cookies: str | None = Field(default=None, max_length=65536)


class ProfileFields(StrictModel):
    change_sensitivity: float = Field(default=0.55, ge=0.05, le=0.95)
    stable_wait_ms: int = Field(default=900, ge=100, le=10_000)
    local_resample_fps: int = Field(default=10, ge=8, le=12)
    review_rounds: int = Field(default=3, ge=1, le=3)
    roi_mask: dict[str, Any] | None = None
    applicable_features: list[str] = Field(default_factory=list, max_length=20)


class ProfileCreate(ProfileFields):
    name: str = Field(min_length=1, max_length=80)
    source_profile_id: str | None = Field(default=None, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class ProfilePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    change_sensitivity: float | None = Field(default=None, ge=0.05, le=0.95)
    stable_wait_ms: int | None = Field(default=None, ge=100, le=10_000)
    local_resample_fps: int | None = Field(default=None, ge=8, le=12)
    review_rounds: int | None = Field(default=None, ge=1, le=3)
    roi_mask: dict[str, Any] | None = None
    applicable_features: list[str] | None = Field(default=None, max_length=20)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class DuplicateProfileRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)


class JobCreate(StrictModel):
    youtube_url: str = Field(min_length=1, max_length=2048)
    youtube_cookies: str | None = Field(default=None, max_length=65536)
    profile_id: str = Field(default="profile_smart_default", min_length=1, max_length=80)
    profile_version: int = Field(default=1, ge=1)
    profile_overrides: dict[str, Any] = Field(default_factory=dict)
    learning_policy: Literal["suggest", "off"] = "suggest"
    transcript_style: Literal["polished", "verbatim"] = "polished"
    include_timestamps: bool = True
    page_density: Literal["compact", "comfortable", "spacious"] = "comfortable"

    @field_validator("profile_overrides")
    @classmethod
    def restrict_overrides(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "change_sensitivity",
            "stable_wait_ms",
            "local_resample_fps",
            "review_rounds",
            "roi_mask",
        }
        unexpected = set(value) - allowed
        if unexpected:
            raise ValueError(f"unsupported profile override(s): {', '.join(sorted(unexpected))}")
        if "change_sensitivity" in value and not 0.05 <= float(value["change_sensitivity"]) <= 0.95:
            raise ValueError("change_sensitivity must be between 0.05 and 0.95")
        if "stable_wait_ms" in value and not 100 <= int(value["stable_wait_ms"]) <= 10_000:
            raise ValueError("stable_wait_ms must be between 100 and 10000")
        if "local_resample_fps" in value and not 8 <= int(value["local_resample_fps"]) <= 12:
            raise ValueError("local_resample_fps must be between 8 and 12")
        if "review_rounds" in value and not 1 <= int(value["review_rounds"]) <= 3:
            raise ValueError("review_rounds must be between 1 and 3")
        return value


class ProposalApplyRequest(StrictModel):
    action: Literal["update", "create"]
    target_profile_id: str | None = Field(default=None, max_length=80)
    expected_version: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_action(self) -> "ProposalApplyRequest":
        if self.action == "update" and not self.target_profile_id:
            raise ValueError("target_profile_id is required for update")
        if self.action == "update" and self.expected_version is None:
            raise ValueError("expected_version is required for update")
        if self.action == "create" and not self.name:
            raise ValueError("name is required for create")
        return self


class VisionCorrection(StrictModel):
    title: str = Field(max_length=300)
    paragraphs: list[str]
    visual_evidence: list[str]
    uncertain_terms: list[str]
    warnings: list[str]


class ReviewDecision(StrictModel):
    action: Literal[
        "keep",
        "drop",
        "merge",
        "merge_previous",
        "merge_next",
        "search_later",
        "search_earlier",
        "resample_local",
        "low_confidence",
    ]
    reason: str = Field(max_length=500)
    confidence: float = Field(ge=0, le=1)
