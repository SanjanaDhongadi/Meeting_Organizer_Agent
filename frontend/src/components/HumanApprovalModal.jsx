import React, { useState } from 'react';
import AgendaPointsEditor from './AgendaPointsEditor';
import FollowUpTasksPanel from './FollowUpTasksPanel';
import { availabilityLabel, availabilityStyle } from '../services/availability';
import { ShieldAlert, CheckCircle2, XCircle, Edit3, Calendar, Clock, Video, AlertTriangle, Users, FileText, Building2 } from 'lucide-react';

const toLocalDateTime = (value) => {
  if (!value) return '';
  const date = new Date(value);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
};

export default function HumanApprovalModal({ meeting, onClose, onApprove, onReject, onSaveEdit, onRefresh }) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedTitle, setEditedTitle] = useState(meeting.title || '');
  const [editedStart, setEditedStart] = useState(toLocalDateTime(meeting.scheduled_start));
  const [editedEnd, setEditedEnd] = useState(toLocalDateTime(meeting.scheduled_end));
  const [editedDuration, setEditedDuration] = useState(meeting.duration_minutes || 30);
  const [editedMode, setEditedMode] = useState(meeting.mode || 'ONLINE');
  const [editedRoom, setEditedRoom] = useState(meeting.room_name || '');
  const [editedAgendaPoints, setEditedAgendaPoints] = useState(
    meeting.agenda_points?.length ? meeting.agenda_points
      : (meeting.agenda || '').split('\\n').map((l) => l.replace(/^\s*\d+[.)]\s*/, '').trim()).filter(Boolean)
  );
  const [editedPurpose, setEditedPurpose] = useState(meeting.purpose || '');
  const [notes, setNotes] = useState('');
  const [submitting, setSubmitting] = useState(false);

  if (!meeting) return null;

  const isOnline = meeting.mode === 'ONLINE';
  // The backend accepts APPROVE at the approval gate, and again after a failed send (retry).
  const isRetry = meeting.status === 'ACTION_FAILED';
  const readiness = meeting.execution_readiness || { ready: true };
  const canApprove = (meeting.status === 'WAITING_FOR_HUMAN_APPROVAL' || isRetry) && readiness.ready !== false;
  const approveTitle = readiness.ready === false
    ? readiness.error
    : (canApprove ? 'Creates the calendar event / sends invitations or the room request' : 'Save edits first: the plan must be re-validated before approval');
  const conflicts = (meeting.conflicts || []).filter(c =>
    !c.conflict?.toLowerCase().includes('availability could not be verified') &&
    !c.conflict?.toLowerCase().includes('not registered')
  );
  const validation = meeting.validation || meeting.parsed_details?.validation || {};
  const warnings = (validation.warnings || []).filter(w =>
    !w.toLowerCase().includes('availability could not be verified') &&
    !w.toLowerCase().includes('is external') &&
    !w.toLowerCase().includes('not registered')
  );

  const handleApprove = async () => {
    setSubmitting(true);
    try {
      await onApprove(meeting.meeting_id || meeting.id, notes);
    } finally {
      setSubmitting(false);
    }
  };

  const handleReject = () => {
    onReject(meeting.meeting_id || meeting.id, notes || 'Rejected by organizer during review.');
  };

  const handleSaveEdit = () => {
    onSaveEdit(meeting.meeting_id || meeting.id, {
      title: editedTitle,
      scheduled_start: editedStart ? new Date(editedStart).toISOString() : '',
      scheduled_end: editedEnd ? new Date(editedEnd).toISOString() : '',
      duration_minutes: Number(editedDuration),
      mode: editedMode,
      room_name: editedRoom,
      agenda_points: editedAgendaPoints.map((p) => p.trim()).filter(Boolean),
      purpose: editedPurpose,
    }, notes);
    setIsEditing(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md overflow-y-auto">
      <div className="relative w-full max-w-3xl glass-panel-glow bg-slate-900 border border-slate-700/80 rounded-2xl p-6 sm:p-8 shadow-2xl space-y-6 max-h-[90vh] overflow-y-auto">
        
        {/* Header */}
        <div className="flex items-start justify-between border-b border-slate-800 pb-4">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-amber-500/20 border border-amber-500/30 flex items-center justify-center text-amber-400">
              <ShieldAlert className="w-6 h-6 animate-pulse" />
            </div>
            <div>
              <h2 className="text-xl font-bold text-white flex items-center space-x-2">
                <span>Review meeting plan</span>
              </h2>
              <p className="text-xs text-slate-400">Review the complete draft before any invitations or booking requests are sent.</p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl">✕</button>
        </div>

        {/* What approval needs but is missing (e.g. no connected Google account), and why a previous send failed */}
        {readiness.ready === false && (
          <div role="alert" className="p-4 rounded-xl bg-rose-50 border border-rose-300 text-rose-800 text-sm space-y-1">
            <div className="flex items-center space-x-2 font-semibold">
              <AlertTriangle className="w-4 h-4" />
              <span>Approval is not possible yet</span>
            </div>
            <p className="text-xs">{readiness.error}</p>
          </div>
        )}
        {isRetry && meeting.execution_error?.error && (
          <div role="status" className="p-4 rounded-xl bg-amber-50 border border-amber-300 text-amber-900 text-sm space-y-1">
            <div className="font-semibold">The last attempt to send this meeting failed</div>
            <p className="text-xs">{meeting.execution_error.error}</p>
            <p className="text-xs">Fix the cause above, then use “Retry sending”. Nothing new is sent until you do.</p>
          </div>
        )}

        {/* Warnings / Conflicts Banner */}
        {(conflicts.length > 0 || warnings.length > 0) && (
          <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-sm space-y-2">
            <div className="flex items-center space-x-2 font-semibold text-amber-400">
              <AlertTriangle className="w-4 h-4" />
              <span>Action Items & Constraints Requiring Operator Review:</span>
            </div>
            <ul className="list-disc list-inside space-y-1 text-xs text-amber-900 pl-2">
              {conflicts.map((c, i) => (
                <li key={i}>
                  <strong>{c.conflict}:</strong> {c.resolution}
                </li>
              ))}
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        )}

        {/* View vs Edit Mode */}
        {!isEditing ? (
          <div className="space-y-4 text-sm">
            
            {/* Title & Purpose */}
            <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800">
              <h3 className="font-semibold text-white text-base mb-1">{meeting.title || 'Untitled Meeting'}</h3>
              <p className="text-slate-300 text-xs">
                <span className="text-slate-500 font-medium">Purpose: </span>
                {meeting.purpose || <em className="text-slate-500">None specified</em>}
              </p>
            </div>

            {/* Core Specs Grid */}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div className="bg-slate-950/60 p-3.5 rounded-xl border border-slate-800 flex items-center space-x-3">
                <Calendar className="w-5 h-5 text-indigo-400 flex-shrink-0" />
                <div>
                  <div className="text-xs text-slate-400">Date</div>
                  <div className="text-xs font-semibold text-slate-100 truncate">
                    {meeting.scheduled_start ? new Date(meeting.scheduled_start).toLocaleDateString() : 'Not provided'}
                  </div>
                </div>
              </div>

              <div className="bg-slate-950/60 p-3.5 rounded-xl border border-slate-800 flex items-center space-x-3">
                <Clock className="w-5 h-5 text-indigo-400 flex-shrink-0" />
                <div>
                  <div className="text-xs text-slate-400">Time</div>
                  <div className="text-xs font-semibold text-slate-100">
                    {meeting.scheduled_start ? new Date(meeting.scheduled_start).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : 'Not provided'}
                  </div>
                </div>
              </div>

              <div className="bg-slate-950/60 p-3.5 rounded-xl border border-slate-800 flex items-center space-x-3">
                <Clock className="w-5 h-5 text-indigo-400 flex-shrink-0" />
                <div>
                  <div className="text-xs text-slate-400">Duration</div>
                  <div className="text-xs font-semibold text-slate-100">{meeting.duration_minutes || 30} Minutes</div>
                </div>
              </div>

              <div className="bg-slate-950/60 p-3.5 rounded-xl border border-slate-800 flex items-center space-x-3">
                {isOnline ? <Video className="w-5 h-5 text-indigo-400" /> : <Building2 className="w-5 h-5 text-indigo-400" />}
                <div>
                  <div className="text-xs text-slate-400">Format and venue</div>
                  <div className="text-xs font-semibold text-slate-100">
                    {isOnline ? (
                      <span>Online · {meeting.meet_url ? (
                        <a href={meeting.meet_url} target="_blank" rel="noreferrer" className="text-indigo-300 underline break-all">{meeting.meet_url}</a>
                      ) : (meeting.meet_error || 'Google Meet link is not available yet.')}</span>
                    ) : (meeting.room_name || 'Room not specified')}
                  </div>
                </div>
              </div>
            </div>

            {/* Offline requests are sent separately by the organizer from the Venue Coordinator tab. */}
            {!isOnline && meeting.venue_coordination && (
              <div className="p-3.5 rounded-xl border border-slate-200 bg-slate-50 text-xs text-slate-700">
                <div className="font-semibold">Venue coordinator: {meeting.venue_coordination.coordinator_email || 'Email not provided'}</div>
                <div className="mt-1">Booking email: {{ NOT_SENT: 'not sent · review and send it from the Venue Coordinator tab', SENDING: 'sending', SENT: 'sent · waiting for reply',
                  CONFIRMED: 'confirmed', DECLINED: 'declined' }[meeting.venue_coordination.request_status] || 'not sent'}</div>
              </div>
            )}

            {/* Participants */}
            <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-2">
              <div className="flex items-center space-x-2 text-xs font-semibold text-slate-400">
                <Users className="w-4 h-4 text-indigo-400" />
                <span>Participants</span>
              </div>
              <div className="space-y-2 pt-1">
                {(meeting.participants || []).map((p, idx) => (
                  <div key={p.email || idx} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-xs">
                    <div>
                      <div className="font-medium text-slate-200">{p.name}</div>
                      <div className="text-slate-400 font-mono text-[11px]">{p.email}</div>
                    </div>
                    <span title={p.availability_reason || ''} className={`border px-2 py-0.5 rounded text-[11px] font-medium ${availabilityStyle(p.calendar_status)}`}>
                      {availabilityLabel(p.calendar_status)}
                    </span>
                  </div>
                ))}
                {meeting.organizer_availability && (
                  <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-xs">
                    <div>
                      <div className="font-medium text-slate-200">You (organizer)</div>
                      <div className="text-slate-400 font-mono text-[11px]">{meeting.organizer_availability.email}</div>
                    </div>
                    <span title={meeting.organizer_availability.reason || ''} className={`border px-2 py-0.5 rounded text-[11px] font-medium ${availabilityStyle(meeting.organizer_availability.status)}`}>
                      {availabilityLabel(meeting.organizer_availability.status)}
                    </span>
                  </div>
                )}
              </div>
            </div>

            {/* Agenda */}
            <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-2">
              <div className="flex items-center justify-between text-xs font-semibold text-slate-400">
                <div className="flex items-center space-x-2">
                  <FileText className="w-4 h-4 text-indigo-400" />
                  <span>Agenda</span>
                </div>
                <span className="text-[11px] text-indigo-400 cursor-pointer hover:underline" onClick={() => setIsEditing(true)}>
                  Edit agenda
                </span>
              </div>
              <div className="p-3 bg-slate-900/90 rounded-lg text-xs font-mono text-slate-300 whitespace-pre-line border border-slate-800 leading-relaxed">
                {meeting.agenda || 'No agenda provided.'}
              </div>
            </div>

          </div>
        ) : (
          /* Edit Mode Form */
          <div className="space-y-4 text-xs">
            <div>
              <label className="text-slate-300 font-semibold mb-1 block">Meeting Title</label>
              <input
                type="text"
                value={editedTitle}
                onChange={(e) => setEditedTitle(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-slate-300 font-semibold mb-1 block">Date and start time</label>
                <input
                  type="datetime-local"
                  value={editedStart}
                  onChange={(e) => setEditedStart(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white"
                />
              </div>
              <div>
                <label className="text-slate-300 font-semibold mb-1 block">End date and time</label>
                <input
                  type="datetime-local"
                  value={editedEnd}
                  onChange={(e) => setEditedEnd(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white"
                />
              </div>
            </div>
            <div>
              <label className="text-slate-300 font-semibold mb-1 block">Duration (minutes)</label>
              <input
                type="number"
                min="1"
                value={editedDuration}
                onChange={(e) => setEditedDuration(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white"
              />
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="text-slate-300 font-semibold mb-1 block">Meeting format</label>
                <select
                  value={editedMode}
                  onChange={(e) => setEditedMode(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white"
                >
                  <option value="ONLINE">Online</option>
                  <option value="OFFLINE">Offline</option>
                </select>
              </div>
              {editedMode === 'OFFLINE' && (
                <div>
                  <label className="text-slate-300 font-semibold mb-1 block">Venue</label>
                  <input
                    type="text"
                    value={editedRoom}
                    onChange={(e) => setEditedRoom(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white"
                  />
                </div>
              )}
            </div>
            <div>
              <label className="text-slate-300 font-semibold mb-1 block">Meeting Purpose</label>
              <input
                type="text"
                value={editedPurpose}
                onChange={(e) => setEditedPurpose(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white"
              />
            </div>
            <AgendaPointsEditor points={editedAgendaPoints} onChange={setEditedAgendaPoints} label="Agenda (optional)" />
          </div>
        )}

        {/* Unfinished tasks from earlier meetings: the organizer picks which ones to discuss */}
        {!isEditing && ['WAITING_FOR_HUMAN_APPROVAL', 'RESCHEDULING_REQUIRED'].includes(meeting.status) && (
          <FollowUpTasksPanel meetingId={meeting.meeting_id || meeting.id} onSaved={onRefresh} />
        )}

        {/* Operator Note */}
        <div>
          <label className="text-xs text-slate-400 font-semibold mb-1 block">Approval / Operator Notes</label>
          <input
            type="text"
            placeholder="Optional signoff notes or conditions..."
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-xs text-white"
          />
        </div>

        {/* Hard Gate Action Buttons */}
        <div className="border-t border-slate-800 pt-4 flex items-center justify-between">
          <button
            onClick={handleReject}
            className="flex items-center space-x-2 px-4 py-2.5 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-400 hover:bg-rose-500/20 font-semibold text-xs transition-all"
          >
            <XCircle className="w-4 h-4" />
            <span>Reject draft</span>
          </button>

          <div className="flex items-center space-x-3">
            {isEditing ? (
              <button
                onClick={handleSaveEdit}
                className="flex items-center space-x-2 px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs transition-all shadow-md shadow-indigo-600/30"
              >
                <CheckCircle2 className="w-4 h-4" />
                <span>Save Edits</span>
              </button>
            ) : (
              <button
                onClick={() => setIsEditing(true)}
                className="flex items-center space-x-2 px-4 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 font-semibold text-xs transition-all border border-slate-700"
              >
                <Edit3 className="w-4 h-4" />
                <span>Edit draft</span>
              </button>
            )}

            <button
              onClick={handleApprove}
              disabled={!canApprove || isEditing || submitting}
              aria-busy={submitting}
              title={approveTitle}
              className="flex items-center space-x-2 px-5 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs transition-all shadow-lg shadow-emerald-600/30 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <CheckCircle2 className="w-4 h-4" />
              <span>{submitting ? 'Sending…' : (isRetry ? 'Retry sending' : 'Approve and send')}</span>
            </button>
          </div>
        </div>

      </div>
    </div>
  );
}
