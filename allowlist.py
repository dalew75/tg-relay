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


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def html_link(url: str | None, label: str) -> str:
    safe_label = escape_html(label)
    if not url:
        return safe_label
    return f'<a href="{escape_html(url)}">{safe_label}</a>'


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
    chat_link: str | None,
    message_link: str | None,
) -> str:
    name_html = html_link(chat_link, name)
    source_html = html_link(message_link, str(source_id))
    allow_cmd = f"/allow {source_id} {name}"
    lines = [
        _PENDING_HEADER,
        f"kind: {escape_html(kind)}",
        f"name: {name_html}",
        f"source_id: {source_html}",
        f"chat_id: {chat_id}",
        "",
        "--- message ---",
        escape_html(message_preview or "[empty]"),
        "",
        escape_html(allow_cmd),
        f"/disallow {source_id}",
        f"/reset {source_id}",
    ]
    return "\n".join(lines)


def parse_pending_notification(text: str) -> dict | None:
    if not text or _PENDING_HEADER not in text:
        return None
    source_match = re.search(
        r"^source_id:\s*(?:<a href=\"[^\"]+\">(\d+)</a>|(\d+))",
        text,
        re.MULTILINE,
    )
    if not source_match:
        return None
    kind_match = re.search(r"^kind:\s*(\S+)", text, re.MULTILINE)
    name_match = re.search(
        r"^name:\s*(?:<a href=\"[^\"]+\">([^<]+)</a>|(.+))",
        text,
        re.MULTILINE,
    )
    return {
        "source_id": int(source_match.group(1) or source_match.group(2)),
        "kind": kind_match.group(1) if kind_match else "source",
        "name": (
            (name_match.group(1) or name_match.group(2)).strip()
            if name_match
            else None
        ),
    }


def source_record(
    source_id: int,
    entry: dict,
    *,
    env: bool = False,
    activity: dict | None = None,
) -> dict:
    activity = activity or {}
    return {
        "id": source_id,
        "label": entry.get("label") or str(source_id),
        "kind": entry.get("kind", "source"),
        "status": entry.get("status", STATUS_ALLOWED),
        "destinations": list(entry.get("destinations") or []),
        "env": env,
        "lastMessageAt": activity.get("lastMessageAt"),
        "messageCount": activity.get("messageCount", 0),
        "lastPreview": activity.get("lastPreview"),
    }
