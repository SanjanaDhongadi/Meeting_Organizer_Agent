import React, { useState } from 'react';
import { Calendar, CheckCircle2, LogOut, Link as LinkIcon, Unlink } from 'lucide-react';
import { api } from '../services/api';

export default function Navbar({ currentUser, onLogout, onUserUpdated }) {
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);

  const handleConnectGoogle = async () => {
    try {
      setConnecting(true);
      const res = await api.getGoogleConnectUrl();
      if (res?.authorization_url) {
        window.location.href = res.authorization_url;
      }
    } catch (err) {
      alert('Could not start Google connection: ' + err.message);
    } finally {
      setConnecting(false);
    }
  };

  const handleDisconnectGoogle = async () => {
    if (!window.confirm('Disconnect your Google Calendar and Gmail integration?')) return;
    try {
      setDisconnecting(true);
      const res = await api.disconnectGoogle();
      if (res?.user && onUserUpdated) {
        onUserUpdated(res.user);
      }
    } catch (err) {
      alert('Failed to disconnect Google: ' + err.message);
    } finally {
      setDisconnecting(false);
    }
  };

  return (
    <header className="sticky top-0 z-40 w-full bg-slate-900 border-b border-slate-800 shadow-sm">
      <div className="max-w-6xl mx-auto px-6 min-h-[64px] flex items-center justify-between gap-4 flex-wrap">
        {/* Left: Brand */}
        <div className="flex items-center space-x-3">
          <div className="w-9 h-9 rounded-xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400">
            <Calendar className="w-5 h-5" />
          </div>
          <div>
            <div className="font-bold text-sm text-white leading-tight">Meeting Organizer Agent</div>
            <div className="text-[11px] text-slate-400">AI-Powered Schedule & Coordination</div>
          </div>
        </div>

        {/* Right: User Status & Actions */}
        {currentUser && (
          <div className="flex items-center gap-3 flex-wrap">
            {/* Google Calendar Connection Status */}
            {currentUser.google_calendar_connected ? (
              <div className="flex items-center gap-2 bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs px-3 py-1.5 rounded-xl">
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                <span className="font-medium">Google Connected</span>
                <button
                  onClick={handleDisconnectGoogle}
                  disabled={disconnecting}
                  title="Disconnect Google"
                  className="ml-1 text-slate-400 hover:text-rose-400 transition-colors cursor-pointer"
                >
                  <Unlink className="w-3.5 h-3.5" />
                </button>
              </div>
            ) : (
              <button
                onClick={handleConnectGoogle}
                disabled={connecting}
                className="flex items-center gap-2 bg-slate-800 hover:bg-slate-700 border border-slate-700 text-indigo-400 text-xs font-semibold px-3 py-1.5 rounded-xl transition-all cursor-pointer"
              >
                <LinkIcon className="w-3.5 h-3.5" />
                <span>{connecting ? 'Connecting…' : 'Connect Google'}</span>
              </button>
            )}

            {/* User Profile */}
            <div className="bg-slate-950 border border-slate-800 px-3 py-1.5 rounded-xl text-xs">
              <span className="font-semibold text-white mr-1.5">{currentUser.name || 'User'}</span>
              <span className="text-slate-400 font-mono text-[11px]">{currentUser.email}</span>
            </div>

            {/* Logout */}
            <button
              onClick={onLogout}
              title="Sign Out"
              className="p-2 text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 rounded-xl transition-all cursor-pointer"
            >
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
