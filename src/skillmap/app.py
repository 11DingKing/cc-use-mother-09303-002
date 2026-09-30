"""应用装配：单一连接上的领域服务集合。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .db.schema import connect, init_db
from .services.comparison import ComparisonService
from .services.proposals import ProposalService
from .services.publishes import PublishService
from .services.standards import StandardService


class SkillMapApp:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # 单进程内所有写操作经此锁串行化；会签在锁内完成“检查-签署-发布”原子序列
        self.write_lock = threading.RLock()
        init_db(conn)
        self.standards = StandardService(conn, self.write_lock)
        self.proposals = ProposalService(conn, self.write_lock)
        self.publishes = PublishService(conn, self.write_lock)
        self.comparison = ComparisonService(conn)

    @classmethod
    def open(cls, path: str | Path = ":memory:") -> "SkillMapApp":
        return cls(connect(path))

    def close(self) -> None:
        self.conn.close()
