from typing import Any

import httpx
import pytest
import respx

from app.config import LiveApiNotAllowed, MissingApiKey, settings
from app.trello import (
    RETRY_PAUSE_SECONDS,
    Trello,
    TrelloError,
    open_trello,
    pause_before_retry,
)


def card_body(**changes: Any) -> dict[str, Any]:
    return {
        "id": "card-1",
        "name": "Заголовок",
        "desc": "Текст",
        "url": "https://trello.com/c/card-1",
        "idList": "list-1",
        "idLabels": ["label-1"],
        **changes,
    }


@pytest.fixture
def client() -> Trello:
    return Trello(httpx.Client(), "test-key", "test-token", "board1")


def test_credentials_travel_in_the_header_and_never_in_the_url(
    client: Trello, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://api.trello.com/1/boards/board1/lists").mock(
        httpx.Response(200, json=[{"id": "list-1", "name": "Backlog"}])
    )
    respx_mock.post("https://api.trello.com/1/cards").mock(httpx.Response(200, json=card_body()))

    client.lists()
    client.create_card("list-1", "Заголовок", "Текст", ["label-1"], 1)

    assert respx_mock.calls
    for call in respx_mock.calls:
        assert call.request.headers["Authorization"] == (
            'OAuth oauth_consumer_key="test-key", oauth_token="test-token"'
        )
        assert "test-key" not in str(call.request.url)
        assert "test-token" not in str(call.request.url)


def test_card_fields_go_in_the_body_not_the_url(
    client: Trello, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("https://api.trello.com/1/cards").mock(
        httpx.Response(200, json=card_body())
    )

    client.create_card("list-1", "Заголовок", "Текст", ["label-1", "label-2"], 1)

    body = route.calls.last.request.content.decode()
    assert "idLabels=label-1%2Clabel-2" in body
    assert "desc=" not in str(route.calls.last.request.url)


def test_reading_cards_asks_for_checklists_attachments_and_archived_ones(
    client: Trello, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get("https://api.trello.com/1/boards/board1/cards").mock(
        httpx.Response(200, json=[card_body(checklists=[{"id": "checklist-1", "name": "DoD"}])])
    )

    cards = client.cards()

    asked = route.calls.last.request.url.params
    assert asked["filter"] == "all"
    assert asked["checklists"] == "all"
    assert asked["attachments"] == "true"
    assert [item.name for item in cards[0].checklists] == ["DoD"]


def test_a_rate_limited_request_is_repeated(client: Trello, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("https://api.trello.com/1/cards").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}, json={"message": "rate limit"}),
            httpx.Response(200, json=card_body()),
        ]
    )

    card = client.create_card("list-1", "Заголовок", "Текст", ["label-1"], 1)

    assert card.id == "card-1"
    assert len(route.calls) == 2


def test_a_failing_write_is_not_repeated(client: Trello, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("https://api.trello.com/1/cards").mock(
        httpx.Response(500, json={"message": "Trello is down"})
    )

    with pytest.raises(TrelloError):
        client.create_card("list-1", "Заголовок", "Текст", ["label-1"], 1)

    assert len(route.calls) == 1


def test_a_failing_read_is_repeated(client: Trello, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get("https://api.trello.com/1/boards/board1/lists").mock(
        side_effect=[
            httpx.Response(503, headers={"retry-after": "0"}, json={"message": "unavailable"}),
            httpx.Response(200, json=[{"id": "list-1", "name": "Backlog"}]),
        ]
    )

    assert [item.name for item in client.lists()] == ["Backlog"]
    assert len(route.calls) == 2


def test_a_checklist_arrives_with_the_items_it_already_has(
    client: Trello, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://api.trello.com/1/boards/board1/cards").mock(
        httpx.Response(
            200,
            json=[
                card_body(
                    checklists=[
                        {
                            "id": "checklist-1",
                            "name": "DoD",
                            "checkItems": [{"id": "item-1", "name": "Первый пункт"}],
                        }
                    ]
                )
            ],
        )
    )

    checklist = client.cards()[0].checklists[0]

    assert [item.name for item in checklist.check_items] == ["Первый пункт"]


def test_an_error_names_the_call_without_the_credentials(
    client: Trello, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://api.trello.com/1/boards/board1/lists").mock(
        httpx.Response(401, text="invalid token")
    )

    with pytest.raises(TrelloError) as refused:
        client.lists()

    message = str(refused.value)
    assert "test-key" not in message and "test-token" not in message
    assert "401" in message and "/boards/board1/lists" in message
    assert "invalid token" in message
    assert "TRELLO_KEY" in message


def test_open_trello_refuses_without_allow_live_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_live_api", False)
    with pytest.raises(LiveApiNotAllowed, match="ALLOW_LIVE_API is not true"):
        open_trello()


def test_open_trello_names_every_missing_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_live_api", True)
    monkeypatch.setattr(settings, "trello_key", "test-key")
    monkeypatch.setattr(settings, "trello_token", "")
    monkeypatch.setattr(settings, "trello_board_id", "")
    with pytest.raises(MissingApiKey, match="TRELLO_TOKEN, TRELLO_BOARD_ID"):
        open_trello()


def test_the_pause_follows_retry_after_and_otherwise_doubles() -> None:
    asked = httpx.Response(429, headers={"retry-after": "7"})
    silent = httpx.Response(429)

    assert pause_before_retry(asked, 0) == 7.0
    assert pause_before_retry(silent, 0) == RETRY_PAUSE_SECONDS
    assert pause_before_retry(silent, 1) == RETRY_PAUSE_SECONDS * 2
