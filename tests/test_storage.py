"""存储层单元测试：迁移、备份（WAL checkpoint/轮转/失败）、版本校验、绑定原子性、任务状态。"""

from pathlib import Path

import aiosqlite
import pytest


@pytest.fixture()
async def db(tmp_path: Path):
    from nonebot_plugin_duty_bot.storage import Database

    database = Database(tmp_path / "test.db")
    await database.init()
    yield database
    await database.close()


async def _make_legacy_db(path: Path, *, row: str = "pre-migration") -> aiosqlite.Connection:
    """构造一个无迁移记录的旧版库（模拟待迁移状态），返回未关闭的连接。

    保留打开的连接以模拟上次进程异常退出后 -wal 日志残留的场景。
    """
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("CREATE TABLE legacy(x TEXT)")
    await conn.execute("INSERT INTO legacy VALUES (?)", (row,))
    await conn.commit()
    return conn


async def test_first_init_no_backup(tmp_path: Path) -> None:
    """全新目录首次初始化：无历史数据，不产生备份文件。"""
    from nonebot_plugin_duty_bot.storage import Database

    db_file = tmp_path / "fresh.db"
    database = Database(db_file)
    await database.init()
    try:
        assert db_file.exists()
        assert await database._applied_versions() == [1]
        assert not db_file.with_suffix(db_file.suffix + ".bak").exists()
        assert not db_file.with_suffix(db_file.suffix + ".bak.old").exists()
    finally:
        await database.close()


async def test_migrations_and_backup(tmp_path: Path) -> None:
    """旧版库迁移：迁移前自动备份，备份为自包含的完整库（含 wal 中最新写入）。"""
    from nonebot_plugin_duty_bot.storage import Database

    db_file = tmp_path / "legacy.db"
    leaked = await _make_legacy_db(db_file)
    database = Database(db_file)
    try:
        await database.init()
        backup = db_file.with_suffix(db_file.suffix + ".bak")
        assert backup.exists()
        assert await database._applied_versions() == [1]

        # 备份必须包含迁移前数据（即便写入仍停留在 -wal 中，
        # checkpoint 后复制的主文件也应自包含）
        async with aiosqlite.connect(backup) as conn:
            cursor = await conn.execute("SELECT x FROM legacy")
            rows = await cursor.fetchall()
        assert [r[0] for r in rows] == ["pre-migration"]
    finally:
        await database.close()
        await leaked.close()


async def test_backup_rotation(tmp_path: Path) -> None:
    """已有备份时再次迁移：旧备份轮转为 .bak.old，新备份覆盖 .bak。"""
    from nonebot_plugin_duty_bot.storage import Database

    db_file = tmp_path / "rotate.db"
    leaked = await _make_legacy_db(db_file, row="first-generation")
    backup = db_file.with_suffix(db_file.suffix + ".bak")
    previous = db_file.with_suffix(db_file.suffix + ".bak.old")
    backup.write_bytes(b"old-backup-marker")

    database = Database(db_file)
    try:
        await database.init()
        assert previous.read_bytes() == b"old-backup-marker"
        assert backup.exists() and backup.read_bytes() != b"old-backup-marker"
    finally:
        await database.close()
        await leaked.close()


async def test_backup_failure_refuses_startup(tmp_path: Path, monkeypatch) -> None:
    """备份 IO 失败：抛 StorageBackupError 拒绝启动（与版本错误区分）。"""
    import shutil as shutil_module

    from nonebot_plugin_duty_bot import storage
    from nonebot_plugin_duty_bot.storage import Database, StorageBackupError

    db_file = tmp_path / "fail.db"
    leaked = await _make_legacy_db(db_file)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(shutil_module, "copy2", boom)
    database = Database(db_file)
    try:
        with pytest.raises(StorageBackupError):
            await database.init()
    finally:
        # 连接已建立但 init 失败，直接清理
        await database.close()
        await leaked.close()


