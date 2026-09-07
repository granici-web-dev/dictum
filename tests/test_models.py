import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.models import IssuesFile


def test_the_real_answer_satisfies_the_contract() -> None:
    f = IssuesFile.model_validate(sample())

    assert [i.id for i in f.issues] == [f"I-{n:03}" for n in range(1, len(f.issues) + 1)]
    assert {d for i in f.issues for d in i.depends_on} <= {i.id for i in f.issues}
    assert all(i.test_hint.strip() for i in f.issues)


def test_the_saved_invalid_answer_is_still_invalid() -> None:
    data = json.loads(Path("fixtures/issues_title_too_long.json").read_text(encoding="utf-8"))

    with pytest.raises(ValidationError, match="title"):
        IssuesFile.model_validate(data)


def sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        Path("fixtures/issues_real.json").read_text(encoding="utf-8")
    )
    return data


@pytest.mark.parametrize("field", ["estimate", "test_hint"])
def test_an_issue_without_the_field_is_rejected(field: str) -> None:
    data = sample()
    del data["issues"][0][field]

    with pytest.raises(ValidationError, match=field):
        IssuesFile.model_validate(data)
