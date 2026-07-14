import json
import re
from pathlib import Path

from telethon.utils import resolve_id

_PENDING_HEADER = "⏳ Pending source"

STATUS_ALLOWED = "allowed"
STATUS_DENIED = "denied"
STATUS_PENDING = "pending"


def load_store(path) -> dict[int, dict]:
    try:
        with Path(path).open(encoding="utf-8") as store_file:
            data = json.load(store_file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}

    sources = {}
    for source_id, entry in data.get("sources", {}).items():
        normalized = dict(entry)
        if "status" not in normalized:
            normalized["status"] = STATUS_ALLOWED
        sources[int(source_id)] = normalized
    return sources


def save_store(path, sources: dict[int, dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"sources": {str(source_id): entry for source_id, entry in sources.items()}}
    with path.open("w", encoding="utf-8") as store_file:
        json.dump(data, store_file, indent=2)


def add_source(
    sources,
    source_id: int,
    destinations: list[str],
    label: str,
    kind: str,
    *,
    status: str = STATUS_ALLOWED,
) -> dict:
    entry = {
        "status": status,
        "destinations": destinations,
        "label": label,
        "kind": kind,
    }
    sources[source_id] = entry
    return entry


def mark_pending(sources, source_id: int, label: str, kind: str) -> dict:
    return add_source(
        sources,
        source_id,
        [],
        label,
        kind,
        status=STATUS_PENDING,
    )


def disallow_source(sources, source_id: int, label: str, kind: str) -> dict:
    return add_source(
        sources,
        source_id,
        [],
        label,
        kind,
        status=STATUS_DENIED,
    )


def remove_source(sources, source_id: int) -> bool:
    if source_id not in sources:
        return False
    del sources[source_id]
    return True


def source_status(entry: dict | None) -> str | None:
    if entry is None:
        return None
    return entry.get("status", STATUS_ALLOWED)


def is_allowed(entry: dict | None) -> bool:
    return source_status(entry) == STATUS_ALLOWED


def is_denied(entry: dict | None) -> bool:
    return source_status(entry) == STATUS_DENIED


def is_pending(entry: dict | None) -> bool:
    return source_status(entry) == STATUS_PENDING


def source_id_from_chat_id(chat_id) -> int | None:
    if chat_id is None:
        return None
    return resolve_id(chat_id)[0]


def parse_command(text: str) -> tuple[str, list[str]] | None:
    tokens = text.split()
    if not tokens or not tokens[0].startswith("/"):
        return None
    return tokens[0].lower(), tokens[1:]


def format_listing(sources: dict[int, dict]) -> str:
    return "\n".join(
        f"{source_id}  status={entry.get('status', STATUS_ALLOWED)}  "
        f"kind={entry['kind']}  label={entry['label']}"
        + (
            f"  -> {','.join(entry['destinations'])}"
            if entry.get("destinations")
            else ""
        )
        for source_id, entry in sources.items()
    )


def format_pending_notification(
    *,
    kind: str,
    name: str,
    source_id: int,
    chat_id: int,
    message_preview: str,
    message_link: str | None,
) -> str:
    lines = [
        _PENDING_HEADER,
        f"kind: {kind}",
        f"name: {name}",
        f"source_id: {source_id}",
        f"chat_id: {chat_id}",
        "",
        "--- message ---",
        message_preview or "[empty]",
    ]
    if message_link:
        lines.extend(["", f"link: {message_link}"])
    lines.extend(
        [
            "",
            "Reply /allow [label] to approve, /disallow to ignore, or /reset to clear.",
        ]
    )
    return "\n".join(lines)


def parse_pending_notification(text: str) -> dict | None:
    if not text or _PENDING_HEADER not in text:
        return None
    source_match = re.search(r"^source_id:\s*(\d+)", text, re.MULTILINE)
    if not source_match:
        return None
    kind_match = re.search(r"^kind:\s*(\S+)", text, re.MULTILINE)
    name_match = re.search(r"^name:\s*(.+)", text, re.MULTILINE)
    return {
        "source_id": int(source_match.group(1)),
        "kind": kind_match.group(1) if kind_match else "source",
        "name": name_match.group(1).strip() if name_match else None,
    }
