import React, { useState, useEffect } from 'react';
import { Send, Calendar, Clock, Video, MapPin, Users, ShieldCheck, RefreshCw } from 'lucide-react';
import { api } from '../../services/api';
import HumanApprovalModal from '../../components/HumanApprovalModal';
import AsyncSimulator from '../../components/AsyncSimulator';
import UnknownParticipantModal from '../../components/UnknownParticipantModal';

export default function MeetingDashboard() {
  const [requestText, setRequestText] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentPlan, setCurrentPlan] = useState(null);
  const [meetings, setMeetings] = useState([]);
  const [employees, setEmployees] = useState([]);
  
  // Modals
  const [showApprovalModal, setShowApprovalModal] = useState(false);
  const [showUnknownModal, setShowUnknownModal] = useState(false);
  const [activeSimulatorMeeting, setActiveSimulatorMeeting] = useState(null);

  const loadData = async () => {
    try {
      const [meetingsData, employeesData] = await Promise.all([
        api.getMeetings(),
        api.getEmployees(),
      ]);
      setMeetings(meetingsData);
      setEmployees(employeesData);
      if (activeSimulatorMeeting) {
        const updated = meetingsData.find(m => m.id === activeSimulatorMeeting.id);
        if (updated) {
          setActiveSimulatorMeeting(updated);
          if ((currentPlan?.id || currentPlan?.meeting_id) === updated.id) setCurrentPlan(updated);
        }
      }
    } catch (e) {
      console.error(e);
    }
  };

  const loadMeetings = loadData;

  useEffect(() => {
    loadData();
  }, []);

  const handleOrchestrate = async (e) => {
    if (e) e.preventDefault();
    if (!requestText.trim()) return;

    setLoading(true);
    try {
      const plan = await api.orchestrateMeeting(requestText);
      setCurrentPlan(plan);
      loadMeetings();

      // Check if unknown or ambiguous
      if ((plan.unknown_participants && plan.unknown_participants.length > 0) ||
          (plan.ambiguous_participants && plan.ambiguous_participants.length > 0)) {
        setShowUnknownModal(true);
      } else if (plan.status === 'WAITING_FOR_HUMAN_APPROVAL') {
        setShowApprovalModal(true);
      }
    } catch (err) {
      alert('Orchestration error: ' + err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (id, notes) => {
    try {
      const result = await api.submitApproval(id, 'APPROVE', notes);
      setShowApprovalModal(false);
      loadMeetings();
      const updated = await api.getMeeting(id);
      setCurrentPlan(updated);
      if (result.success) {
        setActiveSimulatorMeeting(updated);
      } else {
        // Show the real error — never hide it
        const errDetail = result.error || 'The approved meeting action could not be completed.';
        const errData = result.error_data;
        let fullError = errDetail;
        if (errData) {
          if (errData.http_status) fullError += `\n\nHTTP Status: ${errData.http_status}`;
          if (errData.error_reason) fullError += `\nReason: ${errData.error_reason}`;
          if (errData.error_message) fullError += `\nMessage: ${errData.error_message}`;
          if (errData.error_code) fullError += `\nCode: ${errData.error_code}`;
        }
        alert(fullError);
      }
    } catch (e) {
      alert(e.message);
    }
  };

  const handleReject = async (id, notes) => {
    try {
      await api.submitApproval(id, 'REJECT', notes);
      setShowApprovalModal(false);
      const updated = await api.getMeeting(id);
      setCurrentPlan(updated);
      loadMeetings();
    } catch (e) {
      alert(e.message);
    }
  };

  const handleSaveEdit = async (id, edits, notes) => {
    try {
      await api.submitApproval(id, 'EDIT', notes, edits);
      const updated = await api.getMeeting(id);
      setCurrentPlan(updated);
      setShowApprovalModal(true);
      loadMeetings();
    } catch (e) {
      alert(e.message);
    }
  };

  const handleSkipAgenda = async (id) => {
    try {
      await api.updateAgenda(id, { skip: true });
      loadMeetings();
      // Reload current plan
      const updated = await api.getMeeting(id);
      setCurrentPlan(updated);
      setShowApprovalModal(true);
    } catch (e) {
      alert(e.message);
    }
  };

  const getStatusBadge = (status) => {
    switch (status) {
      case 'CONFIRMED':
      case 'BOOKED':
        return 'status-pill status-confirmed';
      case 'WAITING_FOR_HUMAN_APPROVAL':
        return 'status-pill status-waiting';
      case 'WAITING_FOR_ROOM':
      case 'WAITING_FOR_AUDITORIUM_RESPONSE':
      case 'WAITING_FOR_PARTICIPANTS':
        return 'status-pill status-waiting';
      case 'REJECTED':
      case 'RESCHEDULING_REQUIRED':
        return 'status-pill status-rejected';
      case 'APPROVED':
        return 'status-pill status-approved';
      default:
        return 'status-pill status-draft';
    }
  };

  const readableStatus = (status) => ({
    DRAFT: 'Draft',
    WAITING_FOR_HUMAN_APPROVAL: 'Waiting for Approval',
    APPROVED: 'Approved',
    WAITING_FOR_PARTICIPANTS: 'Waiting for Participant Response',
    WAITING_FOR_ROOM: 'Waiting for Auditorium Response',
    WAITING_FOR_AUDITORIUM_RESPONSE: 'Waiting for Auditorium Response',
    ACTION_FAILED: 'Action Failed',
    CONFIRMED: 'Confirmed',
    BOOKED: 'Confirmed',
    REJECTED: 'Rejected',
    RESCHEDULING_REQUIRED: 'Response Rejected · Edit plan',
  }[status] || status || 'Draft');

  return (
    <div className="space-y-6 max-w-7xl mx-auto">
      
      {/* Header */}
      <div className="glass-panel p-6 rounded-2xl border border-slate-800 space-y-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Meeting Organizer Dashboard</h1>
          <p className="text-sm text-slate-400 mt-1">Enter a request to prepare a draft for your review.</p>
        </div>

        {/* Natural Language Prompt Bar */}
        <form onSubmit={handleOrchestrate} className="space-y-3">
          <div className="relative">
            <input
              type="text"
              placeholder="Describe the participants, date, time, format, and purpose"
              value={requestText}
              onChange={(e) => setRequestText(e.target.value)}
              className="w-full bg-slate-950/90 border border-slate-700 rounded-2xl pl-4 pr-32 py-3.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 shadow-inner"
            />
            <button
              type="submit"
              disabled={loading || !requestText.trim()}
              className="absolute right-2 top-2 px-5 py-2 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white rounded-xl text-xs font-bold transition-all shadow-md shadow-indigo-600/30 disabled:opacity-50 flex items-center space-x-2"
            >
              {loading ? (
                <>
                  <RefreshCw className="w-4 h-4 animate-spin" />
                  <span>Preparing...</span>
                </>
              ) : (
                <>
                  <Send className="w-3.5 h-3.5" />
                  <span>Schedule</span>
                </>
              )}
            </button>
          </div>

        </form>
      </div>

      {/* Active Proposal View (if generated) */}
      {currentPlan && (
        <div className="glass-panel p-6 rounded-2xl border border-indigo-500/30 bg-indigo-950/10 space-y-5">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-800 pb-3">
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-sm font-semibold text-slate-500">Draft plan</span>
                <span className={getStatusBadge(currentPlan.status)}>
                  {readableStatus(currentPlan.status)}
                </span>
              </div>
              <h2 className="text-xl font-bold text-white mt-1">{currentPlan.title}</h2>
            </div>

            <div className="flex items-center space-x-2">
              {currentPlan.status === 'WAITING_FOR_HUMAN_APPROVAL' && (
                <button
                  onClick={() => setShowApprovalModal(true)}
                  className="px-4 py-2 rounded-xl bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold text-xs transition-all shadow-md shadow-amber-500/25 flex items-center space-x-1.5"
                >
                  <ShieldCheck className="w-4 h-4" />
                  <span>Review draft</span>
                </button>
              )}

              {/* Missing Purpose / Skip Option */}
              {!currentPlan.purpose && currentPlan.status === 'DRAFT' && (
                <button
                  onClick={() => handleSkipAgenda(currentPlan.meeting_id || currentPlan.id)}
                  className="px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 font-semibold text-xs border border-slate-700 transition-all"
                >
                  Continue without an agenda
                </button>
              )}

              {['WAITING_FOR_ROOM', 'WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_PARTICIPANTS'].includes(currentPlan.status) && (
                <button
                  onClick={() => setActiveSimulatorMeeting(currentPlan)}
                  className="px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 font-semibold text-xs border border-slate-700 transition-all"
                >
                  Demo response
                </button>
              )}
            </div>
          </div>

          {/* Details Grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
            <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
              <span className="text-slate-500 block mb-0.5">Date and time</span>
              <span className="font-semibold text-white">{currentPlan.scheduled_start ? new Date(currentPlan.scheduled_start).toLocaleString() : 'Not provided'}</span>
            </div>
            <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
              <span className="text-slate-500 block mb-0.5">Duration</span>
              <span className="font-semibold text-white">{currentPlan.duration_minutes || 30} minutes</span>
            </div>
            <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
              <span className="text-slate-500 block mb-0.5">Format and venue</span>
              <span className="font-semibold text-white">{currentPlan.mode === 'ONLINE' ? (currentPlan.meet_url || 'Online · Meet link after approval') : (currentPlan.room_name || 'Room not specified')}</span>
            </div>
            <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
              <span className="text-slate-500 block mb-0.5">Purpose</span>
              <span className="font-semibold text-white">{currentPlan.purpose || 'Not provided'}</span>
            </div>
          </div>

          <div className="space-y-1.5 text-sm">
            <h3 className="font-semibold text-slate-700">Participants</h3>
            {(currentPlan.participants || []).map((participant) => (
              <div key={participant.email} className="flex flex-wrap justify-between gap-2 border-b border-slate-200 py-2">
                <span>{participant.name}</span>
                <span className="text-slate-500">{participant.email} · {participant.calendar_status || 'UNVERIFIED'}</span>
              </div>
            ))}
          </div>

          {/* Draft Agenda Snippet */}
          {currentPlan.agenda && (
            <div className="bg-slate-950/80 p-3.5 rounded-xl border border-slate-800 space-y-1 text-xs">
              <span className="text-slate-500 font-semibold text-[11px] block">Draft agenda</span>
              <pre className="text-slate-300 font-mono text-[11px] whitespace-pre-wrap">{currentPlan.agenda}</pre>
            </div>
          )}
        </div>
      )}

      {/* Async Simulator Drawer if active */}
      {activeSimulatorMeeting && (
        <AsyncSimulator
          meeting={activeSimulatorMeeting}
          onStateChange={loadMeetings}
        />
      )}

      {/* Registered Employees Section */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-bold text-white flex items-center space-x-2">
            <Users className="w-4 h-4 text-indigo-400" />
            <span>Registered Employees</span>
          </h2>
          <span className="text-xs text-slate-500">{employees.length} registered</span>
        </div>

        {employees.length === 0 ? (
          <div className="glass-panel p-8 rounded-xl border border-slate-800 text-center text-slate-400 text-sm">
            No employees registered yet.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {employees.map((emp) => (
              <div
                key={emp.id}
                className="glass-panel p-4 rounded-xl border border-slate-800 flex items-center justify-between gap-3 text-xs"
              >
                <div>
                  <div className="font-bold text-white text-sm">{emp.name}</div>
                  <div className="text-[11px] text-indigo-400">{emp.designation} · <span className="text-slate-500">{emp.department}</span></div>
                  <div className="text-[11px] text-slate-400 font-mono mt-0.5">{emp.email}</div>
                </div>
                <div className="flex items-center space-x-1.5 flex-shrink-0">
                  <span className={`w-2 h-2 rounded-full ${emp.google_calendar_connected ? 'bg-emerald-400' : 'bg-slate-600'}`} />
                  <span className="text-[11px] text-slate-400 font-medium">
                    {emp.google_calendar_connected ? 'Connected' : 'Not Connected'}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Scheduled Meetings List */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-bold text-white flex items-center space-x-2">
            <Calendar className="w-4 h-4 text-indigo-400" />
            <span>Meetings</span>
          </h2>
          <button onClick={loadMeetings} className="text-slate-500 hover:text-white text-xs flex items-center space-x-1">
            <RefreshCw className="w-3.5 h-3.5" />
            <span>Refresh</span>
          </button>
        </div>

        {meetings.length === 0 ? (
          <div className="glass-panel p-8 rounded-xl border border-slate-800 text-center text-slate-400 text-sm">
            No meetings scheduled yet.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3">
          {meetings.map((m) => (
            <div
              key={m.id}
              className="glass-panel p-4 rounded-xl border border-slate-800 hover:border-slate-700 transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-3"
            >
              <div className="space-y-1">
                <div className="flex items-center space-x-2">
                  <span className={getStatusBadge(m.status)}>
                    {readableStatus(m.status)}
                  </span>
                  <span className="font-semibold text-white text-sm">{m.title}</span>
                </div>
                <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
                  <span className="flex items-center space-x-1">
                    <Clock className="w-3.5 h-3.5 text-slate-500" />
                    <span>{m.scheduled_start ? new Date(m.scheduled_start).toLocaleString() : 'Not provided'} ({m.duration_minutes} minutes)</span>
                  </span>
                  <span className="flex items-center space-x-1">
                    {m.mode === 'ONLINE' ? <Video className="w-3.5 h-3.5 text-indigo-400" /> : <MapPin className="w-3.5 h-3.5 text-indigo-400" />}
                    <span>{m.mode} {m.meet_url ? `• ${m.meet_url}` : (m.room_name ? `• ${m.room_name}` : '')}</span>
                  </span>
                  <span className="flex items-center space-x-1">
                    <Users className="w-3.5 h-3.5 text-slate-500" />
                    <span>{(m.participants || []).length} participants</span>
                  </span>
                </div>
              </div>

              <div className="flex items-center space-x-2 self-end sm:self-auto">
                {m.status === 'WAITING_FOR_HUMAN_APPROVAL' && (
                  <button
                    onClick={() => {
                      setCurrentPlan(m);
                      setShowApprovalModal(true);
                    }}
                    className="px-3 py-1.5 rounded-lg bg-amber-500/20 text-amber-300 border border-amber-500/30 text-xs font-semibold hover:bg-amber-500/30 transition-all"
                  >
                    Review
                  </button>
                )}

                {m.status === 'RESCHEDULING_REQUIRED' && (
                  <button
                    onClick={() => {
                      setCurrentPlan(m);
                      setShowApprovalModal(true);
                    }}
                    className="px-3 py-1.5 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 text-xs font-semibold"
                  >
                    Edit plan
                  </button>
                )}

                {['WAITING_FOR_ROOM', 'WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_PARTICIPANTS'].includes(m.status) && (
                  <button
                    onClick={() => setActiveSimulatorMeeting(m)}
                    className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-all"
                  >
                    Demo response
                  </button>
                )}
              </div>
            </div>
          ))}
          </div>
        )}
      </div>

      {/* Human Approval Modal */}
      {showApprovalModal && currentPlan && (
        <HumanApprovalModal
          meeting={currentPlan}
          onClose={() => setShowApprovalModal(false)}
          onApprove={handleApprove}
          onReject={handleReject}
          onSaveEdit={handleSaveEdit}
        />
      )}

      {/* Unknown / Ambiguous Participant Resolution Modal */}
      {showUnknownModal && currentPlan && (
        <UnknownParticipantModal
          meetingId={currentPlan.meeting_id || currentPlan.id}
          unknowns={currentPlan.unknown_participants}
          ambiguities={currentPlan.ambiguous_participants}
          onClose={() => setShowUnknownModal(false)}
          onResolved={() => {
            setShowUnknownModal(false);
            loadMeetings();
          }}
        />
      )}

    </div>
  );
}
