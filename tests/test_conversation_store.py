"""Conversation persistence: threads survive a restart with the SQLite store."""

from __future__ import annotations

import pytest

from agent.core import conversation as conversation_module
from agent.core.conversation_store import (
    InMemoryConversationStore,
    SQLiteConversationStore,
    create_store,
)
from agent.models import ConversationStatus
from tests.helpers import make_brief


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "conversations.db"


def _restart(store: SQLiteConversationStore, db_path) -> SQLiteConversationStore:
    """Simulate a process restart: close the store and open a new one on the same file."""
    store.close()
    reopened = SQLiteConversationStore(db_path)
    conversation_module.set_store(reopened)
    return reopened


def test_create_store_picks_backend(db_path):
    assert isinstance(create_store(""), InMemoryConversationStore)
    store = create_store(str(db_path))
    assert isinstance(store, SQLiteConversationStore)
    store.close()


def test_thread_survives_restart(db_path, sample_prospect):
    store = SQLiteConversationStore(db_path)
    conversation_module.set_store(store)

    state = conversation_module.create_conversation(sample_prospect)
    conversation_module.add_message(state.thread_id, "agent", "Hi Jane")
    conversation_module.update_status(state.thread_id, ConversationStatus.OUTBOUND_SENT)
    conversation_module.add_message(state.thread_id, "prospect", "Sounds interesting")

    store = _restart(store, db_path)

    restored = conversation_module.get_conversation(state.thread_id)
    assert restored is not None
    assert restored.status == ConversationStatus.REPLIED
    assert [m.content for m in restored.messages] == ["Hi Jane", "Sounds interesting"]
    assert restored.prospect.contact_email == sample_prospect.contact_email
    store.close()


def test_in_place_edit_persists_only_after_save(db_path, sample_prospect):
    store = SQLiteConversationStore(db_path)
    conversation_module.set_store(store)

    state = conversation_module.create_conversation(sample_prospect)
    state.hubspot_contact_id = "unsaved"
    store = _restart(store, db_path)
    assert conversation_module.get_conversation(state.thread_id).hubspot_contact_id is None

    state = conversation_module.get_conversation(state.thread_id)
    state.hubspot_contact_id = "123"
    state.signal_brief = make_brief(sample_prospect)
    conversation_module.save_conversation(state)
    store = _restart(store, db_path)

    restored = conversation_module.get_conversation(state.thread_id)
    assert restored.hubspot_contact_id == "123"
    assert restored.signal_brief.prospect.company == sample_prospect.company
    store.close()


def test_lookups_work_after_restart(db_path, sample_prospect, sample_funded_prospect):
    store = SQLiteConversationStore(db_path)
    conversation_module.set_store(store)

    phone_prospect = sample_prospect.model_copy(update={"contact_phone": "+254700000001"})
    first = conversation_module.create_conversation(phone_prospect)
    conversation_module.create_conversation(sample_prospect)
    other = conversation_module.create_conversation(sample_funded_prospect)
    other.calcom_booking_id = "bk_1"
    conversation_module.save_conversation(other)

    store = _restart(store, db_path)

    assert conversation_module.get_conversation_by_phone("+254700000001").thread_id == (
        first.thread_id
    )
    assert conversation_module.get_conversation_by_booking_id("bk_1").thread_id == other.thread_id
    assert conversation_module.has_sibling_threads(first.thread_id)
    assert not conversation_module.has_sibling_threads(other.thread_id)
    assert len(conversation_module.get_active_conversations()) == 3
    store.close()


def test_same_object_returned_within_process(db_path, sample_prospect):
    store = SQLiteConversationStore(db_path)
    conversation_module.set_store(store)

    state = conversation_module.create_conversation(sample_prospect)
    assert conversation_module.get_conversation(state.thread_id) is state
    store.close()
