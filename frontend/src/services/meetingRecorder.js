/**
 * Browser-side capture of a Google Meet session's audio.
 *
 * The organizer must explicitly pick the Meet tab in the browser's own share dialog and tick "Share tab audio";
 * nothing is recorded before that, and the browser keeps showing its "sharing" indicator. The tab audio (remote
 * participants) is mixed with the organizer's microphone into one track and recorded with MediaRecorder.
 *
 * The recording is cut into independent segments (default 5 minutes of recorded time, pauses excluded). Each
 * segment is a complete, playable file, so it can be uploaded as soon as it ends, a failed upload can be retried on
 * its own, and the backend never needs one huge file or ffmpeg.
 */

export const SEGMENT_MS = 5 * 60 * 1000;
const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];

export class RecordingError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

export function isRecordingSupported(env = globalThis) {
  return Boolean(env.navigator?.mediaDevices?.getDisplayMedia && env.navigator?.mediaDevices?.getUserMedia
    && env.MediaRecorder && (env.AudioContext || env.webkitAudioContext));
}

export function pickMimeType(env = globalThis) {
  const recorder = env.MediaRecorder;
  if (!recorder?.isTypeSupported) return '';
  return MIME_CANDIDATES.find((type) => recorder.isTypeSupported(type)) || '';
}

/** Turn a getDisplayMedia/getUserMedia failure into a message the organizer can act on. */
export function describeCaptureError(error) {
  if (error instanceof RecordingError) return error;
  const name = error?.name || '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return new RecordingError('PERMISSION_DENIED',
      'The browser did not allow screen/tab sharing. Click Start recording again and choose the Google Meet tab in the browser prompt. Nothing was recorded.');
  }
  if (name === 'NotFoundError') return new RecordingError('NO_DEVICE', 'No audio device or shareable tab was found.');
  if (name === 'AbortError' || name === 'NotReadableError') {
    return new RecordingError('CAPTURE_FAILED', 'The browser could not start capturing. Close other apps using the microphone and try again.');
  }
  return new RecordingError('CAPTURE_FAILED', `Recording could not start: ${error?.message || 'unknown error'}`);
}

export class MeetingRecorder {
  /**
   * @param {object} handlers
   *   onSegment({index, offsetSeconds, durationSeconds, blob}) - a finished segment, ready to upload
   *   onWarning(text) - non-fatal problem (e.g. microphone unavailable)
   *   onInterrupted(text) - capture ended without the organizer pressing Stop; segments so far were emitted
   * @param {object} options segmentMs, tickMs, env (for tests)
   */
  constructor({ onSegment, onWarning = () => {}, onInterrupted = () => {} }, { segmentMs = SEGMENT_MS, tickMs = 500, env = globalThis } = {}) {
    this.onSegment = onSegment;
    this.onWarning = onWarning;
    this.onInterrupted = onInterrupted;
    this.segmentMs = segmentMs;
    this.tickMs = tickMs;
    this.env = env;
    this.state = 'idle';            // idle | recording | paused | stopped | interrupted
    this.recordedMs = 0;
    this.segmentStartMs = 0;
    this.segmentIndex = 0;
    this.recorder = null;
    this.streams = [];
    this.chunks = [];
    this.stopping = false;
  }

  get elapsedSeconds() {
    return Math.floor(this.recordedMs / 1000);
  }

  get segmentCount() {
    return this.segmentIndex;
  }

