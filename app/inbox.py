"""Папка входящих записей: отбор готовых файлов и память о том, про что уже спрашивали.

Вход без терминала (SPEC §3.1): владелец кладёт запись в папку, бот замечает её и спрашивает
про согласие в чате. Здесь только то, что делается до вопроса и мимо Telegram, — обход папки,
журнал `runs/inbox.json` (SPEC §4) и перенос разобранной записи в `done/`. Сам вопрос, кнопки и
прогон живут в `app/bot.py`, и обратной связи с ним у этого модуля нет.
"""

import json
import logging
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.transcribe import TranscriptionError, recording_seconds

logger = logging.getLogger(__name__)

DONE = "done"

# Незаконченная загрузка называет себя сама, и читать её нечего.
HALF_COPIED = (".crdownload", ".download", ".part", ".tmp")

# Отпечатка хватает 24 знаков: он уходит в callback_data кнопки, где всего 64 байта.
FINGERPRINT_LENGTH = 24

Decision = Literal["asked", "yes", "no", "refused", "not_a_recording", "postponed"]

# `postponed` — не решение, а память о том, что про отложенный вопрос человеку уже сказали
# (`SPEC §7.3`). Файл с такой записью обход предлагает дальше и спросит, как только чат
# освободится; закрывают файл остальные пять.
POSTPONED: Decision = "postponed"

FOLDER_MISSING = "Папки входящих {folder} нет. Создайте её или поправьте MEETING_INBOX_DIR в .env."

SEVERAL_CHATS = (
    "За папкой входящих не слежу: разрешённых чатов несколько, и в какой слать разбор — не "
    "угадать. Запись отдавайте командой make meeting FILE=… CHAT=<id>."
)

NOT_A_RECORDING = (
    "Файл {name} в папке входящих я не прочитал как запись и трогать его больше не буду."
)

STALE_BUTTON = "Записи {name} в папке входящих больше нет."

QUESTION_POSTPONED = (
    "Запись {name} вижу. Сейчас жду решения по прогону {run_id} и спрошу про согласие, как "
    "только он решится."
)


class Observation(BaseModel):
    """Чем файл был на прошлом обходе. Растущий файл двух одинаковых наблюдений не даёт."""

    size: int
    mtime_ns: int


class Ready(BaseModel):
    """Файл, который не менялся с прошлого обхода и журналу ещё не известен.

    `seconds is None` — третье сито: `ffprobe` не назвал длительность, и записью это не является.
    """

    path: Path
    fingerprint: str
    observed: Observation
    seconds: int | None


class Entry(BaseModel):
    name: str
    size: int
    mtime_ns: int
    # Длительность, которую назвал `ffprobe` на обходе. Она же идёт в строку лога о начале
    # прогона, а файл под нажатой кнопкой тот же самый: спрашивать её второй раз незачем.
    seconds: int | None = None
    decision: Decision
    run_id: str | None = None


def fingerprint(path: Path, observed: Observation) -> str:
    """Имя, размер и время правки: одного имени мало.

    Диктофон пишет `REC001.m4a`, и после чистки карты следующая встреча получает то же имя. По
    имени бот счёл бы её уже обработанной, и созвон пропал бы без единого сообщения. Содержимое
    как ключ отвергнуто: его пришлось бы прочитать целиком до согласия.
    """
    key = f"{path.name}|{observed.size}|{observed.mtime_ns}"
    return sha256(key.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def observed_now(path: Path) -> Observation:
    stat = path.stat()
    return Observation(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def ready_recordings(
    folder: Path, seen: dict[str, Observation], known: set[str]
) -> tuple[list[Ready], dict[str, Observation]]:
    """Что в папке готово к вопросу. Ничего не пишет: решение принимает тот, кто спрашивает.

    Три сита подряд, каждое дешевле следующего: имя, неизменившиеся размер и время правки,
    прочитанная `ffprobe` длительность. Второе возвращается вторым значением — наблюдения этого
    обхода, вход следующего.
    """
    ready: list[Ready] = []
    observations: dict[str, Observation] = {}
    for path in sorted(folder.iterdir()):
        if path.name.startswith(".") or path.name.endswith(HALF_COPIED) or not path.is_file():
            continue
        observed = observed_now(path)
        observations[path.name] = observed
        if seen.get(path.name) != observed:
            continue
        mark = fingerprint(path, observed)
        if mark in known:
            continue
        ready.append(
            Ready(path=path, fingerprint=mark, observed=observed, seconds=length_of(path))
        )
    return ready, observations


def length_of(recording: Path) -> int | None:
    try:
        return recording_seconds(recording)
    except TranscriptionError:
        return None


class Journal:
    """Память о файлах, про которые бот уже спрашивал: `runs/inbox.json` (SPEC §4).

    Журнал файлом, а не строкой в базе: строки `runs` до согласия нет и быть не должно
    (`CLAUDE.md` §8), а таблица ради памяти о файлах, про которые человек сказал «Нет», была бы
    пустой таблицей. Потеря журнала теряет только память о заданных вопросах.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = read_entries(path)

    def closed(self) -> set[str]:
        return {mark for mark, entry in self.entries.items() if entry.decision != POSTPONED}

    def told_about(self, mark: str) -> bool:
        return mark in self.entries

    def decided(self, ready: Ready, decision: Decision, run_id: str | None = None) -> None:
        self.entries[ready.fingerprint] = Entry(
            name=ready.path.name,
            size=ready.observed.size,
            mtime_ns=ready.observed.mtime_ns,
            seconds=ready.seconds,
            decision=decision,
            run_id=run_id,
        )
        self.write()

    def answered(self, mark: str, decision: Decision, run_id: str | None = None) -> None:
        """Ответ на висевший вопрос: файл тот же, и заново его не описывают."""
        known = self.entries[mark]
        self.entries[mark] = known.model_copy(update={"decision": decision, "run_id": run_id})
        self.write()

    def write(self) -> None:
        """Заменой временного файла: убитый посреди записи процесс не оставляет половины JSON."""
        payload = {mark: entry.model_dump() for mark, entry in self.entries.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)


def read_entries(path: Path) -> dict[str, Entry]:
    """Нечитаемый журнал — пустая память, а не упавший старт: бот переспросит про лежащее."""
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        return {mark: Entry.model_validate(entry) for mark, entry in stored.items()}
    except (OSError, ValueError, ValidationError) as error:
        logger.error("журнал папки входящих %s не прочитан (%s), спрошу заново", path, error)
        return {}


def moved_to_done(recording: Path) -> Path:
    """Разобранная запись уезжает в `done/`, и лежащий там файл она не заменяет.

    Диктофон даёт имена по кругу: `os.rename` поверх прошлой записи с тем же именем — это
    удаление исходника, которого правило 9 не допускает ни при каком исходе.
    """
    done = recording.parent / DONE
    done.mkdir(parents=True, exist_ok=True)
    target = free_name(done, recording.name)
    shutil.move(str(recording), target)
    return target


def free_name(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 2
    while (folder / f"{stem} ({number}){suffix}").exists():
        number += 1
    return folder / f"{stem} ({number}){suffix}"
