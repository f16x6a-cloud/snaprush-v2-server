from io import BytesIO
import os
from PIL import Image
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_health_and_page():
    assert client.get('/health').json()['status'] == 'ok'
    assert client.get('/').status_code == 200
    p = client.get('/join')
    assert p.status_code == 200
    assert 'افتح الكاميرا الأمامية' in p.text
    js = client.get('/static/player.js').text
    assert "facing='user'" in js


def test_two_players_join_ready_photo_unverified(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    a = client.post('/api/rooms', json={'players': 2, 'rounds': 3, 'seconds': 30})
    assert a.status_code == 200, a.text
    room = a.json()
    room_id, host_key = room['room_id'], room['host_key']
    qr_result = client.post(f'/api/rooms/{room_id}/host/qr', json={'host_key': host_key})
    assert qr_result.status_code == 200
    qr = qr_result.json()
    assert qr['qr']['1']['path'] != qr['qr']['2']['path']
    assert qr['qr']['1']['image'].startswith('data:image/png;base64,')
    tokens = [qr['qr'][str(s)]['path'].split('token=')[-1] for s in [1, 2]]
    joiners = []
    for slot, token in enumerate(tokens, 1):
        r = client.post('/api/join', json={'room_id': room_id, 'token': token})
        assert r.status_code == 200, r.text
        assert r.json()['slot'] == slot
        joiners.append(r.json())
        assert client.post('/api/ready', json={'room_id': room_id, 'session': r.json()['session']}).status_code == 200
    repeat = client.post('/api/join', json={'room_id':room_id, 'token': tokens[0]})
    assert repeat.status_code == 409
    state = client.get(f'/api/rooms/{room_id}/state',headers={'x-host-key':host_key}).json()
    assert state['state'] == 'countdown'
    assert state['ready_count'] == 2
    # Simulate passing the countdown without sleeping in test.
    import main
    with main._lock:
        main._rooms[room_id].start_at = 0.001
        main._rooms[room_id].deadline = 0.0
        main._rooms[room_id].state = 'countdown'
    state = client.get(f'/api/rooms/{room_id}/state',headers={'x-host-key':host_key}).json()
    # Deadline now past due to artificially old countdown, so set new deadline.
    with main._lock:
        main._rooms[room_id].state = 'round_active'
        main._rooms[room_id].deadline = __import__('time').time() + 25
    state = client.get(f'/api/rooms/{room_id}/state',headers={'x-host-key':host_key}).json()
    assert state['state'] == 'round_active'
    image = Image.new('RGB',(200,200),(123,32,99)); buf=BytesIO(); image.save(buf,'PNG')
    posted = client.post(f'/api/rooms/{room_id}/photos', data={'session':joiners[0]['session']}, files={'file':('photo.png', buf.getvalue(), 'image/png')})
    assert posted.status_code == 200, posted.text
    assert posted.json()['slot'] == 1
    assert posted.json()['status'] == 'unverified'
    visible = client.get(f'/api/rooms/{room_id}/state', headers={'x-host-key':host_key}).json()
    assert len(visible['photos']) == 1
    assert visible['photos'][0]['slot'] == 1
    assert visible['photos'][0]['status'] == 'unverified'
    assert visible['slots'][0]['points'] == 0
    assert client.get(visible['photos'][0]['photo_url']).status_code == 200
    assert client.get(f'/api/rooms/{room_id}/state').status_code == 401
    assert client.post(f'/api/rooms/{room_id}/reset',json={'host_key':'wrong'}).status_code == 401
    assert client.post(f'/api/rooms/{room_id}/reset',json={'host_key':host_key}).status_code == 200
    reset_state=client.get(f'/api/rooms/{room_id}/state',headers={'x-host-key':host_key}).json()
    assert reset_state['connected'] == 0


def test_ranking_uses_submission_received_order_even_if_ai_finishes_reverse():
    import main
    import time
    room_id=client.post('/api/rooms',json={'players':2,'rounds':1,'seconds':60}).json()['room_id']
    with main._lock:
        room=main._rooms[room_id]
        room.players[1]=main.Player(slot=1,session='player-1')
        room.players[2]=main.Player(slot=2,session='player-2')
        room.state='round_active'
        room.current_round=1
        room.deadline=time.time()+60
        first=main.Submission(id='a',slot=1,round_no=1,created_at=time.time()-2,status='analyzing')
        second=main.Submission(id='b',slot=2,round_no=1,created_at=time.time()-1,status='correct')
        room.submissions=[first,second]
        main._recompute_scores(room)
        assert room.players[2].points==100
        first.status='correct' # First submission AI finishes second.
        main._recompute_scores(room)
        assert room.players[1].points==100
        assert room.players[2].points==70
        assert first.award==100 and second.award==70
