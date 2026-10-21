/* Turn-based voice: explicit microphone consent, bounded recordings, cancellable work. */
(() => {
  const $ = id => document.getElementById(id);
  const dialog = $('voiceDialog'), record = $('voiceRecord'), interrupt = $('voiceInterrupt');
  let recorder, tracks, timer, audio, url, controller, generation = 0, recording = false;
  const status = text => { $('voiceStatus').textContent = text; };
  function releaseMic() { clearTimeout(timer); tracks?.getTracks().forEach(t => t.stop()); tracks = null; }
  function stop() {
    generation++; controller?.abort(); controller = null;
    if (recorder?.state === 'recording') recorder.stop();
    recording = false; releaseMic(); audio?.pause(); audio = null;
    if (url) URL.revokeObjectURL(url); url = null;
    record.disabled = false; record.textContent = 'ابدأ الحكي'; interrupt.hidden = true;
  }
  async function checked(response) {
    if (!response.ok) throw new Error('request');
    return response;
  }
  async function submit(blob, turn) {
    if (turn !== generation) return;
    recording = false; releaseMic(); record.disabled = true; record.textContent = 'عم بجهّز الجواب';
    controller = new AbortController(); const signal = controller.signal;
    const timeout = setTimeout(() => controller?.abort(), 120000);
    try {
      if (!blob.size || blob.size > 1000000) throw new Error('size');
      status('عم بسمع سؤالك…');
      const form = new FormData(); form.append('audio', blob, 'voice');
      const result = await (await checked(await fetch('/api/voice/transcribe', {method:'POST', body:form, signal,
        headers:{'X-CSRF-Token':document.querySelector('meta[name="voice-csrf"]').content}}))).json();
      if (turn !== generation) return;
      if (!result.text) { status('ما سمعت كلام واضح. جرّب مرة ثانية.'); return; }
      $('voiceQuestion').textContent = result.text; $('voiceAnswer').textContent = '';
      status('عم بجهّز جواب مختصر…');
      const response = await checked(await fetch('/api/chat/stream', {method:'POST', signal,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:result.text,voice_mode:true})}));
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '', done;
      while (true) {
        const part = await reader.read(); if (part.done) break;
        buffer += decoder.decode(part.value,{stream:true});
        const events = buffer.split('\n\n'); buffer = events.pop();
        for (const event of events) {
          if (!event.startsWith('data: ')) continue;
          const value = JSON.parse(event.slice(6));
          if (turn !== generation) return;
          if (value.type === 'delta') $('voiceAnswer').textContent += value.text;
          if (value.type === 'reset') $('voiceAnswer').textContent = '';
          if (value.type === 'status') status('عم بدوّر بالمصادر الرسمية…');
          if (value.type === 'error') throw new Error('answer');
          if (value.type === 'done') done = value;
        }
      }
      if (!done) throw new Error('incomplete');
      if (!done.log_id) { status('راجع الرسالة الظاهرة فوق.'); return; }
      status('عم بجهّز الصوت…');
      const sound = await (await checked(await fetch('/api/speech',{method:'POST',signal,
        headers:{'Content-Type':'application/json'},body:JSON.stringify({log_id:done.log_id,voice_mode:true})}))).blob();
      if (turn !== generation) return;
      url = URL.createObjectURL(sound); audio = new Audio(url);
      audio.onended = () => { if (turn === generation) { stop(); status('تفضّل، شو سؤالك الثاني؟'); } };
      await audio.play(); status('أحمد عم بحكي… اكبس إيقاف إذا بدّك تقاطعه.');
    } catch (error) {
      if (turn === generation) status(error.name === 'AbortError' ? 'الطلب أخذ وقت طويل. جرّب مرة ثانية.' : 'تعذّر إكمال الصوت. جرّب مرة ثانية أو استخدم الكتابة.');
    } finally {
      clearTimeout(timeout);
      if (turn === generation && (!audio || audio.paused)) { record.disabled = false; record.textContent = 'ابدأ الحكي'; interrupt.hidden = true; }
    }
  }
  $('voiceLaunch').onclick = () => {
    window.JUSpeech?.stop();
    dialog.showModal();
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { record.disabled = true; status('متصفحك ما بدعم التسجيل. جرّب متصفح حديث عبر HTTPS.'); }
  };
  $('voiceClose').onclick = () => dialog.close();
  dialog.addEventListener('close', stop);
  interrupt.onclick = () => { stop(); status('تم الإيقاف. بتقدر تحكي من جديد.'); };
  record.onclick = async () => {
    if (recording) { recorder.stop(); return; }
    stop(); const turn = generation; record.disabled = true;
    try {
      status('اسمح باستخدام الميكروفون للبدء.');
      const stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
      if (turn !== generation || !dialog.open) { stream.getTracks().forEach(t => t.stop()); return; }
      tracks = stream;
      const mime = ['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(t => MediaRecorder.isTypeSupported(t));
      if (!mime) throw new Error('unsupported');
      recorder = new MediaRecorder(stream,{mimeType:mime,audioBitsPerSecond:32000}); const chunks = [];
      recorder.ondataavailable = event => { if(event.data.size) chunks.push(event.data); };
      recorder.onstop = () => submit(new Blob(chunks,{type:mime}),turn);
      recorder.onerror = () => { stop(); status('صار خطأ بالتسجيل. جرّب مرة ثانية.'); };
      recorder.start(); recording = true; record.disabled = false; record.textContent = 'إرسال السؤال'; interrupt.hidden = false;
      status('عم بسمعك… اكبس إرسال لما تخلص (حد أقصى ٣٠ ثانية).');
      timer = setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); },30000);
    } catch (_) { if(turn === generation) { stop(); status('ما قدرنا نفتح الميكروفون. تأكّد من إذن المتصفح.'); } }
  };
  window.addEventListener('pagehide', stop);
})();
