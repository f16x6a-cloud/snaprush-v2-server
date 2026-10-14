# SNAP RUSH — Camera-Only Website (Unity Lobby Controls)

New user experience: the *mobile website* is only a secure(ish) QR-based camera companion. No names, Ready, round count, round timer, or host controls on the public website. The **Unity lobby** owns all those actions through host-authenticated API requests.

## Files to deploy

Replace the matching files at your GitHub repo `f16x6a-cloud/snaprush-v2-server` with the content of this package. Keep folder structure. GitHub automatically redeploys Render when you commit to `main`.

- `main.py` FastAPI server
- `static/host.html` public waiting/QR instruction page (no settings)
- `static/player.html` mobile camera player screen
- `static/player.js` camera + upload, no Ready button or round control
- `static/style.css` new SNAP RUSH theme
- `tests/test_flow.py` API and mobile UI tests
- `render.yaml`, `requirements.txt` Render deployment configuration

Delete the obsolete `static/host.js` from GitHub; it is not used.

## Actual workflow

1. Unity uses `POST /api/rooms` to create a lobby, requests personalized slot QR codes from `POST /api/rooms/{id}/host/qr` (include `host_key` in JSON). Keep host_key private in Unity; **never** send it to players' phones or include it in QR codes.
2. Phone scans `/join?room=...&token=...`. It automatically claims an unused slot without nickname, and only displays `PLAYER 01`, etc. Token is removed from the URL after successful join.
3. A user taps **Open Front Camera** to grant camera permission. HTTPS required (Render provides HTTPS). Front-facing camera is the default; switch/fallback camera is available.
4. Unity draws **Ready UI**, timer, rounds and target object on the lobby TV. When each player selects Ready in Unity, Unity calls `POST /api/rooms/{id}/host/player-ready` with `{"host_key":"...","slot":1,"ready":true}`.
5. Unity starts the round by calling `POST /api/rooms/{id}/host/start-round` with `{"host_key":"...","target_id":"mouse","seconds":60,"countdown_seconds":3}`. Must have at least two joined players ready for round 1. `target_id` is optional: the server picks from its shuffled catalog. Use `GET /api/catalog` to list IDs.
6. Phone's **Take & Send** button is enabled only when the server confirms `round_active`. Upload uses `POST /api/rooms/{id}/photos` (`session` form field + image `file`).
7. The server immediately returns `photo_url` and `slot`, while any AI request runs in the background. Unity polls `GET /api/rooms/{id}/state` using its private `x-host-key` header and maps `photos[].slot` to the correct character. Example: `photos[0].photo_url = /media/ROOMID/PHOTOID.jpg`. Prepend `https://snaprush-v2.onrender.com`. Show image above character **before** AI result, initially `analyzing`. Status later becomes `correct`, `incorrect`, or `unverified`.
8. At timeout the backend enters `round_review` but **does not start the next round**. Unity controls the transition by calling `/host/start-round` again after it has shown the results. For the end of the match Unity calls `POST /api/rooms/{id}/host/finish` with its host key. Configure rounds/times in Unity, not the website.

### Host-only API summary

| Method | Route | Unity action |
|---|---|---|
| POST | `/api/rooms` | Create room (returns `room_id`, `host_key`) |
| POST | `/api/rooms/{id}/host/qr` | Get slot QR image and path |
| GET | `/api/rooms/{id}/state` | Poll state and latest photo (header `x-host-key`) |
| POST | `/api/rooms/{id}/host/player-ready` | Mark player Ready from lobby |
| POST | `/api/rooms/{id}/host/start-round` | Start next round, challenge and timer |
| POST | `/api/rooms/{id}/host/finish` | End match |
| POST | `/api/rooms/{id}/reset` | Reset room (host key in body) |
| GET | `/api/catalog` | Get supported challenge IDs |

### Scoring / image AI

AI verification is **not enabled** unless `OPENAI_API_KEY` is set privately in Render environment variables. Optionally configure `OPENAI_VISION_MODEL`. Without it, the server still displays player photos immediately but marks submissions `unverified`, awarding **zero** points. Never commit an API key into GitHub.

Correct photos are awarded based on server upload receipt time: 100 / 70 / 40 / 20 / 0 / 0 points.

### Prototype limitations

- Render Free may sleep after inactivity and does not retain local photos or RAM state over restarts. It is for temporary two-player tests, **not production multiplayer**.
- In-memory rooms require exactly **one Python worker** (`render.yaml` currently specifies `--workers 1`).
- Images are served via unguessable paths rather than full photo ACL. Avoid personal/sensitive photos during tests; consider object storage, authenticated media and short retention before release.
- Unity client integration is an **API contract only**; this package does not modify Unity scenes or source code.
- The public website does not and cannot force the camera permission prompt until the user taps a button in a secure HTTPS context.

## Test locally

`pip install -r requirements.txt pytest`

`pytest -q`

`uvicorn main:app --host 127.0.0.1 --port 8000`
