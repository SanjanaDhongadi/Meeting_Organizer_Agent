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
  const [notice, setNotice] = useState(null); // { kind: 'error' | 'warning' | 'info', text }
  const [syncing, setSyncing] = useState(false);

  const loadData = async () => {
    try {
      const [meetingsData, employeesData] = await Promise.all([
        api.getMeetings(),
        api.getEmployees(),
      ]);
      setMeetings(meetingsData);
      setEmployees(employeesData);
      // Functional updates: always compare against the latest state, never a stale closure.
      const byId = (m) => meetingsData.find((x) => x.id === (m?.id || m?.meeting_id));
      setActiveSimulatorMeeting((prev) => (prev ? byId(prev) || prev : prev));
      setCurrentPlan((prev) => (prev ? byId(prev) || prev : prev));
    } catch (e) {
      setNotice({ kind: 'error', text: `Could not load data from the backend: ${e.message}` });
    }
  };

  const loadMeetings = loadData;

  const openPlan = (plan) => {
    setCurrentPlan(plan);
    setActiveSimulatorMeeting(null);
    if ((plan.unknown_participants || []).length > 0 || (plan.ambiguous_participants || []).length > 0) {
      setShowUnknownModal(true);
    } else if (plan.status === 'WAITING_FOR_HUMAN_APPROVAL') {
      setShowApprovalModal(true);
    }
  };

  // Blocked plans: resolve missing participants first, otherwise open the review/edit form.
  const handleEditPlan = (m) => {
    setCurrentPlan(m);
    const missing = (m.unknown_participants || []).length + (m.ambiguous_participants || []).length;
    setShowUnknownModal(missing > 0);
    setShowApprovalModal(missing === 0);
  };

  const refreshPlan = async (id) => {
    const updated = await api.getMeeting(id);
    setCurrentPlan(updated);
    return updated;
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleOrchestrate = async (e) => {
    if (e) e.preventDefault();
    if (!requestText.trim()) return;

    setLoading(true);
    setNotice(null);
    try {
      const plan = await api.orchestrateMeeting(requestText);
      openPlan(plan);
      loadMeetings();
    } catch (err) {
      setNotice({ kind: 'error', text: `Planning failed: ${err.message}` });
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (id, notes) => {
    try {
      const result = await api.submitApproval(id, 'APPROVE', notes);
      setShowApprovalModal(false);
      loadMeetings();
      const updated = await refreshPlan(id);
      if (result.success) {
        setActiveSimulatorMeeting(updated);
        const warnings = result.warnings || [];
        setNotice(warnings.length
          ? { kind: 'warning', text: `Approved and executed with warnings: ${warnings.join(' | ')}` }
          : { kind: 'info', text: 'Approved. The approved actions were executed; see the status below.' });
      } else {
        // Show the real error — never hide it
        const errData = result.error_data || {};
        const parts = [result.error || 'The approved meeting action could not be completed.'];
        if (errData.mcp_capability) parts.push(`MCP capability: ${errData.mcp_capability}`);
        if (errData.http_status) parts.push(`HTTP status: ${errData.http_status}`);
        if (errData.error_reason) parts.push(`Reason: ${errData.error_reason}`);
        if (errData.error_message) parts.push(`Message: ${errData.error_message}`);
        if (errData.node) parts.push(`Workflow node: ${errData.node}`);
        setNotice({ kind: 'error', text: parts.join(' · ') });
      }
    } catch (e) {
      setNotice({ kind: 'error', text: e.message });
    }
  };

  const handleReject = async (id, notes) => {
    try {
      await api.submitApproval(id, 'REJECT', notes);
      setShowApprovalModal(false);
      await refreshPlan(id);
      loadMeetings();
    } catch (e) {
      setNotice({ kind: 'error', text: e.message });
    }
  };

  const handleSaveEdit = async (id, edits, notes) => {
    try {
      await api.submitApproval(id, 'EDIT', notes, edits);
      const updated = await refreshPlan(id);
      // The edited plan was re-checked (availability + validation) by the workflow.
      setShowApprovalModal(updated.status === 'WAITING_FOR_HUMAN_APPROVAL');
      if (updated.status !== 'WAITING_FOR_HUMAN_APPROVAL') {
        setNotice({ kind: 'warning', text: `Edited plan needs attention before approval (status: ${readableStatus(updated.status)}).` });
      }
      loadMeetings();
    } catch (e) {
      setNotice({ kind: 'error', text: e.message });
    }
  };

  const handleSkipAgenda = async (id) => {
    try {
      await api.updateAgenda(id, { skip: true });
      loadMeetings();
      const updated = await refreshPlan(id);
      setShowApprovalModal(updated.status === 'WAITING_FOR_HUMAN_APPROVAL');
    } catch (e) {
      setNotice({ kind: 'error', text: e.message });
    }
  };

  const handleSyncResponses = async (id) => {
    setSyncing(true);
    try {
      const res = await api.syncResponses(id);
      const errors = (res.results || []).filter((r) => r.status === 'ERROR');
      await refreshPlan(id);
      loadMeetings();
      if (errors.length) {
        setNotice({ kind: 'error', text: errors.map((e) => `${e.capability || 'response check'}: ${e.error || e.message}`).join(' | ') });
      } else if ((res.results || []).some((r) => r.status === 'SKIPPED')) {
        setNotice({ kind: 'info', text: 'DEMO_MODE: real responses are not read; use the simulated response panel.' });
      } else {
        setNotice({ kind: 'info', text: `Checked Google for responses. Status: ${readableStatus(res.status)}.` });
      }
    } catch (e) {
      setNotice({ kind: 'error', text: e.message });
    } finally {
      setSyncing(false);
    }
  };

  const noticeStyles = {
    error: 'text-rose-700 bg-rose-50 border-rose-200',
    warning: 'text-amber-800 bg-amber-50 border-amber-200',
    info: 'text-emerald-800 bg-emerald-50 border-emerald-200',
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
    EXECUTING: 'Executing approved actions',
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
          <h1 className="text-2xl font-bold text-white">Meeting Organizer</h1>
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

      {notice && (
        <div role={notice.kind === 'error' ? 'alert' : 'status'} className={`text-sm border rounded-xl p-3 flex justify-between gap-3 ${noticeStyles[notice.kind]}`}>
          <span style={{ whiteSpace: 'pre-wrap' }}>{notice.text}</span>
          <button type="button" onClick={() => setNotice(null)}>✕</button>
        </div>
      )}

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

              {((currentPlan.unknown_participants || []).length > 0 || (currentPlan.ambiguous_participants || []).length > 0) && (
                <button
                  onClick={() => setShowUnknownModal(true)}
                  className="px-3.5 py-2 rounded-xl bg-rose-600 hover:bg-rose-500 text-white font-semibold text-xs transition-all"
                >
                  Resolve participants
                </button>
              )}

              {['WAITING_FOR_ROOM', 'WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_PARTICIPANTS'].includes(currentPlan.status) && (
                <>
                  <button
                    onClick={() => handleSyncResponses(currentPlan.meeting_id || currentPlan.id)}
                    disabled={syncing}
                    className="px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs transition-all disabled:opacity-50"
                  >
                    {syncing ? 'Checking…' : 'Check for responses'}
                  </button>
                  <button
                    onClick={() => setActiveSimulatorMeeting(currentPlan)}
                    className="px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 font-semibold text-xs border border-slate-700 transition-all"
                  >
                    Simulate response
                  </button>
                </>
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
              <span className="font-semibold text-white break-all">
                {currentPlan.mode === 'ONLINE'
                  ? (currentPlan.meet_url
                    ? <a href={currentPlan.meet_url} target="_blank" rel="noreferrer" className="underline">{currentPlan.meet_url}</a>
                    : 'Online · Meet link after approval')
                  : (currentPlan.room_name || 'Room not specified')}
              </span>
            </div>
            <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
              <span className="text-slate-500 block mb-0.5">Purpose</span>
              <span className="font-semibold text-white">{currentPlan.purpose || 'Not provided'}</span>
            </div>
          </div>

          {currentPlan.execution_error && (
            <div role="alert" className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-xl p-3">
              <strong>Execution failed ({currentPlan.execution_error.step}):</strong> {currentPlan.execution_error.error}
            </div>
          )}
          {(currentPlan.warnings || []).length > 0 && (
            <ul className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-xl p-3 list-disc list-inside">
              {currentPlan.warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          )}
          {currentPlan.calendar_event_id && (
            <p className="text-xs text-slate-500">
              Calendar event ID: <span className="font-mono">{currentPlan.calendar_event_id}</span>
              {currentPlan.execution?.organizer_email && <> · organized by {currentPlan.execution.organizer_email}</>}
            </p>
          )}

          <div className="space-y-1.5 text-sm">
            <h3 className="font-semibold text-slate-700">Participants</h3>
            {(currentPlan.participants || []).length === 0 && <p className="text-xs text-slate-500">No confirmed participants yet.</p>}
            {(currentPlan.participants || []).map((participant) => (
              <div key={participant.email} className="flex flex-wrap justify-between gap-2 border-b border-slate-200 py-2">
                <span>{participant.name}</span>
                <span className="text-slate-500">
                  {participant.email} · calendar {participant.calendar_status || 'UNVERIFIED'}
                  {participant.response_status && <> · response {participant.response_status}</>}
                  {participant.notes && <> · {participant.notes}</>}
                </span>
              </div>
            ))}
          </div>

          {(currentPlan.room_bookings || []).length > 0 && (
            <div className="space-y-1.5 text-sm">
              <h3 className="font-semibold text-slate-700">Room / auditorium request</h3>
              {currentPlan.room_bookings.map((rb) => (
                <div key={rb.id} className="text-xs text-slate-600 border-b border-slate-200 py-2">
                  <strong>{rb.room_name}</strong> · {{
                    PENDING: 'Request sent · waiting for auditorium response',
                    CONFIRMED: 'Confirmed',
                    REJECTED: 'Rejected',
                    FAILED: 'Request failed (not sent)',
                  }[rb.status] || rb.status}
                  {rb.notes && <div className="whitespace-pre-wrap text-slate-500 mt-1">{rb.notes}</div>}
                </div>
              ))}
            </div>
          )}

          {currentPlan.rag_context && (((currentPlan.rag_context.historical_meetings || []).length > 0) || Object.keys(currentPlan.rag_context.participant_preferences || {}).length > 0) && (
            <div className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-xl p-3 space-y-1">
              <strong>Context used from long-term memory</strong>
              {(currentPlan.rag_context.historical_meetings || []).map((m) => (
                <div key={m.meeting_id}>Related past meeting: {m.title}</div>
              ))}
              {Object.entries(currentPlan.rag_context.participant_preferences || {}).map(([email, prefs]) => (
                <div key={email}>{email}: {prefs.working_days} · {prefs.working_hours} ({prefs.timezone}){prefs.preferences ? ` · “${prefs.preferences}”` : ''}</div>
              ))}
            </div>
          )}

          {(currentPlan.workflow_trace || []).length > 0 && (
            <p className="text-[11px] text-slate-400 font-mono break-words">Workflow: {currentPlan.workflow_trace.join(' → ')}</p>
          )}

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
                <button
                  onClick={() => setCurrentPlan(m)}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-all"
                >
                  Details
                </button>

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
                    onClick={() => handleEditPlan(m)}
                    className="px-3 py-1.5 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 text-xs font-semibold"
                  >
                    Edit plan
                  </button>
                )}

                {['WAITING_FOR_ROOM', 'WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_PARTICIPANTS'].includes(m.status) && (
                  <button
                    onClick={() => { setCurrentPlan(m); setActiveSimulatorMeeting(m); }}
                    className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-all"
                  >
                    Simulate response
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
          onResolved={async () => {
            // The backend re-planned the same meeting; show what is still missing, or the approval gate.
            const updated = await refreshPlan(currentPlan.meeting_id || currentPlan.id);
            loadMeetings();
            const stillMissing = (updated.unknown_participants || []).length + (updated.ambiguous_participants || []).length;
            setShowUnknownModal(stillMissing > 0);
            if (!stillMissing && updated.status === 'WAITING_FOR_HUMAN_APPROVAL') setShowApprovalModal(true);
          }}
        />
      )}

    </div>
  );
}
