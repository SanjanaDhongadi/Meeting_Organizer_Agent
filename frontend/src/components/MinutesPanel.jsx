import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Plus, X, Send, FileText } from 'lucide-react';
import { api } from '../services/api';
import { MINUTES_STATUS } from './minutesStatus';
import RecordingPanel from './RecordingPanel';

const BUSY = ['GENERATING_MOM', 'DISTRIBUTING', 'TRANSCRIPT_RETRIEVED', 'AUDIO_UPLOADED', 'TRANSCRIBING'];
const inputCls = 'w-full bg-slate-950 border border-slate-700 rounded-lg px-2 py-1.5 text-white text-sm';

export default function MinutesPanel({ meetingId, onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [summary, setSummary] = useState('');
  const [points, setPoints] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [items, setItems] = useState([]);
  const [recordingActive, setRecordingActive] = useState(false);

  const load = useCallback(async () => {
    try {
      const fresh = await api.getMinutes(meetingId);
      setData(fresh);
      setError('');
      return fresh;
    } catch (e) {
      setError(e.message);
      return null;
    }
  }, [meetingId]);

  useEffect(() => { load(); }, [load]);

  // Poll while Google/the language model is working; never overwrite edits the organizer is typing.
  useEffect(() => {
    if (!data || !BUSY.includes(data.status)) return undefined;
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [data, load]);

  const draft = data?.minutes?.mom_draft;
  const editable = data?.status === 'AWAITING_ORGANIZER_APPROVAL';
  useEffect(() => {
    if (!data || dirty) return;
    setSummary(draft?.summary || '');
    setPoints((draft?.discussion_points || []).map((p) => ({ ...p })));
    setDecisions((draft?.decisions || []).map((d) => ({ ...d })));
    setItems((data.action_items || []).map((i) => ({ ...i })));
  }, [data, draft, dirty]);

  const run = async (fn, onDone) => {
    setBusy(true);
    setError('');
    try {
      const result = await fn();
      if (onDone) onDone(result);
      setDirty(false);
      await load();
      if (onChanged) onChanged();
    } catch (e) {
      setError(e.message);
      await load();
    } finally {
      setBusy(false);
    }
  };

  const payload = () => ({
    summary,
    discussion_points: points.filter((p) => (p.topic || '').trim() || (p.details || '').trim())
      .map((p) => ({ topic: p.topic || '', details: p.details || '' })),
    decisions: decisions.filter((d) => (d.text || '').trim()).map((d) => ({ text: d.text, evidence: d.evidence || '' })),
    action_items: items.map((i) => ({
      id: i.id, description: i.description, owner_email: i.owner_email || null, owner_name: i.owner_name || '',
      due_date: i.due_date || null, project_topic: i.project_topic || '',
    })),
  });

  const save = () => run(() => api.saveMinutes(meetingId, payload()));
  const approve = () => run(async () => {
    if (editable) await api.saveMinutes(meetingId, payload());
    return api.approveMinutes(meetingId);
  });
  const regenerate = () => {
    if (!window.confirm('Generate the minutes again from the transcript? Your edits to this draft will be replaced.')) return;
    run(() => api.regenerateMinutes(meetingId));
  };

  const setItem = (index, changes) => { setDirty(true); setItems((all) => all.map((it, i) => (i === index ? { ...it, ...changes } : it))); };
  const people = data?.participants || [];
  const status = data?.status;
  const minutes = data?.minutes;
  const readOnly = !editable;
  const shownItems = editable ? items : (data?.action_items || []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md overflow-y-auto">
      <div className="relative w-full max-w-4xl bg-slate-900 border border-slate-700/80 rounded-2xl p-6 shadow-2xl space-y-5 max-h-[90vh] overflow-y-auto">
        <div className="flex items-start justify-between border-b border-slate-800 pb-3">
          <div className="flex items-center gap-3">
            <FileText className="w-6 h-6 text-indigo-400" />
            <div>
              <h2 className="text-lg font-bold text-white">Minutes of Meeting</h2>
              <p className="text-xs text-slate-400">{draft?.meeting?.title || 'Meeting'} {draft?.meeting?.date ? `· ${draft.meeting.date}` : ''}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={load} aria-label="Refresh"
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-700 text-slate-300 text-xs cursor-pointer">
              <RefreshCw className="w-3.5 h-3.5" /> Refresh
            </button>
            <button type="button" onClick={onClose} disabled={recordingActive} aria-label="Close"
              title={recordingActive ? 'Stop the recording before closing this window' : 'Close'}
              className="text-slate-400 hover:text-white cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed">✕</button>
          </div>
        </div>

        {error && <p role="alert" className="text-sm text-rose-400">{error}</p>}
        {!data && !error && <p className="text-sm text-slate-400">Loading…</p>}

        {data && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <span data-testid="minutes-status" className="px-3 py-1 rounded-full bg-indigo-500/20 border border-indigo-500/40 text-indigo-200 text-xs font-semibold">
                {MINUTES_STATUS[status] || status}
              </span>
              {status === 'WAITING_FOR_TRANSCRIPT' && minutes?.next_attempt_at && (
                <span className="text-xs text-slate-400">Next automatic check: {new Date(minutes.next_attempt_at).toLocaleTimeString()} (attempt {minutes.retrieval_attempts + 1})</span>
              )}
              {['WAITING_FOR_TRANSCRIPT', 'TRANSCRIPT_UNAVAILABLE'].includes(status) && (
                <button type="button" disabled={busy} onClick={() => run(() => api.checkTranscript(meetingId))}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-200 text-xs cursor-pointer disabled:opacity-50">
                  Check for transcript now
                </button>
              )}
              {['MOM_FAILED'].includes(status) && (
                <button type="button" disabled={busy} onClick={() => run(() => api.regenerateMinutes(meetingId))}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-200 text-xs cursor-pointer disabled:opacity-50">
                  Try generating again
                </button>
              )}
            </div>

            <RecordingPanel meetingId={meetingId} status={status} recording={data.recording}
              onChanged={() => { load(); if (onChanged) onChanged(); }} onActiveChange={setRecordingActive} />

            {minutes?.last_error && !['AWAITING_ORGANIZER_APPROVAL', 'APPROVED_AND_DISTRIBUTED'].includes(status) && (
              <p role="status" className="text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-lg p-3">{minutes.last_error}</p>
            )}
            {status === 'WAITING_FOR_TRANSCRIPT' && (
              <p className="text-xs text-slate-400">The transcript is fetched automatically from Google Meet after the meeting ends; nothing needs to be uploaded. Transcription must have been turned on during the meeting.</p>
            )}
            {status === 'APPROVED_AND_DISTRIBUTED' && minutes?.distributed_at && (
              <p className="text-xs text-emerald-300">Sent to the participants on {new Date(minutes.distributed_at).toLocaleString()}.</p>
            )}

            {draft && ['AWAITING_ORGANIZER_APPROVAL', 'APPROVED_SEND_FAILED', 'APPROVED_AND_DISTRIBUTED'].includes(status) && (
              <div className="space-y-5 text-sm">
                <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                  <div className="bg-slate-950 p-3 rounded-xl border border-slate-800"><dt className="text-slate-500">Date</dt><dd className="text-white font-semibold">{draft.meeting?.date || 'Not specified'}</dd></div>
                  <div className="bg-slate-950 p-3 rounded-xl border border-slate-800"><dt className="text-slate-500">Duration</dt><dd className="text-white font-semibold">{draft.meeting?.duration_minutes ? `${draft.meeting.duration_minutes} min` : 'Not specified'}</dd></div>
                  <div className="bg-slate-950 p-3 rounded-xl border border-slate-800 col-span-2"><dt className="text-slate-500">Participants</dt><dd className="text-white font-semibold">{(draft.meeting?.participants || []).map((p) => p.name).join(', ') || 'Not specified'}</dd></div>
                </dl>

                {(draft.warnings || []).length > 0 && (
                  <ul className="text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-lg p-3 list-disc list-inside">
                    {draft.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                )}

                <section>
                  <h3 className="font-semibold text-slate-200 mb-1">Summary</h3>
                  {readOnly ? <p className="text-slate-300 whitespace-pre-wrap">{draft.summary || 'Not specified'}</p>
                    : <textarea aria-label="Summary" rows={4} className={inputCls} value={summary} onChange={(e) => { setDirty(true); setSummary(e.target.value); }} />}
                </section>

                <section>
                  <h3 className="font-semibold text-slate-200 mb-1">Key discussion points</h3>
                  {readOnly ? (
                    <ul className="list-disc list-inside text-slate-300 space-y-1">
                      {(draft.discussion_points || []).map((p, i) => <li key={i}><strong>{p.topic}</strong>{p.topic && p.details ? ': ' : ''}{p.details}</li>)}
                      {(draft.discussion_points || []).length === 0 && <li className="list-none text-slate-500">None recorded</li>}
                    </ul>
                  ) : (
                    <div className="space-y-2">
                      {points.map((p, i) => (
                        <div key={i} className="flex gap-2 items-start">
                          <input aria-label={`Discussion topic ${i + 1}`} placeholder="Topic" className={`${inputCls} w-1/3`} value={p.topic || ''}
                            onChange={(e) => { setDirty(true); setPoints((a) => a.map((x, j) => (j === i ? { ...x, topic: e.target.value } : x))); }} />
                          <input aria-label={`Discussion details ${i + 1}`} placeholder="Details" className={inputCls} value={p.details || ''}
                            onChange={(e) => { setDirty(true); setPoints((a) => a.map((x, j) => (j === i ? { ...x, details: e.target.value } : x))); }} />
                          <button type="button" aria-label={`Remove discussion point ${i + 1}`} className="p-1 text-slate-400 hover:text-rose-400 cursor-pointer"
                            onClick={() => { setDirty(true); setPoints((a) => a.filter((_, j) => j !== i)); }}><X className="w-4 h-4" /></button>
                        </div>
                      ))}
                      <button type="button" className="text-xs text-indigo-300 flex items-center gap-1 cursor-pointer"
                        onClick={() => { setDirty(true); setPoints((a) => [...a, { topic: '', details: '' }]); }}><Plus className="w-3.5 h-3.5" /> Add point</button>
                    </div>
                  )}
                </section>

                <section>
                  <h3 className="font-semibold text-slate-200 mb-1">Decisions</h3>
                  {readOnly ? (
                    <ul className="list-disc list-inside text-slate-300 space-y-1">
                      {(draft.decisions || []).map((d, i) => <li key={i}>{d.text}</li>)}
                      {(draft.decisions || []).length === 0 && <li className="list-none text-slate-500">None recorded</li>}
                    </ul>
                  ) : (
                    <div className="space-y-2">
                      {decisions.map((d, i) => (
                        <div key={i} className="flex gap-2 items-center">
                          <input aria-label={`Decision ${i + 1}`} className={inputCls} value={d.text || ''}
                            onChange={(e) => { setDirty(true); setDecisions((a) => a.map((x, j) => (j === i ? { ...x, text: e.target.value } : x))); }} />
                          <button type="button" aria-label={`Remove decision ${i + 1}`} className="p-1 text-slate-400 hover:text-rose-400 cursor-pointer"
                            onClick={() => { setDirty(true); setDecisions((a) => a.filter((_, j) => j !== i)); }}><X className="w-4 h-4" /></button>
                        </div>
                      ))}
                      <button type="button" className="text-xs text-indigo-300 flex items-center gap-1 cursor-pointer"
                        onClick={() => { setDirty(true); setDecisions((a) => [...a, { text: '', evidence: '' }]); }}><Plus className="w-3.5 h-3.5" /> Add decision</button>
                    </div>
                  )}
                </section>

                <section>
                  <h3 className="font-semibold text-slate-200 mb-1">Action items</h3>
                  <div className="space-y-2">
                    {shownItems.map((it, i) => (
                      <div key={it.id ?? i} className="grid grid-cols-1 md:grid-cols-12 gap-2 items-center bg-slate-950 border border-slate-800 rounded-lg p-2">
                        {readOnly ? (
                          <>
                            <div className="md:col-span-6 text-slate-200">{it.description}</div>
                            <div className="md:col-span-3 text-xs text-slate-400">Owner: {it.owner_name || 'Not specified'}</div>
                            <div className="md:col-span-3 text-xs text-slate-400">Due: {it.due_date || 'Not specified'}</div>
                          </>
                        ) : (
                          <>
                            <input aria-label={`Action item ${i + 1} description`} className={`${inputCls} md:col-span-5`} value={it.description}
                              onChange={(e) => setItem(i, { description: e.target.value })} />
                            <select aria-label={`Action item ${i + 1} owner`} className={`${inputCls} md:col-span-3`} value={it.owner_email || ''}
                              onChange={(e) => setItem(i, { owner_email: e.target.value || null })}>
                              <option value="">Owner: not specified{it.owner_name && !it.owner_email ? ` (heard: ${it.owner_name})` : ''}</option>
                              {people.map((p) => <option key={p.email} value={(p.email || '').toLowerCase()}>{p.name}</option>)}
                            </select>
                            <input aria-label={`Action item ${i + 1} due date`} type="date" className={`${inputCls} md:col-span-3`} value={it.due_date || ''}
                              onChange={(e) => setItem(i, { due_date: e.target.value || null })} />
                            <button type="button" aria-label={`Remove action item ${i + 1}`} className="p-1 text-slate-400 hover:text-rose-400 cursor-pointer md:col-span-1"
                              onClick={() => { setDirty(true); setItems((a) => a.filter((_, j) => j !== i)); }}><X className="w-4 h-4" /></button>
                          </>
                        )}
                      </div>
                    ))}
                    {shownItems.length === 0 && <p className="text-xs text-slate-500">No action items were found in the transcript.</p>}
                    {editable && (
                      <button type="button" className="text-xs text-indigo-300 flex items-center gap-1 cursor-pointer"
                        onClick={() => { setDirty(true); setItems((a) => [...a, { description: '', owner_email: null, owner_name: '', due_date: null, project_topic: draft.topic || '' }]); }}>
                        <Plus className="w-3.5 h-3.5" /> Add action item</button>
                    )}
                  </div>
                  {editable && <p className="text-[11px] text-slate-500 mt-1">Action items become tasks for their owners only after you approve and send.</p>}
                </section>

                <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 pt-4">
                  {editable ? (
                    <button type="button" disabled={busy} onClick={regenerate} className="px-3 py-2 rounded-lg border border-slate-700 text-slate-300 text-xs cursor-pointer disabled:opacity-50">Regenerate from transcript</button>
                  ) : <span />}
                  <div className="flex gap-2">
                    {editable && (
                      <button type="button" disabled={busy || !dirty} onClick={save}
                        className="px-4 py-2 rounded-lg bg-slate-800 border border-slate-700 text-white text-xs font-semibold cursor-pointer disabled:opacity-40">Save edits</button>
                    )}
                    {(editable || status === 'APPROVED_SEND_FAILED') && (
                      <button type="button" disabled={busy} onClick={approve} aria-busy={busy}
                        className="flex items-center gap-2 px-5 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold cursor-pointer disabled:opacity-50">
                        <Send className="w-4 h-4" /> {busy ? 'Sending…' : (status === 'APPROVED_SEND_FAILED' ? 'Retry sending' : 'Approve and send')}
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
