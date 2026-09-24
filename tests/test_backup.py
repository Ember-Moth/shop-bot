import asyncio
import sqlite3
import stat

import pytest

from shop_bot.config import BackupSettings
from shop_bot.db import Database
from shop_bot.services.backup import BackupManager, backup_database


def test_online_backup_includes_wal_and_only_committed_data(tmp_path):
    source = tmp_path / "live.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE ledger (amount INTEGER)")
        conn.execute("INSERT INTO ledger VALUES (100)")
        conn.commit()
        conn.execute("INSERT INTO ledger VALUES (999)")  # 未提交写事务仍在进行
        snapshot = backup_database(source, BackupSettings())
        restored = sqlite3.connect(snapshot)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert restored.execute("SELECT amount FROM ledger").fetchall() == [(100,)]
        finally:
            restored.close()
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
        assert snapshot.parent == tmp_path / "backups"
    finally:
        conn.close()


def test_backup_retention_is_scoped_and_failed_backup_preserves_good_files(tmp_path):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
    settings = BackupSettings(keep=2)
    first = backup_database(source, settings)
    other = first.parent / "other.sqlite3"
    other.write_bytes(b"unrelated")
    second = backup_database(source, settings)
    third = backup_database(source, settings)
    assert not first.exists() and second.exists() and third.exists() and other.exists()
    source.write_bytes(b"corrupt database")
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, settings)
    assert second.exists() and third.exists() and other.read_bytes() == b"unrelated"
    assert not list(first.parent.glob("*.partial"))


def test_missing_source_does_not_create_empty_database(tmp_path):
    source = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        backup_database(source, BackupSettings())
    assert not source.exists()


async def test_restore_wallet_receipts_and_orders(tmp_path):
    source = tmp_path / "live.db"
    db = Database(str(source))
    await db.connect()
    try:
        user = await db.users.upsert_user(42, "buyer")
        await db.wallet.adjust_balance(user.id, 10000, "opening", "USD")
        order = await db.orders.create_order(user.id, 1, 1, 2500, "USD")
        await db.payments.pay_order_with_balance(order.id, user.id, 2500)
        manager = BackupManager(str(source), BackupSettings())
        snapshot = await manager.run_once()
        assert manager.last_success and manager.error is None
    finally:
        await db.close()
    restored = Database(str(snapshot))
    await restored.connect()
    try:
        assert await restored.wallet.get_balance(user.id, "USD") == 7500
        saved = await restored.orders.get_order(order.id)
        assert saved is not None and saved.status.value == "paid"
        ledger = await restored.wallet.list_balance_transactions(user.id)
        assert [entry.kind for entry in ledger] == ["purchase", "adjust"]
    finally:
        await restored.close()


async def test_backup_manager_records_failure_and_recovers(tmp_path):
    source = tmp_path / "live.db"
    manager = BackupManager(str(source), BackupSettings())
    with pytest.raises(FileNotFoundError):
        await manager.run_once()
    assert manager.error == "FileNotFoundError" and manager.last_success is None
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
    results = await asyncio.gather(manager.run_once(), manager.run_once())
    assert len(set(results)) == 2 and manager.error is None
