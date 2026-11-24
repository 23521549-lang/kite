"""Spend store: durable spend rows (caps survive journal loss) and pod billing cursors."""

from __future__ import annotations

from poc.kaggle_worker.spend_ledger import SpendStore

DAY = 1795400000.0        # 2026-11-23 UTC


def test_spend_rows_survive_a_fresh_store_object(tmp_path):
    path = tmp_path / "spend.sqlite3"
    store = SpendStore(path)
    store.add("api", 1.25, DAY)
    store.add("api", 0.75, DAY + 60)
    store.add("pod", 2.0, DAY + 120)
    store.close()

    reopened = SpendStore(path)
    assert reopened.records() == [
        {"worker": "api", "usd": 1.25, "ts": DAY},
        {"worker": "api", "usd": 0.75, "ts": DAY + 60},
        {"worker": "pod", "usd": 2.0, "ts": DAY + 120},
    ]


def test_journal_records_are_imported_exactly_once(tmp_path):
    path = tmp_path / "spend.sqlite3"
    journal = [{"worker": "api", "usd": 0.5, "ts": DAY},
               {"worker": "api", "usd": 0.25, "ts": DAY + 10},
               {"worker": "api", "usd": "not-a-number", "ts": DAY}]     # skipped, like the ledger does
    store = SpendStore(path)
    assert store.import_journal_once(journal) == 2
    assert store.import_journal_once(journal) == 0                       # a restart must not double-count
    assert [r["usd"] for r in store.records()] == [0.5, 0.25]

    reopened = SpendStore(path)                                          # the flag itself is durable
    assert reopened.import_journal_once(journal) == 0
    assert len(reopened.records()) == 2


def test_pod_cursor_round_trip_and_clear(tmp_path):
    store = SpendStore(tmp_path / "spend.sqlite3")
    assert store.pod_cursor("runpod") is None
    store.set_pod_cursor("runpod", DAY + 600)
    assert store.pod_cursor("runpod") == DAY + 600
    store.set_pod_cursor("runpod", DAY + 1200)
    assert store.pod_cursor("runpod") == DAY + 1200
    store.set_pod_cursor("runpod", None)
    assert store.pod_cursor("runpod") is None
