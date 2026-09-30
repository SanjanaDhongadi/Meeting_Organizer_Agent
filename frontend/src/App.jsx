import React, { useState } from 'react';
import Navbar from './components/Navbar';
import EmployeeDashboard from './pages/EmployeeDashboard/EmployeeDashboard';
import MeetingDashboard from './pages/MeetingDashboard/MeetingDashboard';

export default function App() {
  const dashboard = import.meta.env.VITE_DASHBOARD || 'employees';
  const [year] = useState(() => new Date().getFullYear());

  return (
    <div style={{ minHeight: '100vh', background: '#f9fafb', display: 'flex', flexDirection: 'column' }}>
      <Navbar activeTab={dashboard} />

      <main style={{ flex: 1, maxWidth: 1100, width: '100%', margin: '0 auto', padding: '28px 24px' }}>
        {dashboard === 'employees' ? <EmployeeDashboard /> : <MeetingDashboard />}
      </main>

      <footer style={{ borderTop: '1px solid #e5e7eb', padding: '14px 24px', textAlign: 'center', fontSize: 12, color: '#6b7280' }}>
        MEETING ORGANIZER AGENT &copy; {year}
      </footer>
    </div>
  );
}
