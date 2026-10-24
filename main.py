"""SNAP RUSH — Unity-controlled Camera Service.

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

AI_PROVIDER = os.getenv("SNAPRUSH_AI_PROVIDER", "openai").strip().lower()
AI_PENDING_TIMEOUT = 240
AI_CLAIM_SECONDS = 180


def _ai_configured() -> bool:
    if AI_PROVIDER == "local":
        return len(os.getenv("SNAPRUSH_AI_WORKER_KEY", "")) >= 32
    if AI_PROVIDER == "cloudflare":
        return bool(os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip() and
                    os.getenv("CLOUDFLARE_API_TOKEN", "").strip())
    if AI_PROVIDER == "openai":
        return bool(os.getenv("OPENAI_API_KEY"))
    return False


CATALOG_DATA = json.loads((ROOT / "data" / "objects.json").read_text(encoding="utf-8"))
CATALOG_ITEMS = CATALOG_DATA["items"]
CATALOG = [(c["id"], c["ar"], c["en"], c["ai_description"]) for c in CATALOG_ITEMS]
CATALOG_INDEX = {item["id"]: i for i, item in enumerate(CATALOG_ITEMS)}
assert len(CATALOG) == len(CATALOG_INDEX)
assert all(item.get("kind") == "object" and not item.get("color") for item in CATALOG_ITEMS)


def _new_challenge_order(rounds: int, mode: str = "mixed") -> list[int]:
    """Draw randomly from the entire objects catalog without restriction."""
    eligible_indices = list(range(len(CATALOG_ITEMS)))
    if not eligible_indices:
        return []
    rounds_to_pick = min(max(1, rounds), len(eligible_indices))
    return random.SystemRandom().sample(eligible_indices, rounds_to_pick)


app = FastAPI(title="SNAP RUSH — Unity-controlled Camera Service", version="0.2.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
_lock = threading.RLock()
_rooms: dict[str, "Room"] = {}
_ai_worker_last_seen: float = 0.0

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
class DesktopMember:
    slot: int
    key: str
    name: str
    request_id: str = ""
    last_seen: float = field(default_factory=time.time)


@dataclass
class Submission:
    id: str
    slot: int
    round_no: int
    created_at: float
    status: str = "analyzing"
    reason: str = ""
    award: int = 0
    detected_object: str = ""
    target_description: str = ""
    ai_claim: str = ""
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
    room_code: str = ""
    scoring_mode: str = "WinnerTakesAll"
    multiplayer: bool = False
    creation_request: str = ""
    members: dict[int, DesktopMember] = field(default_factory=dict)
    is_public: bool = False
    room_name: str = ""

    def current_target(self) -> Optional[tuple[str, str, str, str]]:
        if not self.current_round:
            return None
        return CATALOG[self.challenge_order[self.current_round - 1]]


class RoomCreate(BaseModel):
    players: int = Field(default=2, ge=2, le=6)
    rounds: int = Field(default=5, ge=1, le=15)
    seconds: int = Field(default=60, ge=15, le=120)
    catalog_mode: Literal["mixed", "objects", "all"] = "mixed"
    player_capacity: Optional[int] = Field(default=None, ge=2, le=4)
    scoring_mode: Optional[Literal["WinnerTakesAll", "RankedTiered"]] = None
    round_count: Optional[int] = Field(default=None, ge=1, le=15)
    duration: Optional[int] = Field(default=None, ge=15, le=120)
    unity_host: bool = False
    is_public: bool = True
    player_name: str = Field(default="", max_length=24)
    room_name: str = Field(default="", max_length=48)
    request_id: str = Field(default="", max_length=64)


class ClientJoin(BaseModel):
    room_code: str = Field(pattern=r"^[0-9]{6}$")
    player_name: str = Field(default="", max_length=24)
    request_id: str = Field(min_length=16, max_length=64)


class ClientRequest(BaseModel):
    player_key: str


class ClientReady(ClientRequest):
    ready: bool


class SkipRequest(BaseModel):
    slot: int = Field(ge=1, le=6)
    round: int = Field(ge=1)
    player_key: str = ""  # Unity credential; phones keep using their x-session header.
    host_key: str = ""  # Shared Unity host display may explicitly forfeit any paired slot.


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
    if room.state == "closed":
        raise HTTPException(410, "The host closed this room; create or join another room")
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


def _auth_client(room: Room, key: str) -> DesktopMember:
    for member in room.members.values():
        if key and secrets.compare_digest(member.key, key):
            member.last_seen = time.time()
            return member
    raise HTTPException(401, "Invalid Unity player session")


def _recompute_scores(room: Room) -> None:
    for p in room.players.values():
        p.points = 0
    for sub in room.submissions:
        sub.award = 0
    for r in range(1, room.current_round + 1):
        current = [sub for sub in room.submissions if sub.round_no == r]
        pending = any(sub.status == "analyzing" for sub in current)
        latest = {sub.slot: sub for sub in current}
        finalizable = not pending and (r in room.closed_rounds or
            (r == room.current_round and room.state == "round_active" and
             all(slot in latest for slot in range(1, room.size + 1))))
        if not finalizable:
            room.round_winners.pop(r, None)
            continue
        correct = sorted((sub for sub in current if sub.status == "correct"),
                         key=lambda sub: (sub.created_at, sub.id))
        room.round_winners[r] = correct[0].slot if correct else 0
        rewarded: set[int] = set()
        for sub in correct:
            if sub.slot in rewarded:
                continue
            rank = len(rewarded)
            rewarded.add(sub.slot)
            sub.award = (100 if rank == 0 else 0) if room.scoring_mode == "WinnerTakesAll" else POINTS[rank]
            if sub.slot in room.players:
                room.players[sub.slot].points += sub.award


def _mark_round_ended(room: Room, now: float, reason: str) -> None:
    if room.state != "round_active":
        return
    room.state = "round_review"
    room.review_end = now + 5
    for player in room.players.values():
        player.ready = False
    room.closed_rounds[room.current_round] = {
        "start_at": room.start_at, "deadline": room.deadline,
        "ended_at": min(now, room.deadline) if reason == "timer" else now,
        "end_reason": reason,
    }
    _recompute_scores(room)


def _all_players_resolved(room: Room) -> bool:
    latest = {sub.slot: sub for sub in room.submissions if sub.round_no == room.current_round}
    return all(slot in latest and latest[slot].status in {"correct", "incorrect", "unverified", "skipped"}
               for slot in range(1, room.size + 1))


def _close_two_player_round_if_ready(room: Room, now: float) -> None:
    # Kept as an alias for integrations; now covers every configured slot.
    if room.state != "round_active":
        return
    if now >= room.deadline:
        _mark_round_ended(room, now, "timer")
        return
    if _all_players_resolved(room):
        _mark_round_ended(room, now, "both_resolved" if room.size == 2 else "all_resolved")


def _resolve_submission(room: Room, sub: Submission, status: str, reason: str,
                        detected_object: str = "") -> bool:
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
    if room.state == "round_review" and not any(
            s.status == "analyzing" for s in room.submissions if s.round_no == room.current_round):
        room.review_end = max(room.review_end, sub.resolved_at + 5)
    return True


def _round_result(room: Room, round_no: int) -> dict:
    current = [sub for sub in room.submissions if sub.round_no == round_no]
    latest = {sub.slot: sub for sub in current}
    closed = room.closed_rounds.get(round_no, {})
    start = closed.get("start_at", room.start_at if round_no == room.current_round else 0)
    winner = room.round_winners.get(round_no, 0)
    finalized = bool(closed) and not any(sub.status == "analyzing" for sub in current)
    approved = sorted((s for s in current if s.status == "correct"), key=lambda s: (s.created_at, s.id))
    ranks = {s.slot: i + 1 for i, s in enumerate(approved)} if finalized else {}
    rows = []
    for slot in range(1, room.size + 1):
        sub = latest.get(slot)
        photo = sub is not None and sub.status != "skipped"
        rows.append({
            "slot": slot, "submitted": photo,
            "submission_id": sub.id if photo else "",
            "photo_url": f"/media/{room.id}/{sub.id}.jpg" if photo else "",
            "status": sub.status if sub else "missing",
            "reason": sub.reason if sub else "No photo submitted",
            "submitted_at": sub.created_at if photo else 0,
            "submitted_seconds": max(0, sub.created_at - start) if photo else 0,
            "skipped_at": sub.created_at if sub and sub.status == "skipped" else 0,
            "approved": bool(sub and sub.status == "correct"),
            "score": sum(s.award for s in current if s.slot == slot),
            "resolved_at": sub.resolved_at if sub else 0,
            "resolution_order": sub.resolution_order if sub else 0,
            "round_points": sum(s.award for s in current if s.slot == slot),
            "rank": ranks.get(slot, 0),
        })
    return {
        "round": round_no, "winner_slot": winner, "finalized": finalized, "scoring_mode": room.scoring_mode,
        "ended_at": closed.get("ended_at", 0),
        "end_reason": closed.get("end_reason", ""),
        "results_ready_at": max([closed.get("ended_at", 0)] +
                                [s.resolved_at for s in current]) if finalized else 0,
        "players": rows,
    }


def _advance(room: Room) -> None:
    now = time.time()
    for sub in room.submissions:
        if sub.status == "analyzing" and now - sub.created_at > AI_PENDING_TIMEOUT:
            _resolve_submission(room, sub, "unverified", "AI analysis timed out; please retry")
    if room.state in {"waiting", "round_review"}:
        for slot, member in room.members.items():
            if now - member.last_seen > 10 and slot in room.players:
                room.players[slot].ready = False
    if room.state == "countdown" and now >= room.start_at:
        room.state = "round_active"
        room.deadline = room.start_at + room.active_seconds
    if room.state == "round_active" and now >= room.deadline:
        _mark_round_ended(room, now, "timer")
    _close_two_player_round_if_ready(room, now)


def _qr_data(url: str) -> str:
    img = qrcode.make(url, box_size=5, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _base_url() -> str:
    return APP_BASE_URL


def _snapshot(room: Room, is_host: bool, current_session: str = "", desktop_slot: int = 0) -> dict:
    _advance(room)
    current = room.current_target()
    current_subs = [s for s in room.submissions if s.round_no == room.current_round]
    actions = {s.slot: s for s in current_subs}
    slots = []
    for i in range(1, room.size + 1):
        p = room.players.get(i)
        member = room.members.get(i)
        entry = {"slot": i, "occupied": bool(p or member), "ready": p.ready if p else False, "points": p.points if p else 0,
                 "phone_connected": bool(p), "desktop_claimed": bool(member),
                 "desktop_connected": bool(member and time.time() - member.last_seen <= 10),
                 "player_name": member.name if member else f"PLAYER {i}",
                 "round_status": actions[i].status if i in actions else "missing",
                 "round_resolved": i in actions and actions[i].status in {"correct", "incorrect", "unverified", "skipped"}}
        if (is_host or desktop_slot == i) and not p and room.state == "waiting":
            entry["join_path"] = f"/join?room={room.id}&token={room.slot_tokens[i]}"
        slots.append(entry)
    latest = {}
    for s in current_subs:
        if s.status == "skipped":
            continue  # A skip has no image file, media URL or AI job.
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
    current_result = _round_result(room, room.current_round) if room.current_round else None
    next_round_ready = (room.state == "round_review" and room.current_round < room.rounds
                        and time.time() >= room.review_end
                        and current_result is not None and current_result["finalized"])
    result = {
        "room_id": room.id, "room_code": room.room_code, "room_name": room.room_name, "state": room.state, "capacity": room.size,
        "scoring_mode": room.scoring_mode,
        "connected": len(set(room.players) | set(room.members)), "ready_count": sum(p.ready for p in room.players.values()),
        "ready_phase": "next_round" if next_round_ready else ("lobby" if room.state == "waiting" else "none"),
        "can_ready_next_round": next_round_ready,
        "skip_enabled": True,
        "host_skip_enabled": True,
        "review_end": room.review_end,
        "rounds": room.rounds, "seconds": room.active_seconds,
        "round": room.current_round, "start_at": room.start_at,
        "deadline": room.deadline, "server_now": time.time(),
        "target": {"id": current[0], "ar": current[1], "en": current[2],
                   "category": CATALOG_ITEMS[CATALOG_INDEX[current[0]]].get("category", "")} if current and room.state in ("round_active", "round_review", "finished") else None,
        "slots": slots, "photos": list(latest.values()),
        "ai_enabled": _ai_configured(),
        "ai_provider": AI_PROVIDER,
        "ai_worker_online": AI_PROVIDER == "local" and (time.time() - _ai_worker_last_seen < 30),
        "control": "unity", "catalog_size": len(CATALOG),
        "gameplay_rules": "multiplayer_submission_order_v2" if room.multiplayer else ("two_player_submission_order_v1" if room.size == 2 else "legacy_multiplayer"),
        "round_result": current_result,
        "round_results": [_round_result(room, r) for r in sorted(room.closed_rounds)],
    }
    if is_host:
        result["general_join_path"] = f"/join?room={room.id}&token={room.auto_token}"
        result["max_rounds"] = room.rounds
    elif desktop_slot:
        result["my_slot"] = desktop_slot
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
        result["capabilities"] = ["multiplayer_lobby_v1", "skip_round_v1", "host_skip_v1", "room_browser_v1"]
        return result


def _client_response(room: Room, member: DesktopMember, host: bool = False) -> dict:
    result = {"room_id": room.id, "room_code": room.room_code, "room_name": room.room_name, "slot": member.slot, "player_key": member.key}
    if host:
        result["host_key"] = room.host_key
    return result


@app.get("/api/rooms")
def list_public_rooms(response: Response):
    """Public lobby metadata only. Never expose pairing/session/host credentials."""
    response.headers["Cache-Control"] = "no-store"
    with _lock:
        now = time.time()
        rooms = []
        for room in sorted(_rooms.values(), key=lambda r: r.created_at, reverse=True):
            host = room.members.get(1)
            if (not room.multiplayer or not room.is_public or room.state != "waiting"
                    or now - room.created_at > ROOM_TTL or not host or now - host.last_seen > 30
                    or len(room.members) >= room.size):
                continue
            rooms.append({"room_code": room.room_code, "room_name": room.room_name, "host_name": host.name,
                          "player_count": len(room.members), "player_capacity": room.size,
                          "scoring_mode": room.scoring_mode, "round_count": room.rounds,
                          "duration": room.seconds})
            if len(rooms) == 100:
                break
        return {"rooms": rooms}


@app.post("/api/rooms")
def create_room(payload: RoomCreate):
    with _lock:
        if payload.request_id:
            for existing in _rooms.values():
                if existing.creation_request == payload.request_id and time.time() - existing.created_at <= ROOM_TTL:
                    if existing.state == "closed" or 1 not in existing.members:
                        raise HTTPException(409, "This creation request belongs to a closed room; start a new session")
                    return _client_response(existing, existing.members[1], True)
        size = payload.player_capacity or payload.players
        rounds = payload.round_count or payload.rounds
        seconds = payload.duration or payload.seconds
        if payload.unity_host and (payload.player_capacity is None or payload.scoring_mode is None):
            raise HTTPException(422, "Unity rooms require player_capacity and scoring_mode")
        room_id = secrets.token_hex(4).upper()
        while room_id in _rooms:
            room_id = secrets.token_hex(4).upper()
        active_codes = {r.room_code for r in _rooms.values() if time.time() - r.created_at <= ROOM_TTL}
        code = f"{secrets.randbelow(1000000):06d}"
        while code in active_codes:
            code = f"{secrets.randbelow(1000000):06d}"
        room = Room(id=room_id, host_key=secrets.token_urlsafe(32), size=size, rounds=rounds, seconds=seconds,
                    active_seconds=seconds, challenge_order=_new_challenge_order(rounds, payload.catalog_mode),
                    catalog_mode=payload.catalog_mode, slot_tokens={i: secrets.token_urlsafe(24) for i in range(1, size + 1)},
                    room_code=code, scoring_mode=payload.scoring_mode or ("WinnerTakesAll" if size == 2 else "RankedTiered"),
                    multiplayer=payload.player_capacity is not None, creation_request=payload.request_id if payload.unity_host else "",
                    is_public=payload.unity_host and payload.is_public,
                    room_name=" ".join(payload.room_name.split()) or
                        (("غرفة " if any("\u0600" <= c <= "\u06ff" for c in payload.player_name) else "Room - ") +
                         (payload.player_name.strip() or "PLAYER 1")))
        _rooms[room_id] = room
        if payload.unity_host:
            member = DesktopMember(1, secrets.token_urlsafe(32), payload.player_name.strip() or "PLAYER 1", payload.request_id)
            room.members[1] = member
            return _client_response(room, member, True)
        return {"room_id": room.id, "host_key": room.host_key, "room_code": code}


@app.post("/api/rooms/join-client")
def join_client(payload: ClientJoin):
    with _lock:
        room = next((r for r in _rooms.values() if r.room_code == payload.room_code and time.time() - r.created_at <= ROOM_TTL), None)
        if room is None:
            raise HTTPException(404, "Room code not found or expired")
        for member in room.members.values():
            if member.request_id == payload.request_id:
                member.last_seen = time.time()
                return _client_response(room, member)
        if not room.multiplayer or room.state != "waiting":
            raise HTTPException(409, "Unity players can only join an open multiplayer lobby")
        slot = next((i for i in range(1, room.size + 1) if i not in room.members), None)
        if slot is None:
            raise HTTPException(409, "All Unity player slots are claimed")
        member = DesktopMember(slot, secrets.token_urlsafe(32), payload.player_name.strip() or f"PLAYER {slot}", payload.request_id)
        room.members[slot] = member
        if slot in room.players:
            room.players[slot].ready = False
        return _client_response(room, member)


@app.get("/api/rooms/{room_id}/state")
def room_state(room_id: str, x_session: str = Header(default=""), x_host_key: str = Header(default=""),
               x_player_key: str = Header(default="")):
    with _lock:
        # Authenticate and refresh the caller's heartbeat before advancing readiness.
        room = _rooms.get(room_id)
        if room is None:
            raise HTTPException(404, "Room not found or expired")
        is_host = bool(x_host_key and secrets.compare_digest(room.host_key, x_host_key))
        member = None
        if is_host and 1 in room.members:
            room.members[1].last_seen = time.time()
        elif x_player_key:
            member = _auth_client(room, x_player_key)
        room = _get_room(room_id)
        return _snapshot(room, is_host, x_session, member.slot if member else 0)


def _validate_ready_phase(room: Room):
    if room.state == "waiting":
        return
    if room.state == "round_review" and room.current_round < room.rounds:
        if time.time() >= room.review_end and _round_result(room, room.current_round)["finalized"]:
            return
    raise HTTPException(409, "Ready is only available in the lobby or after a non-final round review")


@app.post("/api/rooms/{room_id}/player-ready")
def client_ready(room_id: str, payload: ClientReady):
    with _lock:
        room = _get_room(room_id)
        member = _auth_client(room, payload.player_key)
        _validate_ready_phase(room)
        player = room.players.get(member.slot)
        if player is None:
            raise HTTPException(409, "Pair this player's phone before Ready")
        player.ready = payload.ready
        return _snapshot(room, False, desktop_slot=member.slot)


@app.post("/api/rooms/{room_id}/client-qr")
def client_qr(room_id: str, payload: ClientRequest, request: Request):
    with _lock:
        room = _get_room(room_id)
        member = _auth_client(room, payload.player_key)
        images = {}
        if member.slot not in room.players and room.state == "waiting":
            path = f"/join?room={room.id}&token={room.slot_tokens[member.slot]}"
            host = _base_url() or str(request.base_url).rstrip("/")
            images[str(member.slot)] = {"path": path, "image": _qr_data(host + path)}
        return {"qr": images}


@app.post("/api/rooms/{room_id}/client-leave")
def client_leave(room_id: str, payload: ClientRequest):
    with _lock:
        room = _get_room(room_id)
        member = _auth_client(room, payload.player_key)
        del room.members[member.slot]
        if member.slot == 1:
            room.state = "closed"
            for p in room.players.values():
                p.ready = False
            return {"ok": True, "closed": True}
        player = room.players.get(member.slot)
        if player:
            player.ready = False
        if room.state == "waiting":
            room.players.pop(member.slot, None)
            room.slot_tokens[member.slot] = secrets.token_urlsafe(24)
        return {"ok": True}


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
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        if room.multiplayer and payload.slot in room.members and payload.slot != 1:
            raise HTTPException(403, "A Unity player controls their own Ready state")
        if room.state == "waiting":
            pass
        elif room.state == "round_review" and room.current_round < room.rounds:
            result = _round_result(room, room.current_round)
            if time.time() < room.review_end or not result["finalized"]:
                raise HTTPException(409, "Wait for the round results before marking Ready")
        else:
            raise HTTPException(409, "Ready is only available in the lobby or between rounds")
        p = room.players.get(payload.slot)
        if p is None:
            raise HTTPException(404, "Player slot has not joined")
        p.ready = payload.ready
        return _snapshot(room, True)


@app.post("/api/rooms/{room_id}/host/start-round")
def start_round(room_id: str, payload: HostStartRound):
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        if room.state not in {"waiting", "round_review"}:
            raise HTTPException(409, "Finish or review the current round before starting another")
        if room.current_round >= room.rounds:
            raise HTTPException(409, "All configured rounds are complete")
        if room.state == "waiting":
            if len(room.players) != room.size or not all(p.ready for p in room.players.values()):
                raise HTTPException(409, "Every connected player must be Ready before the first round")
        else:
            last_result = _round_result(room, room.current_round)
            if not last_result["finalized"]:
                raise HTTPException(409, "Wait for pending image analysis before continuing")
            if time.time() < room.review_end:
                raise HTTPException(409, "Wait until the round result display finishes")
            if len(room.players) != room.size or not all(p.ready for p in room.players.values()):
                raise HTTPException(409, "All players must press Ready again before the next round")
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
    with _lock:
        room = _get_room(room_id)
        _auth_host(room, payload.host_key)
        if room.state != "finished" and not (
                room.current_round == room.rounds and room.state == "round_review" and
                _round_result(room, room.current_round)["finalized"] and time.time() >= room.review_end):
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
        if room.multiplayer:
            for p in room.players.values():
                p.ready, p.points = False, 0
        else:
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


@app.post("/api/rooms/{room_id}/skip")
def skip_round(room_id: str, payload: SkipRequest, x_session: str = Header(default=""),
               x_player_key: str = Header(default=""), x_host_key: str = Header(default="")):
    with _lock:
        room = _get_room(room_id)
        key = payload.player_key or x_player_key
        host_key = payload.host_key or x_host_key
        if payload.player_key and x_player_key and not secrets.compare_digest(payload.player_key, x_player_key):
            raise HTTPException(401, "Conflicting player credentials")
        if payload.host_key and x_host_key and not secrets.compare_digest(payload.host_key, x_host_key):
            raise HTTPException(401, "Conflicting host credentials")
        if sum(bool(k) for k in (key, host_key, x_session)) != 1:
            raise HTTPException(401, "Provide exactly one host key, Unity player key or phone session")
        if host_key:
            _auth_host(room, host_key)
        member = _auth_client(room, key) if key else None
        player = room.players.get(payload.slot if host_key else member.slot) if host_key or member else _auth_player(room, x_session)
        if player is None:
            raise HTTPException(409, "Pair this player's phone before skipping")
        if not host_key and payload.slot != player.slot:
            raise HTTPException(403, "A player can only skip their own slot")
        if payload.round != room.current_round:
            raise HTTPException(409, "Skip request is for a different round")
        existing = next((s for s in room.submissions if s.round_no == room.current_round and s.slot == player.slot), None)
        if not existing or existing.status != "skipped":
            if room.state != "round_active" or time.time() >= room.deadline:
                raise HTTPException(409, "There is no active round to skip")
            if existing:
                raise HTTPException(409, "A photo is already submitted; this round cannot be skipped")
            now = time.time()
            room.result_sequence += 1
            room.submissions.append(Submission(id=secrets.token_hex(16), slot=player.slot,
                round_no=room.current_round, created_at=now, status="skipped", reason="Player skipped this round",
                resolved_at=now, resolution_order=room.result_sequence))
            _recompute_scores(room)
            _close_two_player_round_if_ready(room, now)
        # Retrying an acknowledged skip is safe, including after it closed the round.
        return _snapshot(room, bool(host_key), current_session=x_session, desktop_slot=member.slot if member else 0)


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
        if any(s.round_no == room.current_round and s.slot == player.slot
               for s in room.submissions):
            raise HTTPException(409, "Only one photo is allowed per player per round")
        target = room.current_target()
        submission = Submission(id=secrets.token_hex(16), slot=player.slot,
                                round_no=room.current_round, created_at=time.time(),
                                target_description=target[3])
        (MEDIA_DIR / f"{room.id}_{submission.id}.jpg").write_bytes(jpg)
        room.submissions.append(submission)
        if not _ai_configured():
            _resolve_submission(room, submission, "unverified", "AI verification not configured")
        elif AI_PROVIDER == "local":
            pass
        else:
            background_tasks.add_task(_analyze_photo, room.id, submission.id, jpg, target[3])
        return {"ok": True, "submission_id": submission.id, "status": submission.status,
                "photo_url": f"/media/{room.id}/{submission.id}.jpg", "slot": player.slot}


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
        queued = []
        for room in _rooms.values():
            if room.state == "closed" or now - room.created_at > ROOM_TTL:
                continue
            _advance(room)
            for sub in room.submissions:
                if sub.status == "analyzing" and now >= sub.ai_leased_until:
                    queued.append((sub.created_at, room, sub))
        queued.sort(key=lambda value: value[0])
        for _, room, sub in queued:
            image_path = MEDIA_DIR / f"{room.id}_{sub.id}.jpg"
            try:
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
    if not (len(room_id) == 8 and len(submission_id) == 32 and all(c in "0123456789ABCDEF" for c in room_id)
            and all(c in "0123456789abcdef" for c in submission_id)):
        raise HTTPException(404, "Photo not found")
    with _lock:
        room = _get_room(room_id)
        if not any(s.id == submission_id for s in room.submissions):
            raise HTTPException(404, "Photo not found")
    media_root = MEDIA_DIR.resolve()
    path = (media_root / f"{room_id}_{submission_id}.jpg").resolve()
    try:
        path.relative_to(media_root)
    except ValueError:
        raise HTTPException(404, "Photo not found")
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
    if offset < 0 or not 1 <= limit <= 5000:
        raise HTTPException(422, "offset must be >=0 and limit must be 1..5000")
    filtered = [item for item in CATALOG_ITEMS if
                (not category or item.get("category") == category) and
                (not difficulty or item.get("difficulty") == difficulty) and
                (not kind or item.get("kind") == kind)]
    result = []
    for item in filtered[offset:offset + limit]:
        result.append({k: item.get(k) for k in ("id", "ar", "en", "category", "difficulty", "kind", "base_id")})
    return {"count": len(filtered), "items": result, "offset": offset, "limit": limit,
            "total_catalog": len(CATALOG_ITEMS)}


@app.get("/api/catalog/stats")
def catalog_stats():
    categories = {}
    for item in CATALOG_ITEMS:
        cat = item.get("category", "general")
        categories[cat] = categories.get(cat, 0) + 1
    return {
        "total_challenges": len(CATALOG_ITEMS),
        "base_object_types": CATALOG_DATA.get("base_object_count", len(CATALOG_ITEMS)),
        "color_variants": CATALOG_DATA.get("variant_count", 0),
        "distinct_object_target": CATALOG_DATA.get("target_distinct_objects", len(CATALOG_ITEMS)),
        "categories": categories,
        "note": CATALOG_DATA.get("note", "")
    }
