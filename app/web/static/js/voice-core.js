/* Pure timing/motion logic: shared by the browser and offline Node tests. */
((root) => {
  class TurnDetector {
    constructor(now) {
      this.start = now; this.previous = now; this.lastVoice = now;
      this.noise = .003; this.voiced = 0; this.onset = 0; this.heard = false;
    }
    update(rms, now) {
      const dt = Math.min(100, Math.max(0, now - this.previous)); this.previous = now;
      const threshold = Math.max(.012, Math.min(.05, this.noise * 3));
      if (rms > threshold) {
        this.onset += dt; this.voiced += dt; this.lastVoice = now;
        if (this.onset >= 160) this.heard = true;
      } else {
        this.onset = 0;
        if (!this.heard) this.noise = this.noise * .97 + Math.min(rms, .016) * .03;
      }
      if (this.heard && this.voiced >= 160 && now - this.lastVoice >= 850) return 'send';
      if (now - this.start >= 29000) return this.heard ? 'send' : 'rotate';
      // Bound silent pre-roll without uploading silence or losing the start of speech.
      if (!this.heard && !this.onset && now - this.start >= 4000) return 'rotate';
      return null;
    }
  }
  class Spring {
    constructor() { this.value = 0; this.velocity = 0; }
    step(target, elapsed) {
      const dt = Math.min(.032, Math.max(0, elapsed));
      // Critically damped: preserve current velocity when the audio target changes.
      this.velocity += (180 * (target - this.value) - 27 * this.velocity) * dt;
      this.value += this.velocity * dt;
      return this.value;
    }
  }
  const api = {TurnDetector, Spring};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.JUVoiceCore = api;
})(typeof window !== 'undefined' ? window : globalThis);
