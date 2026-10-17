"""What may be shared: a list of folder paths to allow and to deny.

A rule is a folder path (case-insensitive, `*` is the only wildcard and matches within a name). It
covers that folder, every folder under it and every notebook in them, and its last part may be a
notebook's title. **Deny wins over allow**, and a notebook that no rule allows is not shared. `"*"`
allows everything (then deny what you keep back). With no policy file, or one that can't be
understood, nothing is shared.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .paper import Notebook

POLICY_FILE = Path(os.environ.get("VIWOODS_MCP_POLICY")
                   or Path.home() / ".config" / "viwoods-mcp" / "policy.json")


def parts(path: str) -> list[str]:
    """A folder path as its names: split on / (or \\), stripped, lower-cased, empty ones dropped."""
    return [p.strip().lower() for p in path.replace("\\", "/").split("/") if p.strip()]


def _name_matches(pattern: str, name: str) -> bool:
    """`*` matches anything; every other character, `[`, `]` and `?` included, matches itself."""
    return re.fullmatch(".*".join(re.escape(piece) for piece in pattern.split("*")), name,
                        re.DOTALL) is not None


@dataclass
class Policy:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    write: bool = False            # may viwoods_add_pdf_notebook put a PDF on the tablet (into an allowed folder)

    @staticmethod
    def covers(rule: str, path: list[str], flat: bool = False) -> bool:
        """Does `rule` cover `path` (folder names, then the notebook's title)? A rule is a prefix
        of the path, name by name, with `*` globs and no regard to case or edge whitespace.

        A name that itself holds a "/" can't be told apart from two names, so `flat` reads the path
        as one string split on every "/": that over-matches, which is what a deny rule wants. An
        allow rule must line up with the names exactly."""
        want = parts(rule)
        have = parts("/".join(path)) if flat else [p.strip().lower() for p in path]
        if not want or len(want) > len(have):
            return False
        return all(_name_matches(w, h) for w, h in zip(want, have))

    def allows(self, path: list[str]) -> bool:
        if any(self.covers(r, path, flat=True) for r in self.deny):
            return False
        return any(self.covers(r, path) for r in self.allow)

    @classmethod
    def load(cls, path: Path = POLICY_FILE) -> "Policy":
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return cls()                                    # no file: nothing is shared
        if not isinstance(raw, dict) or not isinstance(raw.get("deny", []), list):
            return cls()                    # a deny list that isn't read must not mean "no denials"

        def names(key):
            v = raw.get(key, [])
            return [str(x) for x in v] if isinstance(v, list) else []
        return cls(names("allow"), names("deny"), raw.get("write") is True)

    def save(self, path: Path = POLICY_FILE) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"allow": self.allow, "deny": self.deny, "write": self.write},
                                   indent=2) + "\n")
        path.chmod(0o600)


def notebook_path(nb: Notebook) -> list[str]:
    return [p for p in nb.folder.split("/") if p] + [nb.title]


def apply_rule(policy: Policy, action: str, rule: str) -> None:
    """allow / deny / remove, keeping the lists free of duplicates (and a rule out of both)."""
    rule = rule.strip().strip("/")
    if not rule:
        raise ValueError("a folder path is needed")
    key = rule.lower()
    for lst in (policy.allow, policy.deny):
        lst[:] = [r for r in lst if r.lower() != key]
    if action == "allow":
        policy.allow.append(rule)
    elif action == "deny":
        policy.deny.append(rule)
    elif action != "remove":
        raise ValueError(f"unknown action {action}")
