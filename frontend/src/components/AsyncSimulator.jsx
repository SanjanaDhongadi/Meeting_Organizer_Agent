import React, { useState } from 'react';
import { FastForward, CheckCircle2, XCircle, Building2, UserCheck } from 'lucide-react';
import { api } from '../services/api';

export default function AsyncSimulator({ meeting, onStateChange }) {
  const [loading, setLoading] = useState(false);
  const [selectedParticipant, setSelectedParticipant] = useState('');
  const [error, setError] = useState('');

  if (!meeting) return null;

  const participants = meeting.participants || [];
  const roomName = meeting.room_name || '';
  const waitingForParticipants = meeting.status === 'WAITING_FOR_PARTICIPANTS';
  const waitingForRoom = ['WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_ROOM'].includes(meeting.status);
  if (!waitingForParticipants && !waitingForRoom) return null;

  const handleSimulateParticipant = async (response) => {
    if (!selectedParticipant) return;
    setLoading(true);
    setError('');
    try {
      await api.simulateParticipant(
        meeting.id || meeting.meeting_id,
        selectedParticipant,
        response,
        `Simulated ${response.toLowerCase()} (not a real reply)`
      );
      if (onStateChange) onStateChange();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleSimulateRoom = async (confirmed) => {
    setLoading(true);
    setError('');
    try {
      await api.simulateRoom(
        meeting.id || meeting.meeting_id,
        roomName,
        confirmed,
        confirmed ? 'Facility manager signed off on room allocation.' : 'Room unavailable due to maintenance.'
      );
      if (onStateChange) onStateChange();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="glass-panel p-5 rounded-2xl border border-indigo-500/30 bg-indigo-950/10 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-indigo-500/20 text-indigo-400 flex items-center justify-center">
            <FastForward className="w-5 h-5" />
          </div>
          <div>
            <h4 className="font-bold text-white text-sm">Simulated response (testing only)</h4>
            <p className="text-xs text-slate-400">Responses entered here are recorded as SIMULATED, not as real replies. Use “Check for responses” to read real Google Calendar / Gmail replies.</p>
          </div>
        </div>
        <span className="text-xs font-mono px-2.5 py-1 rounded-full bg-slate-900 border border-slate-700 text-indigo-300">
          {waitingForRoom ? 'Request sent · waiting for auditorium response' : 'Waiting for participant response'}
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
        
        {waitingForParticipants && <div className="bg-slate-900/80 p-3.5 rounded-xl border border-slate-800 space-y-3">
          <div className="flex items-center space-x-2 font-semibold text-slate-300">
            <UserCheck className="w-4 h-4 text-blue-400" />
            <span>Simulate Participant Response</span>
          </div>

          <div className="space-y-2">
            <div>
              <label className="text-[11px] text-slate-500 block mb-1">Select Attendee</label>
              <select
                value={selectedParticipant}
                onChange={(e) => setSelectedParticipant(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-slate-200"
              >
                <option value="">-- Choose attendee --</option>
                {participants.map((p) => (
                  <option key={p.email} value={p.email}>
                    {p.name} ({p.email}) - {p.response_status || 'PENDING'}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex items-center space-x-2 pt-1">
              <button
                disabled={!selectedParticipant || loading}
                onClick={() => handleSimulateParticipant('ACCEPTED')}
                className="flex-1 flex items-center justify-center space-x-1.5 py-2 rounded-lg bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 font-semibold border border-emerald-500/30 transition-all disabled:opacity-50"
              >
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Simulate Participant Accept</span>
              </button>

              <button
                disabled={!selectedParticipant || loading}
                onClick={() => handleSimulateParticipant('REJECTED')}
                className="flex-1 flex items-center justify-center space-x-1.5 py-2 rounded-lg bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 font-semibold border border-rose-500/30 transition-all disabled:opacity-50"
              >
                <XCircle className="w-3.5 h-3.5" />
                <span>Simulate Participant Reject</span>
              </button>
            </div>
          </div>
        </div>}

        {waitingForRoom && <div className="bg-slate-900/80 p-3.5 rounded-xl border border-slate-800 space-y-3">
          <div className="flex items-center space-x-2 font-semibold text-slate-300">
            <Building2 className="w-4 h-4 text-cyan-400" />
            <span>Auditorium response</span>
          </div>

          <div className="space-y-2">
            <div>
              <span className="text-[11px] text-slate-500 block mb-1">Target Space</span>
              <div className="p-2 rounded bg-slate-950 border border-slate-800 font-semibold text-slate-200">
                {roomName}
              </div>
            </div>

            <div className="flex items-center space-x-2 pt-1">
              <button
                disabled={loading}
                onClick={() => handleSimulateRoom(true)}
                className="flex-1 flex items-center justify-center space-x-1.5 py-2 rounded-lg bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-400 font-semibold border border-cyan-500/30 transition-all disabled:opacity-50"
              >
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Simulate Auditorium Confirm</span>
              </button>

              <button
                disabled={loading}
                onClick={() => handleSimulateRoom(false)}
                className="flex-1 flex items-center justify-center space-x-1.5 py-2 rounded-lg bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 font-semibold border border-rose-500/30 transition-all disabled:opacity-50"
              >
                <XCircle className="w-3.5 h-3.5" />
                <span>Simulate Auditorium Reject</span>
              </button>
            </div>
          </div>
        </div>}

      </div>
      {error && <p role="alert" className="text-sm text-rose-400">{error}</p>}
    </div>
  );
}
