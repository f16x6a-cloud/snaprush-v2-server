'use strict';
// The phone ONLY captures and uploads photos. Unity owns Ready, rounds and timers.
const $ = (selector) => document.querySelector(selector);
const params = new URLSearchParams(location.search);
const roomId = params.get('room');
const token = params.get('token');
const localKey = 'snaprush_camera_session_' + (roomId || 'unknown');
let current = null;
try { current = JSON.parse(localStorage.getItem(localKey) || 'null'); } catch { localStorage.removeItem(localKey); }
let state = null, stream = null, facing = 'environment', uploading = false, polling = false;
// Anchor to the authoritative server epoch using monotonic time on this device.
let clockServerAtSync = 0, clockMonotonicAtSync = 0;
function estimatedServerTime() {
  return clockServerAtSync + (performance.now() - clockMonotonicAtSync) / 1000;
}
function secondsRemaining() {
  return state?.state === 'round_active'
    ? Math.max(0, Math.ceil(Number(state.deadline) - estimatedServerTime())) : 0;
}
function displayRoundClock() {
  const meta = $('#roundMeta'), face = $('#roundTimer'), bar = $('#roundProgress');
  if (!state) {meta.textContent='بانتظار اتصال Unity';face.textContent='--:--';bar.style.width='0%';return;}
  const round = Number(state.round) || 0;
  const total = Number(state.rounds) || 0;
  meta.textContent = round ? 'الجولة '+round+' / '+total : 'بانتظار بداية الجولة';
  let remaining = 0;
  if (state.state === 'countdown') {
    remaining = Math.max(0, Math.ceil(Number(state.start_at) - estimatedServerTime()));
    face.textContent = remaining ? 'تبدأ خلال '+remaining : 'انطلق!';
    bar.style.width = '100%';
  } else if (state.state === 'round_active') {
    remaining = secondsRemaining();
    face.textContent = String(Math.floor(remaining/60)).padStart(2,'0') + ':' + String(remaining%60).padStart(2,'0');
    const maxTime = Math.max(1, Number(state.seconds) || 60);
    bar.style.width = Math.min(100,remaining/maxTime*100) + '%';
  } else {
    face.textContent = state.state === 'round_review' || state.state === 'finished' ? '00:00' : '--:--';
    bar.style.width='0%';
  }
  const expired = state.state !== 'round_active' || secondsRemaining() <= 0;
  $('#snap').disabled = expired || !stream || uploading;
  face.classList.toggle('urgent',state.state==='round_active' && remaining <= 10);
}

