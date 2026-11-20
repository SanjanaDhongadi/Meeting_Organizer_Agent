import React, { useState } from 'react';
import { Building2, Mail, Send, XCircle } from 'lucide-react';
import { api } from '../services/api';

const REQUEST = {
  NOT_SENT: { label: 'Not sent yet', tone: 'text-slate-600 bg-slate-100 border-slate-200' },
  SENDING: { label: 'Sending · do not retry until status is checked', tone: 'text-amber-900 bg-amber-50 border-amber-200' },
  SENT: { label: 'Request sent · waiting for the coordinator’s reply', tone: 'text-amber-900 bg-amber-50 border-amber-200' },
  CONFIRMED: { label: 'Confirmed by the venue coordinator', tone: 'text-emerald-800 bg-emerald-50 border-emerald-200' },
  DECLINED: { label: 'Declined by the venue coordinator', tone: 'text-rose-800 bg-rose-50 border-rose-200' },
};

// The email can only be sent while the meeting is still being organized.
const CLOSED = ['DELETED', 'REJECTED', 'CONFIRMED', 'APPROVED', 'EXECUTING'];

/**
 * Venue Coordinator tab (in-person meetings): request status,
 * and an editable draft email that is sent ONLY when the organizer clicks Send. The draft lives in the parent
 * (`draft` / `onDraftChange`) so switching tabs or refreshing the plan never loses the organizer's edits.
 */
export default function VenueCoordinatorPanel({ meeting, draft, onDraftChange, onSent }) {
  const venue = meeting.venue_coordination || {};
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState(null); // { kind: 'error' | 'success', text }
  const value = draft || venue.draft || { to: '', subject: '', body: '' };
  const availability = venue.availability?.status === 'BUSY' ? venue.availability : null;
  const request = REQUEST[venue.request_status] || REQUEST.NOT_SENT;
  const canSend = venue.configured && ['NOT_SENT', 'DECLINED'].includes(venue.request_status) && !CLOSED.includes(meeting.status);

  const update = (field) => (e) => onDraftChange({ ...value, [field]: e.target.value });

  const send = async () => {
    setSending(true);
    setResult(null);
    try {
      const res = await api.sendVenueRequest(meeting.meeting_id || meeting.id, { ...value, to: venue.coordinator_email });
      setResult({ kind: 'success', text: `Venue request sent to ${res.to}. The venue is not confirmed until the coordinator replies.` });
      onSent(res.meeting);
    } catch (e) {
      setResult({ kind: 'error', text: e.message });
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
        <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
          <span className="text-slate-500 block mb-0.5">Venue coordinator</span>
          <span className="font-semibold text-white block">{venue.coordinator_name || 'Venue coordinator'}</span>
          <span className="text-slate-400 font-mono break-all">{venue.coordinator_email || 'Not configured'}</span>
        </div>
        <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
          <span className="text-slate-500 block mb-0.5">Venue</span>
          <span className="font-semibold text-white flex items-center gap-1.5"><Building2 className="w-3.5 h-3.5" />{venue.venue || 'Not provided'}</span>
        </div>
        <div className="bg-slate-950 p-3 rounded-xl border border-slate-800">
          <span className="text-slate-500 block mb-0.5">Venue request</span>
          <span className={`inline-block mt-0.5 px-2 py-0.5 rounded-md border text-[11px] font-semibold ${request.tone}`}>{request.label}</span>
          <span className="text-slate-400 block mt-1">Email status: {venue.email_status || 'NOT_SENT'}</span>
        </div>
      </div>

      {venue.configuration_error && (
        <div role="alert" className="text-xs text-rose-800 bg-rose-50 border border-rose-200 rounded-xl p-3">{venue.configuration_error}</div>
      )}

      {availability && (
        <div role="status" className="text-xs border border-rose-200 rounded-xl p-3 flex gap-2 text-rose-800 bg-rose-50">
          <XCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <div><strong>Venue conflict in this application.</strong> {availability.reason}</div>
        </div>
      )}

      {venue.request?.notes && (
        <div className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-xl p-3 whitespace-pre-wrap">{venue.request.notes}</div>
      )}

      {['FAILED', 'SENDING'].includes(venue.email_status) && venue.email_status_message && (
        <div role="status" className="text-xs text-rose-800 bg-rose-50 border border-rose-200 rounded-xl p-3">{venue.email_status_message}</div>
      )}

      <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-3">
        <div className="flex items-center gap-2 font-semibold text-white text-sm"><Mail className="w-4 h-4" />Venue booking request email</div>
        <p className="text-[11px] text-slate-500">
          Drafted from this meeting’s details. Edit anything you like — nothing is sent until you click “Send email”.
          Sending it does not send attendee invitations and does not mark the venue as confirmed.
        </p>
        <label className="block text-xs">
          <span className="text-slate-500">To</span>
          <input id="venue-email-to" type="email" value={venue.coordinator_email || ''} readOnly disabled={!venue.configured}
            className="mt-1 w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white text-xs disabled:opacity-60" />
        </label>
        <label className="block text-xs">
          <span className="text-slate-500">Subject</span>
          <input id="venue-email-subject" type="text" value={value.subject} onChange={update('subject')} disabled={!canSend}
            className="mt-1 w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white text-xs disabled:opacity-60" />
        </label>
        <label className="block text-xs">
          <span className="text-slate-500">Message</span>
          <textarea id="venue-email-body" rows={12} value={value.body} onChange={update('body')} disabled={!canSend}
            className="mt-1 w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white text-xs font-mono disabled:opacity-60" />
        </label>
        {result && (
          <div role={result.kind === 'error' ? 'alert' : 'status'}
            className={`text-xs border rounded-xl p-3 ${result.kind === 'error' ? 'text-rose-800 bg-rose-50 border-rose-200' : 'text-emerald-800 bg-emerald-50 border-emerald-200'}`}>
            {result.text}
          </div>
        )}
        <div className="flex items-center justify-between gap-3">
          <span className="text-[11px] text-slate-500">
            {!venue.configured ? (venue.configuration_error || 'Enter a valid venue coordinator email in meeting details.')
              : venue.request_status === 'SENDING' ? 'The send result is pending. Check status before retrying.'
              : venue.request_status === 'SENT' ? 'Waiting for the coordinator’s reply. Use Refresh to check for it.'
              : venue.request_status === 'CONFIRMED' ? 'The venue is confirmed.'
              : CLOSED.includes(meeting.status) ? 'This meeting can no longer send a venue request.'
              : 'The coordinator replies with CONFIRMED or REJECTED; Refresh reads the reply.'}
          </span>
          <button type="button" onClick={send} disabled={!canSend || sending || !value.to.trim() || !value.subject.trim() || !value.body.trim()}
            className="flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs disabled:opacity-50 disabled:cursor-not-allowed">
            <Send className="w-3.5 h-3.5" />
            <span>{sending ? 'Sending…' : 'Send email'}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
