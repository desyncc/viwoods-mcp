"""A private record of what Claude was shown: one JSON line per tool call.

It lists which notebooks and pages were sent, what was searched for and what was refused (a
notebook asked for that isn't shared), so you can check later what left the tablet. It never holds
page images or recognised text. The file is yours alone (mode 600), kept to about 1 MB (the
previous one is kept as `.1`), and lives at ~/.local/state/viwoods-mcp/audit.log unless
$VIWOODS_MCP_AUDIT_LOG says otherwise (`off` turns it off). `viwoods-mcp audit` shows the end of it.
"""

import json
import os
from datetime import datetime
from pathlib import Path

DEFAULT = Path.home() / ".local" / "state" / "viwoods-mcp" / "audit.log"
OFF = ("off", "none", "false", "0", "no")
MAX_BYTES = 1_000_000


def audit_path(value: str | None) -> Path | None:
    """Where the log goes for a $VIWOODS_MCP_AUDIT_LOG value; None if it is switched off."""
    value = (value or "").strip()
    if value.lower() in OFF:
        return None
    return Path(value).expanduser() if value else DEFAULT


class AuditLog:
    def __init__(self, path: Path | None, max_bytes: int = MAX_BYTES):
        self.path, self.max_bytes = path, max_bytes

    def record(self, tool: str, **fields) -> None:
        """Append an entry. A log that can't be written must not break the call it describes."""
        if self.path is None:
            return
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "tool": tool,
                 **{k: v for k, v in fields.items() if v not in ("", None, [], False)}}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def tail(path: Path, n: int = 20) -> list[dict]:
    """The last `n` entries of the log at `path` (those that can be read), oldest first."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def describe(entry: dict) -> str:
    """One entry as a line a person can read."""
    tool = entry.get("tool", "?")
    when = str(entry.get("ts", ""))[:19].replace("T", " ")
    book = f"“{entry['title']}”" if entry.get("title") else ""
    window = " ".join(f"{k}={entry[k]}" for k in ("since", "until") if entry.get(k))
    if tool == "read_pages":
        what = f"{book} pages {entry.get('pages', 'none')}"
        if entry.get("could_not_read"):
            what += f" (could not read {entry['could_not_read']})"
    elif tool == "show_match":
        what = f"{book} p. {entry.get('page')} for “{entry.get('words', '')}” → {entry.get('crops', 0)} crop(s)" \
               + (" (opened)" if entry.get("opened") else "")
    elif tool == "search":
        what = f"“{entry.get('query', '')}” {window} → {entry.get('shown', 0)} of {entry.get('total', 0)} shown"
    elif tool == "list_notebooks":
        filters = " ".join(f"{k}={entry[k]}" for k in ("folder", "query") if entry.get(k))
        what = f"{filters} {window} → {entry.get('returned', 0)} notebooks"
    elif tool == "list_folders":
        what = f"{entry.get('shared', 0)} notebooks shared"
    elif tool == "add_pdf":
        what = f"“{entry.get('added', '')}” into {entry.get('folder', '(top level)')}"
    elif tool == "refused":
        what = f"asked for “{entry.get('notebook', '')}”: not shared, or not there"
    else:
        what = " ".join(f"{k}={v}" for k, v in entry.items() if k not in ("ts", "tool"))
    return f"{when}  {tool:<14} {' '.join(what.split())}"
