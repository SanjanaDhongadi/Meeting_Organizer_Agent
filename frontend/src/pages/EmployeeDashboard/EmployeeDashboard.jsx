import React, { useState, useEffect } from 'react';
import { UserPlus, Search, Clock, Trash2 } from 'lucide-react';
import { api } from '../../services/api';

export default function EmployeeDashboard() {
  const [employees, setEmployees] = useState([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searchMode, setSearchMode] = useState(false);

  // Form State
  const [formData, setFormData] = useState({
    employee_id: '',
    name: '',
    email: '',
    designation: '',
    department: '',
    working_days: 'Monday,Tuesday,Wednesday,Thursday,Friday',
    working_hours_start: '09:00:00',
    working_hours_end: '17:00:00',
    timezone: 'Asia/Kolkata',
    meeting_preferences: '',
    preferred_duration: 30,
    mode_preference: 'Online',
    location: '',
    other_info: ''
  });
  const [saveMessage, setSaveMessage] = useState('');

  const loadEmployees = async () => {
    try {
      const data = await api.getEmployees();
      setEmployees(data);
      setSearchMode(false);
    } catch (e) {
      console.error(e);
    }
  };

  useEffect(() => {
    loadEmployees();

    const params = new URLSearchParams(window.location.search);
    const calendarStatus = params.get('calendar');
    const email = params.get('email');

    if (calendarStatus === 'connected') {
      setSaveMessage(email ? `✓ Google Calendar connected successfully for ${email}.` : '✓ Google Calendar connected successfully.');
      window.history.replaceState({}, document.title, window.location.pathname);
    } else if (calendarStatus === 'denied') {
      alert('Google Calendar connection was denied.');
      window.history.replaceState({}, document.title, window.location.pathname);
    } else if (calendarStatus === 'unregistered_email') {
      alert(`Google authorization succeeded for ${email || 'account'}, but this email does not match any registered employee. Please register this employee first.`);
      window.history.replaceState({}, document.title, window.location.pathname);
    } else if (calendarStatus === 'error') {
      alert('Google Calendar connection encountered an error.');
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, []);

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!searchQuery.trim()) {
      loadEmployees();
      return;
    }
    try {
      const results = await api.searchEmployees(searchQuery);
      setEmployees(results.map(r => r.employee));
      setSearchMode(true);
    } catch (e) {
      console.error(e);
    }
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    setLoading(true);
    setSaveMessage('');
    try {
      const savedEmployee = await api.createEmployee(formData);
      setIsCreating(false);
      await loadEmployees();
      setSaveMessage(`✓ Profile for ${savedEmployee.name} (${savedEmployee.email}) saved to database successfully.`);
      setFormData({
        employee_id: '',
        name: '',
        email: '',
        designation: '',
        department: '',
        working_days: 'Monday,Tuesday,Wednesday,Thursday,Friday',
        working_hours_start: '09:00:00',
        working_hours_end: '17:00:00',
        timezone: 'Asia/Kolkata',
        meeting_preferences: '',
        preferred_duration: 30,
        mode_preference: 'Online',
        location: '',
        other_info: ''
      });
    } catch (err) {
      alert(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnectCalendar = async (id) => {
    try {
      await api.disconnectCalendar(id);
      loadEmployees();
    } catch (e) {
      console.error(e);
    }
  };

  const handleDelete = async (id) => {
    if (confirm('Delete this employee profile?')) {
      try {
        await api.deleteEmployee(id);
        loadEmployees();
      } catch (e) {
        console.error(e);
      }
    }
  };

  return (
    <div className="space-y-6 max-w-7xl mx-auto">
      
      {/* Top Banner */}
      <div className="glass-panel p-6 rounded-2xl border border-slate-800 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Employee Dashboard</h1>
          <p className="text-sm text-slate-400 mt-1">Profiles, availability, preferences, and calendar connections.</p>
        </div>

        <button
          onClick={() => setIsCreating(true)}
          className="flex items-center space-x-2 px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs transition-all shadow-lg shadow-indigo-600/30 self-start sm:self-auto"
        >
          <UserPlus className="w-4 h-4" />
          <span>Add Employee</span>
        </button>
      </div>
      {saveMessage && <p role="status" className="text-sm text-emerald-700">{saveMessage}</p>}

      {/* Semantic Search Bar */}
      <div className="glass-panel p-4 rounded-xl border border-slate-800">
        <form onSubmit={handleSearch} className="flex items-center space-x-3">
          <div className="relative flex-1">
            <Search className="w-4 h-4 text-slate-500 absolute left-3 top-3" />
            <input
              type="text"
              placeholder="Search employees by name, email, designation, or department"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-slate-950/80 border border-slate-800 rounded-xl pl-9 pr-4 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <button
            type="submit"
            className="flex items-center space-x-1.5 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl text-xs font-semibold border border-slate-700 transition-all"
          >
            <span>Search</span>
          </button>
          {searchMode && (
            <button
              type="button"
              onClick={loadEmployees}
              className="px-3 py-2 text-slate-400 hover:text-white text-xs"
            >
              Reset
            </button>
          )}
        </form>
      </div>

      {/* Create Modal */}
      {isCreating && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
          <div className="relative w-full max-w-2xl glass-panel-glow bg-slate-900 border border-slate-700 rounded-2xl p-6 sm:p-7 shadow-2xl space-y-5 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3">
              <h2 className="text-lg font-bold text-white">Create Employee Profile</h2>
              <button onClick={() => setIsCreating(false)} className="text-slate-400 hover:text-white">✕</button>
            </div>

            <form onSubmit={handleCreate} className="space-y-4 text-xs">
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="text-slate-400 block mb-1">Employee ID *</label>
                  <input
                    required
                    type="text"
                    value={formData.employee_id}
                    onChange={(e) => setFormData({ ...formData, employee_id: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
                <div>
                  <label className="text-slate-400 block mb-1">Name *</label>
                  <input
                    required
                    type="text"
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
                <div>
                  <label className="text-slate-400 block mb-1">Email *</label>
                  <input
                    required
                    type="email"
                    value={formData.email}
                    onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="text-slate-400 block mb-1">Designation</label>
                  <input
                    type="text"
                    value={formData.designation}
                    onChange={(e) => setFormData({ ...formData, designation: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
                <div>
                  <label className="text-slate-400 block mb-1">Department</label>
                  <input
                    type="text"
                    value={formData.department}
                    onChange={(e) => setFormData({ ...formData, department: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="text-slate-400 block mb-1">Working Hours Start</label>
                  <input
                    type="text"
                    value={formData.working_hours_start}
                    onChange={(e) => setFormData({ ...formData, working_hours_start: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white font-mono"
                  />
                </div>
                <div>
                  <label className="text-slate-400 block mb-1">Working Hours End</label>
                  <input
                    type="text"
                    value={formData.working_hours_end}
                    onChange={(e) => setFormData({ ...formData, working_hours_end: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white font-mono"
                  />
                </div>
                <div>
                  <label className="text-slate-400 block mb-1">Time Zone</label>
                  <input
                    type="text"
                    value={formData.timezone}
                    onChange={(e) => setFormData({ ...formData, timezone: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="text-slate-400 block mb-1">Pref. Mode</label>
                  <select
                    value={formData.mode_preference}
                    onChange={(e) => setFormData({ ...formData, mode_preference: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  >
                    <option value="Online">Online</option>
                    <option value="Offline">Offline</option>
                    <option value="Hybrid">Hybrid</option>
                  </select>
                </div>
                <div>
                  <label className="text-slate-400 block mb-1">Pref. Duration (min)</label>
                  <input
                    type="number"
                    value={formData.preferred_duration}
                    onChange={(e) => setFormData({ ...formData, preferred_duration: Number(e.target.value) })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
                <div>
                  <label className="text-slate-400 block mb-1">Office Location</label>
                  <input
                    type="text"
                    value={formData.location}
                    onChange={(e) => setFormData({ ...formData, location: e.target.value })}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  />
                </div>
              </div>

              <div>
                <label className="text-slate-400 block mb-1">Meeting Preferences & Policies</label>
                <textarea
                  rows={2}
                  value={formData.meeting_preferences}
                  onChange={(e) => setFormData({ ...formData, meeting_preferences: e.target.value })}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                  placeholder="e.g. No Friday meetings, prefers mornings, max 30 mins"
                />
              </div>

              <div>
                <label className="text-slate-400 block mb-1">Other Info / Notes</label>
                <input
                  type="text"
                  value={formData.other_info}
                  onChange={(e) => setFormData({ ...formData, other_info: e.target.value })}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-white"
                />
              </div>

              <div className="pt-3 border-t border-slate-800 flex justify-end space-x-3">
                <button
                  type="button"
                  onClick={() => setIsCreating(false)}
                  className="px-4 py-2 rounded-xl text-slate-400 hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={loading}
                  className="px-5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold transition-all shadow-md shadow-indigo-600/30"
                >
                  {loading ? 'Saving...' : 'Submit / Save Profile'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Employees Grid */}
      {employees.length === 0 ? (
        <div className="glass-panel p-10 rounded-2xl border border-slate-800 text-center text-slate-400 text-sm">
          No employees registered yet.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {employees.map((emp) => (
            <div
              key={emp.id}
              className="glass-panel p-5 rounded-2xl border border-slate-800/80 hover:border-slate-700 transition-all flex flex-col justify-between space-y-4"
            >
              <div className="space-y-3">
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="font-bold text-white text-base">{emp.name}</h3>
                    <div className="text-xs text-indigo-400 font-medium">{emp.designation}</div>
                    <div className="text-[11px] text-slate-500">{emp.department}</div>
                  </div>
                  <button
                    onClick={() => handleDelete(emp.id)}
                    className="text-slate-600 hover:text-rose-400 p-1 rounded-lg transition-colors"
                    title="Delete Employee"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                <div className="space-y-1.5 text-xs text-slate-300 bg-slate-950/60 p-3 rounded-xl border border-slate-900">
                  <div className="flex items-center space-x-2 text-[11px] text-slate-400 truncate">
                    <span className="font-mono text-slate-500">Email:</span>
                    <span className="text-slate-200">{emp.email}</span>
                  </div>
                  <div className="flex items-center space-x-2 text-[11px] text-slate-400">
                    <Clock className="w-3.5 h-3.5 text-indigo-400 flex-shrink-0" />
                    <span>{emp.working_days?.replaceAll(',', ', ')} · {emp.working_hours_start?.slice(0,5)}–{emp.working_hours_end?.slice(0,5)} ({emp.timezone})</span>
                  </div>
                </div>

                {emp.meeting_preferences && (
                  <div className="text-xs text-slate-400 bg-slate-900/40 p-2.5 rounded-lg border border-slate-800/60">
                    <span className="text-slate-500 font-semibold text-[10px] block uppercase tracking-wider mb-0.5">Preferences:</span>
                    <span className="text-slate-300 text-[11px]">{emp.meeting_preferences}</span>
                  </div>
                )}
              </div>

              {/* Google Calendar OAuth Connection Button */}
              <div className="pt-2 border-t border-slate-800/80 flex items-center justify-between">
                <div className="flex items-center space-x-1.5">
                  <span className={`w-2 h-2 rounded-full ${emp.google_calendar_connected ? 'bg-emerald-400' : 'bg-slate-600'}`} />
                  <span className="text-[11px] text-slate-400 font-medium">
                    {emp.google_calendar_connected ? 'Connected' : 'Not Connected'}
                  </span>
                </div>

                {emp.google_calendar_connected ? (
                  <button
                    onClick={() => handleDisconnectCalendar(emp.id)}
                    className="text-[11px] font-semibold px-2.5 py-1 rounded-lg border border-slate-300 text-slate-600"
                  >
                    Disconnect
                  </button>
                ) : (
                  <a
                    href={`/api/employees/${emp.id}/calendar/connect`}
                    className="text-[11px] font-semibold px-2.5 py-1 rounded-lg border border-indigo-500/30 bg-indigo-600/20 text-indigo-400"
                  >
                    Connect calendar
                  </a>
                )}
              </div>

            </div>
          ))}
        </div>
      )}

    </div>
  );
}
