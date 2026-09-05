"""统一 SQLite 连接工厂：WAL + busy_timeout + 显式提交/关闭。

项目里曾并存三种连接语义（原生 with 只 commit 不 close / 自定义
contextmanager / 无 WAL 裸 connect），多线程写库时靠超时硬扛
"database is locked"。所有库统一走这里：

- WAL：写不再阻塞读，多线程写冲突面显著缩小；
- busy_timeout：写锁竞争时短暂等待而不是立刻抛 locked；
- session()：commit/rollback + close 都做（sqlite3 的 with 只做前者）。
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterator


def connect(db_path: str, *, timeout: float = 10.0) -> sqlite3.Connection:
    """新连接：Row 工厂 + WAL + busy_timeout。调用方负责关闭（或用 session）。"""
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def sqlite_session(db_path: str, *, timeout: float = 10.0) -> Iterator[sqlite3.Connection]:
    """短生命周期连接：退出时自动 commit（异常则 rollback）并关闭。"""
    conn = connect(db_path, timeout=timeout)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
