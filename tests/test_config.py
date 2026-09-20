"""Настройки, которые обязаны отказать на старте, а не посреди оплаченного прогона."""

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from app.config import LEAST_USEFUL_MAX_TOKENS, MODEL_MAX_TOKENS, Settings


class WithoutEnvFile(Settings):
    """Те же настройки, но мимо `.env`: умолчание спрашивают у кода, а не у чужой машины."""

    model_config = SettingsConfigDict(env_file=None)


def test_the_answer_ceiling_is_half_the_model_by_default() -> None:
    """Умолчание держит разбор двухчасовой встречи: строка в .env для этого больше не нужна."""
    assert WithoutEnvFile().anthropic_max_tokens == 64000
    assert WithoutEnvFile().anthropic_max_tokens * 2 == MODEL_MAX_TOKENS


def test_the_answer_ceiling_above_the_model_limit_is_refused() -> None:
    """Иначе о промахе в .env узнают ответом API 400, когда расшифровка уже оплачена."""
    with pytest.raises(ValidationError, match=str(MODEL_MAX_TOKENS)):
        WithoutEnvFile(anthropic_max_tokens=200000)


def test_the_answer_ceiling_below_the_thinking_minimum_is_refused() -> None:
    """Такой потолок обрывает не длинную запись, а любую."""
    with pytest.raises(ValidationError, match=str(LEAST_USEFUL_MAX_TOKENS)):
        WithoutEnvFile(anthropic_max_tokens=500)
