"""Pydantic models for parsed Pinterest data.

Kept deliberately permissive: every field that Pinterest may omit is Optional,
so a blocked/partial response never raises. Honesty rules are encoded here:
  - `saves` is Optional and only set from real data (never estimated).
  - There is NO `likes` field (Pinterest removed likes in 2017).
  - `reactions` is Optional and only populated if present.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Comment(BaseModel):
    author: str | None = None
    text: str | None = None
    created_at: str | None = None


class PinData(BaseModel):
    pin_id: str
    url: str | None = None
    title: str | None = None
    description: str | None = None
    image: str | None = None

    board_name: str | None = None
    board_url: str | None = None

    pinner_name: str | None = None
    pinner_username: str | None = None
    pinner_url: str | None = None

    created_at: str | None = None

    # Best-effort only. None == unavailable; never an estimate.
    saves: int | None = None
    # Only if Pinterest actually reports a reactions count. No fake likes.
    reactions: int | None = None

    comment_count: int | None = None
    comments: list[Comment] = Field(default_factory=list)

    # Pinterest's keyword annotations / visual tags.
    annotations: list[str] = Field(default_factory=list)

    # Non-fatal notes about missing/blocked fields, surfaced to the UI.
    notes: list[str] = Field(default_factory=list)


class ImageMatch(BaseModel):
    pin_id: str | None = None
    url: str | None = None
    image: str | None = None
    title: str | None = None
    board_name: str | None = None
    match_source: str | None = None
