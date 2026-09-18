import pytest

from app.pipeline import (
    NAMES,
    PUBLISHING,
    ROUTES,
    STAGES,
    after,
    route_end,
    route_of,
    stage_named,
    stages_between,
)
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
    assert tuple(stage.name for stage in stages_between("ingest", "card")) == NAMES


def test_an_unknown_stage_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown stage: publush"):
        stage_named("publush")


def test_the_stage_after_a_gate_is_the_one_the_run_continues_from() -> None:
    """«Дальше» на воротах ведёт за подтверждённую стадию, а не в неё."""
    assert after("brief").name == "research"
    assert after("decompose").name == "publish"


def test_there_is_nothing_after_the_last_stage() -> None:
    """Ворот после card не бывает: вопрос может задать только ошибка в данных."""
    with pytest.raises(ValueError, match="после стадии card"):
        after("card")


def test_an_assignment_goes_from_the_review_to_its_card() -> None:
    """Шаги подтверждают на воротах, и «Дальше» ведёт к карточке, а не обратно в шаги."""
    assert route_of("steps") == ("assignment", "card")
    assert after("steps").name == "card"
    assert stage_named("steps").gate_after == "outputs/steps.md"


def test_an_assignment_asks_the_teamlead_before_the_steps() -> None:
    """Ожидание ответа тимлида длится днями и начинается сразу после нажатия."""
    route = [stage.name for stage in stages_between(*route_of("assignment"))]

    assert route == ["assignment", "clarify", "answers", "approach", "steps", "card"]
    assert stage_named("clarify").runs == "llm"
    assert stage_named("answers").runs == "code"
    assert set(stage_named("steps").inputs) == {
        "inputs/assignment.json",
        "outputs/clarify.json",
        "inputs/answers.md",
        "outputs/approach.json",
        "inputs/project.md",
    }


def test_a_stage_that_searches_reads_the_standards_snapshot() -> None:
    """Предел поисков код берёт по флагу `stack` снимка: без снимка выбирать его нечем (SPEC §7)."""
    searching = [stage for stage in STAGES if stage.web_search is not None]

    assert [stage.name for stage in searching] == ["approach"]
    for stage in searching:
        assert "inputs/project.md" in stage.inputs


def test_only_a_stage_with_one_output_can_be_skipped() -> None:
    """Пропуск кладёт один файл вместо вызова модели, и путь у него объявлен в самой записи."""
    for stage in STAGES:
        if stage.skipped_output is None:
            continue
        path, _ = stage.skipped_output
        assert stage.outputs == (frozenset({path}),)


def test_the_stages_that_write_to_the_board_end_their_routes() -> None:
    """Локальный прогон останавливается перед ними, и опирается на данные, а не на имя."""
    assert PUBLISHING <= {last for _, last in ROUTES}


def test_every_stage_belongs_to_exactly_one_route() -> None:
    """Маршруты режут список без дыр и нахлёстов: иначе обход не знал бы, где ему кончаться."""
    covered = [stage.name for first, last in ROUTES for stage in stages_between(first, last)]
    assert covered == list(NAMES)


def test_the_review_comes_right_after_the_transcript() -> None:
    assert NAMES[:2] == ("ingest", "review")


def test_the_idea_starts_from_the_transcript_of_the_review_and_ends_on_the_board() -> None:
    """Путь идеи берёт расшифровку у разбора: второй маршрут не делит с первым ни одной стадии."""
    assert route_of("handoff") == route_of("intake") == ("handoff", "publish")
    assert route_of("ingest") == ("ingest", "review")
    assert stage_named("handoff").runs == "code"


def test_a_recording_ends_with_its_review_and_an_assignment_with_its_cards() -> None:
    assert route_end("ingest") == "review"
    assert route_end("brief") == "publish"


def test_a_walk_from_any_stage_ends_where_its_route_ends() -> None:
    for first, last in ROUTES:
        for stage in stages_between(first, last):
            assert route_end(stage.name) == last
