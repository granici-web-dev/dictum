import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.models import IssuesFile


def test_fixture_issues_valid() -> None:
    data = json.loads(Path("fixtures/issues_sample.json").read_text(encoding="utf-8"))
    f = IssuesFile.model_validate(data)
    assert f.issues[0].id == "I-001"


def sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        Path("fixtures/issues_sample.json").read_text(encoding="utf-8")
    )
    return data


@pytest.mark.parametrize("field", ["estimate", "test_hint"])
def test_an_issue_without_the_field_is_rejected(field: str) -> None:
    data = sample()
    del data["issues"][0][field]

    with pytest.raises(ValidationError, match=field):
        IssuesFile.model_validate(data)
