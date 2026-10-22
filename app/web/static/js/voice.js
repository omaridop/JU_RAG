/* Hands-free, half-duplex voice. No transcript is rendered or announced. */
(() => {
  const $ = id => document.getElementById(id);
  const dialog = $('voiceDialog'), orb = $('voiceOrb'), mute = $('voiceMute');
  const visual = dialog.querySelector('.voice-orb'), halo = dialog.querySelector('.voice-halo');
  const bars = [...dialog.querySelectorAll('.voice-wave i')], ribbons = [...dialog.querySelectorAll('.voice-ribbon')];
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const {TurnDetector, Spring} = window.JUVoiceCore;
  const spring = new Spring();
  let state = 'ready', version = 0, stream, recorder, context, micSource, micMeter, outMeter, playback;
  let controller, requestTimer, captureTimer, detector, frame, previousFrame = 0, lastActivity = 0;
  const samples = new Float32Array(1024);
  const hints = {
    ready: ['خلّينا نحكي', 'المس الدائرة وابدأ الحكي. أحمد بسمعك وبردّ عليك بصوته.', 'ابدأ المحادثة الصوتية'],
    requesting: ['لحظة، بنفتح الميكروفون', 'اسمح باستخدام الميكروفون من المتصفح.', 'بانتظار إذن الميكروفون'],
    listening: ['سامعك…', 'احكي براحتك. بس تخلّص، أحمد بردّ عليك.', 'إرسال السؤال الآن'],
    processing: ['لحظة، بفكّر معك', 'المس الدائرة إذا بدّك تحكي إشي ثاني.', 'إلغاء الطلب والبدء بسؤال جديد'],
    speaking: ['أحمد بحكي', 'المس الدائرة لتقاطعه. بعدها دورك تحكي.', 'مقاطعة أحمد والبدء بالحكي'],
    muted: ['الميكروفون مسكّر', 'خُذ راحتك. المس الدائرة لما تحب نكمّل.', 'استئناف المحادثة'],
    error: ['ما زبطت هالمرة', 'المس الدائرة للمحاولة من جديد.', 'إعادة المحاولة']
  };
  function setState(next, detail) {
    state = next; dialog.dataset.state = next;
    // Silent recorder rollover must not repeatedly announce the listening status.
    if ($('voiceStatus').textContent !== hints[next][0]) $('voiceStatus').textContent = hints[next][0];
    if ($('voiceHint').textContent !== (detail || hints[next][1])) $('voiceHint').textContent = detail || hints[next][1];
    orb.setAttribute('aria-label', hints[next][2]); orb.disabled = next === 'requesting';
    mute.disabled = ['ready', 'error'].includes(next);
    mute.setAttribute('aria-pressed', String(next === 'muted'));
    mute.setAttribute('aria-label', next === 'muted' ? 'استئناف الميكروفون' : 'كتم الميكروفون');
    $('voiceMuteLabel').textContent = next === 'muted' ? 'استئناف' : 'كتم';
  }
  function discardCapture() {
    clearTimeout(captureTimer);
    if (recorder) {
      recorder.onstop = null; recorder.ondataavailable = null;
      if (recorder.state !== 'inactive') recorder.stop();
      recorder = null;
    }
    detector = null;
  }
  function cancelTurn() {
    version++; controller?.abort(); controller = null; clearTimeout(requestTimer);
    discardCapture();
    if (playback) { playback.onended = null; try { playback.stop(); } catch (_) {} playback.disconnect(); playback = null; }
    outMeter?.disconnect(); outMeter = null;
    return version;
  }
  function releaseMic() {
    micSource?.disconnect(); micSource = null; micMeter?.disconnect(); micMeter = null;
    stream?.getTracks().forEach(track => { track.onended = null; track.stop(); }); stream = null;
  }
  function pause(detail) {
    cancelTurn(); releaseMic(); setState('muted', detail);
    context?.suspend().catch(() => {});
  }
  function close() {
    cancelTurn(); releaseMic(); cancelAnimationFrame(frame);
    const oldContext = context; context = null; oldContext?.close().catch(() => {});
    setState('ready');
  }
  function fail(detail) { cancelTurn(); releaseMic(); setState('error', detail); }
  function micEnabled(enabled) { stream?.getAudioTracks().forEach(track => { track.enabled = enabled; }); }
  function level(meter) {
    if (!meter) return 0;
    meter.getFloatTimeDomainData(samples);
    let sum = 0; for (const sample of samples) sum += sample * sample;
    return Math.sqrt(sum / samples.length);
  }
  function animate(now) {
    if (!dialog.open) return;
    const rms = level(state === 'speaking' ? outMeter : state === 'listening' ? micMeter : null);
    const amount = spring.step(Math.min(1, rms * (state === 'speaking' ? 7 : 15)), (now - (previousFrame || now)) / 1000);
    previousFrame = now;
    if (!reduced.matches) {
      visual.style.transform = `scale(${1 + amount * .09})`;
      halo.style.transform = state === 'processing' ? `rotate(${now * .07}deg)` : `scale(${1 + amount * .13})`;
      halo.style.opacity = String(.3 + amount * .65);
      bars.forEach((bar, i) => { bar.style.transform = `scaleY(${.18 + amount * (.4 + .55 * Math.abs(Math.sin(now / 170 + i * 1.2)))})`; });
      ribbons.forEach((ribbon, i) => { ribbon.style.transform = `rotate(${[-35, 40, 25][i] + amount * [16, -21, 12][i]}deg) scale(${1 + amount * .06})`; });
    }
    if (state === 'listening' && detector) {
      const action = detector.update(rms, now);
      if (detector.heard) lastActivity = now;
      if (action === 'send') finishCapture();
      else if (now - lastActivity > 60000) pause('وقفنا الميكروفون بعد فترة سكوت. المس الدائرة لنكمّل.');
      else if (action === 'rotate') listen(version);
    }
    frame = requestAnimationFrame(animate);
  }
  function listen(turn) {
    if (turn !== version || !dialog.open || !stream) return;
    discardCapture(); micEnabled(true);
    try {
      const mime = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find(type => MediaRecorder.isTypeSupported(type));
      if (!mime) throw new Error('unsupported');
      const current = new MediaRecorder(stream, {mimeType:mime, audioBitsPerSecond:32000});
      const chunks = []; recorder = current; detector = new TurnDetector(performance.now());
      current.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      current.onstop = () => { if (turn === version) submit(new Blob(chunks, {type:mime}), turn); };
      current.onerror = () => { if (turn === version) fail('صار خطأ بالتسجيل. تأكّد من الميكروفون وجرّب من جديد.'); };
      current.start(); setState('listening');
      captureTimer = setTimeout(() => { if (turn === version && recorder === current) finishCapture(); }, 30000);
    } catch (_) { fail('التسجيل مش متاح بهالمتصفح. جرّب Chrome أو Safari محدّث.'); }
  }
  function finishCapture() {
    if (!recorder || recorder.state !== 'recording') return;
    if (!detector?.heard) { listen(version); return; }
    clearTimeout(captureTimer); detector = null; setState('processing');
    recorder.stop(); micEnabled(false);
  }
  async function start() {
    const turn = cancelTurn(); lastActivity = performance.now();
    setState('requesting');
    try {
      const AudioEngine = window.AudioContext || window.webkitAudioContext;
      if (!AudioEngine || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw new Error('unsupported');
      if (!context || context.state === 'closed') context = new AudioEngine();
      await context.resume();
      if (turn !== version || !dialog.open) return;
      if (!stream) {
        const acquired = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation:true, noiseSuppression:true, autoGainControl:true}});
        if (turn !== version || !dialog.open) { acquired.getTracks().forEach(track => track.stop()); return; }
        stream = acquired; micSource = context.createMediaStreamSource(stream); micMeter = context.createAnalyser();
        micMeter.fftSize = 1024; micSource.connect(micMeter);
        stream.getTracks().forEach(track => { track.onended = () => fail('انقطع الميكروفون. وصّله وجرّب من جديد.'); });
      }
      listen(turn);
    } catch (error) {
      if (turn === version) fail(error.name === 'NotAllowedError' ? 'اسمح للمتصفح باستخدام الميكروفون، وبعدها جرّب من جديد.' : 'ما قدرنا نفتح الميكروفون. تأكّد إنه موصول والمتصفح محدّث.');
    }
  }
  async function checked(response) {
    if (response.ok) return response;
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || (response.status === 429 ? 'busy' : 'unavailable'));
  }
  const errors = {
    budget: 'وصلنا لسقف الاستخدام المتاح حالياً. جرّب لاحقاً.',
    limit: 'وصلت لحدّ الأسئلة بهاي المحادثة. ابدأ محادثة جديدة من صفحة الدردشة.',
    rate_limited: 'الطلبات كثيرة هسّا. استنّى شوي وجرّب من جديد.',
    busy: 'خدمة الصوت مشغولة هسّا. استنّى شوي وجرّب من جديد.',
    reload_required: 'حدّث الصفحة عشان نرجع نكمّل المحادثة.'
  };
  async function submit(blob, turn) {
    if (turn !== version) return;
    const request = new AbortController(); controller = request;
    requestTimer = setTimeout(() => request.abort(), 120000);
    try {
      if (!blob.size || blob.size > 1000000) throw new Error('audio');
      const form = new FormData(); form.append('audio', blob, 'voice');
      const result = await (await checked(await fetch('/api/voice/transcribe', {method:'POST', body:form, signal:request.signal,
        headers:{'X-CSRF-Token':document.querySelector('meta[name="voice-csrf"]').content}}))).json();
      if (turn !== version) return;
      if (!result.text) { lastActivity = performance.now(); listen(turn); return; }
      const response = await checked(await fetch('/api/chat/stream', {method:'POST', signal:request.signal,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:result.text, voice_mode:true})}));
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '', done;
      while (true) {
        const part = await reader.read(); if (part.done) break;
        if (turn !== version) return;
        buffer += decoder.decode(part.value, {stream:true});
        const events = buffer.split('\n\n'); buffer = events.pop();
        for (const event of events) {
          if (!event.startsWith('data: ')) continue;
          const value = JSON.parse(event.slice(6));
          if (value.type === 'status') $('voiceStatus').textContent = 'بتأكّد من المصادر…';
          if (value.type === 'error') throw new Error('answer');
          if (value.type === 'done') done = value;
        }
      }
      if (!done?.log_id) throw new Error(done?.served_from || 'incomplete');
      // Speak only the final grounded answer; web fallback may have replaced a draft.
      const sound = await (await checked(await fetch('/api/speech', {method:'POST', signal:request.signal,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({log_id:done.log_id, voice_mode:true})}))).arrayBuffer();
      if (turn !== version || !context) return;
      const decoded = await context.decodeAudioData(sound);
      if (turn !== version || !context) return;
      if (context.state !== 'running') throw new Error('suspended');
      const source = context.createBufferSource(); playback = source; source.buffer = decoded;
      outMeter = context.createAnalyser(); outMeter.fftSize = 1024;
      source.connect(outMeter); outMeter.connect(context.destination);
      source.onended = () => {
        if (turn !== version) return;
        source.disconnect(); outMeter?.disconnect(); playback = null; outMeter = null;
        lastActivity = performance.now(); listen(turn);
      };
      source.start(); setState('speaking');
    } catch (error) {
      if (turn === version) fail(error.name === 'AbortError' ? 'الطلب أخذ وقت طويل. المس الدائرة لنجرّب من جديد.' : errors[error.message] || 'ما قدرنا نكمّل الرد الصوتي. المس الدائرة لنجرّب من جديد.');
    } finally {
      if (turn === version) { clearTimeout(requestTimer); controller = null; }
    }
  }
  $('voiceLaunch').onclick = () => {
    window.JUSpeech?.stop(); dialog.showModal(); setState('ready');
    previousFrame = 0; frame = requestAnimationFrame(animate);
  };
  orb.onclick = () => state === 'listening' ? finishCapture() : start();
  mute.onclick = () => state === 'muted' ? start() : pause();
  $('voiceClose').onclick = () => dialog.close();
  dialog.addEventListener('close', close);
  document.addEventListener('visibilitychange', () => { if (document.hidden && dialog.open && !['ready','muted','error'].includes(state)) pause('وقفنا الميكروفون لما تركت الصفحة. المس الدائرة لنكمّل.'); });
  window.addEventListener('pagehide', close);
})();
