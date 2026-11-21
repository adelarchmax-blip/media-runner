"""Telegram on the runner with a fake Telegram client: the keys leave the environment, messages are
described (video facts, links, linked videos), joining, downloading and stopping. No network."""
import asyncio
import importlib
import os
import sys
import types as pytypes
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from telethon.tl import types  # noqa: E402

from sites import telegram as TG  # noqa: E402
from test_sites import fake_api, go  # noqa: E402

WHEN = datetime(2026, 11, 21, tzinfo=timezone.utc)


def doc_msg(i, name="Film.2020.1080p.mkv", size=900 * 1048576, duration=5400, h=1080, mime="video/x-matroska",
            text="", grouped=None):
    return pytypes.SimpleNamespace(
        id=i, date=WHEN, grouped_id=grouped, message=text, photo=None, action=None, entities=None, reply_markup=None,
        document=object(), file=pytypes.SimpleNamespace(name=name, size=size, duration=duration, height=h, width=1920,
                                                         mime_type=mime, ext=".mkv"))


def text_msg(i, text, photo=False, entities=None, markup=None):
    m = types.Message(id=i, peer_id=types.PeerChannel(1), date=WHEN, message=text, entities=entities,
                      reply_markup=markup)
    if photo:
        m.media = types.MessageMediaPhoto(photo=types.Photo(id=1, access_hash=2, file_reference=b"", date=WHEN, sizes=[], dc_id=4))
    return m


class FakeClient:
    def __init__(self, messages, others=None):
        self.messages, self.others, self.calls = messages, others or {}, []

    async def get_entity(self, peer):
        self.calls.append(("entity", peer))
        if peer in ("closedchan",):
            raise ValueError("no such channel")
        if isinstance(peer, types.PeerChannel):
            return pytypes.SimpleNamespace(id=peer.channel_id, username=None, title="Private")
        return pytypes.SimpleNamespace(id=77 if peer == "mainchan" else 88, username=peer, title="Main")

    async def get_dialogs(self, limit=None):
        self.calls.append(("dialogs",))

    async def iter_messages(self, entity, min_id=0, reverse=False, limit=None):
        assert reverse
        for m in [m for m in self.messages if m.id > min_id][:limit]:
            yield m

    async def get_messages(self, entity, ids):
        return self.others.get((entity.id, ids))

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if type(request).__name__ == "GetFullChannelRequest":
            return pytypes.SimpleNamespace(full_chat=pytypes.SimpleNamespace(about="films"))
        if type(request).__name__ == "CheckChatInviteRequest":
            return pytypes.SimpleNamespace()        # an invite not joined yet: no chat in it
        if type(request).__name__ == "ImportChatInviteRequest":
            return pytypes.SimpleNamespace(chats=[pytypes.SimpleNamespace(id=555, title="Series", username=None)])
        return None


def fake(messages=(), others=None):
    t = TG.Telegram()
    t._client = FakeClient(list(messages), others)
    return t


def test_keys_leave_the_environment_on_load(monkeypatch):
    for k in TG.ENV:
        monkeypatch.setenv(k, "123" if k == "TG_API_ID" else "secret-value")
    mod = importlib.reload(TG)
    try:
        assert mod.configured() and not any(k in os.environ for k in TG.ENV)
    finally:
        for k in TG.ENV:
            monkeypatch.delenv(k, raising=False)
        importlib.reload(TG)
    assert not TG.configured()


def test_video_facts_and_links():
    assert TG.video_facts(doc_msg(4), "mainchan") == {"name": "Film.2020.1080p.mkv", "size": 900 * 1048576,
                                                      "duration": 5400, "h": 1080, "w": 1920, "peer": "mainchan", "msg": 4}
    assert TG.video_facts(doc_msg(5, name="a.pdf", mime="application/pdf"), "x") is None
    assert TG.video_facts(doc_msg(6, name="a.MKV", mime="application/octet-stream"), "x")["msg"] == 6
    assert TG.video_facts(text_msg(7, "hi"), "x") is None
    text = "watch here https://t.me/other/12 and the series"
    m = text_msg(8, text, entities=[types.MessageEntityUrl(offset=11, length=21),
                                    types.MessageEntityTextUrl(offset=37, length=6, url="https://t.me/+AbCdEfGh12")],
                 markup=types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(
                     buttons=[types.KeyboardButton(text="go", type=types.InlineButtonTypeUrl(url="https://t.me/joinchat/ZZZZzzzz99"))])]))
    assert TG.links_of(m) == ["https://t.me/other/12", "https://t.me/+AbCdEfGh12", "https://t.me/joinchat/ZZZZzzzz99"]
    assert TG.peer_of(pytypes.SimpleNamespace(username="Abc_d", id=1)) == "Abc_d"
    assert TG.peer_of(pytypes.SimpleNamespace(username=None, id=99)) == 99
    found = [(m.group(2) or m.group(3), m.group(4)) for m in TG.MSG_LINK.finditer(
        "t.me/c/1234567/89 https://t.me/name_x/5 t.me/name_x t.me/s/name t.me/name_x/5/extra")]
    assert found == [("1234567", "89"), ("name_x", "5")]


