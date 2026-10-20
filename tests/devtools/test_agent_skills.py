"""Agent Skills (https://agentskills.io/specification): `.agents/skills/<name>/SKILL.md` and its link `.claude/skills/<name>`.

The frontmatter has `name` (1 to 64 characters: lowercase letters, digits and single hyphens, not at either end, equal to the folder
name) and `description` (1 to 1024 characters). The body stays under 500 lines. `.claude/skills/<name>` is a relative symlink to
`../../.agents/skills/<name>`."""

import re

import pytest
from support.paths import REPO_ROOT

SKILLS = REPO_ROOT / ".agents" / "skills"
LINKS = REPO_ROOT / ".claude" / "skills"
NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
NAMES = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())


def frontmatter(name: str) -> tuple[dict[str, str], str]:
    text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md starts with a --- frontmatter"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, "the frontmatter ends with a --- line"
    fields: dict[str, str] = {}
    for line in head.splitlines():
        m = re.fullmatch(r"([a-z-]+): (.+)", line)
        assert m, f"a one-line `key: value` field, got {line!r}"
        fields[m.group(1)] = m.group(2).strip()
    return fields, body


def test_there_are_skills():
    assert NAMES


@pytest.mark.parametrize("name", NAMES)
def test_the_frontmatter_follows_the_specification(name):
    fields, body = frontmatter(name)
    assert fields["name"] == name, "name equals the folder name"
    assert 1 <= len(name) <= 64 and NAME.fullmatch(name), name
    assert 1 <= len(fields["description"]) <= 1024, len(fields["description"])
    assert len(body.splitlines()) < 500


@pytest.mark.parametrize("name", NAMES)
def test_a_link_in_claude_skills_resolves_to_the_skill(name):
    link = LINKS / name
    assert link.is_symlink(), f"{link} is a symlink"
    assert link.readlink().as_posix() == f"../../.agents/skills/{name}"
    assert link.resolve() == (SKILLS / name).resolve()
    assert (link / "SKILL.md").is_file()


def test_every_link_names_a_skill():
    assert sorted(p.name for p in LINKS.iterdir()) == NAMES