async def test_migration_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    """迁移 SQL 失败：事务回滚，版本不推进，库文件仍可正常打开。"""
    import sqlite3

    from nonebot_plugin_duty_bot import storage
    from nonebot_plugin_duty_bot.storage import Database

    db_file = tmp_path / "badsql.db"
    leaked = await _make_legacy_db(db_file)

    monkeypatch.setattr(
        storage, "MIGRATIONS", {1: ("CREATE TABLE step_one(x TEXT)", "THIS IS NOT SQL;")}
    )
    database = Database(db_file)
    with pytest.raises(sqlite3.OperationalError):
        await database.init()
    await database.close()
    await leaked.close()

    # 版本未推进（迁移记录未写入），失败语句已回滚，库文件仍可打开
    async with aiosqlite.connect(db_file) as conn:
        cursor = await conn.execute("SELECT version FROM schema_migrations")
        assert await cursor.fetchall() == []
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='step_one'"
        )
        assert await cursor.fetchone() is None


async def test_idempotent_init(tmp_path: Path) -> None:
    """重复初始化不重复迁移、不产生新备份。"""
    from nonebot_plugin_duty_bot.storage import Database

    db_file = tmp_path / "idem.db"
    database = Database(db_file)
    await database.init()
    await database.close()
    database = Database(db_file)
    await database.init()
    try:
        assert await database.count_bindings() == 0
        assert not db_file.with_suffix(db_file.suffix + ".bak").exists()
    finally:
        await database.close()


async def test_version_too_high(tmp_path: Path) -> None:
    """数据库版本高于代码版本时拒绝启动。"""
    from nonebot_plugin_duty_bot.storage import Database, SCHEMA_VERSION, StorageVersionError

    db_file = tmp_path / "future.db"
    async with aiosqlite.connect(db_file) as conn:
        await conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, "2026-10-27T00:00:00"),
        )
        await conn.commit()

    database = Database(db_file)
    with pytest.raises(StorageVersionError):
        await database.init()
    # 失败路径不遗留打开的连接（线程与文件句柄）
    assert not database.connected


async def test_bind_and_conflicts(db) -> None:
    """绑定成功；QQ 与学号各建唯一约束，冲突映射明确。"""
    from nonebot_plugin_duty_bot.storage import BindConflictError

    await db.bind(10001, "2025123456789", "张三")

    # 同一学号被其他 QQ 绑定
    with pytest.raises(BindConflictError) as e:
        await db.bind(10002, "2025123456789", "李四")
    assert e.value.student_no == "2025123456789"

    # 同一 QQ 绑定其他学号
    with pytest.raises(BindConflictError) as e:
        await db.bind(10001, "2025402011236", "王五")
    assert e.value.qq == 10001

    assert await db.count_bindings() == 1


async def test_bind_atomic(db) -> None:
    """并发绑定同学号：唯一约束保证仅一条成功。"""
    import asyncio
    from nonebot_plugin_duty_bot.storage import BindConflictError

    results = await asyncio.gather(
        db.bind(20001, "2025123456789", "张三"),
        db.bind(20002, "2025123456789", "张三"),
        return_exceptions=True,
    )
    success = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, BindConflictError)]
    assert len(success) == 1
    assert len(conflicts) == 1


async def test_unbind_and_lookup(db) -> None:
    await db.bind(10001, "2025123456789", "张三")
    assert (await db.get_binding(qq=10001)).student_no == "2025123456789"
    assert (await db.get_binding(student_no="2025123456789")).qq == 10001

    assert await db.unbind(10001) is True
    assert await db.unbind(10001) is False
    assert await db.get_binding(qq=10001) is None


async def test_task_state_upsert(db) -> None:
    """任务状态每任务一行，覆盖最近一次执行结果。"""
    from nonebot_plugin_duty_bot.storage import TASK_REMIND_POLL

    assert await db.get_task_state(TASK_REMIND_POLL) is None
    await db.set_task_state(TASK_REMIND_POLL, "failed", "2026-10-28", "无可用机器人连接")
    state = await db.get_task_state(TASK_REMIND_POLL)
    assert state.status == "failed"
    assert state.business_key == "2026-10-28"
    assert "机器人" in state.error

    await db.set_task_state(TASK_REMIND_POLL, "success", "2026-10-28", None)
    state = await db.get_task_state(TASK_REMIND_POLL)
    assert state.status == "success" and state.error is None
