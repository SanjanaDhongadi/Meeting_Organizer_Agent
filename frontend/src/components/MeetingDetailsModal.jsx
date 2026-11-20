import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../services/api';
import { availabilityLabel } from '../services/availability';

const WAITING = ['WAITING_FOR_PARTICIPANTS', 'WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_ROOM'];

const fmt = (iso, opts) => (iso ? new Date(iso).toLocaleString([], opts) : null);

/** Meeting details loaded fresh from the backend (never from the cached list). */
export default function MeetingDetailsModal({ meetingId, onClose, onChanged, onDelete, readableStatus, statusClass, children }) {
  const [meeting, setMeeting] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notFound, setNotFound] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState(null);

  const load = useCallback(async ({ sync = false } = {}) => {
    setLoading(true);
    setError('');
    try {
      let syncError = '';
      if (sync) {
        // Pull real Google Calendar / Gmail responses first for meetings that are waiting.
        const res = await api.syncResponses(meetingId);
        const errs = (res.results || []).filter((r) => r.status === 'ERROR');
        if (errs.length) syncError = errs.map((e) => `${e.capability || 'response check'}: ${e.error || e.message}`).join(' | ');
      }
      const data = await api.getMeeting(meetingId);
      setMeeting(data);
      setNotFound(false);
      setRefreshedAt(new Date());
      if (syncError) setError(`Could not read responses from Google: ${syncError}`);
      if (onChanged) onChanged(data);
    } catch (e) {
      if (/not found/i.test(e.message)) setNotFound(true);
      else setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [meetingId, onChanged]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingId]);

  const refresh = () => load({ sync: !!meeting && (WAITING.includes(meeting.status) || meeting.venue_coordination?.request_status === 'SENT') });
  const googleSays = { accepted: 'accepted', declined: 'declined', tentative: 'maybe', needsAction: 'no response yet' };
  const venue = meeting?.venue_coordination;
  const time = { hour: 'numeric', minute: '2-digit' };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md" role="dialog" aria-label="Meeting details">
      <div className="relative w-full max-w-2xl glass-panel-glow bg-slate-900 border border-slate-700 rounded-2xl p-6 shadow-2xl space-y-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-start justify-between gap-3 border-b border-slate-800 pb-3">
          <div>
            <div className="text-xs text-slate-500">Meeting details</div>
            <h2 className="text-lg font-bold text-white">{meeting?.title || (loading ? 'Loading…' : 'Untitled meeting')}</h2>
            {meeting && <span className={statusClass(meeting.status)}>{readableStatus(meeting.status)}</span>}
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={refresh} disabled={loading}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-700 text-slate-300 text-xs" style={{ fontSize: 12 }}>
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
            {meeting && onDelete && !['APPROVED', 'EXECUTING'].includes(meeting.status) && (
              <button type="button" onClick={() => onDelete(meeting)}
                className="px-3 py-1.5 rounded-lg border border-rose-500/30 text-rose-400 text-xs" style={{ fontSize: 12 }}>
                Delete
              </button>
            )}
            <button type="button" onClick={onClose} className="text-slate-400 hover:text-white" aria-label="Close">✕</button>
          </div>
        </div>

        {notFound && <p role="alert" className="text-sm text-rose-700">This meeting no longer exists.</p>}
        {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}

        {meeting && !notFound && (
          <div className="space-y-4 text-sm">
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
              <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
                <dt className="text-slate-500">Purpose</dt><dd className="font-semibold text-white">{meeting.purpose || 'Not provided'}</dd>
              </div>
              <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
                <dt className="text-slate-500">Date and time</dt>
                <dd className="font-semibold text-white">
                  {meeting.scheduled_start
                    ? `${fmt(meeting.scheduled_start, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}, ${fmt(meeting.scheduled_start, time)} – ${fmt(meeting.scheduled_end, time)}`
                    : 'Not set'}
                </dd>
              </div>
              <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
                <dt className="text-slate-500">Duration</dt><dd className="font-semibold text-white">{meeting.duration_minutes ? `${meeting.duration_minutes} minutes` : 'Not set'}</dd>
              </div>
              <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
                <dt className="text-slate-500">{meeting.mode === 'OFFLINE' ? 'Venue' : meeting.mode === 'ONLINE' ? 'Google Meet' : 'Mode'}</dt>
                <dd className="font-semibold text-white break-all">
                  {meeting.mode === 'OFFLINE'
                    ? (meeting.room_name || 'Not set')
                    : meeting.mode === 'ONLINE'
                      ? (meeting.meet_url
                        ? <a href={meeting.meet_url} target="_blank" rel="noreferrer" className="underline">{meeting.meet_url}</a>
                        : 'Online · Google Meet link appears after approval')
                      : 'Not set'}
                </dd>
              </div>
            </dl>

            <div>
              <h3 className="font-semibold text-slate-700 mb-1">Attendees</h3>
              {(meeting.participants || []).length === 0 && <p className="text-xs text-slate-500">No attendees yet.</p>}
              <table className="w-full text-xs">
                <tbody>
                  {(meeting.participants || []).map((p) => (
                    <tr key={p.email} className="border-b border-slate-200">
                      <td className="py-1.5 pr-2">{p.name}<div className="text-slate-500">{p.email}</div></td>
                      <td className="py-1.5 pr-2 text-slate-600">Status: {availabilityLabel(p.calendar_status)}</td>
                      <td className="py-1.5 text-slate-600">
                        Response: <strong>{p.response_status || 'PENDING'}</strong>
                        {p.notes && <div className="text-slate-500">{p.notes}</div>}
                        {meeting.google_sync?.attendees?.[p.email.toLowerCase()] && (
                          <div className="text-slate-500">
                            Google Calendar: {googleSays[meeting.google_sync.attendees[p.email.toLowerCase()]] || meeting.google_sync.attendees[p.email.toLowerCase()]}
                            {' '}(checked {fmt(meeting.google_sync.checked_at, time)})
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div>
              <h3 className="font-semibold text-slate-700 mb-1">Agenda</h3>
              <pre className="text-xs whitespace-pre-wrap text-slate-700 bg-slate-950 p-3 rounded-xl border border-slate-800">{meeting.agenda || 'No agenda provided.'}</pre>
            </div>

            {venue && (
              <div>
                <h3 className="font-semibold text-slate-700 mb-1">Venue coordinator</h3>
                <table className="w-full text-xs">
                  <tbody>
                    <tr className="border-b border-slate-200">
                      <td className="py-1.5 pr-2">{venue.coordinator_name}<div className="text-slate-500">{venue.coordinator_email || 'Not configured'}</div></td>
                      <td className="py-1.5 pr-2 text-slate-600">
                        Venue: {venue.venue || 'Not set'}
                      </td>
                      <td className="py-1.5 text-slate-600">
                        Request: <strong>{{ NOT_SENT: 'Not sent', SENT: 'Sent · waiting for reply', CONFIRMED: 'Confirmed', DECLINED: 'Declined' }[venue.request_status] || venue.request_status}</strong>
                        {venue.request?.notes && <div className="text-slate-500 whitespace-pre-wrap">{venue.request.notes}</div>}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            )}
            {meeting.execution_error && (
              <p role="alert" className="text-xs text-rose-700">
                Execution failed ({meeting.execution_error.step}){meeting.execution_error.failed_at ? ` on ${fmt(meeting.execution_error.failed_at)}` : ''}: {meeting.execution_error.error}
              </p>
            )}
            {(meeting.warnings || []).map((w, i) => <p key={i} className="text-xs text-amber-900">{w}</p>)}
            {refreshedAt && <p className="text-[11px] text-slate-400">Last refreshed {refreshedAt.toLocaleTimeString()}</p>}
            {children && children(meeting)}
          </div>
        )}
      </div>
    </div>
  );
}
