"""What the app keeps in /data: the queue of objects R2 has not confirmed yet,
and its state (where the log stream resumes, what was uploaded per day,
which days are summarised).

Every file is written to a temporary name and renamed, so a crash leaves
either the old or the new file. A queued object is its body file plus a
meta file written after it; without the meta file it does not exist.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class Item:
    seq: int
    key: str             # object key in the bucket
    kind: str            # "log", "snapshots" or "summaries"
    day: str             # local date YYYY-MM-DD the object belongs to
    content_type: str
    size: int
    queued: float        # epoch seconds when it was queued


@dataclass(frozen=True)
class Quarantined:
    name: str
    size: int
    moved: float         # epoch seconds when it was set aside


class Queue:
    def __init__(self, folder: Path, quarantine: Path | None = None) -> None:
        self._folder = folder
        # Where unreadable entries go: a folder the user can reach, to look at
        # them and delete them by hand
        self.quarantine = quarantine or folder / "quarantine"
        folder.mkdir(parents=True, exist_ok=True)
        for tmp in folder.glob(".*.tmp"):
            tmp.unlink()
        # Bodies whose meta file never got written
        metas = {p.stem for p in folder.glob("*.json")}
        for body in folder.glob("*.bin"):
            if body.stem not in metas:
                body.unlink()
        self._items: dict[int, Item] = {}
        for meta in folder.glob("*.json"):
            try:
                raw = json.loads(meta.read_text())
                item = Item(seq=int(meta.stem), **raw)
            except (OSError, ValueError, TypeError) as err:
                # Never drop queued data: set it aside for a look by hand
                self._quarantine(meta, err)
                continue
            self._items[item.seq] = item
        self._keys = {item.key for item in self._items.values()}
        self._next = max(self._items, default=0) + 1

    def _quarantine(self, meta: Path, err: Exception) -> None:
        folder = self.quarantine
        folder.mkdir(parents=True, exist_ok=True)
        for path in (meta, meta.with_suffix(".bin")):
            if path.exists():
                # Another file system possibly: copy, then remove
                shutil.copy2(path, folder / path.name)
                path.unlink()
                os.utime(folder / path.name)
        LOGGER.error("Queued object %s is unreadable (%s); moved to %s", meta.stem, err, folder)

    def quarantined(self) -> list[Quarantined]:
        """What lies in the quarantine; gone once the user deletes it."""
        try:
            paths = sorted(p for p in self.quarantine.iterdir() if p.is_file() and not p.name.startswith("."))
        except OSError:
            return []
        out = []
        for path in paths:
            with contextlib.suppress(OSError):
                stat = path.stat()
                out.append(Quarantined(path.name, stat.st_size, stat.st_mtime))
        return out

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def add(self, key: str, kind: str, day: str, body: bytes, content_type: str) -> bool:
        """Queues an object; False when the key is queued already."""
        if key in self._keys:
            return False
        seq = self._next
        self._next += 1
        write_atomic(self._folder / f"{seq:010d}.bin", body)
        meta = {"key": key, "kind": kind, "day": day, "content_type": content_type, "size": len(body),
                "queued": time.time()}
        write_atomic(self._folder / f"{seq:010d}.json", json.dumps(meta).encode())
        self._items[seq] = Item(seq=seq, **meta)
        self._keys.add(key)
        return True

    def items(self) -> list[Item]:
        return [self._items[s] for s in sorted(self._items)]

    def body(self, item: Item) -> bytes:
        return (self._folder / f"{item.seq:010d}.bin").read_bytes()

    def done(self, item: Item) -> None:
        # Meta first: a crash in between leaves a body that the next start drops
        (self._folder / f"{item.seq:010d}.json").unlink(missing_ok=True)
        (self._folder / f"{item.seq:010d}.bin").unlink(missing_ok=True)
        self._items.pop(item.seq, None)
        self._keys.discard(item.key)

    def oldest(self) -> float | None:
        """When the longest waiting object was queued."""
        return min((item.queued for item in self._items.values()), default=None)

    def days(self) -> set[str]:
        return {item.day for item in self._items.values()}


class State:
    """JSON state with a few known fields; saved on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            self.data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("log_cursor", None)      # "when" of the newest queued log entry
        self.data.setdefault("panel_cursor", None)    # time of the newest panel change handled
        self.data.setdefault("installed", None)       # local date of the first start
        self.data.setdefault("uploaded", {})          # day -> keys confirmed by R2
        self.data.setdefault("summaries", {})         # day -> {"covered": [...], "files": n}
        self.data.setdefault("last_upload", None)
        self.data.setdefault("last_summary", None)
        self.data.setdefault("storage_days", {})      # UTC date -> [account bytes, bucket bytes]

    def save(self) -> None:
        write_atomic(self._path, json.dumps(self.data, separators=(",", ":")).encode())

    def uploaded(self, day: str, key: str) -> None:
        keys = self.data["uploaded"].setdefault(day, [])
        if key not in keys:
            keys.append(key)
