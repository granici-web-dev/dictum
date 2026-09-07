import json
from pathlib import Path

from app.models import IssuesFile
from app.stages import STAGES, load_prompt


def test_fixture_issues_valid() -> None:
    data = json.loads(Path("fixtures/issues_sample.json").read_text(encoding="utf-8"))
    f = IssuesFile.model_validate(data)
    assert f.issues[0].id == "I-001"


def test_all_stage_prompts_exist() -> None:
    for s in STAGES:
        assert "description:" in load_prompt(s)
