import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Circle, Pause, Play, Square, Mic } from 'lucide-react';
import { api } from '../services/api';
import { MeetingRecorder, describeCaptureError, isRecordingSupported } from '../services/meetingRecorder';

const clock = (seconds) => {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const btn = 'px-3 py-1.5 rounded-lg border text-xs font-semibold cursor-pointer disabled:opacity-50 flex items-center gap-1.5';

/**
 * Alternative to Google's own transcript (which personal Gmail accounts do not get): the organizer records the
 * Meet tab's audio and microphone in the browser. Recording only starts after consent is confirmed and the browser's
 * own share prompt is accepted. Segments are uploaded as they finish and can be retried one by one.
 */
export default function RecordingPanel({ meetingId, status, recording, onChanged, onActiveChange }) {
  const [consent, setConsent] = useState(false);
  const [phase, setPhase] = useState('idle');          // idle | starting | recording | paused | stopping | interrupted
  const [elapsed, setElapsed] = useState(0);
  const [uploaded, setUploaded] = useState(0);
  const [total, setTotal] = useState(0);
  const [failed, setFailed] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);   // segments recorded but not yet stored on the server
  const recorderRef = useRef(null);
  const pending = useRef(new Map());                   // index -> segment still to upload (kept in memory until stored)
  const inflight = useRef([]);

  const supported = isRecordingSupported();
  const active = ['starting', 'recording', 'paused', 'stopping'].includes(phase);
  const serverSegments = recording?.segments?.length || 0;

  useEffect(() => { if (onActiveChange) onActiveChange(active); }, [active, onActiveChange]);

  // Closing the tab mid-recording would lose the segment that is still being captured.
  useEffect(() => {
    if (!active) return undefined;
    const warn = (event) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [active]);

  useEffect(() => {
    if (phase !== 'recording') return undefined;
    const timer = setInterval(() => setElapsed(recorderRef.current ? recorderRef.current.elapsedSeconds : 0), 500);
    return () => clearInterval(timer);
  }, [phase]);

  useEffect(() => () => { recorderRef.current?.stop(); }, []);

  const upload = useCallback(async (segment) => {
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        await api.uploadRecordingSegment(meetingId, { ...segment, blob: segment.blob });
        pending.current.delete(segment.index);
        setPendingCount(pending.current.size);
        setFailed((list) => list.filter((i) => i !== segment.index));
        setUploaded((n) => n + 1);
        return true;
      } catch {
        if (attempt < 3) await sleep(1000 * 2 ** attempt);
      }
    }
    setFailed((list) => (list.includes(segment.index) ? list : [...list, segment.index]));
    return false;
  }, [meetingId]);

  const onSegment = useCallback((segment) => {
    pending.current.set(segment.index, segment);
    setPendingCount(pending.current.size);
    setTotal((n) => n + 1);
    inflight.current.push(upload(segment));
  }, [upload]);

  const start = async () => {
    setError('');
    setNotice('');
    setWarnings([]);
    setPhase('starting');
    try {
      const started = await api.startRecording(meetingId);       // consent is recorded server-side; native transcript wins
      if (started.native_transcript) {
        setNotice(started.message);
        setPhase('idle');
        onChanged();
        return;
      }
      pending.current = new Map();
      setPendingCount(0);
      inflight.current = [];
      setElapsed(0); setUploaded(0); setTotal(0); setFailed([]);
      const recorder = new MeetingRecorder({
        onSegment,
        onWarning: (text) => setWarnings((list) => [...list, text]),
        onInterrupted: async (reason) => {
          setPhase('interrupted');
          await Promise.all(inflight.current);
          setError(`${reason} The audio recorded so far was saved. You can finish and transcribe it, or discard it.`);
          onChanged();
        },
      });
      recorderRef.current = recorder;
      await recorder.start();
      setPhase('recording');
      onChanged();
    } catch (e) {
      const problem = describeCaptureError(e);
      setError(problem.message);
      setPhase('idle');
      onChanged();
    }
  };

  const finishAndTranscribe = async () => {
    await Promise.all(inflight.current);
    if (pending.current.size) {
      setError(`${pending.current.size} audio segment(s) could not be uploaded. Retry the upload before finishing.`);
      setPhase('interrupted');
      return;
    }
    await api.finishRecording(meetingId, recorderRef.current ? recorderRef.current.segmentCount : null);
    setPhase('idle');
    setNotice('The recording was uploaded. Transcription and the minutes are being prepared.');
    onChanged();
  };

  const stop = async () => {
    setPhase('stopping');
    setError('');
    try {
      await recorderRef.current.stop();
      await finishAndTranscribe();
    } catch (e) {
      setError(e.message);
      setPhase('interrupted');
    }
  };

  const retryUploads = async () => {
    setError('');
    setBusy(true);
    await Promise.all([...pending.current.values()].map((segment) => upload(segment)));
    setBusy(false);
    if (!pending.current.size) {
      try { await finishAndTranscribe(); } catch (e) { setError(e.message); }
    } else {
      setError('Some audio segments still could not be uploaded. Check your connection and retry.');
    }
  };

  const act = async (fn, done) => {
    setBusy(true);
    setError('');
    try { await fn(); if (done) setNotice(done); onChanged(); } catch (e) { setError(e.message); } finally { setBusy(false); }
  };

  const orphaned = status === 'RECORDING_IN_PROGRESS' && !active && phase !== 'interrupted';
  const canStart = ['WAITING_FOR_MEETING_END', 'WAITING_FOR_TRANSCRIPT', 'TRANSCRIPT_UNAVAILABLE'].includes(status) && phase === 'idle';

  if (!['WAITING_FOR_MEETING_END', 'WAITING_FOR_TRANSCRIPT', 'TRANSCRIPT_UNAVAILABLE', 'RECORDING_IN_PROGRESS', 'AUDIO_UPLOADED',
    'TRANSCRIBING', 'TRANSCRIPTION_FAILED'].includes(status)) return null;

  return (
    <section className="space-y-3 bg-slate-950 border border-slate-800 rounded-xl p-4" aria-label="Record the meeting audio">
      <h3 className="text-sm font-semibold text-slate-200 flex items-center gap-2"><Mic className="w-4 h-4 text-indigo-400" /> Record the meeting audio instead</h3>

      {canStart && (
        <>
          <p className="text-xs text-slate-400">
            Google only creates transcripts for Workspace accounts that have transcription turned on. If this meeting will not have one, you can record it here:
            your browser will ask you to pick the Google Meet tab and share its audio, and to allow your microphone. Recording happens only after you start it, and your
            browser shows that the tab is being shared. The audio is uploaded to this app and transcribed; the transcript then feeds the same minutes workflow.
          </p>
          {recording && recording.transcription_configured === false && (
            <p role="status" className="text-xs text-amber-300">Speech-to-text is not configured on the server (GROQ_API_KEY). Audio can be recorded but will not be transcribed until it is set.</p>
          )}
          {!supported && <p role="alert" className="text-xs text-rose-400">This browser cannot capture tab audio. Use a recent desktop Chrome or Edge.</p>}
          <label className="flex items-start gap-2 text-xs text-slate-300 cursor-pointer">
            <input type="checkbox" className="mt-0.5" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
            <span>I have told every participant that this meeting is being recorded and transcribed, and they agree.</span>
          </label>
          <button type="button" disabled={!consent || !supported || busy} onClick={start}
            className={`${btn} bg-rose-600/20 border-rose-500/40 text-rose-200`}><Circle className="w-3.5 h-3.5 fill-current" /> Start recording</button>
        </>
      )}

      {phase === 'starting' && <p className="text-xs text-slate-400">Choose the Google Meet tab in the browser prompt and tick “Share tab audio”…</p>}

      {(phase === 'recording' || phase === 'paused' || phase === 'stopping') && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <span data-testid="recording-indicator" role="status" className="flex items-center gap-2 text-xs font-bold text-rose-300">
              <span className={`inline-block w-2.5 h-2.5 rounded-full bg-rose-500 ${phase === 'recording' ? 'animate-pulse' : ''}`} />
              {phase === 'recording' ? 'RECORDING' : phase === 'paused' ? 'PAUSED' : 'FINISHING'} · {clock(elapsed)}
            </span>
            {phase === 'recording' && <button type="button" className={`${btn} border-slate-700 text-slate-200`} onClick={() => { recorderRef.current.pause(); setPhase('paused'); }}><Pause className="w-3.5 h-3.5" /> Pause</button>}
            {phase === 'paused' && <button type="button" className={`${btn} border-slate-700 text-slate-200`} onClick={() => { recorderRef.current.resume(); setPhase('recording'); }}><Play className="w-3.5 h-3.5" /> Resume</button>}
            {phase !== 'stopping' && <button type="button" className={`${btn} bg-slate-800 border-slate-600 text-white`} onClick={stop}><Square className="w-3.5 h-3.5" /> Stop and transcribe</button>}
          </div>
          <p className="text-[11px] text-slate-500">Audio uploaded: {uploaded} of {total} finished segments. Keep this window open until you press Stop.</p>
        </div>
      )}

      {phase === 'interrupted' && (
        <div className="flex flex-wrap gap-2">
          {pendingCount > 0 && <button type="button" disabled={busy} className={`${btn} border-slate-700 text-slate-200`} onClick={retryUploads}>Retry upload</button>}
          {pendingCount === 0 && <button type="button" disabled={busy} className={`${btn} border-slate-700 text-slate-200`} onClick={() => act(() => api.finishRecording(meetingId, null).then(() => setPhase('idle')), 'Transcription started.')}>Finish and transcribe what was saved</button>}
          <button type="button" disabled={busy} className={`${btn} border-slate-700 text-slate-400`} onClick={() => act(() => api.discardRecording(meetingId).then(() => setPhase('idle')))}>Discard recording</button>
        </div>
      )}

      {orphaned && (
        <div className="space-y-2">
          <p className="text-xs text-amber-300">A recording was started earlier but is not running in this window{serverSegments ? ` (${serverSegments} segment(s) were saved)` : ''}.</p>
          <div className="flex gap-2">
            {serverSegments > 0 && <button type="button" disabled={busy} className={`${btn} border-slate-700 text-slate-200`} onClick={() => act(() => api.finishRecording(meetingId, null), 'Transcription started.')}>Finish and transcribe what was saved</button>}
            <button type="button" disabled={busy} className={`${btn} border-slate-700 text-slate-400`} onClick={() => act(() => api.discardRecording(meetingId))}>Discard recording</button>
          </div>
        </div>
      )}

      {['AUDIO_UPLOADED', 'TRANSCRIBING'].includes(status) && <p className="text-xs text-slate-400">The audio is being transcribed. This can take a few minutes for long meetings.</p>}
      {status === 'TRANSCRIPTION_FAILED' && (
        <div className="flex gap-2">
          <button type="button" disabled={busy} className={`${btn} border-slate-700 text-slate-200`} onClick={() => act(() => api.retryTranscription(meetingId), 'Retrying the transcription.')}>Retry transcription</button>
          <button type="button" disabled={busy} className={`${btn} border-slate-700 text-slate-400`} onClick={() => act(() => api.discardRecording(meetingId))}>Discard recording</button>
        </div>
      )}

      {failed.length > 0 && phase !== 'interrupted' && <p role="alert" className="text-xs text-amber-300">{failed.length} audio segment(s) are waiting to be uploaded; they will be retried when you stop.</p>}
      {warnings.map((w, i) => <p key={i} role="status" className="text-xs text-amber-300">{w}</p>)}
      {notice && <p role="status" className="text-xs text-emerald-300">{notice}</p>}
      {error && <p role="alert" className="text-xs text-rose-400">{error}</p>}
    </section>
  );
}
