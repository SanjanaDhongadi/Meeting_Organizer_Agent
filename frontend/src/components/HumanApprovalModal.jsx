import React, { useState } from 'react';
import { ShieldAlert, CheckCircle2, XCircle, Edit3, Calendar, Clock, MapPin, Video, AlertTriangle, Users, FileText } from 'lucide-react';

const toLocalDateTime = (value) => {
  if (!value) return '';
  const date = new Date(value);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
};

export default function HumanApprovalModal({ meeting, onClose, onApprove, onReject, onSaveEdit }) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedTitle, setEditedTitle] = useState(meeting.title || '');
  const [editedStart, setEditedStart] = useState(toLocalDateTime(meeting.scheduled_start));
  const [editedEnd, setEditedEnd] = useState(toLocalDateTime(meeting.scheduled_end));
  const [editedDuration, setEditedDuration] = useState(meeting.duration_minutes || 30);
  const [editedMode, setEditedMode] = useState(meeting.mode || 'ONLINE');
  const [editedRoom, setEditedRoom] = useState(meeting.room_name || '');
  const [editedAgenda, setEditedAgenda] = useState(meeting.agenda || '');
  const [editedPurpose, setEditedPurpose] = useState(meeting.purpose || '');
  const [notes, setNotes] = useState('');

  if (!meeting) return null;

  const isOnline = meeting.mode === 'ONLINE';
  // The backend only accepts APPROVE while the plan waits at the approval gate.
  const canApprove = meeting.status === 'WAITING_FOR_HUMAN_APPROVAL';
  const conflicts = meeting.conflicts || [];
  const validation = meeting.validation || meeting.parsed_details?.validation || {};
  const warnings = validation.warnings || [];

  const handleApprove = () => {
    onApprove(meeting.meeting_id || meeting.id, notes);
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
      agenda: editedAgenda,
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

        {/* Warnings / Conflicts Banner */}
        {(conflicts.length > 0 || warnings.length > 0) && (
          <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-sm space-y-2">
            <div className="flex items-center space-x-2 font-semibold text-amber-400">
              <AlertTriangle className="w-4 h-4" />
              <span>Action Items & Constraints Requiring Operator Review:</span>
            </div>
            <ul className="list-disc list-inside space-y-1 text-xs text-amber-200/90 pl-2">
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
                {isOnline ? <Video className="w-5 h-5 text-indigo-400" /> : <MapPin className="w-5 h-5 text-indigo-400" />}
                <div>
                  <div className="text-xs text-slate-400">Format and venue</div>
                  <div className="text-xs font-semibold text-slate-100">
                    {isOnline ? `Online${meeting.meet_url ? ` · ${meeting.meet_url}` : ' · Meet link created after approval'}` : (meeting.room_name || 'Room not specified')}
                  </div>
                </div>
              </div>
            </div>

            {/* Participants */}
            <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-2">
              <div className="flex items-center space-x-2 text-xs font-semibold text-slate-400">
                <Users className="w-4 h-4 text-indigo-400" />
                <span>Participants and availability</span>
              </div>
              <div className="space-y-2 pt-1">
                {(meeting.participants || []).map((p, idx) => (
                  <div key={p.email || idx} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-xs">
                    <div>
                      <div className="font-medium text-slate-200">{p.name}</div>
                      <div className="text-slate-500 font-mono text-[11px]">{p.email}</div>
                    </div>
                    <span className="text-slate-500">
                      {p.calendar_status || (p.is_external ? 'UNVERIFIED' : 'Availability not verified')}
                      {p.response_status && p.response_status !== 'PENDING' ? ` · ${p.response_status}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* Agenda */}
            <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-2">
              <div className="flex items-center justify-between text-xs font-semibold text-slate-400">
                <div className="flex items-center space-x-2">
                  <FileText className="w-4 h-4 text-indigo-400" />
                  <span>Draft agenda</span>
                </div>
                <span className="text-[11px] text-indigo-400 cursor-pointer hover:underline" onClick={() => setIsEditing(true)}>
                  Edit draft
                </span>
              </div>
              <div className="p-3 bg-slate-900/90 rounded-lg text-xs font-mono text-slate-300 whitespace-pre-line border border-slate-800">
                {meeting.agenda || 'No agenda generated.'}
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
                  <label className="text-slate-300 font-semibold mb-1 block">Room or auditorium</label>
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
            <div>
              <label className="text-slate-300 font-semibold mb-1 block">Agenda Content</label>
              <textarea
                rows={5}
                value={editedAgenda}
                onChange={(e) => setEditedAgenda(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-white font-mono"
              />
            </div>
          </div>
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
              disabled={!canApprove || isEditing}
              title={canApprove ? 'Creates the calendar event / sends invitations or the room request' : 'Save edits first: the plan must be re-validated before approval'}
              className="flex items-center space-x-2 px-5 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs transition-all shadow-lg shadow-emerald-600/30 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <CheckCircle2 className="w-4 h-4" />
              <span>Approve and send</span>
            </button>
          </div>
        </div>

      </div>
    </div>
  );
}
