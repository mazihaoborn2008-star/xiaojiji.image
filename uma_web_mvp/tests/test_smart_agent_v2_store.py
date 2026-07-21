import sqlite3
from pathlib import Path
from types import SimpleNamespace

import app.smart_agent.v2_store as store


def _connect_factory(path: Path):
    def _connect(_settings):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS smart_agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        return conn
    return _connect


def test_one_active_turn_and_exact_resolution(tmp_path, monkeypatch):
    db_path = tmp_path / "v2.db"
    monkeypatch.setattr(store, "connect", _connect_factory(db_path))
    settings = SimpleNamespace(deepseek_chat_timeout_seconds=30, deepseek_max_retries=0)

    first = store.begin_turn_atomic(
        settings,
        conversation_id=1,
        client_request_id="req-1",
        generation_requested=True,
        turn_id="turn-1",
    )
    assert first["duplicate"] is False

    duplicate = store.begin_turn_atomic(
        settings,
        conversation_id=1,
        client_request_id="req-1",
        generation_requested=True,
        turn_id="turn-other",
    )
    assert duplicate["duplicate"] is True

    store.bind_turn_message(settings, turn_id="turn-1", message_id=7)
    store.save_message_resolution(
        settings,
        conversation_id=1,
        message_id=7,
        character_ids=["nanami_mami", "nanami_mami"],
        source="test",
    )
    assert store.get_message_resolution(settings, message_id=7) == ["nanami_mami"]
    store.mark_turn_processing(settings, message_id=7)
    store.finish_turn_for_message(settings, message_id=7, status="completed")
    assert store.has_active_turn(settings, conversation_id=1) is False
