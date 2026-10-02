"""Remote skills, in the shape the harness downloads them.

Protocol (packages/core/src/skill/discovery.ts):

    GET <base>/index.json      -> {"skills":[{"name","version"?,"files":[...]}]}
    GET <base>/<name>/<file>   -> the file itself

Two constraints drive this module:

  * The harness sends **no headers** on these requests, so the session token
    travels as a path segment (`/skills/<token>/...`). Query strings are not an
    option: `new URL("index.json", base)` drops them.
  * A cached skill file is only ever re-downloaded when `version` changes
    (discovery.ts:79,152). A missing version means the employee keeps the first
    copy forever, so the version is derived from the content hash here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

#: Files published for each skill. Anything else in the directory stays private.
PUBLISHED_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".py", ".sh", ".js", ".ts"}
#: The harness drops any skill whose files include neither of these.
ENTRY_FILES = ("SKILL.md",)
MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    files: tuple[str, ...]


def _is_publishable(path: Path) -> bool:
    if not path.is_file() or path.name.startswith("."):
        return False
    if path.suffix.lower() not in PUBLISHED_SUFFIXES:
        return False
    return path.stat().st_size <= MAX_FILE_BYTES


def discover(root: Path) -> list[Skill]:
    """List publishable skills under `root`, newest content hash as version."""
    if not root.is_dir():
        return []
    skills: list[Skill] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        # The harness requires a safe single path segment for the name.
        if directory.name in {".", ".."} or "/" in directory.name or "\\" in directory.name:
            continue
        files: list[str] = []
        digest = hashlib.sha256()
        for item in sorted(directory.rglob("*")):
            if not _is_publishable(item):
                continue
            relative = item.relative_to(directory).as_posix()
            files.append(relative)
            digest.update(relative.encode("utf-8"))
            digest.update(item.read_bytes())
        if not any(entry in files for entry in ENTRY_FILES) and f"{directory.name}.md" not in files:
            continue
        skills.append(Skill(name=directory.name, version=digest.hexdigest()[:16], files=tuple(files)))
    return skills


def index(root: Path) -> dict[str, object]:
    return {
        "skills": [
            {"name": skill.name, "version": skill.version, "files": list(skill.files)}
            for skill in discover(root)
        ]
    }


def resolve(root: Path, skill: str, relative: str) -> Path:
    """Resolve one published file, refusing anything outside its skill directory."""
    if skill in {"", ".", ".."} or "/" in skill or "\\" in skill or "\0" in skill:
        raise FileNotFoundError(skill)
    if "\0" in relative or relative.startswith("/"):
        raise FileNotFoundError(relative)

    base = (root / skill).resolve()
    if not base.is_dir() or base.parent != root.resolve():
        raise FileNotFoundError(skill)

    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise FileNotFoundError(relative)
    if not _is_publishable(target):
        raise FileNotFoundError(relative)
    return target
