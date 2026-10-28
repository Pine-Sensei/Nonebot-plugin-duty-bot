"""轻量嵌入式存储：aiosqlite（SQLite 单文件，不引入 ORM）。

- 版本化增量迁移：启动时按序在事务中执行并记录版本；
  迁移失败或库版本高于代码版本时拒绝启动；迁移前自动备份
  （先 WAL checkpoint 保证主文件自包含，保留上一代 .bak.old）。
- 持久数据仅两类：绑定关系、任务状态（每任务一行）。
- 读写操作经 asyncio.Lock 串行化；绑定「查重 + 写入」由唯一约束保证原子。
"""

from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence

import aiosqlite
from nonebot import logger

from .timeutil import BEIJING

# 迁移脚本：按版本号升序执行；每条为独立 SQL 语句。
MIGRATIONS: dict[int, Sequence[str]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS binding (
            qq         INTEGER PRIMARY KEY,
            student_no TEXT NOT NULL UNIQUE,
            name       TEXT NOT NULL,
            bound_at   TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS task_state (
            task_id      TEXT PRIMARY KEY,
            status       TEXT NOT NULL,
            business_key TEXT,
            error        TEXT,
            ran_at       TEXT NOT NULL
        )
        """,
    ),
}
SCHEMA_VERSION = max(MIGRATIONS)

# 任务状态 task_id 常量（供 jobs 与管理端共用）
TASK_REMIND_POLL = "remind_poll"
TASK_PERSONAL_WEEKLY = "personal_weekly"
TASK_GROUP_WEEKLY = "group_weekly"
TASK_TOMORROW = "tomorrow_broadcast"
TASK_IDS = (TASK_REMIND_POLL, TASK_PERSONAL_WEEKLY, TASK_GROUP_WEEKLY, TASK_TOMORROW)


class StorageError(RuntimeError):
    """存储层基础异常（版本/备份等初始化失败，拒绝启动）。"""


class StorageVersionError(StorageError):
    """数据库版本高于代码版本，拒绝启动。"""


class StorageBackupError(StorageError):
    """迁移前备份失败，拒绝启动。"""


class BindConflictError(Exception):
    """绑定冲突（QQ 或学号已被占用）。"""

    def __init__(self, qq: int | None = None, student_no: str | None = None):
        super().__init__(f"conflict qq={qq} student_no={student_no}")
        self.qq = qq
        self.student_no = student_no


@dataclass
class Binding:
    qq: int
    student_no: str
    name: str
    bound_at: str


@dataclass
class TaskState:
    task_id: str
    status: str
    business_key: Optional[str]
    error: Optional[str]
    ran_at: str


def _now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


class Database:
    """SQLite 存储门面：连接管理、迁移与数据访问。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # 串行化读写，避免与进行中的事务交错（asyncio.Lock 不可重入，
        # 持锁路径内禁止再调用加锁的公开方法，见 _query_binding）
        self._lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库尚未初始化")
        return self._conn

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """显式事务：提交成功生效，异常回滚。

        注意：aiosqlite 的 `async with conn` 会重复启动线程并关闭连接，
        因此事务必须显式 BEGIN/COMMIT/ROLLBACK 实现。
        """
        conn = self._get_conn()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    async def init(self) -> None:
        """连接并执行迁移与版本校验；失败抛异常以拒绝启动。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 连接前记录是否已存在：首次初始化无历史数据，无需备份
        existed = self._path.exists()
        self._conn = await aiosqlite.connect(self._path)
        try:
            await self._migrate(existed)
        except BaseException:
            # 版本冲突/备份失败等场景不遗留打开的连接（线程与文件句柄）
            await self.close()
            raise

    async def _migrate(self, existed: bool) -> None:
        conn = self._conn
        assert conn is not None
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        await conn.commit()

        applied = await self._applied_versions()
        current = max(applied) if applied else 0
        if current > SCHEMA_VERSION:
            raise StorageVersionError(
                f"数据库版本 {current} 高于代码版本 {SCHEMA_VERSION}，拒绝启动"
            )
        if current < SCHEMA_VERSION:
            if existed:
                await self._backup()
            for version in sorted(MIGRATIONS):
                if version <= current:
                    continue
                logger.info(f"应用数据库迁移 v{version}")
                async with self._transaction() as conn:
                    for statement in MIGRATIONS[version]:
                        await conn.execute(statement)
                    await conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (?, ?)",
                        (version, _now_iso()),
                    )
            logger.info(f"数据库迁移完成，当前版本 v{SCHEMA_VERSION}")

    async def _applied_versions(self) -> list[int]:
        conn = self._get_conn()
        cursor = await conn.execute("SELECT version FROM schema_migrations")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def _backup(self) -> None:
        """迁移前备份数据文件，保留上一代（.bak.old）。

        先执行 WAL checkpoint 把未合并的 wal 日志刷入主文件，
        保证复制出的备份自包含（上次进程异常退出时也不丢最近写入）；
        文件复制在线程池执行，避免阻塞事件循环。
        """
        conn = self._get_conn()
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup = self._path.with_suffix(self._path.suffix + ".bak")
        previous = backup.with_suffix(backup.suffix + ".old")
        try:
            if backup.exists():
                backup.replace(previous)
            await asyncio.to_thread(shutil.copy2, self._path, backup)
        except OSError as e:
            raise StorageBackupError(f"数据库备份失败: {e}") from e
        logger.info(f"迁移前已备份数据库至 {backup}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def clear_data(self) -> None:
        """清空业务数据（不删除迁移版本记录，仅供测试隔离使用）。"""
        async with self._lock:
            async with self._transaction() as conn:
                await conn.execute("DELETE FROM binding")
                await conn.execute("DELETE FROM task_state")

    # ---------- 绑定关系 ----------

    async def bind(self, qq: int, student_no: str, name: str) -> Binding:
        """建立绑定；QQ 或学号被占用时抛 BindConflictError。

        唯一约束保证「查重 + 写入」原子完成，无需先查后写。
        """
        bound_at = _now_iso()
        async with self._lock:
            try:
                async with self._transaction() as conn:
                    await conn.execute(
                        "INSERT INTO binding (qq, student_no, name, bound_at) "
                        "VALUES (?, ?, ?, ?)",
                        (qq, student_no, name, bound_at),
                    )
            except aiosqlite.IntegrityError:
                # 持锁路径：使用无锁查询内核区分冲突类型，避免锁重入死锁
                existing_qq = await self._query_binding(qq=qq)
                if existing_qq is not None:
                    raise BindConflictError(qq=qq) from None
                existing_no = await self._query_binding(student_no=student_no)
                if existing_no is not None:
                    raise BindConflictError(student_no=student_no) from None
                raise BindConflictError() from None
        return Binding(qq=qq, student_no=student_no, name=name, bound_at=bound_at)

    async def unbind(self, qq: int) -> bool:
        """删除绑定，返回是否删除了记录。"""
        async with self._lock:
            async with self._transaction() as conn:
                cursor = await conn.execute(
                    "DELETE FROM binding WHERE qq = ?", (qq,)
                )
                return cursor.rowcount > 0

    async def _query_binding(
        self, *, qq: int | None = None, student_no: str | None = None
    ) -> Optional[Binding]:
        """绑定查询内核（不加锁，仅供持锁路径内部复用）。"""
        conn = self._get_conn()
        if qq is not None:
            cursor = await conn.execute("SELECT * FROM binding WHERE qq = ?", (qq,))
        elif student_no is not None:
            cursor = await conn.execute(
                "SELECT * FROM binding WHERE student_no = ?", (student_no,)
            )
        else:
            raise ValueError("qq 与 student_no 至少提供一个")
        row = await cursor.fetchone()
        return self._row_to_binding(row) if row else None

    async def get_binding(
        self, *, qq: int | None = None, student_no: str | None = None
    ) -> Optional[Binding]:
        async with self._lock:
            return await self._query_binding(qq=qq, student_no=student_no)

    async def all_bindings(self) -> list[Binding]:
        async with self._lock:
            conn = self._get_conn()
            cursor = await conn.execute(
                "SELECT * FROM binding ORDER BY bound_at ASC, qq ASC"
            )
            rows = await cursor.fetchall()
            return [self._row_to_binding(row) for row in rows]

    async def count_bindings(self) -> int:
        async with self._lock:
            conn = self._get_conn()
            cursor = await conn.execute("SELECT COUNT(*) FROM binding")
            row = await cursor.fetchone()
            return int(row[0])

    @staticmethod
    def _row_to_binding(row: aiosqlite.Row) -> Binding:
        return Binding(
            qq=int(row["qq"]),
            student_no=str(row["student_no"]),
            name=str(row["name"]),
            bound_at=str(row["bound_at"]),
        )

    # ---------- 任务状态 ----------

    async def set_task_state(
        self,
        task_id: str,
        status: str,
        business_key: str | None,
        error: str | None,
    ) -> None:
        """记录/覆盖任务最近一次执行结果。"""
        async with self._lock:
            async with self._transaction() as conn:
                await conn.execute(
                    "INSERT INTO task_state (task_id, status, business_key, error, ran_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(task_id) DO UPDATE SET "
                    "status=excluded.status, business_key=excluded.business_key, "
                    "error=excluded.error, ran_at=excluded.ran_at",
                    (task_id, status, business_key, error, _now_iso()),
                )

    async def get_task_state(self, task_id: str) -> Optional[TaskState]:
        async with self._lock:
            conn = self._get_conn()
            cursor = await conn.execute(
                "SELECT * FROM task_state WHERE task_id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return TaskState(
                task_id=str(row["task_id"]),
                status=str(row["status"]),
                business_key=row["business_key"],
                error=row["error"],
                ran_at=str(row["ran_at"]),
            )


# 全局数据库实例，随宿主启动/关闭管理（见 __init__.py）
db: Database | None = None


async def init_global(path: Path) -> Database:
    """创建并初始化全局数据库（执行迁移与版本校验）。"""
    global db
    database = Database(path)
    await database.init()
    db = database
    return database


async def close_global() -> None:
    """关闭全局数据库并清除引用。"""
    global db
    if db is not None:
        await db.close()
    db = None


def get_db() -> Database:
    """获取全局数据库实例。"""
    global db
    if db is None:
        raise RuntimeError("数据库尚未初始化（插件启动阶段异常）")
    return db


def reset_db() -> None:
    """重置全局数据库引用（测试与关闭时使用）。"""
    global db
    db = None
