from app.ingest import build_transcript, run_id_of


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
