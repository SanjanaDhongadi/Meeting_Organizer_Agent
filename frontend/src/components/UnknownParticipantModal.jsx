import React, { useState } from 'react';
import { UserX, AlertCircle } from 'lucide-react';
import { api } from '../services/api';

export default function UnknownParticipantModal({ meetingId, unknowns, ambiguities, onClose, onResolved }) {
  const [newEmails, setNewEmails] = useState({});
  const [loading, setLoading] = useState(false);

  if ((!unknowns || unknowns.length === 0) && (!ambiguities || ambiguities.length === 0)) {
    return null;
  }

  const handleResolve = async (name, isAmbiguous, candidateEmail = null) => {
    setLoading(true);
    try {
      const emailToUse = candidateEmail || newEmails[name];
      if (!emailToUse) {
        alert('Please provide an email address.');
        return;
      }

      await api.resolveParticipant(meetingId, {
        queried_name: name,
        selected_email: isAmbiguous ? candidateEmail : null,
        new_email: isAmbiguous ? null : emailToUse,
        new_name: name
      });

      if (onResolved) onResolved();
    } catch (e) {
      console.error(e);
      alert('Error resolving participant: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

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
              <p className="text-xs text-slate-400">Select a colleague by email or provide the participant's email address.</p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white">✕</button>
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
                        onClick={() => handleResolve(item.queried_name, true, cand.email)}
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

        {/* Unknown participants */}
        {unknowns && unknowns.length > 0 && (
          <div className="space-y-3">
            <div className="text-xs font-semibold text-rose-400 flex items-center space-x-1.5">
              <AlertCircle className="w-4 h-4" />
              <span>Unregistered Participants (Provide Valid Email):</span>
            </div>
            {unknowns.map((item, idx) => (
              <div key={idx} className="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-2 text-xs">
                <span className="font-bold text-white">Queried: "{item.queried_name}"</span>
                <p className="text-slate-400 text-[11px]">{item.message}</p>
                <div className="flex items-center space-x-2 pt-1">
                  <input
                    type="email"
                    placeholder="colleague@example.com or guest@external.com"
                    value={newEmails[item.queried_name] || ''}
                    onChange={(e) => setNewEmails({ ...newEmails, [item.queried_name]: e.target.value })}
                    className="flex-1 bg-slate-900 border border-slate-700 rounded-lg p-2 text-white text-xs"
                  />
                  <button
                    disabled={loading || !newEmails[item.queried_name]}
                    onClick={() => handleResolve(item.queried_name, false)}
                    className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold text-xs transition-all disabled:opacity-50"
                  >
                    Add
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

      </div>
    </div>
  );
}
