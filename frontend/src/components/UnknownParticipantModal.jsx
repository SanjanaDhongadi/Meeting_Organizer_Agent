import React, { useState } from 'react';
import { UserX, AlertCircle } from 'lucide-react';
import { api } from '../services/api';

export default function UnknownParticipantModal({ meetingId, unknowns, ambiguities, onClose, onResolved }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  if ((!unknowns || unknowns.length === 0) && (!ambiguities || ambiguities.length === 0)) {
    return null;
  }

  const run = async (action) => {
    setLoading(true);
    setError('');
    try {
      const plan = await action();
      if (onResolved) await onResolved(plan);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  // Ambiguous name: the organizer picks one of the REGISTERED employees that matched.
  const handleSelect = (name, candidateEmail) =>
    run(() => api.resolveParticipant(meetingId, { queried_name: name, selected_email: candidateEmail }));
  const handleRecheck = (name) => run(() => api.recheckParticipant(meetingId, name));
  const handleRemove = (name) => run(() => api.removeUnresolvedParticipant(meetingId, name));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
      <div className="relative w-full max-w-xl glass-panel-glow bg-slate-900 border border-slate-700/80 rounded-2xl p-6 sm:p-7 shadow-2xl space-y-5">
        
        <div className="flex items-start justify-between border-b border-slate-800 pb-3">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-rose-500/20 text-rose-400 flex items-center justify-center">
              <UserX className="w-6 h-6" />
            </div>
            <div>
              <h3 className="font-bold text-white text-base">Participant Verification Required</h3>
              <p className="text-xs text-slate-400">Every attendee must be a registered employee. Pick the right person or register them first.</p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white" aria-label="Close">✕</button>
        </div>

        {/* Ambiguous participants */}
        {ambiguities && ambiguities.length > 0 && (
          <div className="space-y-3">
            <div className="text-xs font-semibold text-amber-400 flex items-center space-x-1.5">
              <AlertCircle className="w-4 h-4" />
              <span>Ambiguous Name Collisions (Select Specific Colleague):</span>
            </div>
            {ambiguities.map((item, idx) => (
              <div key={idx} className="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-2 text-xs">
                <span className="font-bold text-white">Queried name: "{item.queried_name}"</span>
                <p className="text-slate-400 text-[11px]">{item.message}</p>
                <div className="space-y-1.5 pt-1">
                  {(item.candidates || []).map((cand, cIdx) => (
                    <div key={cIdx} className="flex items-center justify-between p-2 rounded-lg bg-slate-900 border border-slate-800 hover:border-slate-700">
                      <div>
                        <div className="font-semibold text-slate-200">{cand.name}</div>
                        <div className="text-[11px] text-slate-400 font-mono">{cand.email} • {cand.designation}</div>
                      </div>
                      <button
                        disabled={loading}
                        onClick={() => handleSelect(item.queried_name, cand.email)}
                        className="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-xs transition-all"
                      >
                        Select
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Unregistered participants: no e-mail entry — employees must be registered first */}
        {unknowns && unknowns.length > 0 && (
          <div className="space-y-3">
            <div className="text-xs font-semibold text-rose-400 flex items-center space-x-1.5">
              <AlertCircle className="w-4 h-4" />
              <span>Attendees not found in the employee directory:</span>
            </div>
            {unknowns.map((item) => (
              <div key={item.queried_name} className="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-2 text-xs">
                {item.is_venue_coordinator ? (
                  <p className="text-slate-200" role="alert">{item.message}</p>
                ) : (
                  <p className="text-slate-200" role="alert">
                    The attendee <strong>{item.queried_name}</strong> is not registered in the employee directory.
                    Please ensure that the employee is registered before scheduling the meeting.
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  {!item.is_venue_coordinator && (
                    <>
                      <a href="#employees" className="px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-200 font-medium">
                        Open Employees
                      </a>
                      <button
                        disabled={loading}
                        onClick={() => handleRecheck(item.queried_name)}
                        className="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium"
                      >
                        Re-check (after registering)
                      </button>
                    </>
                  )}
                  <button
                    disabled={loading}
                    onClick={() => handleRemove(item.queried_name)}
                    className="px-3 py-1.5 rounded-lg bg-rose-500/20 border border-rose-500/30 text-rose-400 font-medium"
                  >
                    Remove from meeting
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}

      </div>
    </div>
  );
}
