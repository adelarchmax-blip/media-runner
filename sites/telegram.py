"""Telegram on the runner: reading channels (tg_scan), joining one from its invite (tg_join), the channels
in one of the account's chat folders (tg_folder), small pictures of posts (tg_photos), downloading one
video (worker.py, kind "telegram").

The account's session and app keys come only from three environment variables that the workflow gives
to the one step that runs Python, from the GitHub secrets of the `telegram` environment. They are taken
out of the environment the moment this module loads, so no command started later (Chrome, the download
manager, rclone) can see them; they are never written to disk, printed, logged or sent anywhere but
to Telegram itself. Logs and answers carry counts and error type names only.

The controller decides what a post is (film, episode, chatter); here messages are only read and
described: text, photo, video facts (name, size, length, height) and the links in the text and buttons.
A link to a message in another channel is opened so its video is described too.
"""
import asyncio
import base64
import logging
import os
import re
import threading
import time

ENV = ("TG_API_ID", "TG_API_HASH", "TG_SESSION")
_CREDS = {k: os.environ.pop(k, "") for k in ENV}     # out of the environment before anything else starts
MAX_TEXT = 1500
MAX_LINKED = 40              # message links opened in one scan
SCAN_MAX = 1000
STALL = 5 * 60               # a download with no new bytes for this long has stopped
USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MSG_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:(c)/(\d{4,20})|([A-Za-z][A-Za-z0-9_]{3,31}))/(\d{1,9})(?![\w/])", re.I)
PHOTO_MAX = 200 * 1024      # one post picture (about 320 px wide)
PHOTOS_MAX = 30             # pictures in one job
FOLDER_MAX = 300            # channels taken from one folder
VIDEO_EXT = re.compile(r"\.(mp4|mkv|avi|m4v|webm|mov|mpe?g|ts|wmv)$", re.I)


def configured() -> bool:
    return all(_CREDS.values())


def _is_video(f) -> bool:
    return bool(f) and (str(f.mime_type or "").startswith("video/") or bool(VIDEO_EXT.search(f.name or "")))


def video_facts(message, peer) -> dict:
    """What the controller needs to judge a video, or None for anything else"""
    f = getattr(message, "file", None)
    if not getattr(message, "document", None) or not _is_video(f):
        return None
    return {"name": str(f.name or "")[:200], "size": int(f.size or 0), "duration": int(f.duration or 0),
            "h": int(f.height or 0), "w": int(f.width or 0), "peer": peer, "msg": int(message.id)}


def links_of(message) -> list:
    """Addresses in the text (plain and hidden behind words) and in the buttons under the post"""
    from telethon.tl import types
    out = []
    for ent, text in (message.get_entities_text() if message.entities else []):
        if isinstance(ent, types.MessageEntityTextUrl):
            out.append(ent.url)
        elif isinstance(ent, types.MessageEntityUrl):
            out.append(text)
    for row in getattr(getattr(message, "reply_markup", None), "rows", None) or []:
        for button in row.buttons:          # the address is on the button, or on its type (newer layers)
            url = getattr(button, "url", None) or getattr(getattr(button, "type", None), "url", None)
            if isinstance(url, str):
                out.append(url)
    return [str(x)[:300] for x in out][:30]


def peer_of(entity):
    """A public channel by its name, a private one by its number"""
    name = getattr(entity, "username", None)
    return name if isinstance(name, str) and USER_RE.match(name) else int(entity.id)