def test_scan_describes_messages_opens_linked_videos_and_says_more():
    msgs = [text_msg(1, "🎞 الفيلم X 2020 https://t.me/other/12", photo=True,
                     entities=[types.MessageEntityUrl(offset=17, length=21)]),
            doc_msg(2, text="", grouped=None), doc_msg(3, grouped=9), doc_msg(4, grouped=9),
            types.MessageService(id=5, peer_id=types.PeerChannel(1), date=WHEN, action=types.MessageActionPinMessage()),
            text_msg(6, "تاني t.me/c/5551234/3 t.me/closedchan/4"), text_msg(7, "last")]
    t = fake(msgs, others={(88, 12): doc_msg(12), (5551234, 3): doc_msg(3, name="Ep.E03.mp4")})
    out = asyncio.run(t._scan({"channel": "mainchan", "since_id": 0, "limit": 6, "about": True}))
    assert [m["id"] for m in out["messages"]] == [1, 2, 3, 4, 6] and out["more"] is True and out["last_id"] == 6
    first = out["messages"][0]
    assert first["photo"] and first["videos"] == [] and first["links"] == ["https://t.me/other/12"]
    assert first["linked"] == [TG.video_facts(doc_msg(12), "other")]
    assert out["messages"][2]["grouped_id"] == 9 and out["messages"][1]["videos"][0]["peer"] == "mainchan"
    assert [v["peer"] for v in out["messages"][4]["linked"]] == [5551234]       # the closed channel is skipped
    assert ("dialogs",) in t._client.calls
    assert out["channel"] == {"id": 77, "title": "Main", "about": "films", "restricted": False}
    again = asyncio.run(t._scan({"channel": "mainchan", "since_id": 6, "limit": 5}))
    assert [m["id"] for m in again["messages"]] == [7] and again["more"] is False and again["last_id"] == 7 and "about" not in again["channel"]
    only_service = asyncio.run(t._scan({"channel": "mainchan", "since_id": 4, "limit": 1}))
    assert only_service["messages"] == [] and only_service["last_id"] == 5 and only_service["more"] is True
    with pytest.raises(ValueError):
        asyncio.run(t._scan({"channel": "bad name!"}))
    with pytest.raises(ValueError):
        asyncio.run(t._scan({"channel": -5}))


def test_join_by_invite_and_by_name():
    t = fake()
    assert asyncio.run(t._join({"invite": "AbCdEfGh12"})) == {"id": 555, "title": "Series", "username": None}
    assert t._client.calls[-2:] == ["CheckChatInviteRequest", "ImportChatInviteRequest"]
    assert asyncio.run(t._join({"username": "publicchan"}))["username"] == "publicchan"
    assert t._client.calls[-1] == "JoinChannelRequest"
    for bad in ({"invite": "a b"}, {"username": "x"}, {}):
        with pytest.raises(ValueError):
            asyncio.run(t._join(bad))


class DownloadClient(FakeClient):
    def __init__(self, stop_at=None):
        super().__init__([], {(88, 4): doc_msg(4)})
        self.stop_at = stop_at

    async def download_media(self, msg, file, progress_callback):
        for done in (100, 200, 300):
            progress_callback(done, 300)
        with open(file, "wb") as f:
            f.write(b"x")
        return file


def test_download_size_progress_and_stop(tmp_path):
    t = TG.Telegram()
    t._client = DownloadClient()
    seen = []
    assert asyncio.run(t._size("chan_one", 4)) == 900 * 1048576
    path = asyncio.run(t._download("chan_one", 4, str(tmp_path / "film"), lambda d, total: seen.append(d)))
    assert path.endswith("film.mkv") and seen == [100, 200, 300]

    class Stopped(Exception):
        pass

    def stop(done, total):
        raise Stopped()
    with pytest.raises(Stopped):
        asyncio.run(t._download("chan_one", 4, str(tmp_path / "film2"), stop))
    with pytest.raises(FileNotFoundError):
        asyncio.run(t._size("chan_one", 99))
    for bad in ("tg:bad name/1", "magnet:?x", "tg:chan_one/", None):
        with pytest.raises(ValueError):
            TG.Telegram._link(bad)
    assert TG.Telegram._link("tg:555/3") == (555, 3)


