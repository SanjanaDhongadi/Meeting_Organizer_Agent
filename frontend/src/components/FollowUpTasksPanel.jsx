import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../services/api';

const STATUS_LABEL = { NOT_STARTED: 'Not Started', IN_PROGRESS: 'In Progress', COMPLETED: 'Completed' };

/**
 * Unfinished tasks from the organizer's earlier meetings, offered as follow-up topics for this meeting.
 * Nothing is selected for the organizer; skipped tasks stay open in the Action Item Tracker.
 */
export default function FollowUpTasksPanel({ meetingId, onSaved }) {
  const [data, setData] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const [selected, setSelected] = useState([]);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async (all) => {
    try {
      const result = await api.getFollowupSuggestions(meetingId, all);
      setData(result);
      setSelected((current) => (current.length ? current : result.selected_ids || []));
      setError('');
    } catch (e) {
      setError(e.message);
    }
  }, [meetingId]);

  useEffect(() => { load(showAll); }, [load, showAll]);

  const toggle = (id) => {
    setMessage('');
    setSelected((all) => (all.includes(id) ? all.filter((x) => x !== id) : [...all, id]));
  };

  const save = async () => {
    setSaving(true);
    try {
      await api.saveFollowups(meetingId, selected);
      setMessage(selected.length ? `${selected.length} follow-up task(s) added after your agenda.` : 'No follow-up tasks selected.');
      setError('');
      if (onSaved) onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  if (!data) return error ? <p role="alert" className="text-xs text-rose-400">{error}</p> : null;
  if (data.total_unfinished === 0 && !error) return null;

  const row = (task) => (
    <li key={task.id} className="flex gap-2 items-start bg-slate-950 border border-slate-800 rounded-lg p-2.5">
      <input type="checkbox" id={`followup-${task.id}`} checked={selected.includes(task.id)} onChange={() => toggle(task.id)} className="mt-1" />
      <label htmlFor={`followup-${task.id}`} className="text-xs text-slate-200 cursor-pointer flex-1">
        <span className="font-semibold">{task.description}</span>
        <span className="block text-slate-400">
          Owner: {task.owner_name || task.owner_email || 'Not specified'} · {STATUS_LABEL[task.status]}
          {task.due_date ? ` · due ${task.due_date}` : ''} · from “{task.meeting_title}”
        </span>
        {(task.reasons || []).length > 0 && <span className="block text-indigo-300">{task.reasons.join(' · ')}</span>}
      </label>
    </li>
  );

  return (
    <div className="space-y-2" aria-label="Follow-up tasks">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-slate-300">Follow up on earlier tasks (optional)</h3>
        <button type="button" onClick={() => setShowAll((v) => !v)} className="text-xs text-indigo-300 cursor-pointer">
          {showAll ? 'Show relevant only' : `Show all unfinished tasks (${data.total_unfinished})`}
        </button>
      </div>
      <p className="text-[11px] text-slate-500">Select the tasks to discuss in this meeting. They are added after your own agenda. Tasks you skip stay open.</p>
      {data.relevant.length === 0 && <p className="text-xs text-slate-500">No unfinished tasks look relevant to this meeting.</p>}
      <ul className="space-y-1.5">{data.relevant.map(row)}</ul>
      {showAll && data.others.length > 0 && (
        <>
          <h4 className="text-[11px] text-slate-500 pt-1">Other unfinished tasks</h4>
          <ul className="space-y-1.5">{data.others.map(row)}</ul>
        </>
      )}
      <div className="flex items-center gap-3">
        <button type="button" onClick={save} disabled={saving}
          className="px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-200 text-xs cursor-pointer disabled:opacity-50">
          {saving ? 'Saving…' : 'Save follow-up selection'}
        </button>
        {message && <span role="status" className="text-xs text-emerald-300">{message}</span>}
        {error && <span role="alert" className="text-xs text-rose-400">{error}</span>}
      </div>
    </div>
  );
}
