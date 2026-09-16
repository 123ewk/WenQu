"""会话与消息仓储。"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models import Conversation, Message


class ConversationRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def create(self, conversation: Conversation) -> Conversation:
        self._db.add(conversation)
        self._db.flush()
        return conversation

    def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return self._db.get(Conversation, conversation_id)

    def save(self, conversation: Conversation) -> Conversation:
        self._db.flush()
        return conversation

    def delete(self, conversation: Conversation) -> None:
        self._db.delete(conversation)
        self._db.flush()

    def list_for_user(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[Conversation]:
        return list(
            self._db.scalars(
                select(Conversation)
                .where(Conversation.space_id == space_id, Conversation.user_id == user_id)
                .order_by(Conversation.updated_at.desc())
            )
        )


class MessageRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def add(self, message: Message) -> Message:
        self._db.add(message)
        self._db.flush()
        return message

    def list_for_conversation(self, conversation_id: uuid.UUID) -> list[Message]:
        return list(
            self._db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq)
            )
        )

    def next_seq(self, conversation_id: uuid.UUID) -> int:
        current = self._db.scalar(
            select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
        )
        return int(current or 0) + 1
