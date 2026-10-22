// Offline browser-controller tests with synthetic microphone/AudioContext objects.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {TurnDetector, Spring} = require('../app/web/static/js/voice-core.js');

test('turn detection ignores silence and click noise; sends after a natural pause', () => {
  const d = new TurnDetector(0);
  for (let t=20;t<=1000;t+=20) assert.equal(d.update(.002,t),null);
  d.update(.08,1020); d.update(.002,1040); assert.equal(d.heard,false);
  for(let t=1060;t<=1360;t+=20) d.update(.06,t);
  assert.equal(d.heard,true);
  assert.equal(d.update(.002,1900),null);
  assert.equal(d.update(.002,2220),'send');
});
test('silence rotates locally and continuous speech has a bounded turn', () => {
  const silent = new TurnDetector(0);
  assert.equal(silent.update(.001,4100),'rotate');
  const talking = new TurnDetector(0);
  let result; for(let t=20;t<=29000;t+=20) result = talking.update(.06,t);
  assert.equal(result,'send');
});
test('spring carries velocity on interruption and settles without instability', () => {
  const s = new Spring(); for(let i=0;i<20;i++) s.step(1,1/60);
  const before=s.value; s.step(0,1/60);
  assert.ok(Math.abs(s.value-before)<.1);
  for(let i=0;i<200;i++) s.step(0,1/60);
  assert.ok(Math.abs(s.value)<.001);
});

