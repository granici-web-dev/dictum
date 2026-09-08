import pytest

from app.pipeline import NAMES, STAGES, Stage, stage_named, stages_from
from app.stages import COMMANDS_DIR


def test_every_llm_stage_has_a_prompt_to_run() -> None:
    for stage in STAGES:
        if stage.runs == "llm":
            assert (COMMANDS_DIR / f"{stage.name}.md").is_file(), stage.name


def test_a_code_stage_without_content_is_refused_when_the_list_is_built() -> None:
    with pytest.raises(ValueError, match="пишет один файл"):
        Stage(name="ingest", runs="code", outputs=(frozenset({"inputs/transcript.md"}),))


def test_a_code_stage_that_would_write_two_files_is_refused() -> None:
    with pytest.raises(ValueError, match="пишет один файл"):
        Stage(name="ingest", runs="code", outputs=(frozenset({"a", "b"}),), content="x")


def test_every_input_is_written_by_an_earlier_stage_or_arrives_before_the_first() -> None:
    written = {"inputs/transcript.md"}
    for stage in STAGES:
        for path in stage.inputs:
            assert path in written, f"{stage.name} читает {path}, которого ещё нет"
        for outputs in stage.outputs:
            written |= outputs



def test_a_walk_starts_at_the_stage_it_was_asked_for() -> None:
    assert tuple(stage.name for stage in stages_from("prd")) == ("prd", "decompose")
    assert tuple(stage.name for stage in stages_from("intake")) == NAMES


def test_an_unknown_stage_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown stage: publush"):
        stage_named("publush")



