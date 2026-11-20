import React, { useState } from 'react';
import { Trash2 } from 'lucide-react';

/** Confirmation before deleting one meeting; explains exactly what will be cancelled. */
export default function ConfirmDeleteDialog({ meeting, onConfirm, onCancel }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const hasEvent = !!meeting.calendar_event_id;
  const hasRoomRequest = (meeting.room_bookings || []).some((rb) => ['PENDING', 'CONFIRMED'].includes(rb.status));

  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      await onConfirm(meeting);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/80" role="alertdialog" aria-label="Delete meeting">
      <div className="w-full max-w-md glass-panel-glow bg-slate-900 border border-slate-700 rounded-2xl p-5 space-y-3">
        <div className="flex items-center gap-2">
          <Trash2 className="w-5 h-5 text-rose-400" />
          <h3 className="font-bold text-white">Delete “{meeting.title || 'Untitled meeting'}”?</h3>
        </div>
        <ul className="text-sm text-slate-300 list-disc pl-5 space-y-1">
          {hasEvent && <li>The Google Calendar event will be cancelled and attendees will be notified by Google.</li>}
          {hasRoomRequest && <li>A cancellation will be sent for the room/auditorium request.</li>}
          {!hasEvent && !hasRoomRequest && <li>Nothing has been sent for this meeting yet.</li>}
          <li>The meeting is removed from the list; its audit history is kept.</li>
        </ul>
        {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onCancel} disabled={busy} className="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-sm">Keep meeting</button>
          <button type="button" onClick={confirm} disabled={busy}
            className="px-4 py-2 rounded-xl text-white text-sm font-semibold" style={{ background: '#a23f43' }}>
            {busy ? 'Deleting…' : 'Delete meeting'}
          </button>
        </div>
      </div>
    </div>
  );
}