def test_lane_asks_telegram_jobs_only_when_set_up(monkeypatch):
    monkeypatch.setattr(TG, "configured", lambda: False)
    api, _answers, asks = fake_api([])
    go(api)
    assert all("tg_scan" not in a for a in asks)
    monkeypatch.setattr(TG, "configured", lambda: True)

    class Client:
        closed = False

        def scan(self, work):
            return {"messages": [], "more": False, "channel": {"id": 1, "title": ""}}

        def join(self, work):
            raise PermissionError("signed out")
    monkeypatch.setattr(TG, "client", lambda: Client())
    monkeypatch.setattr(TG, "close", lambda: setattr(Client, "closed", True))
    api, answers, asks = fake_api([{"id": 1, "kind": "tg_scan", "work": {"channel": "abcd"}},
                                   {"id": 2, "kind": "tg_join", "work": {"invite": "AbCdEfGh12"}}])
    assert go(api, browser_cls=lambda s: pytest.fail("no browser for telegram")) == 2
    done = {a["id"]: a for a in answers if a["op"] == "done"}
    assert done[1]["result"]["more"] is False and done[2]["error"] == "PermissionError"
    assert "tg_scan" in asks[0] and Client.closed


def test_scan_brings_the_channel_picture_members_and_restriction():
    t = fake([text_msg(1, "x")])

    async def picture(entity, file, download_big):
        assert file is bytes and download_big is False
        return b"\xff\xd8jpeg"
    t._client.download_profile_photo = picture

    async def full(request):
        return pytypes.SimpleNamespace(full_chat=pytypes.SimpleNamespace(about="", participants_count=1234))
    FakeClient.__call__, keep = (lambda self, r: full(r)), FakeClient.__call__
    try:
        out = asyncio.run(t._scan({"channel": "mainchan", "about": True, "photo": True}))
    finally:
        FakeClient.__call__ = keep
    assert out["channel"]["members"] == 1234 and out["channel"]["restricted"] is False
    assert TG.base64.b64decode(out["channel"]["photo"]) == b"\xff\xd8jpeg"
    plain = asyncio.run(fake([text_msg(1, "x")])._scan({"channel": "mainchan"}))
    assert "photo" not in plain["channel"] and "members" not in plain["channel"]


def test_folder_lists_its_channels_by_a_loose_name():
    t = fake()
    chan = types.InputPeerChannel(channel_id=5, access_hash=1)
    folders = pytypes.SimpleNamespace(filters=[
        types.DialogFilterDefault(),
        pytypes.SimpleNamespace(title=pytypes.SimpleNamespace(text="🎬 أفلام"), pinned_peers=[chan],
                                include_peers=[types.InputPeerUser(user_id=3, access_hash=1), types.InputPeerChannel(channel_id=6, access_hash=1)])])

    async def call(request):
        assert type(request).__name__ == "GetDialogFiltersRequest"
        return folders

    async def entity(peer):
        if peer.channel_id == 6:
            raise ValueError("gone")
        return pytypes.SimpleNamespace(id=peer.channel_id, title="Films", username="films_hd")
    t._client.get_entity = entity
    keep = FakeClient.__call__
    FakeClient.__call__ = lambda self, r: call(r)
    try:
        out = asyncio.run(t._folder({"name": "افلام"}))
        none = asyncio.run(t._folder({"name": "مسلسلات"}))
    finally:
        FakeClient.__call__ = keep
    assert out == {"found": True, "channels": [{"id": 5, "title": "Films", "username": "films_hd"}]}
    assert none == {"found": False, "channels": []}
    with pytest.raises(ValueError):
        asyncio.run(t._folder({"name": "🎬"}))


def test_post_pictures_pick_the_middle_size_and_skip_the_broken():
    small = types.PhotoSize(type="s", w=90, h=90, size=100)
    mid = types.PhotoSize(type="m", w=320, h=480, size=9000)
    big = types.PhotoSize(type="x", w=800, h=1200, size=90000)
    photo_msg = pytypes.SimpleNamespace(photo=pytypes.SimpleNamespace(sizes=[small, big, mid]), document=None)
    video_msg = pytypes.SimpleNamespace(photo=None, document=pytypes.SimpleNamespace(thumbs=[small]))
    t = fake(others={(77, 10): photo_msg, (77, 11): video_msg})
    got = []

    async def media(msg, file, thumb):
        got.append(thumb)
        return b"\xff\xd8" + bytes(10)
    t._client.download_media = media
    out = asyncio.run(t._photos({"items": [{"peer": "mainchan", "msg": 10}, {"peer": "mainchan", "msg": 11},
                                           {"peer": "mainchan", "msg": 12}, {"peer": "bad name!", "msg": 1}, {"msg": True}]}))
    assert got == [mid, small] and [p["msg"] for p in out["photos"]] == [10, 11]


def test_close_waits_for_the_disconnect_on_the_clients_own_loop():
    t = TG.Telegram()
    done = []

    class Client:
        async def disconnect(self):
            done.append(asyncio.get_running_loop() is t._loop)
    t._loop = loop = asyncio.new_event_loop()
    t._thread = TG.threading.Thread(target=t._loop.run_forever, daemon=True)
    t._thread.start()
    t._client = Client()
    t.close()
    assert done == [True] and t._client is None and t._loop is None and loop.is_closed()
