import React, { useEffect, useState } from 'react';
import Navbar from './components/Navbar';
import EmployeeDashboard from './pages/EmployeeDashboard/EmployeeDashboard';
import MeetingDashboard from './pages/MeetingDashboard/MeetingDashboard';
import { api } from './services/api';

const VIEWS = ['meetings', 'employees'];

// Returning from Google OAuth (?calendar=...) always lands on the Employees view.
const initialView = () => {
  if (new URLSearchParams(window.location.search).get('calendar')) return 'employees';
  const fromHash = window.location.hash.replace('#', '');
  return VIEWS.includes(fromHash) ? fromHash : 'meetings';
};

export default function App() {
  const [view, setView] = useState(initialView);
  const [systemInfo, setSystemInfo] = useState(null);
  const [year] = useState(() => new Date().getFullYear());

  useEffect(() => {
    api.getSystemInfo().then(setSystemInfo).catch(() => setSystemInfo(null));
    const onHash = () => {
      const next = window.location.hash.replace('#', '');
      if (VIEWS.includes(next)) setView(next);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const changeView = (next) => {
    setView(next);
    window.history.replaceState({}, document.title, `${window.location.pathname}${window.location.search}#${next}`);
  };

  return (
    <div style={{ minHeight: '100vh', background: '#f9fafb', display: 'flex', flexDirection: 'column' }}>
      <Navbar activeTab={view} onChange={changeView} />

      {systemInfo?.demo_mode && (
        <div role="status" style={{ background: '#fef3c7', borderBottom: '1px solid #f59e0b', color: '#92400e', fontSize: 12, padding: '8px 24px', textAlign: 'center' }}>
          DEMO_MODE is on: Google Calendar, Meet and Gmail are replaced by offline test adapters. Event IDs and Meet links shown are simulated.
        </div>
      )}
      {systemInfo && !systemInfo.demo_mode && !systemInfo.google?.oauth_configured && (
        <div role="alert" style={{ background: '#fee2e2', borderBottom: '1px solid #ef4444', color: '#991b1b', fontSize: 12, padding: '8px 24px', textAlign: 'center' }}>
          Google OAuth is not configured on the backend (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SECRET_KEY). Calendar connections will fail until it is.
        </div>
      )}

      <main style={{ flex: 1, maxWidth: 1100, width: '100%', margin: '0 auto', padding: '28px 24px' }}>
        {view === 'employees' ? <EmployeeDashboard /> : <MeetingDashboard />}
      </main>

      <footer style={{ borderTop: '1px solid #e5e7eb', padding: '14px 24px', textAlign: 'center', fontSize: 12, color: '#6b7280' }}>
        MEETING ORGANIZER AGENT &copy; {year}
      </footer>
    </div>
  );
}
