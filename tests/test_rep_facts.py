"""Tests for the rep-facts durable-memory layer (app/storage/repos.py)."""

import pytest


@pytest.fixture()
def state_db(tmp_path, monkeypatch):
    """Point the SQLite state DB at a temp file and initialise the schema."""
    db_path = tmp_path / "state.sqlite"
    monkeypatch.setattr("app.storage.db.state_db_path", lambda: db_path)
    from app.storage.db import init_db

    init_db()
    return db_path


def test_save_rep_fact_inserts_and_dedups(state_db) -> None:
    from app.storage.repos import list_rep_facts, save_rep_fact

    fid1 = save_rep_fact(
        rep_id="205",
        fact_text="Account 51149 is closed.",
        fact_type="account_closed",
        account_number="51149",
        account_label="KMAF ASSOCIATES",
    )
    # Same rep + account + type → refresh, not a new row.
    fid2 = save_rep_fact(
        rep_id="205",
        fact_text="51149 shut its doors last month.",
        fact_type="account_closed",
        account_number="51149",
    )
    assert fid1 == fid2

    facts = list_rep_facts(rep_id="205")
    assert len(facts) == 1
    assert facts[0].fact_text == "51149 shut its doors last month."
    assert facts[0].account_number == "51149"
    assert facts[0].active is True


def test_deactivate_and_delete_rep_fact(state_db) -> None:
    from app.storage.repos import (
        delete_rep_fact,
        list_rep_facts,
        save_rep_fact,
        set_rep_fact_active,
    )

    fid = save_rep_fact(rep_id="7", fact_text="Prefers concise emails.", fact_type="preference")

    set_rep_fact_active(fid, False)
    assert list_rep_facts(rep_id="7", active_only=True) == []
    inactive = list_rep_facts(rep_id="7", active_only=False)
    assert len(inactive) == 1 and inactive[0].active is False

    delete_rep_fact(fid)
    assert list_rep_facts(rep_id="7", active_only=False) == []


def test_closed_account_numbers_and_block(state_db) -> None:
    from app.storage.repos import (
        closed_account_numbers,
        rep_facts_block,
        save_rep_fact,
    )

    save_rep_fact(
        rep_id="9",
        fact_text="Account 12345 is closed.",
        fact_type="account_closed",
        account_number="12345",
    )
    save_rep_fact(
        rep_id="9",
        fact_text="Buyer switched to net-30 terms.",
        fact_type="account_note",
        account_number="22222",
    )

    closed = closed_account_numbers(rep_id="9")
    assert "12345" in closed
    assert "22222" not in closed

    block = rep_facts_block("9")
    assert "KNOWN FACTS" in block
    assert "12345" in block
    # Empty rep → empty block.
    assert rep_facts_block("does-not-exist") == ""
