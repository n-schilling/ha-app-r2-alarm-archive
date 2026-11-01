"""What goes into the bucket and under which key: log entries, snapshots and
daily summaries; and whether the bucket's rules match the options.

Folders follow the local date, so they match the daily summary; times inside
the objects are UTC.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# Prefix per kind of data
PREFIX = {"log": "protokoll/", "snapshots": "snapshots/", "summaries": "zusammenfassung/"}
DAY_SECONDS = 86400


def utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def stamp(moment: datetime) -> str:
    """20261031T082409.123456Z: sorts by time, safe in a key."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def local_day(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).date().isoformat()


def day_folder(day: str) -> str:
    return day.replace("-", "/") + "/"


# --- Log ----------------------------------------------------------------------
def log_entry(raw: dict, log_entity: str) -> dict | None:
    """A log line from the logbook stream, or None for anything else
    (state changes of the anchor entity, other entities)."""
    if raw.get("entity_id") != log_entity or "state" in raw or not raw.get("message"):
        return None
    try:
        when = float(raw["when"])
    except (KeyError, TypeError, ValueError):
        return None
    entry = {"time": utc(when).isoformat(), "when": when, "message": str(raw["message"])}
    if raw.get("name"):
        entry["name"] = str(raw["name"])
    if raw.get("context_user_id"):
        entry["user_id"] = str(raw["context_user_id"])
    return entry


def log_object(entry: dict, tz: ZoneInfo) -> tuple[str, str, bytes]:
    """(key, day, body) of a log entry. The key comes from time and text, so
    the same entry always gets the same key."""
    moment = utc(entry["when"])
    digest = hashlib.sha256(f"{entry['when']!r}|{entry['message']}".encode()).hexdigest()[:8]
    day = local_day(moment, tz)
    key = f"{PREFIX['log']}{day_folder(day)}{stamp(moment)}_{digest}.json"
    body = json.dumps({k: v for k, v in entry.items() if k != "when"}, ensure_ascii=False,
                      indent=1).encode()
    return key, day, body


# --- Snapshots ------------------------------------------------------------------
def snapshot_folder(moment: datetime, state: str, tz: ZoneInfo) -> tuple[str, str]:
    day = local_day(moment, tz)
    return f"{PREFIX['snapshots']}{day_folder(day)}{stamp(moment)}_{state}/", day


def camera_file(entity_id: str) -> str:
    return f"kamera_{entity_id.split('.', 1)[1]}.jpg"


def states_body(moment: datetime, states: list[dict], reconstructed: bool) -> bytes:
    data = {"time": moment.astimezone(UTC).isoformat(), "reconstructed": reconstructed,
            "states": states}
    return gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0)


def meta_body(moment: datetime, panel: str, old: str | None, new: str, attributes: dict,
              user_id: str | None, cameras: dict[str, str | None], reconstructed: bool) -> bytes:
    """`cameras`: entity -> file name, or None when its picture is missing."""
    data = {"time": moment.astimezone(UTC).isoformat(), "panel": panel, "from": old, "to": new,
            "attributes": attributes, "user_id": user_id, "reconstructed": reconstructed,
            "cameras": {cam: name for cam, name in cameras.items() if name},
            "missing_cameras": [cam for cam, name in cameras.items() if not name]}
    return json.dumps(data, ensure_ascii=False, indent=1).encode()


# --- Daily summary --------------------------------------------------------------
def summary_key(day: str, part: int) -> str:
    year, month, _ = day.split("-")
    suffix = "" if part == 0 else f"-nachtrag-{part}"
    return f"{PREFIX['summaries']}{year}/{month}/{day}{suffix}.txt"


