import React, { useEffect, useState } from 'react';
import Navbar from './components/Navbar';
import MeetingDashboard from './pages/MeetingDashboard/MeetingDashboard';
import AuthPage from './pages/Auth/AuthPage';
import ActionItemsPage from './pages/ActionItems/ActionItemsPage';
import { api, authStorage } from './services/api';

export default function App() {
  const [currentUser, setCurrentUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState(null);
  const [view, setView] = useState('meetings');
  const [systemInfo, setSystemInfo] = useState(null);
  const [year] = useState(() => new Date().getFullYear());

  useEffect(() => {
    // 1. Check for token in URL hash or query params (from Google OAuth callback)
    const hash = window.location.hash || '';
    const search = window.location.search || '';

    let token = null;
    let authErr = null;

    if (hash.includes('auth_token=')) {
      const match = hash.match(/auth_token=([^&]+)/);
      if (match) token = match[1];
    } else if (search.includes('auth_token=')) {
      const match = search.match(/auth_token=([^&]+)/);
      if (match) token = match[1];
    }

    if (hash.includes('auth_error=') || search.includes('auth_error=')) {
      const match = (hash + search).match(/auth_error=([^&]+)/);
      if (match) authErr = decodeURIComponent(match[1]);
    }

    if (token) {
      authStorage.setToken(token);
      // Clean URL hash without reloading
      window.history.replaceState({}, document.title, window.location.pathname);
    }

    if (authErr) {
      setAuthError(authErr);
      window.history.replaceState({}, document.title, window.location.pathname);
    }

    // 2. Fetch current user if token exists
    const storedToken = authStorage.getToken();
    if (storedToken) {
      api.getMe()
        .then((user) => {
          setCurrentUser(user);
        })
        .catch(() => {
          authStorage.clearToken();
          setCurrentUser(null);
        })
        .finally(() => {
          setLoading(false);
        });
    } else {
      setLoading(false);
    }

    api.getSystemInfo().then(setSystemInfo).catch(() => setSystemInfo(null));
  }, []);

  const handleLogout = () => {
    api.logout();
    setCurrentUser(null);
  };

  const handleAuthSuccess = (user) => {
    setCurrentUser(user);
    setAuthError(null);
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center text-slate-400 text-sm">
        Loading Meeting Organizer…
      </div>
    );
  }

  if (!currentUser) {
    return <AuthPage onAuthSuccess={handleAuthSuccess} initialError={authError} />;
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      <Navbar
        currentUser={currentUser}
        onLogout={handleLogout}
        onUserUpdated={(updated) => setCurrentUser(updated)}
      />

      {systemInfo?.demo_mode && (
        <div role="status" className="bg-amber-500/10 border-b border-amber-500/30 text-amber-300 text-xs py-2 px-6 text-center">
          DEMO_MODE is on: Google Calendar, Meet and Gmail are simulated by offline adapters.
        </div>
      )}

      {/* Main Dashboard Area */}
      <main className="flex-1 max-w-6xl w-full mx-auto p-6 space-y-6">
        <nav role="tablist" aria-label="Sections" className="flex gap-2 border-b border-slate-800">
          {[['meetings', 'Meetings'], ['actions', 'Action items']].map(([key, label]) => (
            <button key={key} role="tab" aria-selected={view === key} onClick={() => setView(key)}
              className={`px-4 py-2 text-sm font-semibold cursor-pointer border-b-2 -mb-px ${view === key ? 'border-indigo-500 text-white' : 'border-transparent text-slate-400 hover:text-slate-200'}`}>
              {label}
            </button>
          ))}
        </nav>
        {view === 'meetings'
          ? <MeetingDashboard demoMode={!!systemInfo?.demo_mode} currentUser={currentUser} />
          : <ActionItemsPage />}
      </main>

      <footer className="border-t border-slate-800 py-3.5 px-6 text-center text-xs text-slate-500">
        MEETING ORGANIZER AGENT &copy; {year}
      </footer>
    </div>
  );
}
