import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../../services/api';

const STATUS_LABEL = { NOT_STARTED: 'Not Started', IN_PROGRESS: 'In Progress', COMPLETED: 'Completed' };
const STATUS_STYLE = {
  NOT_STARTED: 'bg-slate-800 text-slate-300 border-slate-700',
  IN_PROGRESS: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
  COMPLETED: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
};

const isOverdue = (item) => item.due_date && item.status !== 'COMPLETED' && item.due_date < new Date().toISOString().slice(0, 10);

export default function ActionItemsPage() {
  const [scope, setScope] = useState('mine');
  const [filter, setFilter] = useState('');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [savingId, setSavingId] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await api.getActionItems(scope, filter));
      setError('');
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [scope, filter]);

  useEffect(() => { load(); }, [load]);

  const changeStatus = async (item, status) => {
    setSavingId(item.id);
    try {
      const updated = await api.updateActionItem(item.id, { status });
      setItems((all) => all.map((i) => (i.id === item.id ? updated : i)));
      setError('');
    } catch (e) {
      setError(e.message);
    } finally {
      setSavingId(null);
    }
  };

  return (
    <section className="space-y-4" aria-label="Action item tracker">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-white">Action items</h2>
          <p className="text-xs text-slate-400">
            {scope === 'mine' ? 'Tasks assigned to you from approved meeting minutes.' : 'Tasks from meetings you organized.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div role="tablist" className="flex rounded-lg border border-slate-700 overflow-hidden text-xs">
            {[['mine', 'Assigned to me'], ['organized', 'From my meetings']].map(([key, label]) => (
              <button key={key} role="tab" aria-selected={scope === key} onClick={() => setScope(key)}
                className={`px-3 py-1.5 cursor-pointer ${scope === key ? 'bg-indigo-600 text-white' : 'bg-slate-900 text-slate-300'}`}>{label}</button>
            ))}
          </div>
          <select aria-label="Filter by status" value={filter} onChange={(e) => setFilter(e.target.value)}
            className="bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-white">
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
      </div>

      {error && <p role="alert" className="text-sm text-rose-400">{error}</p>}
      {loading && <p className="text-sm text-slate-400">Loading…</p>}
      {!loading && items.length === 0 && (
        <p className="text-sm text-slate-500 bg-slate-900 border border-slate-800 rounded-xl p-6 text-center">
          {scope === 'mine' ? 'No action items are assigned to you.' : 'No action items yet. They appear here after you approve the minutes of a meeting.'}
        </p>
      )}

      {items.length > 0 && (
        <div className="overflow-x-auto bg-slate-900 border border-slate-800 rounded-xl">
          <table className="w-full text-xs">
            <thead className="text-slate-400 text-left border-b border-slate-800">
              <tr>
                <th className="p-3">Task</th><th className="p-3">Assigned to</th><th className="p-3">Due</th>
                <th className="p-3">Meeting</th><th className="p-3">Topic</th><th className="p-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className="border-b border-slate-800/60 align-top">
                  <td className="p-3 text-slate-100 max-w-xs">{item.description}</td>
                  <td className="p-3 text-slate-300">
                    {item.owner_name || 'Not specified'}
                    {item.owner_email && <div className="text-slate-500">{item.owner_email}</div>}
                  </td>
                  <td className={`p-3 ${isOverdue(item) ? 'text-rose-400 font-semibold' : 'text-slate-300'}`}>
                    {item.due_date || 'Not specified'}{isOverdue(item) ? ' (overdue)' : ''}
                  </td>
                  <td className="p-3 text-slate-300">
                    {item.meeting_title}
                    {item.meeting_date && <div className="text-slate-500">{new Date(item.meeting_date).toLocaleDateString()}</div>}
                    {scope === 'mine' && item.organizer_name && <div className="text-slate-500">Organizer: {item.organizer_name}</div>}
                  </td>
                  <td className="p-3 text-slate-400">{item.project_topic || '—'}</td>
                  <td className="p-3">
                    <select aria-label={`Status of ${item.description}`} value={item.status} disabled={savingId === item.id}
                      onChange={(e) => changeStatus(item, e.target.value)}
                      className={`rounded-lg border px-2 py-1 text-xs cursor-pointer ${STATUS_STYLE[item.status]}`}>
                      {Object.entries(STATUS_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
