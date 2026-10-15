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
from typing import Optional, Literal

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, UnidentifiedImageError
import qrcode

from ai_verifier import verify_image

ROOT = Path(__file__).resolve().parent
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/tmp/snaprush_media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
POINTS = [100, 70, 40, 20, 0, 0]
ROOM_TTL = 2 * 60 * 60
# Local AI workers poll Render over HTTPS; Ollama stays on 127.0.0.1.
# OPENAI mode is kept for backward compatibility until you explicitly select local.
AI_PROVIDER = os.getenv("SNAPRUSH_AI_PROVIDER", "openai").strip().lower()
AI_PENDING_TIMEOUT = 240  # seconds: fail safely so a broken/offline PC never traps a round
AI_CLAIM_SECONDS = 180  # long enough for a single Ollama image request


def _ai_configured() -> bool:
    if AI_PROVIDER == "local":
        return len(os.getenv("SNAPRUSH_AI_WORKER_KEY", "")) >= 32
    if AI_PROVIDER == "cloudflare":
        return bool(os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip() and
                    os.getenv("CLOUDFLARE_API_TOKEN", "").strip())
    if AI_PROVIDER == "openai":
        return bool(os.getenv("OPENAI_API_KEY"))
    return False

# Object-only bilingual catalog. Color variants intentionally excluded.
# Keep the CATALOG tuple format and target IDs unchanged for Unity compatibility.
CATALOG_DATA = json.loads((ROOT / "data" / "objects.json").read_text(encoding="utf-8"))
CATALOG_ITEMS = CATALOG_DATA["items"]
CATALOG = [(c["id"], c["ar"], c["en"], c["ai_description"]) for c in CATALOG_ITEMS]
CATALOG_INDEX = {item["id"]: i for i, item in enumerate(CATALOG_ITEMS)}
assert len(CATALOG) == len(CATALOG_INDEX) == CATALOG_DATA["count"]
assert all(item.get("kind") == "object" and not item.get("color") for item in CATALOG_ITEMS)


# SNAP RUSH party mode: keep the complete 3,042-item catalog available, but
# draw from a small, hand-picked pool of everyday objects in the default mode.
# Matching by exact English label retains every existing Unity target_id.
EASY_CHALLENGE_NAMES = (
    "coffee mug", "drinking glass", "dinner plate", "tablespoon", "dinner fork",
    "water bottle", "TV remote control", "air conditioner remote", "smartphone",
    "phone charger", "ballpoint pen", "pencil", "notebook", "toothbrush",
    "toothpaste tube", "bath towel", "hand soap bar", "tissue box", "socks",
    "sneaker", "T-shirt", "baseball cap", "backpack", "wallet",
    "sunglasses", "prescription glasses", "hair comb", "hairbrush",
    "sleeping pillow", "throw blanket", "wall clock", "wall mirror",
    "table lamp", "paper scissors", "apple", "banana", "orange", "lemon",
    "potato", "tomato", "cucumber", "onion", "carrot", "egg carton",
    "cereal box", "potato chips bag", "milk carton", "watermelon",
    "school backpack", "hand towel", "door key", "car key",
    "computer mouse", "computer keyboard", "television", "analog wristwatch",
    "rice bag", "plastic cup", "tea box", "shoe rack", "soap dispenser",
    "bookshelf",
)
_EASY_NAME_SET = set(EASY_CHALLENGE_NAMES)
_EASY_CANDIDATES = [i for i, item in enumerate(CATALOG_ITEMS)
                    if item["en"] in _EASY_NAME_SET
                    and item.get("safety", "general") == "general"]
assert len(_EASY_CANDIDATES) == len(EASY_CHALLENGE_NAMES), "Easy challenge catalog missing an item"


def _new_challenge_order(rounds: int, mode: str = "mixed") -> list[int]:
    """Select easy, recognizable targets in normal gameplay.

    The 3,042-item catalog and all existing target IDs remain unchanged.
    'mixed' (Unity's default) uses curated easy home items.
    'all'/'objects' deliberately allow the full general-safety object catalog.
    """
    if mode == "mixed":
        eligible_indices = _EASY_CANDIDATES
    else:
        eligible_indices = [i for i, item in enumerate(CATALOG_ITEMS)
                            if item.get("safety", "general") == "general"]
    if not 1 <= rounds <= len(eligible_indices):
        raise ValueError("Round count exceeds the available object pool")
    return random.SystemRandom().sample(eligible_indices, rounds)


