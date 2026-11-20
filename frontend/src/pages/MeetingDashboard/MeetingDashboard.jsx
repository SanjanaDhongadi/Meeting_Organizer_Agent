import React, { useState, useEffect } from 'react';
import { Send, Calendar, Clock, Video, Building2, Users, ShieldCheck, RefreshCw } from 'lucide-react';
import { api } from '../../services/api';
import { availabilityLabel } from '../../services/availability';
import HumanApprovalModal from '../../components/HumanApprovalModal';
import AsyncSimulator from '../../components/AsyncSimulator';
import VenueCoordinatorPanel from '../../components/VenueCoordinatorPanel';
import MeetingDetailsStep from '../../components/MeetingDetailsStep';
import MeetingDetailsModal from '../../components/MeetingDetailsModal';
import ConfirmDeleteDialog from '../../components/ConfirmDeleteDialog';
import MinutesPanel from '../../components/MinutesPanel';
import { MINUTES_STATUS } from '../../components/minutesStatus';

export default function MeetingDashboard({ demoMode = false, currentUser = null }) {
  const [requestText, setRequestText] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentPlan, setCurrentPlan] = useState(null);
  const [sections, setSections] = useState({ organized: [], past: [], requests: [] });
  const [activeSection, setActiveSection] = useState('organized');
  
  // Modals
  const [showApprovalModal, setShowApprovalModal] = useState(false);
  const [showDetailsStep, setShowDetailsStep] = useState(false);
  const [detailsMeetingId, setDetailsMeetingId] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [listRefreshedAt, setListRefreshedAt] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [minutesMeetingId, setMinutesMeetingId] = useState(null);
  const [activeSimulatorMeeting, setActiveSimulatorMeeting] = useState(null);
  const [notice, setNotice] = useState(null); // { kind: 'error' | 'warning' | 'info', text }
  const [syncing, setSyncing] = useState(false);
  const [planTab, setPlanTab] = useState('attendee');
  // Unsent venue-request edits per meeting: kept here so switching tabs or reloading the plan never loses them.
  const [venueDrafts, setVenueDrafts] = useState({});

  const loadData = async () => {
    try {
      const sectionsData = await api.getMeetingSections();
      setSections(sectionsData || { organized: [], past: [], requests: [] });
      const allMeetings = [
        ...(sectionsData?.organized || []),
        ...(sectionsData?.past || []),
        ...(sectionsData?.requests || [])
      ];
      const byId = (m) => allMeetings.find((x) => x.id === (m?.id || m?.meeting_id));
      setActiveSimulatorMeeting((prev) => (prev ? byId(prev) || prev : prev));
      setCurrentPlan((prev) => (prev ? byId(prev) || prev : prev));
    } catch (e) {
      setNotice({ kind: 'error', text: `Could not load data from the backend: ${e.message}` });
    }
  };

  const handleToggleAttendance = async (meetingId, attendedStatus) => {
    try {
      await api.recordAttendance(meetingId, { email: currentUser?.email, attended: !attendedStatus });
      await loadData();
    } catch (e) {
      setNotice({ kind: 'error', text: `Failed to update attendance: ${e.message}` });
    }
  };

  const loadMeetings = loadData;

  const openPlan = (plan) => {
    setCurrentPlan(plan);
    setActiveSimulatorMeeting(null);
    setShowDetailsStep(false);
    setShowApprovalModal(false);
    if ((plan.missing_fields || []).length > 0 || plan.status === 'NEEDS_INFORMATION') {
      setShowDetailsStep(true);  // ask the agent's questions first
    } else if (plan.status === 'WAITING_FOR_HUMAN_APPROVAL' && !plan.details_confirmed) {
      setShowDetailsStep(true);  // agenda (and venue) step between Schedule and Review
    } else if (plan.status === 'WAITING_FOR_HUMAN_APPROVAL') {
      setShowApprovalModal(true);
    }
  };

  // Open the review/edit form or details step if info is needed.
  const handleEditPlan = (m) => {
    setCurrentPlan(m);
    const missingDetails = (m.missing_fields || []).length > 0 || m.status === 'NEEDS_INFORMATION';
    setShowDetailsStep(missingDetails);
    setShowApprovalModal(!missingDetails);
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
        if (demoMode) setActiveSimulatorMeeting(updated);
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
      // Approval was refused before anything was sent (e.g. no connected Google account): show why and
      // reload the plan so the review screen shows what is missing.
      setNotice({ kind: 'error', text: e.message });
      try { await refreshPlan(id); } catch { /* the notice already explains the failure */ }
      loadMeetings();
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

  // Refresh: first read REAL responses from Google for waiting meetings, then reload from the backend.
  const handleRefreshList = async () => {
    setRefreshing(true);
    try {
      const res = await api.syncAllResponses();
      if ((res.errors || []).length) {
        setNotice({ kind: 'error', text: res.errors.map((e) => `${e.meeting_id}: ${e.capability || 'response check'} — ${e.error || e.message}`).join(' | ') });
      }
    } catch (e) {
      setNotice({ kind: 'error', text: `Could not check Google for responses: ${e.message}` });
    }
    await loadData();
    setListRefreshedAt(new Date());
    setRefreshing(false);
  };

  const handleDeleteConfirmed = async (meeting) => {
    const id = meeting.id || meeting.meeting_id;
    const res = await api.deleteMeeting(id);  // throws with the backend's reason (e.g. Google cancellation failed)
    setDeleteTarget(null);
    setDetailsMeetingId((prev) => (prev === id ? null : prev));
    setCurrentPlan((prev) => (prev && (prev.id || prev.meeting_id) === id ? null : prev));
    setActiveSimulatorMeeting((prev) => (prev && (prev.id || prev.meeting_id) === id ? null : prev));
    setNotice({ kind: 'info', text: `Deleted “${meeting.title || 'Untitled meeting'}”${(res.cancelled || []).length ? ` (${res.cancelled.join(', ').replaceAll('_', ' ')})` : ''}.` });
    await loadData();
  };

  // Keep the list and the plan card in sync with a meeting re-loaded in the Details view.
  const handleDetailsLoaded = (fresh) => {
    const id = fresh?.id || fresh?.meeting_id;
    if (!id) return;
    setSections((prev) => ({
      organized: (prev.organized || []).map((x) => ((x.id || x.meeting_id) === id ? { ...x, ...fresh } : x)),
      past: (prev.past || []).map((x) => ((x.id || x.meeting_id) === id ? { ...x, ...fresh } : x)),
      requests: (prev.requests || []).map((x) => ((x.id || x.meeting_id) === id ? { ...x, ...fresh } : x)),
    }));
    setCurrentPlan((prev) => (prev && (prev.id || prev.meeting_id) === id ? { ...prev, ...fresh } : prev));
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
      case 'NEEDS_INFORMATION':
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
    NEEDS_INFORMATION: 'Needs information',
    WAITING_FOR_HUMAN_APPROVAL: 'Waiting for Approval',
    APPROVED: 'Approved',
    WAITING_FOR_PARTICIPANTS: 'Waiting for Participant Response',
    WAITING_FOR_ROOM: 'Waiting for venue confirmation',
    WAITING_FOR_AUDITORIUM_RESPONSE: 'Waiting for venue confirmation',
    ACTION_FAILED: 'Action Failed',
    EXECUTING: 'Executing approved actions',
    CONFIRMED: 'Confirmed',
    BOOKED: 'Confirmed',
    REJECTED: 'Rejected',
    RESCHEDULING_REQUIRED: 'Rescheduling required · Edit plan',
  }[status] || status || 'Draft');

  const planId = currentPlan ? (currentPlan.meeting_id || currentPlan.id) : null;
  const isOfflinePlan = !!currentPlan && currentPlan.mode === 'OFFLINE' && !!currentPlan.venue_coordination;
  const activePlanTab = isOfflinePlan ? planTab : 'attendee';

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
              className="w-full bg-slate-950/90 border border-slate-700 rounded-2xl pl-4 pr-28 py-3.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 shadow-inner"
            />
            <button
              type="submit"
              disabled={loading || !requestText.trim()}
              // Sized to sit inside the input: the global `button { font: inherit }` rule would otherwise enlarge it.
              style={{ fontSize: 13, lineHeight: '18px', padding: '7px 14px', top: '50%', transform: 'translateY(-50%)' }}
              className="absolute right-2 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white rounded-lg font-semibold transition-all shadow-sm disabled:opacity-50 flex items-center space-x-1.5"
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
              <h2 className="text-xl font-bold text-white mt-1">{currentPlan.title || 'Untitled meeting'}</h2>
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

              {(currentPlan.missing_fields || []).length > 0 && (
                <button
                  onClick={() => setShowDetailsStep(true)}
                  className="px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs transition-all"
                >
                  Answer questions
                </button>
              )}



              {(['WAITING_FOR_ROOM', 'WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_PARTICIPANTS'].includes(currentPlan.status)
                || currentPlan.venue_coordination?.request_status === 'SENT') && (
                <>
                  <button
                    onClick={() => handleSyncResponses(currentPlan.meeting_id || currentPlan.id)}
                    disabled={syncing}
                    className="px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs transition-all disabled:opacity-50"
                  >
                    {syncing ? 'Checking…' : 'Check for responses'}
                  </button>
                  {demoMode && (
                    <button
                      onClick={() => setActiveSimulatorMeeting(currentPlan)}
                      className="px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 font-semibold text-xs border border-slate-700 transition-all"
                    >
                      Simulate response
                    </button>
                  )}
                </>
              )}
            </div>
          </div>

          {isOfflinePlan && (
            <div role="tablist" aria-label="Draft plan sections" className="flex gap-1 border-b border-slate-200">
              {[['attendee', 'Attendee'], ['venue', 'Venue Coordinator']].map(([key, label]) => (
                <button key={key} type="button" role="tab" aria-selected={activePlanTab === key} id={`plan-tab-${key}`}
                  onClick={() => setPlanTab(key)}
                  className={`px-4 py-2 text-xs font-semibold border-b-2 -mb-px transition-all ${activePlanTab === key
                    ? 'border-indigo-600 text-indigo-700' : 'border-transparent text-slate-500 hover:text-slate-800'}`}>
                  {label}
                  {key === 'venue' && currentPlan.venue_coordination?.availability?.status === 'BUSY' && <span className="ml-1.5 text-rose-600">●</span>}
                </button>
              ))}
            </div>
          )}

          {isOfflinePlan && (
            <div role="tabpanel" aria-labelledby="plan-tab-venue" hidden={activePlanTab !== 'venue'}>
              <VenueCoordinatorPanel
                meeting={currentPlan}
                draft={venueDrafts[planId]}
                onDraftChange={(d) => setVenueDrafts((prev) => ({ ...prev, [planId]: d }))}
                onSent={(fresh) => {
                  setCurrentPlan(fresh);
                  setVenueDrafts((prev) => { const next = { ...prev }; delete next[planId]; return next; });
                  loadMeetings();
                }}
              />
            </div>
          )}

          <div role={isOfflinePlan ? 'tabpanel' : undefined} aria-labelledby={isOfflinePlan ? 'plan-tab-attendee' : undefined}
            hidden={isOfflinePlan && activePlanTab !== 'attendee'} className="space-y-5">
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
              <strong>Execution failed ({currentPlan.execution_error.step}){currentPlan.execution_error.failed_at
                ? ` on ${new Date(currentPlan.execution_error.failed_at).toLocaleString()}` : ''}:</strong> {currentPlan.execution_error.error}
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
                  {participant.email} · {availabilityLabel(participant.calendar_status)}
                  {participant.response_status && <> · response {participant.response_status}</>}
                  {participant.notes && <> · {participant.notes}</>}
                </span>
              </div>
            ))}
          </div>

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

          {/* Draft Agenda Snippet */}
          {currentPlan.agenda && (
            <div className="bg-slate-950/80 p-3.5 rounded-xl border border-slate-800 space-y-1 text-xs">
              <span className="text-slate-500 font-semibold text-[11px] block">Draft agenda</span>
              <pre className="text-slate-300 font-mono text-[11px] whitespace-pre-wrap">{currentPlan.agenda}</pre>
            </div>
          )}
          </div>
        </div>
      )}

      {/* Async Simulator Drawer if active */}
      {demoMode && activeSimulatorMeeting && (
        <AsyncSimulator
          meeting={activeSimulatorMeeting}
          onStateChange={loadMeetings}
        />
      )}

      {/* Dashboard Sections: Organized, Past, Requests */}
      <div className="space-y-4">
        {/* Section Tabs */}
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 pb-3">
          <div role="tablist" aria-label="Meeting sections" className="flex items-center gap-2">
            {[
              { id: 'organized', label: 'Organized Meetings', count: (sections.organized || []).length },
              { id: 'past', label: 'Past Meetings (Attended)', count: (sections.past || []).length },
              { id: 'requests', label: 'Meeting Requests', count: (sections.requests || []).length },
            ].map(({ id, label, count }) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={activeSection === id}
                onClick={() => setActiveSection(id)}
                className={`flex items-center gap-2 px-3.5 py-2 rounded-xl text-xs font-semibold transition-all cursor-pointer ${
                  activeSection === id
                    ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/30'
                    : 'bg-slate-900 text-slate-400 hover:text-white hover:bg-slate-800 border border-slate-800'
                }`}
              >
                <span>{label}</span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${activeSection === id ? 'bg-indigo-700 text-indigo-100' : 'bg-slate-800 text-slate-400'}`}>
                  {count}
                </span>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-3">
            {listRefreshedAt && <span className="text-[11px] text-slate-400">Updated {listRefreshedAt.toLocaleTimeString()}</span>}
            <button onClick={handleRefreshList} disabled={refreshing} className="text-slate-500 hover:text-white text-xs flex items-center space-x-1 cursor-pointer">
              <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
              <span>{refreshing ? 'Refreshing…' : 'Refresh'}</span>
            </button>
          </div>
        </div>

        {/* Meetings List for Current Active Tab */}
        {(() => {
          const currentList = sections[activeSection] || [];
          if (currentList.length === 0) {
            return (
              <div className="glass-panel p-8 rounded-xl border border-slate-800 text-center text-slate-400 text-sm">
                {activeSection === 'organized' && 'You have not organized any meetings yet. Type a command above to plan one!'}
                {activeSection === 'past' && 'No attended meetings on record yet.'}
                {activeSection === 'requests' && 'No meeting requests waiting for your response.'}
              </div>
            );
          }

          return (
            <div className="grid grid-cols-1 gap-3">
              {currentList.map((m) => {
                const userParticipant = (m.participants || []).find(
                  (p) => (p.email || '').toLowerCase() === (currentUser?.email || '').toLowerCase()
                );
                const hasAttended = userParticipant?.attended || userParticipant?.attendance_recorded;

                return (
                  <div
                    key={m.id}
                    className="glass-panel p-4 rounded-xl border border-slate-800 hover:border-slate-700 transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-3"
                  >
                    <div className="space-y-1">
                      <div className="flex items-center space-x-2">
                        <span className={getStatusBadge(m.status)}>
                          {readableStatus(m.status)}
                        </span>
                        {hasAttended && (
                          <span className="text-[10px] bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 px-2 py-0.5 rounded-full font-medium">
                            Attended
                          </span>
                        )}
                        <span className="font-semibold text-white text-sm">{m.title || 'Untitled meeting'}</span>
                      </div>
                      <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
                        <span className="flex items-center space-x-1">
                          <Clock className="w-3.5 h-3.5 text-slate-500" />
                          <span>{m.scheduled_start ? new Date(m.scheduled_start).toLocaleString() : 'Not provided'} ({m.duration_minutes} minutes)</span>
                        </span>
                        <span className="flex items-center space-x-1">
                          {m.mode === 'ONLINE' ? <Video className="w-3.5 h-3.5 text-indigo-400" /> : <Building2 className="w-3.5 h-3.5 text-indigo-400" />}
                          <span>{m.mode} {m.meet_url ? `• ${m.meet_url}` : (m.room_name ? `• ${m.room_name}` : '')}</span>
                        </span>
                        <span className="flex items-center space-x-1">
                          <Users className="w-3.5 h-3.5 text-slate-500" />
                          <span>{(m.participants || []).length} participants</span>
                        </span>
                      </div>
                    </div>

                    <div className="flex items-center space-x-2 self-end sm:self-auto flex-wrap">
                      {/* Attendance Toggle for Past & Request Meetings */}
                      {userParticipant && (
                        <button
                          type="button"
                          onClick={() => handleToggleAttendance(m.id, userParticipant.attended)}
                          className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all cursor-pointer ${
                            userParticipant.attended
                              ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40 hover:bg-emerald-500/30'
                              : 'bg-slate-800 text-slate-400 border-slate-700 hover:text-white'
                          }`}
                        >
                          {userParticipant.attended ? 'Marked Attended ✓' : 'Mark Attended'}
                        </button>
                      )}

                      {/* Organizer-Only Controls */}
                      {activeSection === 'organized' && !['APPROVED', 'EXECUTING'].includes(m.status) && (
                        <button
                          onClick={() => setDeleteTarget(m)}
                          aria-label={`Delete ${m.title || 'meeting'}`}
                          className="px-3 py-1.5 rounded-lg bg-rose-500/20 text-rose-400 border border-rose-500/30 text-xs font-medium cursor-pointer"
                        >
                          Delete
                        </button>
                      )}

                      <button
                        onClick={() => setDetailsMeetingId(m.id)}
                        className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-all cursor-pointer"
                      >
                        Details
                      </button>

                      {activeSection === 'organized' && m.mode === 'ONLINE' && m.meet_url && m.calendar_event_id
                        && !['DELETED', 'REJECTED', 'DRAFT', 'NEEDS_INFORMATION', 'WAITING_FOR_HUMAN_APPROVAL', 'APPROVED', 'EXECUTING', 'FAILED', 'ACTION_FAILED'].includes(m.status) && (
                        <button
                          onClick={() => setMinutesMeetingId(m.id)}
                          className="px-3 py-1.5 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 text-xs font-semibold cursor-pointer"
                          title="Transcript and minutes of meeting"
                        >
                          Minutes{m.minutes?.status ? `: ${MINUTES_STATUS[m.minutes.status] || m.minutes.status}` : ''}
                        </button>
                      )}

                      {activeSection === 'organized' && m.status === 'WAITING_FOR_HUMAN_APPROVAL' && (
                        <button
                          onClick={() => {
                            setCurrentPlan(m);
                            setShowApprovalModal(true);
                          }}
                          className="px-3 py-1.5 rounded-lg bg-amber-500/20 text-amber-300 border border-amber-500/30 text-xs font-semibold hover:bg-amber-500/30 transition-all cursor-pointer"
                        >
                          Review
                        </button>
                      )}

                      {activeSection === 'organized' && m.status === 'ACTION_FAILED' && (
                        <button
                          onClick={() => {
                            setCurrentPlan(m);
                            setShowApprovalModal(true);
                          }}
                          className="px-3 py-1.5 rounded-lg bg-amber-500/20 text-amber-300 border border-amber-500/30 text-xs font-semibold hover:bg-amber-500/30 transition-all cursor-pointer"
                        >
                          Review &amp; retry
                        </button>
                      )}

                      {activeSection === 'organized' && m.status === 'NEEDS_INFORMATION' && (
                        <button
                          onClick={() => handleEditPlan(m)}
                          className="px-3 py-1.5 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 text-xs font-semibold cursor-pointer"
                        >
                          Complete details
                        </button>
                      )}

                      {activeSection === 'organized' && m.status === 'RESCHEDULING_REQUIRED' && (
                        <button
                          onClick={() => handleEditPlan(m)}
                          className="px-3 py-1.5 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 text-xs font-semibold cursor-pointer"
                        >
                          Edit plan
                        </button>
                      )}

                      {activeSection === 'organized' && m.mode === 'OFFLINE' && m.venue_coordination && !['REJECTED', 'DELETED'].includes(m.status) && (
                        <button
                          onClick={() => { setCurrentPlan(m); setPlanTab('venue'); window.scrollTo({ top: 0, behavior: 'smooth' }); }}
                          className="px-3 py-1.5 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 text-xs font-semibold cursor-pointer"
                        >
                          Venue request
                        </button>
                      )}

                      {demoMode && ['WAITING_FOR_ROOM', 'WAITING_FOR_AUDITORIUM_RESPONSE', 'WAITING_FOR_PARTICIPANTS'].includes(m.status) && (
                        <button
                          onClick={() => { setCurrentPlan(m); setActiveSimulatorMeeting(m); }}
                          className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-all cursor-pointer"
                        >
                          Simulate response
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })()}
      </div>

      {/* Human Approval Modal */}
      {showApprovalModal && currentPlan && (
        <HumanApprovalModal
          meeting={currentPlan}
          onClose={() => setShowApprovalModal(false)}
          onApprove={handleApprove}
          onReject={handleReject}
          onSaveEdit={handleSaveEdit}
          onRefresh={loadData}
        />
      )}

      {/* Meeting details (fresh from the backend) */}
      {detailsMeetingId && (
        <MeetingDetailsModal
          meetingId={detailsMeetingId}
          onClose={() => setDetailsMeetingId(null)}
          onChanged={handleDetailsLoaded}
          onDelete={setDeleteTarget}
          readableStatus={readableStatus}
          statusClass={getStatusBadge}
        />
      )}

      {minutesMeetingId && (
        <MinutesPanel meetingId={minutesMeetingId} onClose={() => setMinutesMeetingId(null)} onChanged={loadData} />
      )}

      {deleteTarget && (
        <ConfirmDeleteDialog meeting={deleteTarget} onConfirm={handleDeleteConfirmed} onCancel={() => setDeleteTarget(null)} />
      )}

      {/* Clarifying questions for missing / ambiguous details */}
      {showDetailsStep && currentPlan && (
        <MeetingDetailsStep
          key={currentPlan.meeting_id || currentPlan.id}
          meeting={currentPlan}
          onClose={() => setShowDetailsStep(false)}
          onSubmitted={(plan) => {
            loadMeetings();
            openPlan(plan);
          }}
        />
      )}



    </div>
  );
}