const statuses = {
  analyzing: ['⏳ جاري تحليل الصورة', 'تحليلك بدأ والصورة وصلت للعبة'],
  correct: ['✅ صورة صحيحة!', 'تم احتساب النقاط في اللعبة'],
  incorrect: ['❌ الغرض غير مطابق', 'تقدر تعيد المحاولة خلال الجولة'],
  unverified: ['⚠️ التحليل غير متاح', 'الصورة وصلت للعبة، لكن لم يُحتسب لها نقاط']
};
function notice(message) { $('#toast').textContent = message; }
async function api(path, options={}) {
  const response = await fetch(path, {cache:'no-store', ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'تعذّر الاتصال بالسيرفر');
  return data;
}
function setConnected(ok) {
  const chip = $('#connection'); chip.classList.toggle('online', !!ok);
  chip.lastChild.textContent = ok ? ' متصل باللعبة' : ' غير متصل';
  $('#openCamera').disabled = !ok;
  $('#useSystemCamera').disabled = !ok;
  $('#leave').disabled = !ok;
}
async function connect() {
  if (!roomId) {
    $('#playerNumber').textContent = 'INVALID QR';
    $('#statusLine').textContent = 'افتح الرابط من QR الموجود فوق شخصيتك في لعبة SNAP RUSH';
    return;
  }
  try {
    // A securely saved session can reconnect even after removing the used QR token from the address bar.
    if (current && current.room_id === roomId && current.session) {
      try {
        const s = await api('/api/rooms/'+roomId+'/state', {headers:{'x-session':current.session}});
        setConnected(true); $('#playerNumber').textContent = 'PLAYER '+String(current.slot).padStart(2,'0');
        render(s); return;
      } catch { current = null; localStorage.removeItem(localKey); }
    }
    if (!token) throw new Error('امسح QR جديد من اللعبة للدخول');
    current = await api('/api/join', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({room_id:roomId,token})});
    localStorage.setItem(localKey, JSON.stringify(current));
    // Hide one-time join token from browser history after successful claim.
    history.replaceState(null,'', '/join?room='+encodeURIComponent(roomId));
    $('#playerNumber').textContent = 'PLAYER '+String(current.slot).padStart(2,'0');
    setConnected(true); notice('✓ جوالك مرتبط بالشخصية رقم '+current.slot);
    await tick();
  } catch (e) { setConnected(false); $('#playerNumber').textContent='TRY AGAIN'; $('#statusLine').textContent=e.message; notice(e.message); }
}
function render(s, rttMs=0) {
  state = s;
  clockServerAtSync = Number(s.server_now) + Math.max(0,rttMs)/2000;
  clockMonotonicAtSync = performance.now();
  displayRoundClock();
  const active = s.state === 'round_active';
  const messages = {
    waiting:'جاهز للتصوير. انتظر بداية التحدي داخل اللعبة.',
    countdown:'اللوبي بدأ العد التنازلي! استعد للتصوير.',
    round_active:'⚡ التحدي شغّال! صوّر الغرض المطلوب وأرسل.',
    round_review:'انتهت الجولة. انتظر التحدي التالي من اللوبي.',
    finished:'🏆 انتهت المباراة. تقدر تغلق الكاميرا.'
  };
  $('#statusLine').textContent=messages[s.state] || 'متصل باللعبة';
  $('#snap').disabled=!active || secondsRemaining()===0 || !stream || uploading;
  $('#flip').disabled=!stream || uploading;
  const shot=s.photos.find(p=>p.slot===current.slot);
  const container=$('#myPhoto');
  container.replaceChildren();
  if (!shot) {
    const empty=document.createElement('div'); empty.className='empty-photo';
    empty.textContent='صورتك ونتيجة التحليل بتظهر هنا بعد الإرسال ✨';container.append(empty);
    $('#photoBadge').textContent='NO SNAP YET'; return;
  }
  const wrapper=document.createElement('div'); wrapper.className='photo-line';
  const img=document.createElement('img'); img.src=shot.photo_url; img.alt='آخر صورة التقطتها';
  img.addEventListener('click',()=>window.open(shot.photo_url,'_blank','noopener'));
  const details=document.createElement('div');
  const strong=document.createElement('strong'); strong.textContent=(statuses[shot.status]||[shot.status])[0];
  const reason=document.createElement('div'); reason.className='reason';
  reason.textContent=shot.reason || (statuses[shot.status]||['','الصورة أُرسلت'])[1];
  if (shot.award) reason.textContent += ' • +'+shot.award+' نقطة';
  details.append(strong,reason); wrapper.append(img,details);container.append(wrapper);
  $('#photoBadge').textContent=shot.status.toUpperCase();
}
async function tick() {
  if (!current || polling) return;
  polling=true;
  try {
    const began=performance.now();
    const s=await api('/api/rooms/'+current.room_id+'/state', {headers:{'x-session':current.session}});
    render(s, performance.now()-began);
    setConnected(true);
  }
  catch(e) {setConnected(false); $('#statusLine').textContent='انقطع الاتصال، نحاول مجددًا...';notice(e.message);}
  finally {polling=false;}
}
function stopCamera() {
  if (stream) stream.getTracks().forEach(track=>track.stop());
  stream=null;$('#video').srcObject=null;
  $('#cameraBox').classList.remove('active');$('#snap').disabled=true;$('#flip').disabled=true;
}
async function startCamera() {
  stopCamera();
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('المتصفح لا يدعم الكاميرا');
    stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:facing},width:{ideal:1280},height:{ideal:960}}});
    $('#video').srcObject=stream;
    await $('#video').play();
    $('#cameraBox').classList.add('active');
    $('#cameraBox').classList.toggle('selfie',facing==='user');
    $('#openCamera').classList.add('hidden');
    if (state) render(state);
  } catch (e) {
    notice('تعذّر فتح الكاميرا. جرّب زر تصوير الجوال أسفل الكاميرا.');
    stopCamera();
    $('#openCamera').classList.remove('hidden');
  }
}
async function uploadBlob(blob) {
  if (!current || uploading) return;
  if (state?.state!=='round_active' || secondsRemaining()===0) {notice('انتهى وقت الجولة أو لم تبدأ بعد.');return;}
  uploading=true;$('#snap').disabled=true;
  notice('⏳ جاري إرسال الصورة إلى اللعبة...');
  try {
    const form=new FormData();form.append('session',current.session);form.append('file',blob,'snapshot.jpg');
    const result=await api('/api/rooms/'+current.room_id+'/photos',{method:'POST',body:form});
    notice(result.status==='unverified' ? '📸 وصلت الصورة للعبة، لكن تحليل AI غير مفعّل.' : '📸 وصلت الصورة للعبة! جاري التحليل بالخلفية.');
    await tick();
  } catch(e) {notice(e.message);}
  finally {uploading=false;if(state) render(state);}
}
function snap() {
  if (!stream || uploading) return;
  const video=$('#video');
  if (!video.videoWidth) {notice('الكاميرا لم تجهز بعد');return;}
  const ratio=Math.min(1,1280/Math.max(video.videoWidth,video.videoHeight));
  const canvas=document.createElement('canvas');canvas.width=Math.round(video.videoWidth*ratio);canvas.height=Math.round(video.videoHeight*ratio);
  canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
  canvas.toBlob(blob=>{if(blob)uploadBlob(blob);else notice('تعذر إنشاء الصورة')},'image/jpeg',.82);
}
$('#openCamera').addEventListener('click',startCamera);
$('#snap').addEventListener('click',snap);
$('#flip').addEventListener('click',()=>{facing=facing==='user'?'environment':'user';startCamera()});
$('#useSystemCamera').addEventListener('click',()=>{$('#fallback').setAttribute('capture',facing);$('#fallback').click()});
$('#fallback').addEventListener('change',(e)=>{const file=e.target.files?.[0];if(file)uploadBlob(file);e.target.value='';});
$('#leave').addEventListener('click',async()=>{
  const old=current;stopCamera();if(!old)return;
  try {await api('/api/leave',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({room_id:old.room_id,session:old.session})})} catch{}
  current=null;localStorage.removeItem(localKey);
  location.assign('/');
});
window.addEventListener('pagehide',stopCamera);
connect();setInterval(tick,1200);setInterval(displayRoundClock,200);
