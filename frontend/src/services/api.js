const API_BASE = '/api';

export const api = {
  // System Info
  async getSystemInfo() {
    const res = await fetch(`${API_BASE}/system-info`);
    return res.json();
  },

  // Employee profiles
  async getEmployees() {
    const res = await fetch(`${API_BASE}/employees`);
    return res.json();
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
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to create employee');
    }
    return res.json();
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
    return res.json();
  },

  async deleteEmployee(id) {
    const res = await fetch(`${API_BASE}/employees/${id}`, { method: 'DELETE' });
    return res.json();
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

  async getCalendarStatusByEmail(email) {
    const res = await fetch(`${API_BASE}/employees/by-email/${encodeURIComponent(email)}/calendar/status`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch calendar status');
    return data;
  },

  async searchEmployees(query) {
    const res = await fetch(`${API_BASE}/employees/search?q=${encodeURIComponent(query)}`);
    return res.json();
  },

  // Meeting workflow
  async orchestrateMeeting(requestText) {
    const res = await fetch(`${API_BASE}/meetings/orchestrate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request: requestText }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Orchestration failed');
    }
    return res.json();
  },

  async getMeetings() {
    const res = await fetch(`${API_BASE}/meetings`);
    return res.json();
  },

  async getMeeting(id) {
    const res = await fetch(`${API_BASE}/meetings/${id}`);
    return res.json();
  },

  async submitApproval(id, action, notes = '', edits = null) {
    const res = await fetch(`${API_BASE}/meetings/${id}/approval`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, notes, edits }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Meeting approval update failed');
    return data;
  },

  async updateAgenda(id, { purpose, agenda, skip }) {
    const res = await fetch(`${API_BASE}/meetings/${id}/agenda`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ purpose, agenda, skip }),
    });
    return res.json();
  },

  async resolveParticipant(id, payload) {
    const res = await fetch(`${API_BASE}/meetings/${id}/resolve-participant`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Participant update failed');
    return data;
  },

  // Async Simulations
  async simulateParticipant(id, email, response, notes = '') {
    const res = await fetch(`${API_BASE}/meetings/${id}/simulate-participant`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, response, notes }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Participant response failed');
    return data;
  },

  async simulateRoom(id, roomName, confirmed, notes = '') {
    const res = await fetch(`${API_BASE}/meetings/${id}/simulate-room`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room_name: roomName, confirmed, notes }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Auditorium response failed');
    return data;
  },

  // Internal audit records
  async getAuditLogs() {
    const res = await fetch(`${API_BASE}/audit-logs`);
    return res.json();
  },

  async getMeetingTrace(id) {
    const res = await fetch(`${API_BASE}/audit-logs/${id}`);
    return res.json();
  },
};
