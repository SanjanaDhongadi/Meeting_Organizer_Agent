// Run with: node --test src/services/meetingRecorder.test.js   (no browser needed; the browser APIs are faked)
import test from 'node:test';
import assert from 'node:assert/strict';
import { MeetingRecorder, RecordingError, describeCaptureError, isRecordingSupported, pickMimeType } from './meetingRecorder.js';

class FakeTrack {
  constructor(kind, settings = {}) { this.kind = kind; this.stopped = false; this.listeners = {}; this.settings = settings; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  getSettings() { return this.settings; }
  stop() { this.stopped = true; }
  end() { this.listeners.ended?.(); }
}
class FakeStream {
  constructor(tracks = []) { this.tracks = tracks; }
  getTracks() { return this.tracks; }
  getAudioTracks() { return this.tracks.filter((t) => t.kind === 'audio'); }
  getVideoTracks() { return this.tracks.filter((t) => t.kind === 'video'); }
}
class FakeMediaRecorder {
  static isTypeSupported(type) { return type === 'audio/webm;codecs=opus'; }
  constructor(stream, options) { this.options = options; this.state = 'inactive'; FakeMediaRecorder.instances.push(this); }
  start() { this.state = 'recording'; setTimeout(() => this.ondataavailable?.({ data: { size: 100 } }), 0); }
  pause() { this.state = 'paused'; }
  resume() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; setTimeout(() => this.onstop?.(), 0); }
}
FakeMediaRecorder.instances = [];
class FakeContext {
  constructor() { this.state = 'running'; }
  createMediaStreamDestination() { return { stream: new FakeStream() }; }
  createMediaStreamSource() { return { connect() {} }; }
  close() { this.closed = true; }
}
const FakeBlob = class { constructor(parts) { this.size = parts.length * 100; } };

function makeEnv({ displayTracks, micFails = false, displayError = null } = {}) {
  FakeMediaRecorder.instances = [];
  const display = new FakeStream(displayTracks || [new FakeTrack('video', { displaySurface: 'browser' }), new FakeTrack('audio')]);
  const mic = new FakeStream([new FakeTrack('audio')]);
  return {
    display, mic,
    navigator: { mediaDevices: {
      getDisplayMedia: async () => { if (displayError) throw displayError; return display; },
      getUserMedia: async () => { if (micFails) throw Object.assign(new Error('no'), { name: 'NotAllowedError' }); return mic; },
    } },
    MediaRecorder: FakeMediaRecorder, AudioContext: FakeContext, MediaStream: FakeStream, Blob: FakeBlob,
  };
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function recorder(env, handlers = {}, options = {}) {
  const segments = [];
  const warnings = [];
  const interrupted = [];
  const r = new MeetingRecorder({
    onSegment: (s) => segments.push(s), onWarning: (w) => warnings.push(w), onInterrupted: (w) => interrupted.push(w), ...handlers,
  }, { segmentMs: 100, tickMs: 10, env, ...options });
  return { r, segments, warnings, interrupted };
}

test('unsupported browsers are detected and refuse to start', async () => {
  assert.equal(isRecordingSupported({ navigator: {} }), false);
  const { r } = recorder({ navigator: {} });
  await assert.rejects(r.start(), (e) => e.code === 'UNSUPPORTED');
  assert.equal(pickMimeType(makeEnv()), 'audio/webm;codecs=opus');
});

test('denied permission is reported and nothing is recorded', async () => {
  const env = makeEnv({ displayError: Object.assign(new Error('denied'), { name: 'NotAllowedError' }) });
  const { r, segments } = recorder(env);
  await assert.rejects(r.start(), (e) => e instanceof RecordingError && e.code === 'PERMISSION_DENIED' && /Nothing was recorded/.test(e.message));
  assert.equal(FakeMediaRecorder.instances.length, 0);
  assert.equal(segments.length, 0);
  assert.equal(describeCaptureError({ name: 'NotFoundError' }).code, 'NO_DEVICE');
});

test('a share without audio is rejected and released', async () => {
  const env = makeEnv({ displayTracks: [new FakeTrack('video')] });
  const { r } = recorder(env);
  await assert.rejects(r.start(), (e) => e.code === 'NO_AUDIO_TRACK');
  assert.ok(env.display.getTracks().every((t) => t.stopped));
  assert.equal(FakeMediaRecorder.instances.length, 0);
});

test('a missing microphone only warns; sharing a window warns', async () => {
  const env = makeEnv({ micFails: true, displayTracks: [new FakeTrack('video', { displaySurface: 'monitor' }), new FakeTrack('audio')] });
  const { r, warnings } = recorder(env);
  await r.start();
  assert.equal(warnings.length, 2);
  assert.match(warnings.join(' '), /microphone/);
  assert.match(warnings.join(' '), /window or screen/);
  await r.stop();
});

test('recording is cut into segments with offsets that exclude paused time', async () => {
  const env = makeEnv();
  const { r, segments } = recorder(env);
  await r.start();
  assert.equal(r.state, 'recording');
  await wait(150);                        // > one 100 ms segment
  r.pause();
  const frozen = r.recordedMs;
  await wait(100);
  assert.equal(r.recordedMs, frozen, 'time does not advance while paused');
  r.resume();
  await wait(120);
  await r.stop();
  assert.equal(r.state, 'stopped');
  assert.ok(segments.length >= 2, `expected rotation, got ${segments.length}`);
  assert.deepEqual(segments.map((s) => s.index), segments.map((_, i) => i));
  segments.reduce((expectedOffset, s) => {
    assert.ok(Math.abs(s.offsetSeconds - expectedOffset) < 1e-6, 'each segment starts where the previous one ended');
    assert.ok(s.durationSeconds > 0 && s.blob.size > 0);
    return s.offsetSeconds + s.durationSeconds;
  }, 0);
  assert.ok(env.display.getTracks().every((t) => t.stopped) && env.mic.getTracks().every((t) => t.stopped), 'capture released');
  assert.equal(FakeMediaRecorder.instances[0].options.mimeType, 'audio/webm;codecs=opus');
});

test('stopping the share from the browser is an interruption that keeps the audio so far', async () => {
  const env = makeEnv();
  const { r, segments, interrupted } = recorder(env, {}, { segmentMs: 10000 });
  await r.start();
  await wait(60);
  env.display.getTracks()[0].end();
  await wait(30);
  assert.equal(r.state, 'interrupted');
  assert.equal(interrupted.length, 1);
  assert.equal(segments.length, 1);
  assert.ok(env.mic.getTracks().every((t) => t.stopped));
});
