import json
from pathlib import Path

from app.models import IssuesFile


def test_fixture_issues_valid() -> None:
    data = json.loads(Path("fixtures/issues_sample.json").read_text(encoding="utf-8"))
    f = IssuesFile.model_validate(data)
    assert f.issues[0].id == "I-001"
