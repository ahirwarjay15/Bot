from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from app.handlers.anime import AnimeStates, receive_anime_message


class FakeState:
    def __init__(self, current=None):
        self.current = current

    async def get_state(self):
        return self.current


def make_message(*, text=None, chat_type="private", from_user=True):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42) if from_user else None,
        chat=SimpleNamespace(type=chat_type),
        bot=SimpleNamespace(id=1),
        text=text,
    )


@pytest.mark.asyncio
async def test_anime_receiver_passes_unrelated_messages_to_later_routers(monkeypatch):
    state = FakeState()
    monkeypatch.setattr("app.handlers.anime._user_state", lambda *_: state)
    monkeypatch.setattr("app.handlers.anime._receive_locks", {})

    with pytest.raises(SkipHandler):
        await receive_anime_message(
            make_message(text="/settings"), object(), object()
        )


@pytest.mark.asyncio
async def test_anime_receiver_passes_commands_during_collection_to_command_routers(monkeypatch):
    state = FakeState(AnimeStates.collecting.state)
    monkeypatch.setattr("app.handlers.anime._user_state", lambda *_: state)
    monkeypatch.setattr("app.handlers.anime._receive_locks", {})

    with pytest.raises(SkipHandler):
        await receive_anime_message(
            make_message(text="/cancel"), object(), object()
        )


@pytest.mark.asyncio
async def test_anime_receiver_passes_non_private_messages_to_other_routers():
    with pytest.raises(SkipHandler):
        await receive_anime_message(
            make_message(text="hello", chat_type="group"), object(), object()
        )
