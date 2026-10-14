"""Local flow tests: mobile cannot control game; Unity host can."""
from io import BytesIO
import time
from PIL import Image
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)

def create_room():
    r=client.post('/api/rooms',json={'players':2,'rounds':3,'seconds':30})
    assert r.status_code==200,r.text
    return r.json()

def make_photo():
    img=Image.new('RGB',(300,300),(40,90,190));out=BytesIO();img.save(out,'PNG');return out.getvalue()

def test_site_only_camera_controls():
    r=client.get('/');assert r.status_code==200
    assert 'عدد الجولات' not in r.text
    assert 'إنشاء غرفة جديدة' not in r.text
    assert 'READY' not in r.text
    r=client.get('/join');assert r.status_code==200
    assert 'افتح الكاميرا الأمامية' in r.text
    assert 'أنا جاهز' not in r.text and 'id="ready"' not in r.text
    assert 'id="rounds"' not in r.text and 'id="timer"' not in r.text
    js=client.get('/static/player.js').text
    assert "facing = 'user'" in js
    assert "api('/api/ready'" not in js
    assert client.get('/health').json()['status']=='ok'

def test_unity_controls_ready_rounds_and_photo(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    data=create_room();rid=data['room_id'];key=data['host_key']
    qr=client.post(f'/api/rooms/{rid}/host/qr',json={'host_key':key})
    assert qr.status_code==200
    qr=qr.json()
    assert qr['qr']['1']['path'] != qr['qr']['2']['path']
    assert qr['qr']['1']['image'].startswith('data:image/png;base64,')
    sessions=[]
    for slot in (1,2):
        token=qr['qr'][str(slot)]['path'].split('token=')[-1]
        joined=client.post('/api/join',json={'room_id':rid,'token':token})
        assert joined.status_code==200,joined.text
        assert joined.json()['slot']==slot
        sessions.append(joined.json()['session'])
    assert client.post('/api/ready',json={'room_id':rid,'session':sessions[0]}).status_code==404
    r=client.post(f'/api/rooms/{rid}/host/start-round',json={'host_key':key})
    assert r.status_code==409
    assert client.post(f'/api/rooms/{rid}/host/player-ready',json={'host_key':'wrong','slot':1}).status_code==401
    for slot in (1,2):
        r=client.post(f'/api/rooms/{rid}/host/player-ready',json={'host_key':key,'slot':slot})
        assert r.status_code==200,r.text
    r=client.post(f'/api/rooms/{rid}/host/start-round',json={'host_key':key,'target_id':'mouse','seconds':30,'countdown_seconds':0})
    assert r.status_code==200,r.text
    assert r.json()['state']=='round_active'
    assert r.json()['target']['id']=='mouse'
    assert r.json()['ready_count']==2
    photo=client.post(f'/api/rooms/{rid}/photos',data={'session':sessions[0]},files={'file':('capture.png',make_photo(),'image/png')})
    assert photo.status_code==200,photo.text
    assert photo.json()['slot']==1
    assert photo.json()['status']=='unverified'
    state=client.get(f'/api/rooms/{rid}/state',headers={'x-host-key':key}).json()
    assert len(state['photos'])==1
    assert state['photos'][0]['slot']==1
    assert state['photos'][0]['status']=='unverified'
    assert client.get(state['photos'][0]['photo_url']).status_code==200
    assert state['slots'][0]['points']==0
    # Unity cannot start round 2 while current is active, regardless of Ready.
    assert client.post(f'/api/rooms/{rid}/host/start-round',json={'host_key':key}).status_code==409
    assert client.get(f'/api/rooms/{rid}/state').status_code==401
    with main._lock:
        main._rooms[rid].deadline=time.time()-1
    s=client.get(f'/api/rooms/{rid}/state',headers={'x-host-key':key}).json()
    assert s['state']=='round_review'
    # Merely calling /state does not auto-advance. Unity explicitly triggers the next round.
    with main._lock:
        main._rooms[rid].review_end=time.time()-10
    assert client.get(f'/api/rooms/{rid}/state',headers={'x-host-key':key}).json()['round']==1
    r=client.post(f'/api/rooms/{rid}/host/start-round',json={'host_key':key,'target_id':'cup','countdown_seconds':0})
    assert r.status_code==200,r.text
    assert r.json()['round']==2 and r.json()['target']['id']=='cup'
    assert client.post(f'/api/rooms/{rid}/host/finish',json={'host_key':key}).json()['state']=='finished'

def test_photo_accepted_only_during_active_round():
    room=create_room();rid=room['room_id'];key=room['host_key']
    token=client.post(f'/api/rooms/{rid}/host/qr',json={'host_key':key}).json()['qr']['1']['path'].split('token=')[-1]
    session=client.post('/api/join',json={'room_id':rid,'token':token}).json()['session']
    r=client.post(f'/api/rooms/{rid}/photos',data={'session':session},files={'file':('capture.png',make_photo(),'image/png')})
    assert r.status_code==409

def test_ai_scoring_is_based_on_server_received_timestamp():
    room=create_room();rid=room['room_id']
    with main._lock:
        r=main._rooms[rid]
        r.players[1]=main.Player(slot=1,session='s1');r.players[2]=main.Player(slot=2,session='s2')
        r.current_round=1
        a=main.Submission(id='1',slot=1,round_no=1,created_at=time.time()-3,status='analyzing')
        b=main.Submission(id='2',slot=2,round_no=1,created_at=time.time()-1,status='correct')
        r.submissions=[a,b]
        main._recompute_scores(r)
        assert r.players[2].points==100
        a.status='correct';main._recompute_scores(r)
        assert r.players[1].points==100
        assert r.players[2].points==70
