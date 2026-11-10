"""What goes into the bucket and under which key: log entries, snapshots and
daily summaries; and whether the bucket's rules match the options.

Folders follow the local date, so they match the daily summary; times inside
the objects are UTC.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# Prefix per kind of data
PREFIX = {"log": "protokoll/", "snapshots": "snapshots/", "summaries": "zusammenfassung/"}
DAY_SECONDS = 86400
# Folder state of a snapshot an automation asked for, and the longest reason kept
REQUEST = "request"
REASON_MAX = 200
PANEL_STATES = ("armed_home", "armed_away", "armed_night", "armed_vacation", "armed_custom_bypass",
                "arming", "pending", "triggered", "disarmed")
# Attributes that hold access tokens (cameras, media players); they stay out of the archive
SECRET_ATTRIBUTES = ("access_token", "entity_picture")

# The keys the app writes, and nothing else: a key read back from the bucket
# reaches the web page and the ZIP export only when it has this form
_DAY = r"(?P<y>\d{4})/(?P<m>\d{2})/(?P<d>\d{2})/"
_STAMP = r"\d{8}T\d{6}\.\d{6}Z"
LOG_KEY = re.compile(rf"^protokoll/{_DAY}{_STAMP}_[0-9a-f]{{8}}\.json$")
SNAPSHOT_FOLDER = re.compile(rf"^snapshots/{_DAY}{_STAMP}_(?:{'|'.join((*PANEL_STATES, REQUEST))})$")
SNAPSHOT_FILE = re.compile(r"^(?:meta\.json|zustand\.json\.gz|kamera_[a-z0-9_]+\.jpg)$")


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


def request_reason(raw: object) -> str | None:
    """The reason of a snapshot request: text, trimmed, at most REASON_MAX characters."""
    if raw is None:
        return None
    text = " ".join(str(raw).split())[:REASON_MAX].strip()
    return text or None


def camera_file(entity_id: str, number: int = 1) -> str:
    """The picture of a camera; further pictures of a series get _2, _3, ..."""
    suffix = "" if number == 1 else f"_{number}"
    return f"kamera_{entity_id.split('.', 1)[1]}{suffix}.jpg"


def without_secrets(attributes: dict) -> dict:
    """Attributes without access tokens."""
    return {k: v for k, v in attributes.items() if k not in SECRET_ATTRIBUTES}


def valid_folder(folder: str) -> bool:
    """A snapshot folder (without the trailing /) as the app names it."""
    match = SNAPSHOT_FOLDER.match(folder)
    return bool(match) and _real_day(match)


def valid_key(key: str) -> bool:
    """A log or snapshot key as the app writes it, or a summary key."""
    if match := LOG_KEY.match(key):
        return _real_day(match)
    if key.startswith(PREFIX["snapshots"]):
        folder, _, name = key.rpartition("/")
        return valid_folder(folder) and bool(SNAPSHOT_FILE.match(name))
    return summary_part(key) is not None


def _real_day(match: re.Match) -> bool:
    try:
        date(int(match["y"]), int(match["m"]), int(match["d"]))
    except ValueError:
        return False
    return True


def key_day(key: str) -> str | None:
    """The local day of a log or snapshot key from its folders."""
    parts = key.split("/")
    try:
        return date(int(parts[1]), int(parts[2]), int(parts[3])).isoformat()
    except (IndexError, ValueError):
        return None


def summary_part(key: str) -> tuple[str, int] | None:
    """(day, part) of a summary key: 0 for the summary, N for nachtrag-N."""
    name = key.rsplit("/", 1)[-1].removesuffix(".txt")
    day, rest = name[:10], name[10:]
    try:
        date.fromisoformat(day)
        part = int(rest.removeprefix("-nachtrag-")) if rest else 0
    except ValueError:
        return None
    if summary_key(day, part) != key:
        return None
    return day, part


def states_body(moment: datetime, states: list[dict], reconstructed: bool) -> bytes:
    data = {"time": moment.astimezone(UTC).isoformat(), "reconstructed": reconstructed,
            "states": states}
    return gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0)


def meta_body(moment: datetime, panel: str, old: str | None, new: str, attributes: dict,
              user_id: str | None, cameras: dict[str, str | None], reconstructed: bool,
              series: dict[str, list[str]] | None = None, reason: str | None = None) -> bytes:
    """`cameras`: entity -> file name, or None when its picture is missing;
    `series`: entity -> all file names of its picture series, the first included;
    `reason`: what an automation gave when it asked for the snapshot."""
    data = {"time": moment.astimezone(UTC).isoformat(), "panel": panel, "from": old, "to": new,
            "reason": reason, "attributes": attributes, "user_id": user_id, "reconstructed": reconstructed,
            "cameras": {cam: name for cam, name in cameras.items() if name},
            "missing_cameras": [cam for cam, name in cameras.items() if not name]}
    if series:
        data["series"] = series
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