def summary_text(day: str, entries: list[tuple[str, bytes]], snapshots: list[str], tz: ZoneInfo,
                 part: int, created: datetime) -> bytes:
    """`entries`: (key, body) of the log objects in R2, `snapshots`: their folders."""
    shown = date.fromisoformat(day).strftime("%d.%m.%Y")
    title = f"Alarm-Protokoll {shown}" + (f" – Nachtrag {part}" if part else "")
    lines = [title, "=" * len(title), ""]
    rows = []
    for key, body in entries:
        try:
            entry = json.loads(body)
            moment = datetime.fromisoformat(entry["time"]).astimezone(tz)
            rows.append((moment, f"{moment.strftime('%d.%m.%Y %H:%M:%S')} - {entry['message']}"))
        except (ValueError, KeyError, TypeError):
            rows.append((datetime.max.replace(tzinfo=UTC), f"(nicht lesbar: {key})"))
    lines += [text for _, text in sorted(rows)] or ["(keine Einträge)"]
    if snapshots:
        lines += ["", "Snapshots:"] + [f"  {folder}" for folder in sorted(snapshots)]
    lines += ["", f"{len(entries)} Einträge, erstellt {created.astimezone(tz).isoformat()}", "",
              "SHA-256:"]
    lines += [f"  {hashlib.sha256(body).hexdigest()}  {key}" for key, body in sorted(entries)]
    return ("\n".join(lines) + "\n").encode()


# --- Bucket rules ---------------------------------------------------------------
def _covers(rule_prefix: str, prefix: str) -> bool:
    return prefix.startswith(rule_prefix or "")


def rule_problems(lifecycle: list[dict], locks: list[dict], retention: dict, lock: dict) -> list[str]:
    """What differs between the bucket's rules and the options; empty if nothing."""
    problems = []
    for kind, prefix in PREFIX.items():
        deletes = [r for r in lifecycle if r.get("enabled")
                   and _covers((r.get("conditions") or {}).get("prefix", ""), prefix)
                   and (r.get("deleteObjectsTransition") or {}).get("condition")]
        want = retention[kind]
        if want is None:
            if deletes:
                problems.append(f"{prefix}: retention unlimited, but lifecycle rule "
                                f"{deletes[0].get('id')!r} deletes objects")
        else:
            ages = [(r["deleteObjectsTransition"]["condition"] or {}).get("maxAge") for r in deletes]
            if ages != [want * DAY_SECONDS]:
                problems.append(f"{prefix}: retention {want} days needs exactly one lifecycle rule "
                                f"deleting after {want * DAY_SECONDS} s, found {ages or 'none'}")
        held = [r for r in locks if r.get("enabled") and _covers(r.get("prefix", ""), prefix)]
        conditions = [r.get("condition") or {} for r in held]
        want_lock = lock[kind]
        if want_lock is None:
            if held:
                problems.append(f"{prefix}: lock off, but lock rule {held[0].get('id')!r} holds objects")
        elif want_lock == "indefinite":
            if [c.get("type") for c in conditions] != ["Indefinite"]:
                problems.append(f"{prefix}: lock indefinite needs exactly one indefinite lock rule, "
                                f"found {conditions or 'none'}")
        elif [(c.get("type"), c.get("maxAgeSeconds")) for c in conditions] != \
                [("Age", want_lock * DAY_SECONDS)]:
            problems.append(f"{prefix}: lock {want_lock} days needs exactly one lock rule of "
                            f"{want_lock * DAY_SECONDS} s, found {conditions or 'none'}")
    return problems


def rule_commands(bucket: str, retention: dict, lock: dict, jurisdiction: str = "default") -> list[str]:
    """Wrangler commands that set the rules the options ask for."""
    commands = []
    where = "" if jurisdiction == "default" else f" --jurisdiction {jurisdiction}"
    for kind, prefix in PREFIX.items():
        if retention[kind] is not None:
            commands.append(f"npx wrangler r2 bucket lifecycle add {bucket} delete-{kind} {prefix} "
                            f"--expire-days {retention[kind]}{where}")
        if lock[kind] == "indefinite":
            commands.append(f"npx wrangler r2 bucket lock add {bucket} lock-{kind} {prefix} "
                            f"--retention-indefinite{where}")
        elif lock[kind] is not None:
            commands.append(f"npx wrangler r2 bucket lock add {bucket} lock-{kind} {prefix} "
                            f"--retention-days {lock[kind]}{where}")
    return commands
