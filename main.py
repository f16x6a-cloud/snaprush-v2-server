"""Snap Rush phone-camera service controlled by a Unity lobby.

Run with: uvicorn main:app --host 0.0.0.0 --port 7860
For production: replace volatile storage, use a single instance or shared state, add durable media.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from io import BytesIO
import json
import os
from pathlib import Path
import random
import secrets
import threading
import time
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, UnidentifiedImageError
import httpx
import qrcode

ROOT = Path(__file__).resolve().parent
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/tmp/snaprush_media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
AI_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini")
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
POINTS = [100, 70, 40, 20, 0, 0]
ROOM_TTL = 2 * 60 * 60

# Explicit aliases prevent ambiguity (animal mouse vs computer mouse, for example).
CATALOG = [
    ("mouse", "ماوس كمبيوتر", "Computer mouse", "A computer pointing device, not an animal."),
    ("cup", "كوب", "Cup", "A drinking cup or mug."),
    ("bottle", "قارورة ماء", "Water bottle", "A bottle used to hold drinking water."),
    ("keys", "مفاتيح", "Keys", "One or more physical keys."),
    ("watch", "ساعة يد", "Wristwatch", "A watch worn on the wrist."),
    ("keyboard", "لوحة مفاتيح", "Keyboard", "A computer keyboard."),
    ("headphones", "سماعة رأس", "Headphones", "Headphones or over-ear headset."),
    ("spoon", "ملعقة", "Spoon", "Eating utensil spoon."),
    ("fork", "شوكة", "Fork", "Eating utensil fork."),
    ("book", "كتاب", "Book", "A physical book."),
    ("pen", "قلم", "Pen", "A writing pen."),
    ("shoe", "حذاء", "Shoe", "One visible shoe or sneaker."),
    ("glasses", "نظارة", "Glasses", "Eyeglasses or sunglasses."),
    ("remote", "ريموت", "Remote control", "A TV or device remote control."),
    ("pillow", "مخدة", "Pillow", "A cushion or pillow."),
    ("lamp", "مصباح", "Lamp", "A household lamp."),
    ("wallet", "محفظة", "Wallet", "A wallet for cards or money."),
    ("notebook", "دفتر", "Notebook", "Paper notebook."),
    ("chair", "كرسي", "Chair", "A chair for sitting."),
    ("toothbrush", "فرشاة أسنان", "Toothbrush", "A toothbrush."),
    ("towel", "منشفة", "Towel", "A fabric towel."),
    ("phone", "جوال", "Mobile phone", "A mobile smartphone."),
    ("charger", "شاحن", "Charger", "A phone or laptop charging adapter or cable."),
    ("scissors", "مقص", "Scissors", "A pair of scissors."),
    ("plate", "صحن", "Plate", "A dish plate for food."),
    ("brush", "فرشاة شعر", "Hairbrush", "A hairbrush."),
    ("backpack", "شنطة ظهر", "Backpack", "A backpack."),
    ("cap", "كاب", "Baseball cap", "A baseball cap."),
    ("comb", "مشط", "Comb", "A comb for hair."),
    ("paper", "ورقة", "Paper sheet", "A sheet of paper."),
]

app = FastAPI(title="SNAP RUSH — Unity-controlled Camera Service", version="0.2.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
_lock = threading.RLock()
_rooms: dict[str, "Room"] = {}


@dataclass
class Player:
    slot: int
    session: str
    ready: bool = False
    points: int = 0
    joined_at: float = field(default_factory=time.time)


@dataclass
class Submission:
    id: str
    slot: int
    round_no: int
    created_at: float
    status: str = "analyzing"  # analyzing, correct, incorrect, unverified
    reason: str = ""
    award: int = 0


@dataclass
class Room:
    id: str
    host_key: str
    size: int
    rounds: int
    seconds: int
    created_at: float = field(default_factory=time.time)
    state: str = "waiting"
    players: dict[int, Player] = field(default_factory=dict)
    slot_tokens: dict[int, str] = field(default_factory=dict)
    auto_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    start_at: float = 0
    deadline: float = 0
    review_end: float = 0
    current_round: int = 0
    challenge_order: list[int] = field(default_factory=list)
    active_seconds: int = 60
    submissions: list[Submission] = field(default_factory=list)

    def current_target(self) -> Optional[tuple[str, str, str, str]]:
        if not self.current_round:
            return None
        return CATALOG[self.challenge_order[self.current_round - 1]]


class RoomCreate(BaseModel):
    players: int = Field(default=2, ge=2, le=6)
    rounds: int = Field(default=5, ge=1, le=15)
    seconds: int = Field(default=60, ge=15, le=120)


class JoinRequest(BaseModel):
    room_id: str
    token: str
    previous_session: Optional[str] = None


class PlayerRequest(BaseModel):
    room_id: str
    session: str


class HostPlayerReady(BaseModel):
    host_key: str
    slot: int = Field(ge=1, le=6)
    ready: bool = True


class HostStartRound(BaseModel):
    # Called only by the Unity lobby, never by a player's phone.
    host_key: str
    target_id: Optional[str] = None
    seconds: Optional[int] = Field(default=None, ge=15, le=120)
    countdown_seconds: int = Field(default=3, ge=0, le=10)


class HostRequest(BaseModel):
    host_key: str


def _get_room(room_id: str) -> Room:
    room = _rooms.get(room_id)
    if room is None:
        raise HTTPException(404, "Room not found or expired")
    if time.time() - room.created_at > ROOM_TTL:
        del _rooms[room_id]
        raise HTTPException(410, "Room expired; create a new room")
    _advance(room)
    return room


def _auth_player(room: Room, session: str) -> Player:
    for player in room.players.values():
        if secrets.compare_digest(player.session, session):
            return player
    raise HTTPException(401, "Invalid player session")


def _auth_host(room: Room, key: str) -> None:
    if not key or not secrets.compare_digest(room.host_key, key):
        raise HTTPException(401, "Invalid host key")


def _recompute_scores(room: Room) -> None:
    for p in room.players.values():
        p.points = 0
    for s in room.submissions:
        s.award = 0
    for r in range(1, room.current_round + 1):
        correct = sorted(
            (s for s in room.submissions if s.round_no == r and s.status == "correct"),
            key=lambda s: (s.created_at, s.id),
        )
        rewarded: set[int] = set()
        for sub in correct:
            if sub.slot in rewarded:
                continue
            position = len(rewarded)
            rewarded.add(sub.slot)
            sub.award = POINTS[position]
            if sub.slot in room.players:
                room.players[sub.slot].points += sub.award


def _advance(room: Room) -> None:
    """Apply time-based state transitions, but NEVER start another round.

    Unity is the only caller allowed to choose when a new round starts.
    """
    now = time.time()
    if room.state == "countdown" and now >= room.start_at:
        room.state = "round_active"
        room.deadline = room.start_at + room.active_seconds
    if room.state == "round_active" and now >= room.deadline:
        room.state = "round_review"
        room.review_end = now + 7
    # Intentionally do not auto-progress to the next round: Unity owns it.


def _qr_data(url: str) -> str:
    img = qrcode.make(url, box_size=5, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _base_url() -> str:
    # Prefer the exact public HTTPS hostname configured after deployment.
    # An empty value falls back to browser location for QR URLs via JS.
    return APP_BASE_URL


def _snapshot(room: Room, is_host: bool, current_session: str = "") -> dict:
    _advance(room)
    current = room.current_target()
    slots = []
    for i in range(1, room.size + 1):
        p = room.players.get(i)
        entry = {"slot": i, "occupied": bool(p), "ready": p.ready if p else False, "points": p.points if p else 0}
        if is_host and not p and room.state == "waiting":
            entry["join_path"] = f"/join?room={room.id}&token={room.slot_tokens[i]}"
        slots.append(entry)
    current_subs = [s for s in room.submissions if s.round_no == room.current_round]
    latest = {}
    for s in current_subs:
        latest[s.slot] = {
            "slot": s.slot, "photo_url": f"/media/{room.id}/{s.id}.jpg",
            "status": s.status, "reason": s.reason,
            "award": s.award, "created_at": s.created_at,
        }
    result = {
        "room_id": room.id, "state": room.state, "capacity": room.size,
        "connected": len(room.players), "ready_count": sum(p.ready for p in room.players.values()),
        "rounds": room.rounds, "seconds": room.active_seconds,
        "round": room.current_round, "start_at": room.start_at,
        "deadline": room.deadline, "server_now": time.time(),
        "target": {"id": current[0], "ar": current[1], "en": current[2]} if current and room.state in ("round_active", "round_review", "finished") else None,
        "slots": slots, "photos": list(latest.values()),
        "ai_enabled": bool(os.getenv("OPENAI_API_KEY")),
        "control": "unity",
    }
    if is_host:
        result["general_join_path"] = f"/join?room={room.id}&token={room.auto_token}"
        result["max_rounds"] = room.rounds
    else:
        p = _auth_player(room, current_session)
        result["my_slot"] = p.slot
        result["my_ready"] = p.ready
        result["my_points"] = p.points
    return result


@app.get("/")
def host_page():
    return FileResponse(ROOT / "static" / "host.html", headers={"Cache-Control": "no-store"})


@app.get("/join")
def join_page():
    return FileResponse(ROOT / "static" / "player.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "snaprush", "version": app.version, "ai_configured": bool(os.getenv("OPENAI_API_KEY"))}


@app.post("/api/rooms")
def create_room(payload: RoomCreate):
    with _lock:
        room_id = secrets.token_hex(4).upper()
        while room_id in _rooms:
            room_id = secrets.token_hex(4).upper()
        order = list(range(len(CATALOG)))
        random.SystemRandom().shuffle(order)
        room = Room(
            id=room_id, host_key=secrets.token_urlsafe(32),
            size=payload.players, rounds=payload.rounds, seconds=payload.seconds,
            active_seconds=payload.seconds,
            challenge_order=(order * (payload.rounds // len(order) + 1))[:payload.rounds],
            slot_tokens={i: secrets.token_urlsafe(24) for i in range(1, payload.players + 1)},
        )
        _rooms[room_id] = room
        return {"room_id": room.id, "host_key": room.host_key}


@app.get("/api/rooms/{room_id}/state")
def room_state(room_id: str, x_session: str = Header(default=""), x_host_key: str = Header(default="")):
    with _lock:
        room = _get_room(room_id)
        if x_host_key and secrets.compare_digest(room.host_key, x_host_key):
            return _snapshot(room, True)
        return _snapshot(room, False, x_session)


@app.post("/api/join")
def join_room(payload: JoinRequest):
    with _lock:
        room = _get_room(payload.room_id)
        if payload.previous_session:
            for p in room.players.values():
                if secrets.compare_digest(payload.previous_session, p.session):
                    return {"room_id": room.id, "session": p.session, "slot": p.slot}
        if room.state != "waiting":
            raise HTTPException(409, "Match already started")
        if secrets.compare_digest(payload.token, room.auto_token):
            slot = next((i for i in range(1, room.size + 1) if i not in room.players), None)
        else:
            slot = next((i for i, token in room.slot_tokens.items() if secrets.compare_digest(payload.token, token)), None)
        if slot is None:
            raise HTTPException(409, "QR code invalid, already used, or room full")
        if slot in room.players:
            raise HTTPException(409, "This player slot is already occupied")
        p = Player(slot=slot, session=secrets.token_urlsafe(32))
        room.players[slot] = p
        room.slot_tokens[slot] = secrets.token_urlsafe(24)
        return {"room_id": room.id, "session": p.session, "slot": slot}


@app.post("/api/rooms/{room_id}/host/player-ready")
def host_player_ready(room_id: str, payload: HostPlayerReady):
    """Only Unity can mark a joined player as Ready from its lobby UI."""
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        if room.state != "waiting":
            raise HTTPException(409, "Ready can only change while waiting")
        p = room.players.get(payload.slot)
        if p is None:
            raise HTTPException(404, "Player slot has not joined")
        p.ready = payload.ready
        return _snapshot(room, True)


@app.post("/api/rooms/{room_id}/host/start-round")
def start_round(room_id: str, payload: HostStartRound):
    """Unity lobby explicitly begins each round; the mobile site cannot.

    If target_id is omitted, uses the server's shuffled object catalog.
    """
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        if room.state not in {"waiting", "round_review"}:
            raise HTTPException(409, "Finish or review the current round before starting another")
        if room.current_round >= room.rounds:
            raise HTTPException(409, "All configured rounds are complete")
        if room.state == "waiting" and (len(room.players) < 2 or not all(p.ready for p in room.players.values())):
            raise HTTPException(409, "At least two connected players must be marked Ready by the Unity lobby")
        if room.state == "round_review" and any(
            s.status == "analyzing" and s.round_no == room.current_round
            for s in room.submissions
        ):
            raise HTTPException(409, "Wait for pending image analysis before continuing")
        next_round = room.current_round + 1
        if payload.target_id is not None:
            match_index = next((i for i, c in enumerate(CATALOG) if c[0] == payload.target_id), None)
            if match_index is None:
                raise HTTPException(422, "Unknown challenge target_id")
            room.challenge_order[next_round - 1] = match_index
        room.current_round = next_round
        room.active_seconds = payload.seconds or room.seconds
        room.start_at = time.time() + payload.countdown_seconds
        room.deadline = room.start_at + room.active_seconds
        room.review_end = 0
        room.state = "countdown" if payload.countdown_seconds else "round_active"
        return _snapshot(room, True)


@app.post("/api/rooms/{room_id}/host/finish")
def finish_match(room_id: str, payload: HostRequest):
    """Finish the match from Unity, never from the player website."""
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        room.state = "finished"
        return _snapshot(room, True)


@app.post("/api/leave")
def leave(payload: PlayerRequest):
    with _lock:
        room = _get_room(payload.room_id)
        p = _auth_player(room, payload.session)
        if room.state == "waiting":
            del room.players[p.slot]
            room.slot_tokens[p.slot] = secrets.token_urlsafe(24)
        else:
            # Retain the claimed slot during a live match to avoid score/session impersonation.
            p.ready = False
        return {"ok": True}


@app.post("/api/rooms/{room_id}/reset")
def reset_room(room_id: str, payload: HostRequest):
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        room.state = "waiting"
        room.current_round = 0
        room.start_at = room.deadline = room.review_end = 0
        room.active_seconds = room.seconds
        room.players.clear()
        room.submissions.clear()
        room.slot_tokens = {i: secrets.token_urlsafe(24) for i in range(1, room.size + 1)}
        room.auto_token = secrets.token_urlsafe(24)
        order = list(range(len(CATALOG)))
        random.SystemRandom().shuffle(order)
        room.challenge_order = (order * (room.rounds // len(order) + 1))[:room.rounds]
        return {"ok": True}


def _decode_image(raw: bytes) -> bytes:
    try:
        with Image.open(BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            out = BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=83, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "Invalid or unsupported image") from exc


def _analyze_photo(room_id: str, submission_id: str, jpg: bytes, requested_description: str):
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        with _lock:
            room = _rooms.get(room_id)
            if room:
                s = next((v for v in room.submissions if v.id == submission_id), None)
                if s:
                    s.status, s.reason = "unverified", "AI provider not configured"
        return
    encoded = base64.b64encode(jpg).decode("ascii")
    prompt = (
        "You are a strict image object verifier for a family party game. "
        f"Requested object: {requested_description}. "
        "Determine whether the requested physical object is clearly visible in the provided photo. "
        "Do not accept text labels, screens showing pictures of the object, or an unrelated lookalike. "
        'Return JSON only: {"is_match":true or false,"reason":"brief explanation"}. '
        "If uncertain, set is_match to false."
    )
    try:
        with httpx.Client(timeout=35) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": AI_MODEL,
                      "messages": [{"role": "user", "content": [
                          {"type": "text", "text": prompt},
                          {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded, "detail": "low"}}
                      ]}], "temperature": 0, "response_format": {"type": "json_object"}, "max_tokens": 140},
            )
            response.raise_for_status()
            result = json.loads(response.json()["choices"][0]["message"]["content"])
            status = "correct" if result.get("is_match") is True else "incorrect"
            reason = str(result.get("reason", ""))[:180]
    except Exception:
        status, reason = "unverified", "AI could not verify this image; no points awarded"
    with _lock:
        room = _rooms.get(room_id)
        if room:
            s = next((v for v in room.submissions if v.id == submission_id), None)
            if s:
                s.status, s.reason = status, reason
                _recompute_scores(room)


@app.post("/api/rooms/{room_id}/photos")
def upload_photo(room_id: str, background_tasks: BackgroundTasks, session: str = Form(...), file: UploadFile = File(...)):
    raw = file.file.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(413, "Image exceeds 5 MB")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Unsupported image type")
    jpg = _decode_image(raw)
    with _lock:
        room = _get_room(room_id)
        player = _auth_player(room, session)
        if room.state != "round_active" or time.time() >= room.deadline:
            raise HTTPException(409, "There is no active photo challenge")
        current = [s for s in room.submissions if s.round_no == room.current_round and s.slot == player.slot]
        if len(current) >= 3:
            raise HTTPException(429, "Maximum 3 photos per round")
        if any(s.status == "correct" for s in current):
            raise HTTPException(409, "You already submitted a correct photo")
        if any(s.status == "analyzing" for s in current):
            raise HTTPException(409, "Wait for your previous photo to finish analyzing")
        submission = Submission(id=secrets.token_hex(16), slot=player.slot, round_no=room.current_round, created_at=time.time())
        (MEDIA_DIR / f"{room.id}_{submission.id}.jpg").write_bytes(jpg)
        room.submissions.append(submission)
        target = room.current_target()
        if not os.getenv("OPENAI_API_KEY"):
            submission.status, submission.reason = "unverified", "AI verification not configured"
        else:
            background_tasks.add_task(_analyze_photo, room.id, submission.id, jpg, target[3])
        return {"ok": True, "submission_id": submission.id, "status": submission.status,
                "photo_url": f"/media/{room.id}/{submission.id}.jpg", "slot": player.slot}


@app.get("/media/{room_id}/{submission_id}.jpg")
def get_photo(room_id: str, submission_id: str):
    # Unpredictable IDs for prototype; production requires room membership authorization.
    if not (len(room_id) == 8 and len(submission_id) == 32 and all(c in "0123456789ABCDEF" for c in room_id)
            and all(c in "0123456789abcdef" for c in submission_id)):
        raise HTTPException(404, "Photo not found")
    with _lock:
        room = _get_room(room_id)
        if not any(s.id == submission_id for s in room.submissions):
            raise HTTPException(404, "Photo not found")
    path = MEDIA_DIR / f"{room_id}_{submission_id}.jpg"
    if not path.is_file():
        raise HTTPException(404, "Photo not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/api/rooms/{room_id}/host/qr")
def host_qr(room_id: str, payload: HostRequest, request: Request):
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        host = _base_url() or str(request.base_url).rstrip("/")
        images = {}
        for slot in range(1, room.size + 1):
            if slot not in room.players and room.state == "waiting":
                path = f"/join?room={room.id}&token={room.slot_tokens[slot]}"
                if host:
                    images[str(slot)] = {"image": _qr_data(host + path), "path": path}
                else:
                    images[str(slot)] = {"path": path}
        auto_path = f"/join?room={room.id}&token={room.auto_token}"
        return {"qr": images, "general": {"image": _qr_data(host + auto_path) if host else None, "path": auto_path}}


@app.get("/api/catalog")
def get_catalog():
    return {"items": [{"id": obj[0], "ar": obj[1], "en": obj[2]} for obj in CATALOG]}
