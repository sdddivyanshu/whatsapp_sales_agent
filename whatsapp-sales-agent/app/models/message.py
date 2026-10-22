"""Messaging models shared by the WhatsApp provider and webhook handling."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InboundMessage(BaseModel):
    wa_message_id: str
    from_phone: str  # E.164 with leading +
    timestamp: str | None = None
    msg_type: str  # text | image | document | audio | interactive | button | ...
    text: str | None = None
    media_id: str | None = None
    caption: str | None = None
    profile_name: str | None = None
    to_phone_number_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def content_for_agent(self) -> str:
        """Text the agent should reason over, with a marker for non-text content."""
        if self.text:
            return self.text
        if self.caption:
            return f"[{self.msg_type} attachment] {self.caption}"
        return f"[{self.msg_type} attachment without text]"


class StatusUpdate(BaseModel):
    wa_message_id: str
    status: str  # sent | delivered | read | failed
    recipient: str | None = None
    timestamp: str | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class SendResult(BaseModel):
    wa_message_id: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ParsedWebhook(BaseModel):
    messages: list[InboundMessage] = Field(default_factory=list)
    statuses: list[StatusUpdate] = Field(default_factory=list)
    ignored: int = 0
