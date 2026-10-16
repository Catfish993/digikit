/** Browser PCM sink for the coupled core: AudioContext + AudioWorklet. */
const base = import.meta.env.BASE_URL.replace(/\/?$/, '/');

export interface AudioStats {
  queuedSeconds: number;
  playing: boolean;
  underruns: number;
  playedSeconds: number;
  receivedSeconds: number;
  highWaterSeconds: number;
  silenceSeconds: number;
  underrunSeconds: number;
  portAttached: boolean;
  receivedPcm: boolean;
  renderedPcm: boolean;
}

/** Unlock a suspended context on the next real gesture (file-input change events may not count). */
export function resumeOnNextGesture(sink: PcmSink): void {
  if (sink.context.state === 'running') return;
  const events = ['pointerdown', 'keydown'] as const;
  const fire = () => {
    for (const e of events) window.removeEventListener(e, fire);
    void sink.resume().catch(() => {});
  };
  for (const e of events) window.addEventListener(e, fire);
}

export class PcmSink {
  readonly context: AudioContext;
  private node?: AudioWorkletNode;
  private resumed?: Promise<void>;
  private closed = false;
  /** Latest worklet report. */
  stats: AudioStats = {
    queuedSeconds: 0,
    playing: false,
    underruns: 0,
    playedSeconds: 0,
    receivedSeconds: 0,
    highWaterSeconds: 0,
    silenceSeconds: 0,
    underrunSeconds: 0,
    portAttached: false,
    receivedPcm: false,
    renderedPcm: false,
  };

  /** Construct from a user gesture. 48 kHz is the DSP rate; the browser resamples to the device. */
  constructor() {
    this.context = new AudioContext({ sampleRate: 48_000, latencyHint: 'playback' });
  }

  /** Call synchronously from the click handler so the user gesture still counts. */
  resume(): Promise<void> {
    this.resumed = this.context.resume();
    return this.resumed;
  }

  /** Loads the worklet and returns the port the emulator worker posts PCM to. */
  async open(startSeconds: number): Promise<MessagePort> {
    await this.context.audioWorklet.addModule(`${base}pcm-worklet.js`);
    this.node = new AudioWorkletNode(this.context, 'pcm-player', { numberOfInputs: 0, outputChannelCount: [2] });
    this.node.connect(this.context.destination);
    this.node.port.onmessage = ({ data }: MessageEvent<AudioStats>) => {
      this.stats = data;
    };
    this.setBuffer(startSeconds);
    const channel = new MessageChannel();
    this.node.port.postMessage({ port: channel.port2 }, [channel.port2]);
    return channel.port1;
  }

  /** Start playing once this much audio is queued (0: immediately). */
  setBuffer(seconds: number) {
    this.node?.port.postMessage({ type: 'config', startSeconds: seconds });
  }
  /** Worklet observations plus host connection state; not proof of audibility. */
  report() {
    return {
      ...this.stats,
      contextState: this.context.state,
      sampleRate: this.context.sampleRate,
      nodeConnected: Boolean(this.node),
    };
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.node?.disconnect();
    this.node = undefined;
    this.context.close().catch(() => {});
  }
}
