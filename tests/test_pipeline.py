import pytest

from app.pipeline import NAMES, STAGES, after, stage_named, stages_between
from app.run import BODIES
from app.stages import COMMANDS_DIR


def test_every_llm_stage_has_a_prompt_to_run() -> None:
    for stage in STAGES:
        if stage.runs == "llm":
            assert (COMMANDS_DIR / f"{stage.name}.md").is_file(), stage.name


def test_every_code_stage_has_a_body_to_run() -> None:
    for stage in STAGES:
        if stage.runs == "code":
            assert stage.name in BODIES, stage.name


def test_every_input_is_written_by_an_earlier_stage() -> None:
    written: set[str] = set()
    for stage in STAGES:
        for path in stage.inputs:
            assert path in written, f"{stage.name} читает {path}, которого ещё нет"
        for outputs in stage.outputs:
            written |= outputs



def test_a_walk_covers_the_stages_it_was_asked_for_and_no_others() -> None:
    walked = tuple(stage.name for stage in stages_between("prd", "decompose"))
    assert walked == ("prd", "decompose")
    assert tuple(stage.name for stage in stages_between("ingest", "publish")) == NAMES


def test_an_unknown_stage_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown stage: publush"):
        stage_named("publush")


def test_the_stage_after_a_gate_is_the_one_the_run_continues_from() -> None:
    """«Дальше» на воротах ведёт за подтверждённую стадию, а не в неё."""
    assert after("brief").name == "research"
    assert after("decompose").name == "publish"


def test_there_is_nothing_after_the_last_stage() -> None:
    """Ворот после publish не бывает: вопрос может задать только ошибка в данных."""
    with pytest.raises(ValueError, match="после стадии publish"):
        after("publish")
