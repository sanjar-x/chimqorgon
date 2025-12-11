import asyncio
from typing import Optional, Set, List
import socketio

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = socketio.ASGIApp(sio)

lock = asyncio.Lock()
NS = "/"

SPEAKERS_ROOM = "__speakers__"
ALL_LISTENERS_ROOM = "__listeners__"

CURRENT_SPEAKER: Optional[str] = None
SPEAKER_ALL: bool = True
SPEAKER_ROOMS: Set[str] = set()

# ✅ Кеш первого чанка (init segment WebM)
INIT_CHUNK: Optional[bytes] = None


def participants(room: str) -> List[str]:
    return [sid for sid, _ in sio.manager.get_participants(NS, room)]


@sio.event
async def disconnect(sid):
    global CURRENT_SPEAKER, SPEAKER_ALL, SPEAKER_ROOMS
    async with lock:
        if CURRENT_SPEAKER == sid:
            CURRENT_SPEAKER = None
            SPEAKER_ALL = True
            SPEAKER_ROOMS = set()


@sio.event
async def listener_join(sid, data):
    """
    data = {"room": "pc1"}
    """
    global INIT_CHUNK
    room = str((data or {}).get("room") or f"listener:{sid}")

    # кикаем старых из этой комнаты
    async with lock:
        to_kick = [x for x in participants(room) if x != sid]
        init_now = INIT_CHUNK

    for old in to_kick:
        try:
            await sio.emit("listener_kicked", {"room": room}, to=old)
        except Exception:
            pass
        try:
            await sio.disconnect(old)
        except Exception:
            pass

    # ✅ ВАЖНО: если поток уже идёт — сначала отдать init этому слушателю,
    # и только потом добавлять его в комнаты, чтобы он не получил audio_chunk раньше init
    if init_now:
        await sio.emit("audio_init", init_now, to=sid)

    await sio.enter_room(sid, room)
    await sio.enter_room(sid, ALL_LISTENERS_ROOM)

    await sio.emit("listener_joined", {"room": room}, to=sid)


@sio.event
async def speaker_join(sid, data):
    """
    data = {"all": true} или {"rooms": ["pc1","pc2"]}
    """
    global CURRENT_SPEAKER, SPEAKER_ALL, SPEAKER_ROOMS, INIT_CHUNK

    want_all = bool((data or {}).get("all", False))
    rooms = set(map(str, (data or {}).get("rooms") or []))

    # кик остальных speakers
    async with lock:
        other_speakers = [x for x in participants(SPEAKERS_ROOM) if x != sid]

    for old in other_speakers:
        try:
            await sio.emit("speaker_kicked", {"reason": "new_speaker"}, to=old)
        except Exception:
            pass
        try:
            await sio.disconnect(old)
        except Exception:
            pass

    # ✅ новый speaker = новый поток => сброс init, попросим listeners сбросить MSE
    async with lock:
        CURRENT_SPEAKER = sid
        SPEAKER_ALL = want_all if want_all else (len(rooms) == 0)
        SPEAKER_ROOMS = rooms
        INIT_CHUNK = None

    await sio.enter_room(sid, SPEAKERS_ROOM)
    await sio.emit("stream_reset", room=ALL_LISTENERS_ROOM)

    await sio.emit("speaker_joined", {"all": SPEAKER_ALL, "rooms": sorted(SPEAKER_ROOMS)}, to=sid)


@sio.event
async def speaker_set_targets(sid, data):
    global SPEAKER_ALL, SPEAKER_ROOMS

    want_all = bool((data or {}).get("all", False))
    rooms = set(map(str, (data or {}).get("rooms") or []))

    async with lock:
        if sid != CURRENT_SPEAKER:
            return
        SPEAKER_ALL = want_all if want_all else (len(rooms) == 0)
        SPEAKER_ROOMS = rooms

    await sio.emit("speaker_targets", {"all": SPEAKER_ALL, "rooms": sorted(SPEAKER_ROOMS)}, to=sid)


@sio.event
async def audio_chunk(sid, data):
    global INIT_CHUNK
    if not isinstance(data, dict):
        return

    chunk = data.get("chunk", b"")
    if not chunk:
        return

    async with lock:
        if sid != CURRENT_SPEAKER:
            return

        # ✅ если init ещё не установлен — первый чанк считаем init и рассылаем как audio_init
        if INIT_CHUNK is None:
            INIT_CHUNK = chunk

    # если это был init — разошлём и НЕ дублируем как audio_chunk
    if INIT_CHUNK == chunk:
        await sio.emit("audio_init", chunk, room=ALL_LISTENERS_ROOM)
        return

    async with lock:
        override_all = bool(data.get("all", False))
        override_rooms = data.get("rooms")
        override_rooms = set(map(str, override_rooms)) if override_rooms is not None else None

        if override_rooms is not None:
            targets = list(override_rooms)
            all_mode = False
        else:
            all_mode = override_all or SPEAKER_ALL
            targets = list(SPEAKER_ROOMS)

    if all_mode:
        await sio.emit("audio_chunk", chunk, room=ALL_LISTENERS_ROOM)
        return

    for r in targets:
        await sio.emit("audio_chunk", chunk, room=r)
