"""行程存储：内存为主 + 轻量 JSON 落盘（失败不影响主流程）。

单机 Demo 用不上数据库；但保留落盘可以让「重启后还能看到昨天的行程」，
也顺手给出了后续替换为 Redis/Postgres 的接口边界。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import ChangeLog, Trip
from .settings import BASE_DIR

DATA_DIR = BASE_DIR / ".data"
DATA_FILE = DATA_DIR / "trips.json"


class Store:
    def __init__(self, persist: bool = True) -> None:
        self._items: dict[str, Trip] = {}
        self._lock = threading.RLock()
        self._persist = persist
        if persist:
            self._load()

    # ------------------------------------------------------------------ #
    def save(self, trip: Trip, *, reason: str = "", changes: list[str] | None = None,
             bump: bool = False) -> Trip:
        with self._lock:
            old = self._items.get(trip.trip_id)
            if old is not None:
                trip.version = old.version + (1 if bump else 0)
                trip.history = list(old.history)
                trip.created_at = old.created_at
            trip.updated_at = datetime.now().isoformat(timespec="seconds")
            if reason:
                trip.history.append(ChangeLog(version=trip.version, at=trip.updated_at,
                                              reason=reason, changes=changes or []))
            self._items[trip.trip_id] = trip
            self._flush()
            return trip

    def get(self, trip_id: str) -> Optional[Trip]:
        with self._lock:
            return self._items.get(trip_id)

    def delete(self, trip_id: str) -> bool:
        with self._lock:
            existed = self._items.pop(trip_id, None) is not None
            if existed:
                self._flush()
            return existed

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda t: t.updated_at, reverse=True)
            return [{
                "trip_id": t.trip_id,
                "destinations": t.request.destinations,
                "start_date": t.request.start_date.isoformat(),
                "end_date": t.request.end_date.isoformat(),
                "days": len(t.days),
                "travelers": t.request.travelers,
                "budget_low": t.budget.total_low if t.budget else None,
                "budget_high": t.budget.total_high if t.budget else None,
                "engine": t.engine,
                "updated_at": t.updated_at,
            } for t in items[:limit]]

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
            self._flush()
            return count

    # ------------------------------------------------------------------ #
    def _flush(self) -> None:
        if not self._persist:
            return
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {k: v.model_dump(mode="json") for k, v in self._items.items()}
            tmp = DATA_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(DATA_FILE)
        except Exception:                            # noqa: BLE001 —— 落盘失败不影响主流程
            pass

    def _load(self) -> None:
        try:
            if not DATA_FILE.exists():
                return
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            for key, value in (raw or {}).items():
                try:
                    self._items[key] = Trip.model_validate(value)
                except Exception:                    # noqa: BLE001
                    continue
        except Exception:                            # noqa: BLE001
            pass


STORE = Store()
