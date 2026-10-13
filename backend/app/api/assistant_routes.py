"""Planning assistant endpoint (stateless: the client sends the conversation each time)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import assistant

router = APIRouter()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class AssistantIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=60)


@router.post("/api/assistant")
def ask_assistant(body: AssistantIn, db: Session = Depends(get_db)):
    try:
        return assistant.answer(db, [m.model_dump() for m in body.messages])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