class Telegram:
    """One signed-in client on its own event loop (a thread of its own); every call waits for its answer."""

    def __init__(self):
        self._loop = None
        self._client = None
        self._thread = None
        self._lock = threading.Lock()
        self._dialogs = False

    def _call(self, coro_fn, timeout):
        with self._lock:
            if self._client is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="tg")
                self._thread.start()
                try:
                    asyncio.run_coroutine_threadsafe(self._connect(), self._loop).result(120)
                except BaseException:
                    self._end_loop()
                    raise
        future = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()            # a call that ran too long (or was stopped) ends on the loop too
            raise

    async def _connect(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        logging.getLogger("telethon").setLevel(logging.WARNING)
        if not configured():
            raise PermissionError("telegram not set up")
        client = TelegramClient(StringSession(_CREDS["TG_SESSION"]), int(_CREDS["TG_API_ID"]), _CREDS["TG_API_HASH"],
                                flood_sleep_threshold=120, receive_updates=False)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise PermissionError("telegram session signed out")
        self._client = client

    def close(self):
        with self._lock:
            if self._client is not None:
                client = self._client

                async def bye():          # awaited on the client's own loop (called elsewhere it returns nothing)
                    await client.disconnect()
                try:   # disconnect only: the session stays valid for the next run
                    asyncio.run_coroutine_threadsafe(bye(), self._loop).result(30)
                except Exception as e:
                    logging.info(f"telegram close: {type(e).__name__}")
            if self._loop is not None:
                self._end_loop()
            self._client, self._dialogs = None, False

    def _end_loop(self):
        """The loop stops, its thread ends, and it is closed (no open sockets left behind)"""
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(10)
        if not self._loop.is_running():
            self._loop.close()
        self._loop, self._thread = None, None

    async def _entity(self, peer):
        from telethon.tl.types import PeerChannel
        if isinstance(peer, str) and USER_RE.match(peer):
            return await self._client.get_entity(peer)
        if isinstance(peer, int) and not isinstance(peer, bool) and peer > 0:
            if not self._dialogs:            # private channels are known only through the account's chats
                await self._client.get_dialogs(limit=None)
                self._dialogs = True
            return await self._client.get_entity(PeerChannel(peer))
        raise ValueError("bad channel")

    # ---------- reading a channel ----------

    def scan(self, work: dict) -> dict:
        return self._call(lambda: self._scan(work), 20 * 60)

    async def _scan(self, work):
        from telethon import functions
        entity = await self._entity(work.get("channel"))
        since = work.get("since_id") if isinstance(work.get("since_id"), int) and work["since_id"] > 0 else 0
        limit = min(SCAN_MAX, work["limit"]) if isinstance(work.get("limit"), int) and work["limit"] > 0 else 500
        peer, messages, more, opened, last, read = peer_of(entity), [], False, {"n": 0}, since, 0
        async for m in self._client.iter_messages(entity, min_id=since, reverse=True, limit=limit + 1):
            read += 1
            if read > limit:
                more = True
                break
            last = int(m.id)
            if getattr(m, "action", None):        # "joined", "pinned" and other service lines
                continue
            links = links_of(m)
            video = video_facts(m, peer)
            messages.append({"id": int(m.id), "date": int(m.date.timestamp()) if m.date else 0,
                             "grouped_id": int(m.grouped_id) if m.grouped_id else None,
                             "text": str(m.message or "")[:MAX_TEXT], "photo": bool(m.photo),
                             "videos": [video] if video else [],
                             "linked": await self._linked(" ".join([m.message or ""] + links), opened),
                             "links": links})
        out = {"messages": messages, "more": more, "last_id": last,
               "channel": {"id": int(entity.id), "title": str(getattr(entity, "title", "") or "")[:120],
                           "restricted": bool(getattr(entity, "restricted", False))}}
        if work.get("about"):
            try:
                full = await self._client(functions.channels.GetFullChannelRequest(entity))
                out["channel"]["about"] = str(full.full_chat.about or "")[:500]
                members = getattr(full.full_chat, "participants_count", None)
                if isinstance(members, int):
                    out["channel"]["members"] = members
            except Exception as e:
                logging.info(f"telegram about: {type(e).__name__}")
        if work.get("photo"):
            try:                        # the channel's small picture (160 px)
                pic = await self._client.download_profile_photo(entity, file=bytes, download_big=False)
                if isinstance(pic, bytes) and 0 < len(pic) <= PHOTO_MAX:
                    out["channel"]["photo"] = base64.b64encode(pic).decode()
            except Exception as e:
                logging.info(f"telegram photo: {type(e).__name__}")
        logging.info(f"telegram scan: {len(messages)} message(s), more={more}, {opened['n']} link(s) opened")
        return out

    async def _linked(self, text: str, opened: dict) -> list:
        """Videos in the messages this post links to (the "photo here, video in another channel" kind)"""
        found, seen = [], set()
        for m in MSG_LINK.finditer(text or ""):
            key = (m.group(2) or m.group(3).lower(), m.group(4))
            if key in seen:            # the same link in the text and behind a word: opened once
                continue
            seen.add(key)
            if opened["n"] >= MAX_LINKED or len(found) >= 10:
                break
            opened["n"] += 1
            try:
                entity = await self._entity(int(m.group(2)) if m.group(1) else m.group(3))
                msg = await self._client.get_messages(entity, ids=int(m.group(4)))
            except Exception as e:   # a closed or private channel: the post is judged without it
                logging.info(f"telegram link not opened: {type(e).__name__}")
                continue
            facts = video_facts(msg, peer_of(entity)) if msg else None
            if facts:
                found.append(facts)
        return found

    # ---------- joining ----------

    def join(self, work: dict) -> dict:
        return self._call(lambda: self._join(work), 5 * 60)

    async def _join(self, work):
        from telethon import functions
        code = work.get("invite")
        if code:
            if not isinstance(code, str) or not INVITE_RE.match(code):
                raise ValueError("bad invite")
            info = await self._client(functions.messages.CheckChatInviteRequest(code))
            chat = getattr(info, "chat", None)          # already a member (or a peek): no new join
            if chat is None:
                chat = (await self._client(functions.messages.ImportChatInviteRequest(code))).chats[0]
        else:
            name = work.get("username")
            if not isinstance(name, str) or not USER_RE.match(name):
                raise ValueError("bad channel")
            chat = await self._client.get_entity(name)
            await self._client(functions.channels.JoinChannelRequest(chat))
        self._dialogs = False
        logging.info("telegram: joined a channel")
        return {"id": int(chat.id), "title": str(getattr(chat, "title", "") or "")[:120],
                "username": getattr(chat, "username", None)}

    # ---------- a chat folder ----------

    def folder(self, work: dict) -> dict:
        return self._call(lambda: self._folder(work), 10 * 60)

    @staticmethod
    def _plain(text) -> str:
        """A folder name without emoji, spaces or the alef/yeh/teh-marbuta spellings, to compare names"""
        t = str(getattr(text, "text", text) or "").casefold()
        t = re.sub("[إأآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")
        return re.sub(r"[^\w]", "", t)

    async def _folder(self, work):
        from telethon import functions
        want = self._plain(work.get("name"))
        if not want or len(want) > 64:
            raise ValueError("bad folder name")
        res = await self._client(functions.messages.GetDialogFiltersRequest())
        found = None
        for f in getattr(res, "filters", res) or []:
            if self._plain(getattr(f, "title", "")) == want:
                found = f
                break
        if found is None:
            logging.info("telegram folder: not found")
            return {"found": False, "channels": []}
        out = []
        for peer in (list(getattr(found, "pinned_peers", None) or []) + list(getattr(found, "include_peers", None) or []))[:FOLDER_MAX]:
            if type(peer).__name__ != "InputPeerChannel":
                continue                # people, bots and small groups are not channels
            try:
                chat = await self._client.get_entity(peer)
            except Exception as e:
                logging.info(f"telegram folder channel: {type(e).__name__}")
                continue
            name = getattr(chat, "username", None)
            out.append({"id": int(chat.id), "title": str(getattr(chat, "title", "") or "")[:120],
                        "username": name if isinstance(name, str) and USER_RE.match(name) else None})
        logging.info(f"telegram folder: {len(out)} channel(s)")
        return {"found": True, "channels": out}

    # ---------- post pictures ----------

    def photos(self, work: dict) -> dict:
        return self._call(lambda: self._photos(work), 10 * 60)

    @staticmethod
    def _middle_size(msg):
        """The picture size closest to 320 px wide: the poster of the post, or the video's own cover"""
        sizes = list(getattr(getattr(msg, "photo", None), "sizes", None) or []) or \
            list(getattr(getattr(msg, "document", None), "thumbs", None) or [])
        sized = [s for s in sizes if isinstance(getattr(s, "w", None), int) and type(s).__name__ in ("PhotoSize", "PhotoSizeProgressive")]
        return min(sized, key=lambda s: abs(s.w - 320)) if sized else None

    async def _photos(self, work):
        out = []
        for want in (work.get("items") or [])[:PHOTOS_MAX]:
            peer, msg_id = want.get("peer") if isinstance(want, dict) else None, want.get("msg") if isinstance(want, dict) else None
            if not isinstance(msg_id, int) or isinstance(msg_id, bool) or msg_id <= 0:
                continue
            try:
                msg = await self._client.get_messages(await self._entity(peer), ids=msg_id)
                size = self._middle_size(msg) if msg else None
                pic = await self._client.download_media(msg, file=bytes, thumb=size) if size else None
            except Exception as e:
                logging.info(f"telegram picture: {type(e).__name__}")
                continue
            if isinstance(pic, bytes) and 0 < len(pic) <= PHOTO_MAX:
                out.append({"peer": peer, "msg": msg_id, "jpg": base64.b64encode(pic).decode()})
        logging.info(f"telegram pictures: {len(out)} of {len(work.get('items') or [])}")
        return {"photos": out}

    # ---------- downloading ----------

    @staticmethod
    def _link(link):
        m = re.fullmatch(r"tg:([A-Za-z][A-Za-z0-9_]{3,31}|\d{1,20})/(\d{1,10})", str(link or ""))
        if not m:
            raise ValueError("bad telegram link")
        return (int(m.group(1)) if m.group(1).isdigit() else m.group(1)), int(m.group(2))

    def size(self, link: str) -> int:
        """The video's size, so room on the disk is kept before it starts"""
        peer, msg_id = self._link(link)
        return self._call(lambda: self._size(peer, msg_id), 5 * 60)

    async def _size(self, peer, msg_id):
        facts = video_facts(await self._client.get_messages(await self._entity(peer), ids=msg_id), peer)
        if not facts:
            raise FileNotFoundError("the video is gone")
        return facts["size"]

    def download(self, link: str, out_base: str, on_progress, timeout: float) -> str:
        """`tg:<channel>/<message>` -> the saved file. on_progress(done, total) may raise to stop it."""
        peer, msg_id = self._link(link)
        return self._call(lambda: self._download(peer, msg_id, out_base, on_progress), timeout)

    async def _download(self, peer, msg_id, out_base, on_progress):
        msg = await self._client.get_messages(await self._entity(peer), ids=msg_id)
        if not msg or not video_facts(msg, peer):
            raise FileNotFoundError("the video is gone")
        ext = (msg.file.ext or "") if VIDEO_EXT.search(msg.file.ext or "") else ".mp4"
        last = {"at": time.time(), "bytes": 0}

        def progress(done, total):
            if done > last["bytes"]:
                last["at"], last["bytes"] = time.time(), done
            on_progress(done, total)        # "stop" from the owner raises here: the download ends at once
        task = asyncio.ensure_future(self._client.download_media(msg, file=out_base + ext, progress_callback=progress))
        while not task.done():
            await asyncio.wait({task}, timeout=30)
            if not task.done() and time.time() - last["at"] > STALL:
                task.cancel()
                raise TimeoutError("telegram download stalled")
        return task.result()


_client = None
_client_lock = threading.Lock()


def client() -> Telegram:
    global _client
    with _client_lock:
        if _client is None:
            _client = Telegram()
        return _client


def close() -> None:
    with _client_lock:
        if _client is not None:
            _client.close()
