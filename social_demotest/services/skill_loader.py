"""Thin trusted loader for local Profile Skill instruction packs.

Runtime currently loads the reusable recent-context and memory skills. The
basic/deep assessment files are contract documentation and are not loaded by
the assessment session runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


@lru_cache(maxsize=16)
def load_skill(name: str) -> dict[str, str]:
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


# Preserve the original cached loader surface while allowing Event skills to
# share the same trusted local parser.
load_profile_skill = load_skill