function harness(options={}) {
  const elements = new Map(), listeners = {}, timers = new Map(), rafs = new Map();
  let clock=0, id=0, amplitude=0, lastSource, permissionResolve;
  const calls=[], tracks=[];
  function el(name) {
    if (!elements.has(name)) elements.set(name, {
      style:{}, dataset:{}, textContent:'', disabled:false, attrs:{}, open:false,
      setAttribute(k,v){this.attrs[k]=v;}, addEventListener(k,fn){this[k]=fn;},
      showModal(){this.open=true;}, close(){this.open=false;this.closeHandler?.();},
      querySelector(s){return el(s);}, querySelectorAll(s){return Array.from({length: s.includes('ribbon')?3:5},(_,i)=>el(s+i));}
    });
    return elements.get(name);
  }
  const dialog=el('voiceDialog');
  dialog.addEventListener=(k,fn)=> {if(k==='close') dialog.closeHandler=fn;};
  function acquire() {
    const track={enabled:true,stopped:false,stop(){this.stopped=true;}}; tracks.push(track);
    return {getTracks:()=>[track],getAudioTracks:()=>[track]};
  }
  class MediaRecorder {
    static isTypeSupported(){return true;}
    constructor(){this.state='inactive';}
    start(){this.state='recording';}
    stop(){this.state='inactive';const data=this.ondataavailable,stop=this.onstop;
      queueMicrotask(()=>{data?.({data:new Blob(['synthetic'])});stop?.();});}
  }
  class AudioContext {
    constructor(){this.state='running';this.destination={};}
    async resume(){this.state='running';} async suspend(){this.state='suspended';} async close(){this.state='closed';}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createAnalyser(){return {connect(){},disconnect(){},getFloatTimeDomainData(a){a.fill(amplitude);}};}
    async decodeAudioData(){return {};}
    createBufferSource(){lastSource={connect(){},disconnect(){},start(){},stop(){this.stopped=true;}};return lastSource;}
  }
  const document={hidden:false,getElementById:el,querySelector:()=>({content:'csrf'}),addEventListener:(n,fn)=>{listeners[n]=fn;}};
  const window={JUVoiceCore:{TurnDetector,Spring},AudioContext,MediaRecorder,addEventListener:(n,fn)=>{listeners[n]=fn;}};
  const ctx = {window,document,MediaRecorder,Float32Array,Blob,FormData,AbortController,TextDecoder,Math,JSON,
    matchMedia:()=>({matches:!!options.reduced}),performance:{now:()=>clock},
    requestAnimationFrame:fn=>{rafs.set(++id,fn);return id;},cancelAnimationFrame:i=>rafs.delete(i),
    setTimeout:(fn,ms)=>{timers.set(++id,{fn,ms});return id;},clearTimeout:i=>timers.delete(i),
    navigator:{mediaDevices:{getUserMedia:()=>options.pendingPermission ? new Promise(resolve=>{permissionResolve=()=>resolve(acquire());}) : Promise.resolve(acquire())}},
    fetch:async (url,init)=>{
      calls.push({url,init});
      if(options.fetch) return options.fetch(url,init);
      if(url.endsWith('transcribe')) return new Response(JSON.stringify({text: options.empty ? '' : 'مرحبا'}));
      if(url.endsWith('stream')) return new Response('data: {"type":"delta","text":"PRIVATE ANSWER"}\n\ndata: {"type":"done","log_id":1,"served_from":"rag"}\n\n');
      return new Response('synthetic audio');
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../app/web/static/js/voice.js'),'utf8'),ctx);
  async function flush(){for(let i=0;i<12;i++) await new Promise(resolve=>setImmediate(resolve));}
  function tick(ms,level=0){amplitude=level;for(let t=0;t<ms;t+=20){clock+=20;const jobs=[...rafs.values()];rafs.clear();jobs.forEach(fn=>fn(clock));}}
  return {el,dialog,tracks,calls,listeners,document,timers,flush,tick,source:()=>lastSource,
    resolvePermission:()=>permissionResolve(),
    async start(){el('voiceLaunch').onclick();await el('voiceOrb').onclick();await flush();},
    async say(){tick(400,.05);tick(900,0);await flush();}
  };
}
test('automatic speech turn plays audio, shows no transcript, then listens again', async () => {
  const h=harness();await h.start();assert.equal(h.dialog.dataset.state,'listening');
  h.tick(9000,0);await h.flush();assert.equal(h.calls.length,0);
  await h.say();assert.equal(h.dialog.dataset.state,'speaking');assert.equal(h.calls.length,3);
  assert.equal(h.tracks[0].enabled,false);
  assert.equal(JSON.parse(h.calls[2].init.body).voice_mode,true);
  assert.ok(![...['voiceStatus','voiceHint']].some(id=>h.el(id).textContent.includes('PRIVATE')));
  h.source().onended();assert.equal(h.dialog.dataset.state,'listening');assert.equal(h.tracks[0].enabled,true);
  h.dialog.close();assert.equal(h.tracks[0].stopped,true);
});
test('tap interrupts playback, mute stops tracks, resume reacquires, hidden tab pauses', async () => {
  const h=harness();await h.start();await h.say();const source=h.source();
  await h.el('voiceOrb').onclick();assert.equal(source.stopped,true);assert.equal(h.dialog.dataset.state,'listening');
  h.el('voiceMute').onclick();assert.equal(h.tracks[0].stopped,true);assert.equal(h.dialog.dataset.state,'muted');
  await h.el('voiceMute').onclick();await h.flush();assert.equal(h.dialog.dataset.state,'listening');
  h.document.hidden=true;h.listeners.visibilitychange();assert.equal(h.tracks[1].stopped,true);
  assert.equal(h.dialog.dataset.state,'muted');h.dialog.close();
});
test('late microphone permission after closing is released without recording', async () => {
  const h=harness({pendingPermission:true});h.el('voiceLaunch').onclick();const start=h.el('voiceOrb').onclick();
  await h.flush();h.dialog.close();h.resolvePermission();await start;
  assert.equal(h.tracks[0].stopped,true);assert.equal(h.calls.length,0);
});
test('empty transcription returns to listening without sending a chat', async () => {
  const h=harness({empty:true});await h.start();await h.say();
  assert.equal(h.calls.length,1);assert.equal(h.dialog.dataset.state,'listening');h.dialog.close();
});
test('provider error stops microphone and shows retry; no automatic billing loop', async () => {
  const h=harness({fetch:async()=>new Response('{"error":"budget"}',{status:403})});
  await h.start();await h.say();assert.equal(h.dialog.dataset.state,'error');
  assert.equal(h.tracks[0].stopped,true);assert.equal(h.calls.length,1);h.dialog.close();
});
test('muting while transcription is pending prevents stale playback', async () => {
  let resolve;
  const h=harness({fetch:()=>new Promise(r=>{resolve=r;})});await h.start();await h.say();
  h.el('voiceMute').onclick();resolve(new Response('{"text":"مرحبا"}'));await h.flush();
  assert.equal(h.dialog.dataset.state,'muted');assert.equal(h.calls.length,1);assert.equal(h.source(),undefined);h.dialog.close();
});
test('idle listening pauses instead of uploading silence', async () => {
  const h=harness({reduced:true});await h.start();h.tick(61000,0);await h.flush();
  assert.equal(h.calls.length,0);assert.equal(h.dialog.dataset.state,'muted');assert.equal(h.tracks[0].stopped,true);h.dialog.close();
});
