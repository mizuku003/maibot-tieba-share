"""麦麦贴吧搬石 —— 搬运记录。

只干一件事：记住「哪个吧的哪个帖子已经搬过了」，顺便记一下每个吧最后一次搬的时间。
存在插件自己的 data 目录里（ctx.paths.data_dir），不动宿主的数据库。

为什么不落数据库：这几条记录是纯粹的本机去重状态，没有跨会话共享的需求，
一个 JSON 文件足够；而且插件卸载、换版本都不用担心破坏宿主表结构。

写盘用「先写临时文件再改名」，避免进程被杀时留下半个 JSON。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = 1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class StoneStore:
    """搬运记录的读写。构造后先 load()，退出前 flush()。"""

    def __init__(self, path: Path, *, keep_per_forum: int = 500) -> None:
        self.path = Path(path)
        self.keep = max(20, int(keep_per_forum or 500))
        self._data: dict[str, Any] = {"version": _SCHEMA, "forums": {}}

    # -- 读写 -----------------------------------------------------------------

    def load(self) -> None:
        """读记录文件。读不到或坏了都当成空记录（不抛异常，插件照常能用）。"""
        try:
            raw = self.path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搬石/记录] 读取失败，按空记录处理：%s", exc)
            return
        try:
            obj = json.loads(raw or "{}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搬石/记录] 内容不是合法 JSON，按空记录处理：%s", exc)
            return
        forums = obj.get("forums") if isinstance(obj, dict) else None
        if isinstance(forums, dict):
            self._data = {"version": _SCHEMA, "forums": forums}

    def flush(self) -> None:
        """落盘。失败只记日志 —— 记录写不进去不该影响搬运本身。"""
        payload = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_bytes(payload.encode("utf-8"))
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搬石/记录] 写入失败（本轮去重可能不准）：%s", exc)

    # -- 查询与标记 -----------------------------------------------------------

    def _bucket(self, forum: str) -> dict[str, Any]:
        forums = self._data.setdefault("forums", {})
        bucket = forums.get(forum)
        if not isinstance(bucket, dict):
            bucket = {"moved": [], "last_run": "", "total": 0}
            forums[forum] = bucket
        if not isinstance(bucket.get("moved"), list):
            bucket["moved"] = []
        return bucket

    def _peek(self, forum: str) -> dict[str, Any]:
        """只读地看一眼某个吧的桶，不存在就给个空的（**不写进数据里**）。

        ``_bucket`` 是「要写」的入口，它会把不存在的吧补进 ``forums``；
        查询路径必须走这里，否则 /搬石 状态 一列就凭空多出一堆「最近 0 条」的幽灵吧。
        """
        forums = self._data.get("forums")
        bucket = forums.get(forum) if isinstance(forums, dict) else None
        if not isinstance(bucket, dict):
            return {"moved": [], "last_run": "", "total": 0}
        if not isinstance(bucket.get("moved"), list):
            bucket = dict(bucket)
            bucket["moved"] = []
        return bucket

    def known(self, forum: str, tid: str) -> bool:
        tid = str(tid)
        return any(str(item.get("tid")) == tid for item in self._peek(forum)["moved"])

    def mark(self, forum: str, tid: str, title: str = "") -> None:
        """记一条「搬过了」，并把老记录裁到 keep 条。"""
        bucket = self._bucket(forum)
        tid = str(tid)
        if any(str(item.get("tid")) == tid for item in bucket["moved"]):
            return
        bucket["moved"].append({"tid": tid, "title": str(title or "")[:80], "at": _now()})
        bucket["total"] = int(bucket.get("total") or 0) + 1
        bucket["last_run"] = _now()
        if len(bucket["moved"]) > self.keep:
            bucket["moved"] = bucket["moved"][-self.keep :]

    def mark_run(self, forum: str) -> None:
        """就算这一轮没搬新帖，也记一下「这个吧我来看过了」。"""
        self._bucket(forum)["last_run"] = _now()

    def reset(self, forum: str = "") -> int:
        """清空记录（不给吧名就清全部），返回清掉多少条。"""
        forums = self._data.setdefault("forums", {})
        if forum:
            bucket = forums.get(forum)
            if not isinstance(bucket, dict):
                return 0
            count = len(bucket.get("moved") or [])
            bucket["moved"] = []
            return count
        count = 0
        for bucket in forums.values():
            if isinstance(bucket, dict):
                count += len(bucket.get("moved") or [])
                bucket["moved"] = []
        return count

    def stats(self) -> dict[str, dict[str, Any]]:
        """给 /搬石 状态 用：每个吧搬过多少条、最后一次看是什么时候。"""
        out: dict[str, dict[str, Any]] = {}
        forums = self._data.get("forums") or {}
        if not isinstance(forums, dict):
            return out
        for forum, bucket in forums.items():
            if not isinstance(bucket, dict):
                continue
            out[str(forum)] = {
                "recent": len(bucket.get("moved") or []),
                "total": int(bucket.get("total") or 0),
                "last_run": str(bucket.get("last_run") or ""),
            }
        return out
