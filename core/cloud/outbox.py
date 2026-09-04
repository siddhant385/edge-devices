"""Bounded durable JSONL outbox for alert payloads that failed to deliver.

The outbox is a single append-only file. On every append, ``enforce_cap``
rewrites the file to keep only the most recent N records, atomically via
tmp+replace so a power loss can't truncate the live file. On flush, the
sender reads the file, retries each record, and writes back any that
still failed.

One Outbox instance per AlertSender. Thread-safe via an internal asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

import aiofiles

logger = logging.getLogger(__name__)


class Outbox:
    """Async JSONL queue with FIFO cap enforcement."""

    def __init__(self, path: Path, max_records: int = 500) -> None:
        self._path = path
        self._max_records = max_records
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def append(self, payload: dict[str, Any]) -> None:
        """Serialize, append one line, and enforce the cap.

        Failures are logged but never raised - the outbox is best-effort
        durability, not a hard contract.
        """
        async with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(self._serializable(payload), separators=(",", ":")) + "\n"
                async with aiofiles.open(self._path, mode="a", encoding="utf-8") as f:
                    await f.write(line)
                await self._enforce_cap_locked()
            except Exception as error:
                logger.error("Failed to append to outbox %s: %s", self._path, error)

    async def read_all(self) -> list[dict[str, Any]]:
        """Read every record. Decodes base64 evidence_jpeg back to bytes."""
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            async with aiofiles.open(self._path, mode="r", encoding="utf-8") as f:
                async for line in f:
                    try:
                        record = json.loads(line)
                        jpeg = record.get("evidence_jpeg")
                        if jpeg:
                            record["evidence_jpeg"] = base64.b64decode(jpeg)
                        records.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception as error:
            logger.error("Failed to read outbox %s: %s", self._path, error)
            return []
        return records

    async def write_tail(self, records: list[dict[str, Any]]) -> None:
        """Atomically replace the file with the given records.

        Used by flush() to drop records that were successfully delivered.
        """
        async with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                content = "".join(
                    json.dumps(self._serializable(r), separators=(",", ":")) + "\n" for r in records
                )
                async with aiofiles.open(tmp, mode="w", encoding="utf-8") as f:
                    await f.write(content)
                tmp.replace(self._path)
            except Exception as error:
                logger.error("Failed to rewrite outbox %s: %s", self._path, error)

    async def _enforce_cap_locked(self) -> None:
        """Trim the file to the last N lines. Caller must hold the lock."""
        if self._max_records <= 0:
            return
        if not self._path.exists():
            return
        async with aiofiles.open(self._path, mode="r", encoding="utf-8") as f:
            raw = await f.read()
        lines = raw.splitlines()
        if len(lines) <= self._max_records:
            return
        kept = lines[-self._max_records :]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        async with aiofiles.open(tmp, mode="w", encoding="utf-8") as f:
            await f.write("\n".join(kept) + "\n")
        tmp.replace(self._path)
        logger.warning(
            "Outbox cap %d exceeded (%d lines), dropped %d oldest records",
            self._max_records,
            len(lines),
            len(lines) - len(kept),
        )

    @staticmethod
    def _serializable(record: dict[str, Any]) -> dict[str, Any]:
        """Base64-encode evidence_jpeg (bytes) for JSON storage."""
        value = record.get("evidence_jpeg")
        if not isinstance(value, bytes):
            return record
        out = record.copy()
        out["evidence_jpeg"] = base64.b64encode(value).decode("ascii")
        return out
