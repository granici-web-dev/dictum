"""Стандарты рабочего проекта и их снимок (app/project.py).

fixtures/project_frontend/ составлен вручную для тестов и не повторяет стек ни одного реального
проекта: первая строка каждого файла это говорит. Настоящие стандарты настоящего проекта здесь
одни, CONVENTIONS.md этого репозитория, и тесты кладут на него символические ссылки так же, как
лежит .agents/context.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import settings
from app.project import (
    MAX_STANDARDS_CHARACTERS,
    ProjectContextMissing,
    ProjectContextTooLarge,
    configured_directory,
    find_standards_dir,
    project_snapshot,
    read_project,
    read_standards,
    refreshed_snapshot,
)
from tests.helpers import FIXTURES

ROOT = Path(__file__).resolve().parent.parent
CONVENTIONS = ROOT / "CONVENTIONS.md"
FRONTEND = FIXTURES / "project_frontend"
TAKEN_AT = datetime(2026, 9, 17, 10, 2, tzinfo=UTC)
LATER = datetime(2026, 9, 19, 8, 14, tzinfo=UTC)

REAL_TEXT = "Real standards text that is long enough not to be a placeholder. " * 3


def write(directory: Path, name: str, text: str = REAL_TEXT) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def conventions_repository(root: Path) -> Path:
    context = root / ".agents" / "context"
    context.mkdir(parents=True)
    for name in ("PRINCIPLES.md", "STACK.md", "TESTING.md"):
        (context / name).symlink_to(CONVENTIONS)
    return root


def snapshot_of(directory: Path) -> str:
    return project_snapshot(read_standards(directory), TAKEN_AT)


def test_symlinks_to_one_conventions_file_put_its_text_once_under_the_first_name(
    tmp_path: Path,
) -> None:
    repository = conventions_repository(tmp_path / "dictum")

    standards = read_standards(repository)
    snapshot = project_snapshot(standards, TAKEN_AT)
    project = read_project(snapshot)

    assert list(standards.texts) == ["STACK.md"]
    assert standards.same_as == {"PRINCIPLES.md": "STACK.md", "TESTING.md": "STACK.md"}
    assert snapshot.count("<standard ") == 1
    assert (project.stack, project.principles, project.testing) == (True, True, True)
    conventions = CONVENTIONS.read_text(encoding="utf-8").rstrip()
    assert project.texts == {
        "STACK.md": conventions,
        "PRINCIPLES.md": conventions,
        "TESTING.md": conventions,
    }


def test_the_frontend_fixture_is_read_with_its_files_and_without_principles() -> None:
    project = read_project(snapshot_of(FRONTEND))

    assert (project.configured, project.source) == (True, "project_frontend")
    assert (project.stack, project.principles, project.testing) == (True, False, True)
    assert "React Hook Form" in project.texts["STACK.md"]
    assert project.taken_at == TAKEN_AT


def test_a_root_without_standards_finds_them_in_agents_context(tmp_path: Path) -> None:
    write(tmp_path / ".agents" / "context", "STACK.md")
    write(tmp_path / "docs", "STACK.md")

    assert find_standards_dir(tmp_path) == tmp_path / ".agents" / "context"


def test_standards_in_the_root_win_over_the_ones_in_docs(tmp_path: Path) -> None:
    write(tmp_path, "TESTING.md")
    write(tmp_path / "docs", "STACK.md")

    project = read_project(snapshot_of(tmp_path))

    assert (project.stack, project.testing) == (False, True)


def test_a_directory_with_stack_but_without_principles_is_still_chosen(tmp_path: Path) -> None:
    """Загрузчик Rigorous такой каталог не выбрал бы, а боту STACK.md важнее PRINCIPLES.md."""
    write(tmp_path, "STACK.md")

    assert read_project(snapshot_of(tmp_path)).stack is True


@pytest.mark.parametrize("name", ["TECH_STACK.md", "stack.md", "tech_stack.md"])
def test_aliases_and_case_variants_count_as_stack(tmp_path: Path, name: str) -> None:
    write(tmp_path, name)

    assert read_project(snapshot_of(tmp_path)).stack is True


@pytest.mark.parametrize(
    "text",
    ["x" * 80, "[TODO] " + "x" * 293, "<!-- " + "x" * 200 + " -->\nshort", "[todo] " + "x" * 293],
)
def test_a_placeholder_does_not_count_as_a_standard(tmp_path: Path, text: str) -> None:
    write(tmp_path, "STACK.md", text)

    project = read_project(snapshot_of(tmp_path))

    assert (project.configured, project.stack, project.texts) == (True, False, {})


def test_a_placeholder_under_the_first_name_is_not_replaced_by_an_alias(tmp_path: Path) -> None:
    """Так же делает загрузчик Rigorous: первое найденное имя берётся, даже если это заглушка."""
    write(tmp_path, "STACK.md", "[TODO]")
    write(tmp_path, "TECH_STACK.md")

    assert read_project(snapshot_of(tmp_path)).stack is False


def test_a_directory_without_standards_is_configured_and_empty(tmp_path: Path) -> None:
    write(tmp_path, "README.md")

    project = read_project(snapshot_of(tmp_path))

    assert project.configured is True
    assert (project.stack, project.principles, project.testing) == (False, False, False)
    assert project.texts == {}


def test_an_unset_setting_gives_an_unconfigured_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "project_context_dir", "  ")

    assert configured_directory() is None
    project = read_project(project_snapshot(None, TAKEN_AT))
    assert (project.configured, project.source, project.stack, project.texts) == (
        False,
        None,
        False,
        {},
    )


def test_the_setting_expands_the_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "project_context_dir", "~/work/shop")

    assert configured_directory() == Path.home() / "work" / "shop"


def test_a_missing_directory_is_named_by_its_last_part_only(tmp_path: Path) -> None:
    missing = tmp_path / "gone" / "shop-frontend"

    with pytest.raises(ProjectContextMissing) as raised:
        read_standards(missing)

    assert "shop-frontend" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_standards_over_the_limit_are_refused_instead_of_cut(tmp_path: Path) -> None:
    write(tmp_path, "STACK.md", "x" * (MAX_STANDARDS_CHARACTERS - 99))
    write(tmp_path, "TESTING.md", "y" * 100)

    with pytest.raises(ProjectContextTooLarge):
        read_standards(tmp_path)


def test_standards_at_the_limit_are_taken(tmp_path: Path) -> None:
    write(tmp_path, "STACK.md", "x" * (MAX_STANDARDS_CHARACTERS - 100))
    write(tmp_path, "TESTING.md", "y" * 100)

    assert read_project(snapshot_of(tmp_path)).testing is True


def test_the_snapshot_holds_the_last_directory_name_and_never_the_full_path(
    tmp_path: Path,
) -> None:
    repository = conventions_repository(tmp_path / "work" / "shop-frontend")

    snapshot = snapshot_of(repository)

    assert read_project(snapshot).source == "shop-frontend"
    assert str(tmp_path) not in snapshot
    assert "/work/" not in snapshot


def test_a_directory_named_with_digits_stays_a_name(tmp_path: Path) -> None:
    write(tmp_path / "2026", "STACK.md")

    assert read_project(snapshot_of(tmp_path / "2026")).source == "2026"


def test_the_answers_stage_takes_a_fresh_snapshot_of_changed_standards(tmp_path: Path) -> None:
    write(tmp_path, "STACK.md")
    previous = snapshot_of(tmp_path)
    write(tmp_path, "TESTING.md")

    snapshot, kept = refreshed_snapshot(previous, read_standards(tmp_path), LATER)

    assert kept is None
    project = read_project(snapshot)
    assert (project.testing, project.taken_at) == (True, LATER)


@pytest.mark.parametrize(
    ("fresh", "reason"),
    [
        (ProjectContextMissing("shop-frontend"), "missing"),
        (ProjectContextTooLarge(), "too_large"),
    ],
)
def test_the_answers_stage_keeps_the_previous_snapshot_when_standards_cannot_be_read(
    tmp_path: Path, fresh: ProjectContextMissing | ProjectContextTooLarge, reason: str
) -> None:
    write(tmp_path, "STACK.md")
    previous = snapshot_of(tmp_path)

    assert refreshed_snapshot(previous, fresh, LATER) == (previous, reason)


def test_the_answers_stage_keeps_full_standards_when_the_directory_went_empty(
    tmp_path: Path,
) -> None:
    stack = write(tmp_path, "STACK.md")
    previous = snapshot_of(tmp_path)
    stack.unlink()

    assert refreshed_snapshot(previous, read_standards(tmp_path), LATER) == (previous, "empty")


def test_the_answers_stage_follows_a_cleared_setting(tmp_path: Path) -> None:
    write(tmp_path, "STACK.md")
    previous = snapshot_of(tmp_path)

    snapshot, kept = refreshed_snapshot(previous, None, LATER)

    assert kept is None
    assert read_project(snapshot).configured is False
