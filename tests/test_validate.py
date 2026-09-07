import json
from pathlib import Path
from typing import Any

from app.validate import check_issues, main
from tests.helpers import BROKEN_ISSUES, FIXTURES, REAL_ISSUES


def real_issues() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(REAL_ISSUES)
    return data


def problems_of(data: dict[str, Any]) -> list[str]:
    return check_issues(json.dumps(data, ensure_ascii=False))


def test_the_real_answer_has_no_problems() -> None:
    assert check_issues(REAL_ISSUES) == []


def test_a_title_over_the_limit_is_reported() -> None:
    assert check_issues(BROKEN_ISSUES) == [
        "issues.6.title: String should have at most 60 characters"
    ]


def test_text_that_is_not_json_is_reported() -> None:
    problems = check_issues("вот ваши issues, но без json")

    assert len(problems) == 1
    assert problems[0].startswith("не разбирается как JSON")


def test_a_duplicate_identifier_is_reported() -> None:
    data = real_issues()
    data["issues"][1]["id"] = data["issues"][0]["id"]

    assert "идентификатор I-001 встречается 2 раза" in problems_of(data)


def test_a_dependency_on_a_missing_issue_is_reported() -> None:
    data = real_issues()
    data["issues"][2]["depends_on"] = ["I-404"]

    assert "I-003 зависит от I-404, которого нет" in problems_of(data)


def test_a_dependency_on_a_later_phase_is_reported() -> None:
    data = real_issues()
    first, last = data["issues"][0], data["issues"][-1]
    first["depends_on"] = [last["id"]]

    assert (
        f"{first['id']} из фазы {first['phase']} зависит от {last['id']} "
        f"из более поздней фазы {last['phase']}"
    ) in problems_of(data)


def test_a_cycle_is_reported() -> None:
    data = real_issues()
    data["issues"][0]["depends_on"] = ["I-002"]
    data["issues"][1]["depends_on"] = ["I-001"]

    assert "цикл в зависимостях: I-001 → I-002 → I-001" in problems_of(data)


def test_an_issue_depending_on_itself_is_reported() -> None:
    data = real_issues()
    data["issues"][0]["depends_on"] = ["I-001"]

    assert "цикл в зависимостях: I-001 → I-001" in problems_of(data)


def test_the_command_exits_nonzero_on_a_broken_file(tmp_path: Path) -> None:
    broken = tmp_path / "issues.json"
    broken.write_text('{"source": "x"}', encoding="utf-8")

    assert main(str(broken)) == 1
    assert main(str(FIXTURES / "issues_real.json")) == 0