  async start() {
    const env = this.env;
    if (!isRecordingSupported(env)) {
      throw new RecordingError('UNSUPPORTED', 'This browser cannot capture tab audio. Use a recent desktop Chrome or Edge.');
    }
    let display;
    try {
      // video is requested because browsers require it; only the audio is recorded.
      display = await env.navigator.mediaDevices.getDisplayMedia({ video: true, audio: true, selfBrowserSurface: 'exclude', preferCurrentTab: false });
    } catch (error) {
      throw describeCaptureError(error);
    }
    this.streams.push(display);
    if (display.getAudioTracks().length === 0) {
      this.releaseStreams();
      throw new RecordingError('NO_AUDIO_TRACK',
        'No audio was shared. Start again, choose the Google Meet browser tab, and tick "Also share tab audio".');
    }
    const surface = display.getVideoTracks()[0]?.getSettings?.().displaySurface;
    if (surface && surface !== 'browser') {
      this.onWarning('You shared a window or screen instead of a browser tab. If the meeting audio is silent, share the Google Meet tab with "Share tab audio".');
    }

    let mic = null;
    try {
      mic = await env.navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      this.streams.push(mic);
    } catch {
      this.onWarning('Your microphone was not available, so only the meeting audio (other participants) is being recorded.');
    }

    const Context = env.AudioContext || env.webkitAudioContext;
    this.context = new Context();
    const destination = this.context.createMediaStreamDestination();
    this.context.createMediaStreamSource(new env.MediaStream(display.getAudioTracks())).connect(destination);
    if (mic) this.context.createMediaStreamSource(mic).connect(destination);
    if (this.context.state === 'suspended') await this.context.resume();
    this.mixed = destination.stream;
    this.mimeType = pickMimeType(env);

    // The user ending the share from the browser's own bar must not look like a normal stop.
    [...display.getTracks()].forEach((track) => track.addEventListener('ended', () => this.interrupt('Screen sharing was stopped.')));

    this.state = 'recording';
    this.beginSegment();
    this.timer = setInterval(() => this.tick(), this.tickMs);
  }

  beginSegment() {
    const options = { audioBitsPerSecond: 48000, ...(this.mimeType ? { mimeType: this.mimeType } : {}) };
    const recorder = new this.env.MediaRecorder(this.mixed, options);
    this.chunks = [];
    this.segmentStartMs = this.recordedMs;
    recorder.ondataavailable = (event) => { if (event.data && event.data.size > 0) this.chunks.push(event.data); };
    recorder.onerror = () => this.interrupt('The browser stopped recording unexpectedly.');
    recorder.onstop = () => this.finishSegment(recorder);
    recorder.start(1000);
    this.recorder = recorder;
  }

  finishSegment(recorder) {
    const durationMs = this.recordedMs - this.segmentStartMs;
    const blob = new this.env.Blob(this.chunks, { type: (recorder.mimeType || this.mimeType || 'audio/webm').split(';')[0] });
    this.chunks = [];
    const index = this.segmentIndex;
    if (blob.size > 0 && durationMs > 0) {
      this.segmentIndex += 1;
      this.onSegment({ index, offsetSeconds: this.segmentStartMs / 1000, durationSeconds: durationMs / 1000, blob });
    }
    if (this.state === 'recording' || this.state === 'paused') this.beginSegment();       // rotation, not the end
    else this.settle?.();
  }

  tick() {
    if (this.state !== 'recording') return;
    this.recordedMs += this.tickMs;
    if (this.recordedMs - this.segmentStartMs >= this.segmentMs) this.recorder.stop();    // onstop starts the next segment
  }

  pause() {
    if (this.state !== 'recording') return;
    this.recorder.pause();
    this.state = 'paused';
  }

  resume() {
    if (this.state !== 'paused') return;
    this.recorder.resume();
    this.state = 'recording';
  }

  /** Stops capture, emits the last segment, and releases the microphone and the tab share. */
  stop() {
    if (this.state !== 'recording' && this.state !== 'paused') return Promise.resolve();
    return new Promise((resolve) => {
      this.settle = () => { this.cleanup(); resolve(); };
      this.state = 'stopped';
      this.recorder.stop();
    });
  }

  interrupt(reason) {
    if (this.state !== 'recording' && this.state !== 'paused') return;
    this.state = 'interrupted';
    this.settle = () => { this.cleanup(); this.onInterrupted(reason); };
    try { this.recorder.stop(); } catch { this.settle(); }
  }

  cleanup() {
    clearInterval(this.timer);
    this.releaseStreams();
    try { this.context?.close(); } catch { /* already closed */ }
  }

  releaseStreams() {
    this.streams.forEach((stream) => stream.getTracks().forEach((track) => track.stop()));
    this.streams = [];
  }
}
