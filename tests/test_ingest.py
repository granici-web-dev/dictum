import frontmatter
import pytest

from app.ingest import build_transcript, child_transcript, run_id_of


def test_a_transcript_gives_back_the_run_id_it_was_built_with() -> None:
    assert run_id_of(build_transcript("Идея", "ru", "a1b2c3d4e5f60718", "text", None, None)) == (
        "a1b2c3d4e5f60718"
    )


def test_a_run_id_of_nothing_but_digits_is_still_a_run_id() -> None:
    """Примерно один hex-идентификатор из тысячи восьмисот — одни цифры, и YAML читал его числом.

    `run_id_of` возвращал тогда None, и `--from` отказывался продолжать прогон словами
    «транскрипт старше этого правила», хотя id стоял в файле.
    """
    assert run_id_of(build_transcript("Идея", "ru", "9139399168808917", "text", None, None)) == (
        "9139399168808917"
    )


def test_a_transcript_without_a_run_id_says_so() -> None:
    assert run_id_of("---\nsource: text\n---\n\nИдея\n") is None


def test_a_child_transcript_is_the_recording_of_its_parent_under_its_own_run_id() -> None:
    parent = build_transcript("Kannst du das prüfen?", "de", "3f9c1a7e5b2d8c40", "file", 312, True)

    child = child_transcript(parent, "9139399168808917", "3f9c1a7e5b2d8c40")

    assert 'run_id: "9139399168808917"' in child
    assert 'parent_run_id: "3f9c1a7e5b2d8c40"' in child
    assert run_id_of(child) == "9139399168808917"
    written, heard = frontmatter.loads(child), frontmatter.loads(parent)
    assert written.content == heard.content
    assert {
        name: value for name, value in written.metadata.items() if "run_id" not in name
    } == {name: value for name, value in heard.metadata.items() if name != "run_id"}


def test_a_transcript_of_another_run_is_not_handed_to_the_child() -> None:
    parent = build_transcript("Идея", "ru", "a1b2c3d4e5f60718", "voice", 12, None)

    with pytest.raises(ValueError, match="0000000000000000"):
        child_transcript(parent, "ребёнок", "0000000000000000")
