import pytest

from app.pipeline import NAMES, STAGES, Stage, stage_named, stages_from
from app.stages import COMMANDS_DIR


def test_every_llm_stage_has_a_prompt_to_run() -> None:
    for stage in STAGES:
        if stage.runs == "llm":
            assert (COMMANDS_DIR / f"{stage.name}.md").is_file(), stage.name


def test_a_code_stage_carries_what_it_writes_and_asks_the_model_for_nothing() -> None:
    for stage in STAGES:
        if stage.runs == "code":
            assert stage.content is not None, stage.name
            assert len(stage.outputs) == 1, stage.name
            assert len(stage.outputs[0]) == 1, stage.name


def test_every_input_is_written_by_an_earlier_stage_or_arrives_before_the_first() -> None:
    written = {"inputs/transcript.md"}
    for stage in STAGES:
        for path in stage.inputs:
            assert path in written, f"{stage.name} читает {path}, которого ещё нет"
        for outputs in stage.outputs:
            written |= outputs


def test_the_order_is_the_pipeline_of_the_spec() -> None:
    assert NAMES == ("intake", "brief", "research", "prd", "decompose")


def test_a_walk_starts_at_the_stage_it_was_asked_for() -> None:
    assert tuple(stage.name for stage in stages_from("prd")) == ("prd", "decompose")
    assert tuple(stage.name for stage in stages_from("intake")) == NAMES


def test_an_unknown_stage_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown stage: publush"):
        stage_named("publush")


def test_a_stage_that_wants_no_parameters_carries_none() -> None:
    assert stage_named("decompose").params == {}
    assert stage_named("brief").params == {"mode": "batch", "interactive": "false"}


def test_two_stages_never_claim_the_same_name() -> None:
    assert len(set(NAMES)) == len(NAMES)


def test_the_record_refuses_an_executor_it_does_not_know() -> None:
    with pytest.raises(ValueError):
        Stage(name="ingest", runs="magic", outputs=(frozenset({"x"}),))  # type: ignore[arg-type]
