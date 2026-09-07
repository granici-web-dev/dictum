import json
import subprocess
import sys
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
    problems = check_issues(BROKEN_ISSUES)

    assert len(problems) == 1
    assert problems[0].startswith("I-007.title:")


def test_text_that_is_not_json_is_reported() -> None:
    problems = check_issues("вот ваши issues, но без json")

    assert len(problems) == 1
    assert problems[0].startswith("не разбирается как JSON")


def test_a_duplicate_identifier_is_reported() -> None:
    data = real_issues()
    data["issues"][1]["id"] = data["issues"][0]["id"]

    assert problems_of(data) == ["идентификатор I-001 встречается несколько раз"]


def test_a_deferred_scope_without_a_title_is_reported() -> None:
    data = real_issues()
    del data["deferred"][0]["title"]

    problems = check_issues(json.dumps(data, ensure_ascii=False))

    assert any("title" in problem for problem in problems)


def test_a_scope_id_shared_by_two_deferred_scopes_is_reported() -> None:
    data = real_issues()
    data["deferred"][1]["scope_id"] = data["deferred"][0]["scope_id"]

    problems = check_issues(json.dumps(data, ensure_ascii=False))

    assert any("встречается несколько раз" in problem for problem in problems)


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


def test_a_phase_that_is_not_declared_is_reported() -> None:
    data = real_issues()
    data["issues"][3]["phase"] = 9

    assert "I-004 стоит в фазе 9, которой нет в phases" in problems_of(data)


def test_every_problem_is_reported_not_only_the_first() -> None:
    data = real_issues()
    data["issues"][2]["depends_on"] = ["I-404"]
    first, last = data["issues"][0], data["issues"][-1]
    first["depends_on"] = [last["id"]]

    problems = problems_of(data)

    assert any("I-404" in problem for problem in problems)
    assert any("более поздней фазы" in problem for problem in problems)


def test_a_cycle_away_from_the_first_issue_is_reported() -> None:
    data = real_issues()
    data["issues"][-1]["depends_on"] = [data["issues"][-2]["id"]]
    data["issues"][-2]["depends_on"] = [data["issues"][-1]["id"]]

    assert any("цикл в зависимостях" in problem for problem in problems_of(data))


def test_the_module_exits_nonzero_when_run_as_a_command(tmp_path: Path) -> None:
    broken = tmp_path / "issues.json"
    broken.write_text('{"source": "x"}', encoding="utf-8")

    finished = subprocess.run(
        [sys.executable, "-m", "app.validate", str(broken)],
        cwd=FIXTURES.parent,
        capture_output=True,
        text=True,
    )

    assert finished.returncode == 1
    assert "проблем" in finished.stderr
