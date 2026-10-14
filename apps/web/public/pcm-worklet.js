// Consumes interleaved stereo f32 PCM blocks (48 kHz; the AudioContext is
// created at that rate so the browser resamples to the device) posted from the
// emulator worker. Playback starts once `startSeconds` of audio are queued
// ("buffer N seconds, then play", never less than 0.25 s); on underrun it
// outputs silence and waits for the same amount again, so an emulator that is
// slower than real time plays in bursts instead of stuttering. Like the
// desktop sink, each burst fades in over 128 frames when it starts, and when
// the queue runs dry the last 128 frames written fade out before the silence,
// so bursts do not click.
const FADE = 128;
class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.head = 0; // read offset (floats) into queue[0]
    this.queued = 0; // frames (L/R pairs) waiting
    this.playing = false;
    this.minStart = Math.round(0.25 * sampleRate);
    this.start = Math.max(this.minStart, 0.5 * sampleRate);
    this.fadeIn = 0; // frames of fade-in still to apply
    this.fades = 0; // fade-outs applied (underruns)
    this.underruns = 0;
    this.played = 0;
    this.received = 0;
    this.highWater = 0;
    this.silence = 0;
    this.underrunFrames = 0;
    this.portAttached = false;
    this.sinceReport = 0;
    const onData = ({ data }) => {
      if (data instanceof Float32Array) {
        this.queue.push(data);
        this.queued += data.length / 2;
        this.received += data.length / 2;
        this.highWater = Math.max(this.highWater, this.queued);
      } else if (data && data.type === 'config') {
        this.start = Math.max(this.minStart, data.startSeconds * sampleRate);
      } else if (data && data.type === 'flush') {
        this.queue = []; this.head = 0; this.queued = 0; this.playing = false;
      }
    };
    this.port.onmessage = ({ data }) => {
      // The emulator worker's PCM port arrives here.
      if (data && data.port) { this.portAttached = true; data.port.onmessage = onData; }
      else onData({ data });
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0];
    const left = out[0];
    const right = out[1] ?? out[0];
    const n = left.length;
    if (!this.playing && this.queued > 0 && this.queued >= this.start) { this.playing = true; this.fadeIn = FADE; }
    let i = 0;
    if (this.playing) {
      while (i < n && this.queue.length) {
        const block = this.queue[0];
        let l = block[this.head];
        let r = block[this.head + 1];
        if (this.fadeIn > 0) {
          const g = (FADE - this.fadeIn) / FADE;
          l *= g; r *= g; this.fadeIn -= 1;
        }
        left[i] = l;
        right[i] = r;
        i += 1;
        this.head += 2;
        if (this.head >= block.length) { this.queue.shift(); this.head = 0; }
      }
      this.queued -= i;
      this.played += i;
      if (i < n) {
        this.playing = false; this.underruns += 1;
        const k = Math.min(i, FADE);
        for (let j = 0; j < k; j += 1) {
          const g = (k - j) / (k + 1);
          left[i - k + j] *= g; right[i - k + j] *= g;
        }
        if (k > 0) this.fades += 1;
      }
    }
    this.silence += n - i;
    // Initial buffering is silence, not an underrun. Include all rebuffering
    // silence after the first consumed PCM frame, not just the event quantum.
    if (this.played > 0) this.underrunFrames += n - i;
    for (; i < n; i += 1) { left[i] = 0; right[i] = 0; }
    this.sinceReport += n;
    if (this.sinceReport >= sampleRate / 4) {
      this.sinceReport = 0;
      this.port.postMessage({
        queuedSeconds: this.queued / sampleRate, playing: this.playing,
        underruns: this.underruns, fades: this.fades, playedSeconds: this.played / sampleRate,
        receivedSeconds: this.received / sampleRate,
        highWaterSeconds: this.highWater / sampleRate,
        silenceSeconds: this.silence / sampleRate,
        underrunSeconds: this.underrunFrames / sampleRate,
        portAttached: this.portAttached,
        receivedPcm: this.received > 0, renderedPcm: this.played > 0,
      });
    }
    return true;
  }
}
registerProcessor('pcm-player', PcmProcessor);