app = FastAPI(title="SNAP RUSH — Unity-controlled Camera Service", version="0.2.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
_lock = threading.RLock()
_rooms: dict[str, "Room"] = {}
_ai_worker_last_seen: float = 0.0
# Telemetry only since this Render process started (not Cloudflare billing totals).
_cloudflare_stats = {"completed": 0, "approved": 0, "rejected": 0,
                     "unverified": 0, "neurons": 0.0}


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
    detected_object: str = ""
    target_description: str = ""  # snapshotted at upload so changing rounds cannot change a job
    ai_claim: str = ""  # per-claim random token prevents stale/duplicate worker results
    ai_leased_until: float = 0.0
    resolved_at: float = 0.0
    resolution_order: int = 0


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
    catalog_mode: str = "mixed"
    submissions: list[Submission] = field(default_factory=list)
    result_sequence: int = 0
    round_winners: dict[int, int] = field(default_factory=dict)
    closed_rounds: dict[int, dict] = field(default_factory=dict)

    def current_target(self) -> Optional[tuple[str, str, str, str]]:
        if not self.current_round:
            return None
        return CATALOG[self.challenge_order[self.current_round - 1]]


class RoomCreate(BaseModel):
    players: int = Field(default=2, ge=2, le=6)
    rounds: int = Field(default=5, ge=1, le=15)
    seconds: int = Field(default=60, ge=15, le=120)
    catalog_mode: Literal["mixed", "objects", "all"] = "mixed"


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
        if room.size == 2:
            current = [sub for sub in room.submissions if sub.round_no == r]
            latest = {sub.slot: sub for sub in current}
            pending = any(sub.status == "analyzing" for sub in current)
            # Finalize only after the round closes, or both submitted results are terminal.
            finalizable = not pending and (r in room.closed_rounds or
                (r == room.current_round and room.state == "round_active" and
                 all(slot in latest for slot in (1, 2))))
            if correct and finalizable:
                # 'correct' is sorted by SERVER submission timestamp, then stable ID.
                winner = correct[0].slot
                room.round_winners[r] = winner
                correct[0].award = 100
                if winner in room.players:
                    room.players[winner].points += 100
            else:
                room.round_winners.pop(r, None)
            continue
        rewarded: set[int] = set()
        for sub in correct:
            if sub.slot in rewarded:
                continue
            position = len(rewarded)
            rewarded.add(sub.slot)
            sub.award = POINTS[position]
            if sub.slot in room.players:
                room.players[sub.slot].points += sub.award



def _mark_round_ended(room: Room, now: float, reason: str) -> None:
    if room.state != "round_active":
        return
    room.state = "round_review"
    room.review_end = now + 7
    room.closed_rounds[room.current_round] = {
        "start_at": room.start_at, "deadline": room.deadline,
        "ended_at": min(now, room.deadline) if reason == "timer" else now,
        "end_reason": reason,
    }
    _recompute_scores(room)


def _close_two_player_round_if_ready(room: Room, now: float) -> None:
    if room.size != 2 or room.state != "round_active":
        return
    if now >= room.deadline:
        _mark_round_ended(room, now, "timer")
        return
    current = [sub for sub in room.submissions if sub.round_no == room.current_round]
    latest = {sub.slot: sub for sub in current}
    if (all(slot in latest for slot in (1, 2)) and
            all(sub.status != "analyzing" for sub in current)):
        _mark_round_ended(room, now, "both_resolved")


def _resolve_submission(room: Room, sub: Submission, status: str, reason: str,
                        detected_object: str = "") -> bool:
    # Called under the existing room lock by every AI provider/timeout path.
    if sub.status != "analyzing":
        return False
    if status not in {"correct", "incorrect", "unverified"}:
        raise ValueError("Unsupported terminal AI status")
    room.result_sequence += 1
    sub.status, sub.reason, sub.detected_object = status, reason, detected_object
    sub.resolved_at, sub.resolution_order = time.time(), room.result_sequence
    sub.ai_claim, sub.ai_leased_until = "", 0.0
    _recompute_scores(room)
    _close_two_player_round_if_ready(room, sub.resolved_at)
    return True


def _round_result(room: Room, round_no: int) -> dict:
    current = [sub for sub in room.submissions if sub.round_no == round_no]
    latest = {sub.slot: sub for sub in current}
    closed = room.closed_rounds.get(round_no, {})
    start = closed.get("start_at", room.start_at if round_no == room.current_round else 0)
    winner = room.round_winners.get(round_no, 0)
    finalized = bool(closed) and not any(sub.status == "analyzing" for sub in current)
    rows = []
    for slot in range(1, room.size + 1):
        sub = latest.get(slot)
        rows.append({
            "slot": slot, "submitted": sub is not None,
            "submission_id": sub.id if sub else "",
            "photo_url": f"/media/{room.id}/{sub.id}.jpg" if sub else "",
            "status": sub.status if sub else "missing",
            "reason": sub.reason if sub else "No photo submitted",
            "submitted_at": sub.created_at if sub else 0,
            "submitted_seconds": max(0, sub.created_at - start) if sub else 0,
            "resolved_at": sub.resolved_at if sub else 0,
            "resolution_order": sub.resolution_order if sub else 0,
            "round_points": (100 if slot == winner else 0) if room.size == 2
                            else sum(s.award for s in current if s.slot == slot),
        })
    return {
        "round": round_no, "winner_slot": winner, "finalized": finalized,
        "ended_at": closed.get("ended_at", 0),
        "end_reason": closed.get("end_reason", ""),
        "results_ready_at": max([closed.get("ended_at", 0)] +
                                [s.resolved_at for s in current]) if finalized else 0,
        "players": rows,
    }


def _advance(room: Room) -> None:
    """Apply time-based state transitions, but NEVER start another round.

    Unity is the only caller allowed to choose when a new round starts.
    """
    now = time.time()
    # Never keep the lobby waiting forever if a local worker is disconnected.
    if AI_PROVIDER == "local":
        for sub in room.submissions:
            if sub.status == "analyzing" and now - sub.created_at > AI_PENDING_TIMEOUT:
                _resolve_submission(room, sub, "unverified", "AI analysis timed out; please retry")
    if room.state == "countdown" and now >= room.start_at:
        room.state = "round_active"
        room.deadline = room.start_at + room.active_seconds
    if room.state == "round_active" and now >= room.deadline:
        _mark_round_ended(room, now, "timer")
    _close_two_player_round_if_ready(room, now)
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
            "verdict": {"correct": "approved", "incorrect": "rejected", "analyzing": "pending", "unverified": "unavailable"}.get(s.status, "unavailable"),
            "approved": True if s.status == "correct" else False if s.status == "incorrect" else None,
            "detected_object": s.detected_object,
            "award": s.award, "created_at": s.created_at,
            "submission_id": s.id, "resolved_at": s.resolved_at,
            "resolution_order": s.resolution_order,
        }
    result = {
        "room_id": room.id, "state": room.state, "capacity": room.size,
        "connected": len(room.players), "ready_count": sum(p.ready for p in room.players.values()),
        "rounds": room.rounds, "seconds": room.active_seconds,
        "round": room.current_round, "start_at": room.start_at,
        "deadline": room.deadline, "server_now": time.time(),
        "target": {"id": current[0], "ar": current[1], "en": current[2]} if current and room.state in ("round_active", "round_review", "finished") else None,
        "slots": slots, "photos": list(latest.values()),
        "ai_enabled": _ai_configured(),
        "ai_provider": AI_PROVIDER,
        "ai_worker_online": AI_PROVIDER == "local" and (time.time() - _ai_worker_last_seen < 30),
        "control": "unity", "catalog_size": len(CATALOG),
        "gameplay_rules": "two_player_submission_order_v1" if room.size == 2 else "legacy_multiplayer",
        "round_result": _round_result(room, room.current_round) if room.current_round else None,
        "round_results": [_round_result(room, r) for r in sorted(room.closed_rounds)],
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
    # Status counters reveal whether a worker is merely polling or actually
    # processing photographs. No keys, photos or player data are returned.
    with _lock:
        now = time.time()
        pending = [sub for room in _rooms.values() for sub in room.submissions
                   if sub.status == "analyzing"]
        result = {"status": "ok", "service": "snaprush", "version": app.version,
                  "ai_configured": _ai_configured(), "ai_provider": AI_PROVIDER,
                  "ai_worker_online": AI_PROVIDER == "local" and (now - _ai_worker_last_seen < 30),
                  "ai_jobs_waiting": sum(now >= sub.ai_leased_until for sub in pending),
                  "ai_jobs_processing": sum(now < sub.ai_leased_until for sub in pending)}
        if AI_PROVIDER == "cloudflare":
            result["cloudflare_since_restart"] = {
                **{k: v for k, v in _cloudflare_stats.items() if k != "neurons"},
                "neurons": round(_cloudflare_stats["neurons"], 3),
            }
        return result


@app.post("/api/rooms")
def create_room(payload: RoomCreate):
    with _lock:
        room_id = secrets.token_hex(4).upper()
        while room_id in _rooms:
            room_id = secrets.token_hex(4).upper()
        order = _new_challenge_order(payload.rounds, payload.catalog_mode)
        room = Room(
            id=room_id, host_key=secrets.token_urlsafe(32),
            size=payload.players, rounds=payload.rounds, seconds=payload.seconds,
            active_seconds=payload.seconds,
            challenge_order=order,
            catalog_mode=payload.catalog_mode,
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
            match_index = CATALOG_INDEX.get(payload.target_id)
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
        if room.size == 2 and room.state != "finished" and not (
                room.current_round == room.rounds and room.state == "round_review" and
                _round_result(room, room.current_round)["finalized"]):
            raise HTTPException(409, "Wait for the final round and all AI results")
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
        room.result_sequence = 0
        room.round_winners.clear()
        room.closed_rounds.clear()
        room.slot_tokens = {i: secrets.token_urlsafe(24) for i in range(1, room.size + 1)}
        room.auto_token = secrets.token_urlsafe(24)
        room.challenge_order = _new_challenge_order(room.rounds, room.catalog_mode)
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
    # This function runs after HTTP upload succeeds so Unity sees the photo
    # immediately as `analyzing`. Never block upload while calling the AI.
    result = verify_image(jpg, requested_description)
    with _lock:
        if AI_PROVIDER == "cloudflare":
            _cloudflare_stats["completed"] += 1
            _cloudflare_stats[{"correct": "approved", "incorrect": "rejected",
                               "unverified": "unverified"}[result.status]] += 1
            _cloudflare_stats["neurons"] += getattr(result, "neurons", 0.0)
        room = _rooms.get(room_id)
        if room:
            sub = next((v for v in room.submissions if v.id == submission_id), None)
            if sub and sub.status == "analyzing":
                _resolve_submission(room, sub, result.status, result.reason, result.detected_object)


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
        target = room.current_target()
        submission = Submission(id=secrets.token_hex(16), slot=player.slot,
                                round_no=room.current_round, created_at=time.time(),
                                target_description=target[3])
        (MEDIA_DIR / f"{room.id}_{submission.id}.jpg").write_bytes(jpg)
        room.submissions.append(submission)
        if not _ai_configured():
            _resolve_submission(room, submission, "unverified", "AI verification not configured")
        elif AI_PROVIDER == "local":
            # The local worker will claim this queued submission via authenticated HTTPS polling.
            # Do NOT try connecting from Render to localhost or Ollama's port 11434.
            pass
        else:
            background_tasks.add_task(_analyze_photo, room.id, submission.id, jpg, target[3])
        return {"ok": True, "submission_id": submission.id, "status": submission.status,
                "photo_url": f"/media/{room.id}/{submission.id}.jpg", "slot": player.slot}


# Authenticated pull-queue for a desktop-based Ollama worker.
# All requests are outgoing from the PC to Render; no inbound home network access.
def _require_ai_worker(x_ai_worker_key: str) -> None:
    expected = os.getenv("SNAPRUSH_AI_WORKER_KEY", "")
    if AI_PROVIDER != "local":
        raise HTTPException(404, "Local AI mode disabled")
    if len(expected) < 32 or not x_ai_worker_key or not secrets.compare_digest(x_ai_worker_key, expected):
        raise HTTPException(401, "Invalid AI worker credentials")


class AIWorkerResult(BaseModel):
    claim_id: str
    status: Literal["correct", "incorrect", "unverified"]
    reason: str = Field(default="", max_length=160)
    detected_object: str = Field(default="", max_length=90)


@app.get("/api/ai/jobs/next")
def ai_next_job(x_ai_worker_key: str = Header(default="")):
    global _ai_worker_last_seen
    _require_ai_worker(x_ai_worker_key)
    with _lock:
        now = time.time()
        _ai_worker_last_seen = now
        # Search by original receipt time, not claim time, to preserve fairness.
        queued = []
        for room in _rooms.values():
            if now - room.created_at > ROOM_TTL:
                continue
            _advance(room)
            for sub in room.submissions:
                if sub.status == "analyzing" and now >= sub.ai_leased_until:
                    queued.append((sub.created_at, room, sub))
        queued.sort(key=lambda value: value[0])
        for _, room, sub in queued:
            image_path = MEDIA_DIR / f"{room.id}_{sub.id}.jpg"
            try:
                # Keep the original 1024px server photo for Unity display.
                # Send a smaller 640px variant only to local Ollama, to reduce
                # vision tokens and processing time on the RTX 3060.
                with Image.open(image_path) as img:
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((640, 640), Image.Resampling.LANCZOS)
                    ai_buf = BytesIO()
                    img.convert("RGB").save(ai_buf, "JPEG", quality=78)
                    ai_jpg = ai_buf.getvalue()
            except (OSError, ValueError, UnidentifiedImageError):
                _resolve_submission(room, sub, "unverified", "Photo unavailable for AI verification")
                continue
            sub.ai_claim = secrets.token_urlsafe(32)
            sub.ai_leased_until = now + AI_CLAIM_SECONDS
            return {"job": {
                "room_id": room.id, "submission_id": sub.id,
                "claim_id": sub.ai_claim, "target_description": sub.target_description,
                "image_base64": base64.b64encode(ai_jpg).decode("ascii"),
            }}
        return {"job": None}


@app.post("/api/ai/jobs/{room_id}/{submission_id}/result")
def ai_submit_result(room_id: str, submission_id: str, result: AIWorkerResult,
                     x_ai_worker_key: str = Header(default="")):
    global _ai_worker_last_seen
    _require_ai_worker(x_ai_worker_key)
    with _lock:
        _ai_worker_last_seen = time.time()
        room = _get_room(room_id)
        sub = next((s for s in room.submissions if s.id == submission_id), None)
        if sub is None:
            raise HTTPException(404, "Unknown photo job")
        if sub.status != "analyzing" or not sub.ai_claim or not secrets.compare_digest(sub.ai_claim, result.claim_id):
            raise HTTPException(409, "Job already completed or claim expired")
        if time.time() > sub.ai_leased_until:
            raise HTTPException(409, "AI worker claim expired; result ignored")
        _resolve_submission(room, sub, result.status,
                            result.reason or ("Object verified" if result.status == "correct" else "Not verified"),
                            result.detected_object)
        return {"ok": True, "status": sub.status, "award": sub.award}


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
def get_catalog(category: Optional[str] = None, difficulty: Optional[str] = None,
                kind: Optional[str] = None, offset: int = 0, limit: int = 5000):
    """Bilingual photo targets; only physical objects, never color variants."""
    if offset < 0 or not 1 <= limit <= 5000:
        raise HTTPException(422, "offset must be >=0 and limit must be 1..5000")
    filtered = [item for item in CATALOG_ITEMS if
                (not category or item["category"] == category) and
                (not difficulty or item["difficulty"] == difficulty) and
                (not kind or item["kind"] == kind)]
    result = []
    for item in filtered[offset:offset + limit]:
        result.append({k: item[k] for k in ("id", "ar", "en", "category", "difficulty", "kind", "base_id")})
    return {"count": len(filtered), "items": result, "offset": offset, "limit": limit,
            "total_catalog": len(CATALOG_ITEMS)}


@app.get("/api/catalog/stats")
def catalog_stats():
    categories = {}
    for item in CATALOG_ITEMS:
        categories[item["category"]] = categories.get(item["category"], 0) + 1
    return {"total_challenges": len(CATALOG_ITEMS),
            "base_object_types": CATALOG_DATA["base_object_count"],
            "color_variants": CATALOG_DATA["variant_count"],
            "distinct_object_target": CATALOG_DATA["target_distinct_objects"],
            "categories": categories,
            "note": CATALOG_DATA["note"]}

