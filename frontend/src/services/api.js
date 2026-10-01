const API_BASE = '/api';

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
  // System Info
  async getSystemInfo() {
    const res = await fetch(`${API_BASE}/system-info`);
    return readJson(res, 'Failed to load system info');
  },

  // Employee profiles
  async getEmployees() {
    const res = await fetch(`${API_BASE}/employees`);
    return readJson(res, 'Failed to load employees');
  },

  async getEmployeeByEmail(email) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Employee not found');
    return data;
  },

  async createEmployee(data) {
    const res = await fetch(`${API_BASE}/employees`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    return readJson(res, 'Failed to create employee');
  },

  async updateEmployeeByEmail(email, data) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || 'Failed to update employee');
    return result;
  },

  async updateEmployee(id, data) {
    const res = await fetch(`${API_BASE}/employees/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    return readJson(res, 'Failed to update employee');
  },

  async deleteEmployee(id) {
    const res = await fetch(`${API_BASE}/employees/${id}`, { method: 'DELETE' });
    return readJson(res, 'Failed to delete employee');
  },

  async disconnectCalendar(id) {
    const res = await fetch(`${API_BASE}/employees/${id}/calendar`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Calendar disconnect failed');
    return data;
  },

  async disconnectCalendarByEmail(email) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}/calendar`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Calendar disconnect failed');
    return data;
  },

  async getCalendarStatus(id) {
    const res = await fetch(`${API_BASE}/employees/${id}/calendar/status`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch calendar status');
    return data;
  },

  // verify=true asks the backend to load/refresh the stored Google token instead of trusting a flag.
  async getCalendarStatusByEmail(email, verify = false) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}/calendar/status${verify ? '?verify=true' : ''}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch calendar status');
    return data;
  },

  async searchEmployees(query) {
    const res = await fetch(`${API_BASE}/employees/search?q=${encodeURIComponent(query)}`);
    return readJson(res, 'Employee search failed');
  },

  // Meeting workflow
  async orchestrateMeeting(requestText) {
    const res = await fetch(`${API_BASE}/meetings/orchestrate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request: requestText }),
    });
    return readJson(res, 'Orchestration failed');
  },

  async getMeetings() {
    const res = await fetch(`${API_BASE}/meetings`);
    return readJson(res, 'Failed to load meetings');
  },

  async getMeeting(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}`);
    return readJson(res, 'Failed to load meeting');
  },

  // Checks REAL responses now (Google Calendar attendee status, Gmail auditorium replies).
  async syncResponses(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}/sync-responses`, { method: 'POST' });
    return readJson(res, 'Response check failed');
  },

  async submitApproval(id, action, notes = '', edits = null) {
    const res = await fetch(`${API_BASE}/meetings/${id}/approval`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, notes, edits }),
    });
    return readJson(res, 'Meeting approval update failed');
  },

  async updateAgenda(id, { purpose, agenda, skip }) {
    const res = await fetch(`${API_BASE}/meetings/${id}/agenda`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ purpose, agenda, skip }),
    });
    return readJson(res, 'Agenda update failed');
  },

  async resolveParticipant(id, payload) {
    const res = await fetch(`${API_BASE}/meetings/${id}/resolve-participant`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return readJson(res, 'Participant update failed');
  },

  // Async Simulations
  async simulateParticipant(id, email, response, notes = '') {
    const res = await fetch(`${API_BASE}/meetings/${id}/simulate-participant`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, response, notes }),
    });
    return readJson(res, 'Participant response failed');
  },

  async simulateRoom(id, roomName, confirmed, notes = '') {
    const res = await fetch(`${API_BASE}/meetings/${id}/simulate-room`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room_name: roomName, confirmed, notes }),
    });
    return readJson(res, 'Auditorium response failed');
  },

  // Internal audit records
  async getAuditLogs() {
    const res = await fetch(`${API_BASE}/audit-logs`);
    return readJson(res, 'Failed to load audit logs');
  },

  async getMeetingTrace(id) {
    const res = await fetch(`${API_BASE}/audit-logs/${id}`);
    return readJson(res, 'Failed to load meeting trace');
  },
};
