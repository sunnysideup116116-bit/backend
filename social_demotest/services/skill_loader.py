"""Thin trusted loader for the two local Profile Skill instruction packs."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


@lru_cache(maxsize=8)
def load_profile_skill(name: str) -> dict[str, str]:
    path = SKILLS_ROOT / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{name} SKILL.md has no frontmatter")
    _, frontmatter, body = text.split("---\n", 2)
    fields = {}
    for line in frontmatter.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    if fields.get("name") != name or not fields.get("version") or not body.strip():
        raise ValueError(f"invalid {name} SKILL.md")
    return {"name": name, "version": fields["version"], "instructions": body.strip()}
