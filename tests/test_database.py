"""Tests for the database schema initialisation."""

import asyncio
import pytest
import os


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point the database at a temp dir so tests don't touch real data."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("PLATY_DB_PATH", str(db_path))
    # Reset cached settings.
    import platysearch.config as cfg
    cfg._settings = None
    yield db_path
    cfg._settings = None


@pytest.mark.asyncio
async def test_init_creates_tables(tmp_db):
    from platysearch.database import init_db, get_db

    await init_db()
    db = await get_db()
    try:
        tables = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in tables}
        assert "pages" in names
        assert "terms" in names
        assert "postings" in names
        assert "crawl_queue" in names
    finally:
        await db.close()
