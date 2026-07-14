import json
from pathlib import Path

from telethon.utils import resolve_id


def load_store(path) -> dict[int, dict]:
    try:
        with Path(path).open(encoding="utf-8") as store_file:
            data = json.load(store_file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}

    return {int(source_id): entry for source_id, entry in data.get("sources", {}).items()}


def save_store(path, sources: dict[int, dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"sources": {str(source_id): entry for source_id, entry in sources.items()}}
    with path.open("w", encoding="utf-8") as store_file:
        json.dump(data, store_file, indent=2)


def add_source(
    sources, source_id: int, destinations: list[str], label: str, kind: str
) -> dict:
    entry = {
        "destinations": destinations,
        "label": label,
        "kind": kind,
    }
    sources[source_id] = entry
    return entry


def remove_source(sources, source_id: int) -> bool:
    if source_id not in sources:
        return False
    del sources[source_id]
    return True


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
        f"{source_id}  kind={entry['kind']}  label={entry['label']}  -> "
        f"{','.join(entry['destinations'])}"
        for source_id, entry in sources.items()
    )
