# SNAP RUSH — Mobile Camera Server (Render Free)

This project is a two-phone **prototype** for SNAP RUSH. The previous Hugging Face Space is untouched.

## Deploy FREE on Render (no Hugging Face Docker required)

1. Create a NEW empty GitHub repository, such as `snaprush-v2-server`. Do not put secrets in it.
2. Upload the CONTENTS of this package (not the enclosing folder) to the root of that repo: `main.py`, `requirements.txt`, `render.yaml`, `static/`, etc.
3. Visit https://dashboard.render.com/ and sign in with GitHub.
4. New > Web Service > Connect the new GitHub repository.
5. Choose Python runtime and a **Free** instance. Set:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers`
   - Health Check Path (optional): `/health`
6. Deploy. Wait for a URL such as `https://your-service.onrender.com`.
7. Open the service homepage on your PC. Use the host panel to create a two-player room, and scan each player's individual QR with two phones.
8. Optionally set `APP_BASE_URL` to the exact Render HTTPS service URL in Render Environment settings, and redeploy. Without this setting, generated QR links use the forwarded request URL.
9. To enable REAL AI photo matching, add `OPENAI_API_KEY` in Render Environment, NOT in GitHub; OpenAI API usage may have a separate cost.

**DO NOT select Static Site:** this FastAPI application needs a backend. Choose **Web Service (Free)**.

## What works (prototype)

- 2–6 slot rooms, individual QR per player, general join QR (no names)
- Ready synchronization and a 3-second start countdown
- Random object challenges and timer
- Mobile **front-facing camera** first with flip button
- Photo appears immediately after upload; AI check may complete afterward
- First valid submission gets 100 points, second 70, third 40, fourth 20
- Unity integration possible by polling the documented HTTP APIs
- Images are stored temporarily, and AI results require an optional provider API key

## Important limitations on free Render

- Service sleeps after ~15 minutes without incoming traffic and may take about one minute to wake.
- The app currently stores player/room state only in RAM and photos on ephemeral local disk. Reboots, sleep, and redeploys erase live rooms and uploads.
- This is a testing prototype, **not production**; persistence, stronger upload authorization, moderation/retention, and scalable real-time multiplayer still need implementation.
- WebSocket service isn't implemented in this V1; Unity should poll the state API.
- Free service has monthly usage limits. Check Render's current terms and quotas.

## Local test

```bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 7860
```

Then open http://127.0.0.1:7860. Phone camera use on a different device typically requires HTTPS.

## Unity API quick reference

- POST `/api/rooms` JSON `{ "players": 2, "rounds": 5, "seconds": 60 }` returns `room_id` and private `host_key`.
- POST `/api/rooms/{room_id}/host/qr` JSON `{ "host_key": "..." }` returns distinct QR images and a general join URL.
- GET `/api/rooms/{room_id}/state` with header `x-host-key: ...` returns status, player slots, timer, target, photos, and scores.
- POST `/api/join` assigns the scanning phone automatically to its QR's unique slot.
- POST `/api/ready` sets a player's ready state.
- POST `/api/rooms/{room_id}/photos` multipart file + session receives the photo immediately.
- GET `/health` verifies process health.

**Security:** Never commit secrets or private player links. Use test photos until authenticated media access and deletion/retention are implemented.
