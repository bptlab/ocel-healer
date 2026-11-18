"""Staleness check of the examination guide (src.examination.report_store)."""

import sqlite3

from src.examination import report_store


def _make_log(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE object (ocel_id TEXT, ocel_type TEXT);"
        "CREATE TABLE event_object (ocel_qualifier TEXT);"
        "CREATE TABLE object_object (ocel_qualifier TEXT);"
        "INSERT INTO object VALUES ('o1', 'orders');"
    )
    conn.commit()
    conn.close()


def test_fingerprint_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    db = tmp_path / "log.sqlite"
    _make_log(db)
    calls = []
    from src.examination import db_profiler

    real = db_profiler.schema_fingerprint
    monkeypatch.setattr(
        db_profiler, "schema_fingerprint", lambda conn: calls.append(1) or real(conn)
    )

    first = report_store.current_fingerprint(db)
    assert report_store.current_fingerprint(db) == first
    assert len(calls) == 1  # second call served from the cache

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE object_Orders (ocel_id TEXT)")
    conn.commit()
    conn.close()
    assert report_store.current_fingerprint(db) != first
    assert len(calls) == 2


def test_guide_is_stale_follows_structure_changes(tmp_path):
    db = tmp_path / "log.sqlite"
    _make_log(db)
    assert report_store.guide_is_stale(db)  # no guide yet

    report_store.save_json(
        report_store.guide_path(db),
        {"source_fingerprint": report_store.current_fingerprint(db)},
    )
    assert not report_store.guide_is_stale(db)

    conn = sqlite3.connect(db)
    conn.execute("UPDATE object SET ocel_type = 'Orders'")  # value change only
    conn.commit()
    conn.close()
    assert report_store.guide_is_stale(db)  # the type set changed → structural

    report_store.save_json(
        report_store.guide_path(db),
        {"source_fingerprint": report_store.current_fingerprint(db)},
    )
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO object VALUES ('o2', 'Orders')")  # same structure
    conn.commit()
    conn.close()
    assert not report_store.guide_is_stale(db)
