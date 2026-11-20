const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api';

// Token storage helpers
export const authStorage = {
  getToken: () => localStorage.getItem('meeting_auth_token'),
  setToken: (token) => {
    if (token) localStorage.setItem('meeting_auth_token', token);
    else localStorage.removeItem('meeting_auth_token');
  },
  clearToken: () => localStorage.removeItem('meeting_auth_token'),
};

const getHeaders = (extra = {}) => {
  const headers = { 'Content-Type': 'application/json', ...extra };
  const token = authStorage.getToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
};

// FastAPI errors arrive as {detail: string} or {detail: {message, node, ...}} (LangGraph node failures).
export const errorMessage = (data, fallback) => {
  const detail = data?.detail;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
  return detail.message || JSON.stringify(detail);
};

const readJson = async (res, fallback) => {
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) throw new Error(errorMessage(data, `${fallback} (HTTP ${res.status})`));
  return data;
};

export const api = {
  // Auth
  async register(email, password, name) {
    const res = await fetch(`${API_BASE}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, name }),
    });
    const data = await readJson(res, 'Registration failed');
    if (data.token) authStorage.setToken(data.token);
    return data;
  },

  async login(email, password) {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const data = await readJson(res, 'Login failed');
    if (data.token) authStorage.setToken(data.token);
    return data;
  },

  async getMe() {
    const res = await fetch(`${API_BASE}/auth/me`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to get profile');
  },

  async getGoogleLoginUrl() {
    const res = await fetch(`${API_BASE}/auth/google/login`, {
      headers: { 'Accept': 'application/json' },
    });
    return readJson(res, 'Failed to get Google login URL');
  },

  async getGoogleConnectUrl() {
    const res = await fetch(`${API_BASE}/auth/google/connect`, {
      headers: getHeaders({ 'Accept': 'application/json' }),
    });
    return readJson(res, 'Failed to get Google connect URL');
  },

  async disconnectGoogle() {
    const res = await fetch(`${API_BASE}/auth/google/disconnect`, {
      method: 'POST',
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to disconnect Google account');
  },

  logout() {
    authStorage.clearToken();
  },

  // System Info
  async getSystemInfo() {
    const res = await fetch(`${API_BASE}/system-info`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to load system info');
  },

  // Employee profiles
  async getEmployees() {
    const res = await fetch(`${API_BASE}/employees`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to load employees');
  },

  async getEmployeeByEmail(email) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}`, {
      headers: getHeaders(),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Employee not found');
    return data;
  },

  async createEmployee(data) {
    const res = await fetch(`${API_BASE}/employees`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify(data),
    });
    return readJson(res, 'Failed to create employee');
  },

  async updateEmployeeByEmail(email, data) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}`, {
      method: 'PUT',
      headers: getHeaders(),
      body: JSON.stringify(data),
    });
    return readJson(res, 'Failed to update employee');
  },

  async updateEmployee(id, data) {
    const res = await fetch(`${API_BASE}/employees/${id}`, {
      method: 'PUT',
      headers: getHeaders(),
      body: JSON.stringify(data),
    });
    return readJson(res, 'Failed to update employee');
  },

  async deleteEmployee(id) {
    const res = await fetch(`${API_BASE}/employees/${id}`, {
      method: 'DELETE',
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to delete employee');
  },

  async disconnectEmployeeCalendar(id) {
    const res = await fetch(`${API_BASE}/employees/${id}/calendar`, {
      method: 'DELETE',
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to disconnect calendar');
  },

  async disconnectEmployeeCalendarByEmail(email) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}/calendar`, {
      method: 'DELETE',
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to disconnect calendar');
  },

  async getEmployeeCalendarStatus(id, verify = false) {
    const res = await fetch(`${API_BASE}/employees/${id}/calendar/status${verify ? '?verify=true' : ''}`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to fetch calendar status');
  },

  async getEmployeeCalendarStatusByEmail(email, verify = false) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}/calendar/status${verify ? '?verify=true' : ''}`, {
      headers: getHeaders(),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch calendar status');
    return data;
  },

  async searchEmployees(query) {
    const res = await fetch(`${API_BASE}/employees/search?q=${encodeURIComponent(query)}`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Employee search failed');
  },

  // Meeting workflow
  async orchestrateMeeting(requestText) {
    const res = await fetch(`${API_BASE}/meetings/orchestrate`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ request: requestText }),
    });
    return readJson(res, 'Orchestration failed');
  },

  async getMeetings(section = null) {
    const query = section ? `?section=${encodeURIComponent(section)}` : '';
    const res = await fetch(`${API_BASE}/meetings${query}`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to load meetings');
  },

  async getMeetingSections() {
    const res = await fetch(`${API_BASE}/meetings?section=sections`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to load meeting sections');
  },

  async getMeeting(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to load meeting');
  },

  async clarifyMeeting(id, answers) {
    const res = await fetch(`${API_BASE}/meetings/${id}/clarify`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify(answers),
    });
    return readJson(res, 'Could not save the meeting details');
  },

  async sendVenueRequest(id, { to, subject, body }) {
    const res = await fetch(`${API_BASE}/meetings/${id}/venue-request`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ to, subject, body }),
    });
    return readJson(res, 'The venue request could not be sent');
  },

  async deleteMeeting(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}`, {
      method: 'DELETE',
      headers: getHeaders(),
    });
    return readJson(res, 'Delete failed');
  },

  async syncAllResponses() {
    const res = await fetch(`${API_BASE}/meetings/sync-responses`, {
      method: 'POST',
      headers: getHeaders(),
    });
    return readJson(res, 'Response check failed');
  },

  async syncResponses(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/sync-responses`, {
      method: 'POST',
      headers: getHeaders(),
    });
    return readJson(res, 'Response check failed');
  },

  async submitApproval(id, action, notes = '', edits = null) {
    const res = await fetch(`${API_BASE}/meetings/${id}/approval`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ action, notes, edits }),
    });
    return readJson(res, 'Meeting approval update failed');
  },

  async recordAttendance(id, { participant_id, email, attended = true }) {
    const res = await fetch(`${API_BASE}/meetings/${id}/attendance`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ participant_id, email, attended }),
    });
    return readJson(res, 'Failed to record attendance');
  },

  async updateAgenda(id, { purpose, agenda, skip }) {
    const res = await fetch(`${API_BASE}/meetings/${id}/agenda`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ purpose, agenda, skip }),
    });
    return readJson(res, 'Agenda update failed');
  },

  async resolveParticipant(id, payload) {
    const res = await fetch(`${API_BASE}/meetings/${id}/resolve-participant`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify(payload),
    });
    return readJson(res, 'Participant update failed');
  },

  async recheckParticipant(id, queriedName) {
    const res = await fetch(`${API_BASE}/meetings/${id}/recheck-participant`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ queried_name: queriedName }),
    });
    return readJson(res, 'Re-check failed');
  },

  async removeUnresolvedParticipant(id, queriedName) {
    const res = await fetch(`${API_BASE}/meetings/${id}/remove-participant`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ queried_name: queriedName }),
    });
    return readJson(res, 'Could not remove the attendee');
  },

  // Async Simulations
  async simulateParticipant(id, email, response, notes = '') {
    const res = await fetch(`${API_BASE}/meetings/${id}/simulate-participant`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ email, response, notes }),
    });
    return readJson(res, 'Participant response failed');
  },

  async simulateRoom(id, roomName, confirmed, notes = '') {
    const res = await fetch(`${API_BASE}/meetings/${id}/simulate-room`, {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify({ room_name: roomName, confirmed, notes }),
    });
    return readJson(res, 'Auditorium response failed');
  },

  // Internal audit records
  async getAuditLogs() {
    const res = await fetch(`${API_BASE}/audit-logs`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to load audit logs');
  },

  async getMeetingTrace(id) {
    const res = await fetch(`${API_BASE}/audit-logs/${id}`, {
      headers: getHeaders(),
    });
    return readJson(res, 'Failed to load meeting trace');
  },

  // Minutes of Meeting (organizer only)
  async getMinutes(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/minutes`, { headers: getHeaders() });
    return readJson(res, 'Failed to load the minutes');
  },

  async checkTranscript(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/minutes/check-transcript`, { method: 'POST', headers: getHeaders() });
    return readJson(res, 'Could not check for the transcript');
  },

  async regenerateMinutes(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/minutes/regenerate`, { method: 'POST', headers: getHeaders() });
    return readJson(res, 'Could not regenerate the minutes');
  },

  async saveMinutes(id, edits) {
    const res = await fetch(`${API_BASE}/meetings/${id}/minutes`, {
      method: 'PUT',
      headers: getHeaders(),
      body: JSON.stringify(edits),
    });
    return readJson(res, 'Could not save the minutes');
  },

  async approveMinutes(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/minutes/approve`, { method: 'POST', headers: getHeaders() });
    return readJson(res, 'Could not approve and send the minutes');
  },

  // Browser-recorded audio (alternative to Google's own transcript)
  async startRecording(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/recording/start`, {
      method: 'POST', headers: getHeaders(), body: JSON.stringify({ consent: true }),
    });
    return readJson(res, 'Could not start the recording');
  },

  async uploadRecordingSegment(id, { index, offsetSeconds, durationSeconds, blob }) {
    const form = new FormData();
    form.append('segment_index', String(index));
    form.append('offset_seconds', String(offsetSeconds));
    form.append('duration_seconds', String(durationSeconds));
    form.append('file', blob, `segment_${index}.webm`);
    const headers = getHeaders();
    delete headers['Content-Type'];            // the browser sets the multipart boundary
    const res = await fetch(`${API_BASE}/meetings/${id}/recording/segments`, { method: 'POST', headers, body: form });
    return readJson(res, 'Audio upload failed');
  },

  async finishRecording(id, segmentCount = null) {
    const res = await fetch(`${API_BASE}/meetings/${id}/recording/finish`, {
      method: 'POST', headers: getHeaders(), body: JSON.stringify({ segment_count: segmentCount }),
    });
    return readJson(res, 'Could not finish the recording');
  },

  async retryTranscription(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/recording/retry`, { method: 'POST', headers: getHeaders() });
    return readJson(res, 'Could not retry the transcription');
  },

  async discardRecording(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/recording/discard`, { method: 'POST', headers: getHeaders() });
    return readJson(res, 'Could not discard the recording');
  },

  async deleteRecordingAudio(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/recording/audio`, { method: 'DELETE', headers: getHeaders() });
    return readJson(res, 'Could not delete the audio');
  },

  // Action item tracker
  async getActionItems(scope = 'mine', status = '') {
    const query = new URLSearchParams({ scope, ...(status ? { status } : {}) });
    const res = await fetch(`${API_BASE}/action-items?${query}`, { headers: getHeaders() });
    return readJson(res, 'Failed to load action items');
  },

  async updateActionItem(id, changes) {
    const res = await fetch(`${API_BASE}/action-items/${id}`, {
      method: 'PATCH',
      headers: getHeaders(),
      body: JSON.stringify(changes),
    });
    return readJson(res, 'Could not update the action item');
  },

  // Carry-forward of unfinished tasks into a new meeting
  async getFollowupSuggestions(id, showAll = false) {
    const res = await fetch(`${API_BASE}/meetings/${id}/followup-suggestions?show_all=${showAll}`, { headers: getHeaders() });
    return readJson(res, 'Failed to load follow-up suggestions');
  },

  async saveFollowups(id, actionItemIds) {
    const res = await fetch(`${API_BASE}/meetings/${id}/followups`, {
      method: 'PUT',
      headers: getHeaders(),
      body: JSON.stringify({ action_item_ids: actionItemIds }),
    });
    return readJson(res, 'Could not save the follow-up tasks');
  },
};
