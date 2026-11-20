import React, { useState } from 'react';
import { HelpCircle, ListChecks } from 'lucide-react';
import { api } from '../services/api';
import AgendaPointsEditor from './AgendaPointsEditor';

const DURATIONS = [15, 30, 45, 60, 90, 120];
const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const formatClock = (clock) => {
  if (!clock) return null;
  const [h, m] = clock.split(':').map(Number);
  return `${h % 12 === 0 ? 12 : h % 12}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
};

/**
 * The step between "Schedule" and "Review meeting plan":
 *  - asks ONLY for missing/ambiguous details (e.g. participant emails),
 *  - shows a mandatory venue box for in-person meetings,
 *  - collects optional/improved agenda points.
 */
export default function MeetingDetailsStep({ meeting, onSubmitted, onClose }) {
  const understood = meeting.understood || {};
  const baseQuestions = (meeting.questions || []).filter((q) => q.field !== 'venue' && !q.field.startsWith('participant_email_'));

  // Ensure every identified participant without an email gets their own explicit email input field
  const rawParticipants = meeting.understood?.participants || meeting.participants || [];
  const participantQuestions = rawParticipants
    .filter((p) => {
      const name = typeof p === 'string' ? p : p.name;
      const email = typeof p === 'object' ? p.email : '';
      return name && !email;
    })
    .map((p, index) => {
      const name = typeof p === 'string' ? p : p.name;
      return {
        field: `participant_email_${index}`,
        participant_name: name,
        input: 'email',
      };
    });

  // Assign sequential numbering: "Participant 1 – Vijeta", "Participant 2 – Karen"
  let pIndex = 1;
  const questions = [...baseQuestions, ...participantQuestions].map((q) => {
    if (q.field === 'venue_coordinator_email') return q;
    if (q.input === 'email' || q.field.startsWith('participant_email_')) {
      const pName = q.participant_name || '';
      const item = {
        ...q,
        input: 'email',
        participant_name: pName,
        participant_index: pIndex,
        question: `Participant ${pIndex} – ${pName}`,
        placeholder: `Enter ${pName}'s email address`,
      };
      pIndex += 1;
      return item;
    }
    return q;
  });

  const [answers, setAnswers] = useState(() => {
    const initial = { venue_coordinator_email: understood.venue_coordinator_email || '' };
    (rawParticipants || []).forEach((p) => {
      const name = typeof p === 'string' ? p : p.name;
      const email = typeof p === 'object' ? p.email : '';
      if (name && email) {
        initial[`participant_email_${name}`] = email;
      }
    });
    return initial;
  });
  const [fieldErrors, setFieldErrors] = useState({});
  const [venue, setVenue] = useState(understood.room_name || '');
  // Agenda points the organizer already gave in the request arrive pre-filled, in their original order. An empty
  // array is truthy in JS, so it must not mask the points found in `understood`.
  const providedAgenda = [meeting.agenda_points, understood.agenda_points].find((list) => Array.isArray(list) && list.length > 0) || [];
  const [agendaPoints, setAgendaPoints] = useState(providedAgenda);
  const agendaProvided = providedAgenda.length > 0;
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const set = (field, value) => {
    setAnswers((prev) => ({ ...prev, [field]: value }));
  };
  const effectiveMode = answers.mode || understood.mode;
  const isOffline = effectiveMode === 'OFFLINE';
  const agendaOnly = questions.length === 0 && !isOffline;

  const summary = [
    ['Title', understood.title],
    ['Purpose', understood.purpose],
    ['Date', understood.date],
    ['Start', formatClock(understood.start_clock)],
    ['Duration', understood.duration_minutes ? `${understood.duration_minutes} min` : null],
    ['Mode', understood.mode === 'OFFLINE' ? 'In person' : understood.mode === 'ONLINE' ? 'Online' : null],
    ['Attendees', rawParticipants.map((p) => (typeof p === 'string' ? p : p.name || p.email)).join(', ') || null],
  ].filter(([, value]) => value);

  const unanswered = questions.some((q) => {
    const v = answers[q.field];
    return v === undefined || v === null || String(v).trim() === '';
  });
  const venueMissing = isOffline && !venue.trim();

  const submit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError('');

    // Validate each participant email independently
    const validationErrors = {};
    questions.forEach((q) => {
      if (q.input === 'email') {
        const val = String(answers[q.field] || '').trim();
        if (!val || !EMAIL_REGEX.test(val)) {
          validationErrors[q.field] = `Please enter a valid email address for ${q.participant_name || 'this participant'}.`;
        }
      }
    });

    if (Object.keys(validationErrors).length > 0) {
      setFieldErrors(validationErrors);
      setError('Please correct the highlighted email address fields.');
      setSubmitting(false);
      return;
    }

    try {
      const payload = { agenda_points: agendaPoints.map((p) => p.trim()).filter(Boolean) };
      const participant_emails = {};

      Object.entries(answers).forEach(([field, value]) => {
        if (value === '' || value === undefined) return;
        if (field.startsWith('participant_email_')) {
          const participant = participantQuestions[Number(field.replace('participant_email_', ''))];
          if (participant) participant_emails[participant.participant_name] = String(value).trim();
        } else if (field === 'duration') {
          payload.duration_minutes = Number(value);
        } else if (field === 'participants') {
          payload.attendees = String(value).split(',').map((s) => s.trim()).filter(Boolean);
        } else {
          payload[field] = value;
        }
      });

      if (Object.keys(participant_emails).length > 0) {
        payload.participant_emails = participant_emails;
      }
      if (isOffline) payload.venue = venue.trim();
      const plan = await api.clarifyMeeting(meeting.meeting_id || meeting.id, payload);
      onSubmitted(plan);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass = 'w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-white text-sm focus:border-indigo-500 focus:outline-none';
  const renderInput = (q) => {
    const id = `clarify-${q.field}`;
    switch (q.input) {
      case 'date':
        return <input id={id} type="date" className={inputClass} value={answers.date || ''} onChange={(e) => set('date', e.target.value)} required />;
      case 'time':
        return <input id={id} type="time" className={inputClass} value={answers.start_time || ''} onChange={(e) => set('start_time', e.target.value)} required />;
      case 'duration':
        return (
          <select id={id} className={inputClass} value={answers.duration || ''} onChange={(e) => set('duration', e.target.value)} required>
            <option value="">Select duration</option>
            {DURATIONS.map((d) => <option key={d} value={d}>{d < 60 ? `${d} minutes` : `${d / 60} hour${d > 60 ? 's' : ''}`}</option>)}
          </select>
        );
      case 'mode':
        return (
          <select id={id} className={inputClass} value={answers.mode || ''} onChange={(e) => set('mode', e.target.value)} required>
            <option value="">Select</option>
            <option value="ONLINE">Online (Google Meet)</option>
            <option value="OFFLINE">In person (offline)</option>
          </select>
        );
      case 'email': {
        const emailErr = fieldErrors[q.field];
        return (
          <div className="space-y-1">
            <input
              id={id}
              type="email"
              placeholder={q.placeholder || `Enter ${q.participant_name || 'attendee'}'s email address`}
              className={`${inputClass} ${emailErr ? 'border-rose-500 focus:border-rose-500 ring-1 ring-rose-500/50' : ''}`}
              value={answers[q.field] || ''}
              onChange={(e) => {
                const val = e.target.value;
                set(q.field, val);
                if (val && !EMAIL_REGEX.test(val.trim())) {
                  setFieldErrors((prev) => ({
                    ...prev,
                    [q.field]: `Please enter a valid email address for ${q.participant_name || 'this participant'}.`,
                  }));
                } else {
                  setFieldErrors((prev) => {
                    const copy = { ...prev };
                    delete copy[q.field];
                    return copy;
                  });
                }
              }}
              required
            />
            {emailErr && (
              <p role="alert" className="text-[11px] text-rose-400 font-medium">{emailErr}</p>
            )}
          </div>
        );
      }
      case 'attendees':
        return <input id={id} type="text" className={inputClass} placeholder="e.g. Vijeta, Karen" value={answers.participants || ''} onChange={(e) => set('participants', e.target.value)} required />;
      default:
        return <input id={id} type="text" className={inputClass} value={answers[q.field] || ''} onChange={(e) => set(q.field, e.target.value)} required />;
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
      <form onSubmit={submit} className={`relative w-full ${agendaOnly ? 'max-w-md' : 'max-w-lg'} glass-panel-glow bg-slate-900 border border-slate-700 rounded-2xl p-5 shadow-2xl space-y-4 max-h-[90vh] overflow-y-auto`}>
        <div className="flex items-start justify-between border-b border-slate-800 pb-3">
          <div className="flex items-center space-x-3">
            {agendaOnly ? <ListChecks className="w-5 h-5 text-indigo-400" /> : <HelpCircle className="w-6 h-6 text-indigo-400" />}
            <div>
              <h3 className="font-bold text-white text-base">{agendaOnly ? 'Agenda' : 'A few details are needed'}</h3>
              <p className="text-xs text-slate-400">
                {agendaOnly
                  ? (agendaProvided
                    ? 'We took these agenda points from your request. Edit, add or remove points, or continue as they are.'
                    : 'Add the topics you want to discuss, or continue without an agenda.')
                  : 'Answer the questions below to continue with the meeting.'}
              </p>
            </div>
          </div>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-white" aria-label="Close">✕</button>
        </div>

        {!agendaOnly && summary.length > 0 && (
          <div className="text-xs bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-1">
            <div className="font-semibold text-slate-300">Already understood</div>
            {summary.map(([label, value]) => (
              <div key={label}><span className="text-slate-500">{label}:</span> <span className="text-slate-200">{value}</span></div>
            ))}
          </div>
        )}

        {questions.length > 0 && (
          <div className="space-y-3">
            {questions.map((q) => (
              <div key={q.field} className="space-y-1">
                <label htmlFor={`clarify-${q.field}`} className="text-xs font-semibold text-slate-300 block">{q.question}</label>
                {q.note && <p className="text-[11px] text-amber-500">{q.note}</p>}
                {renderInput(q)}
              </div>
            ))}
            {meeting.meeting_timezone && questions.some((q) => ['date', 'time'].includes(q.input)) && (
              <p className="text-[11px] text-slate-500">Dates and times are in {meeting.meeting_timezone}.</p>
            )}
          </div>
        )}

        {isOffline && (
          <div className="space-y-1">
            <label htmlFor="meeting-venue" className="text-xs font-semibold text-slate-300 block">Venue (required for in-person meetings)</label>
            <input
              id="meeting-venue"
              type="text"
              required
              value={venue}
              onChange={(e) => setVenue(e.target.value)}
              placeholder="e.g. Conference Room B, 2nd floor"
              className={inputClass}
            />
          </div>
        )}

        <AgendaPointsEditor points={agendaPoints} onChange={setAgendaPoints}
          label={agendaProvided ? 'Agenda (from your request)' : 'Agenda (optional)'} />

        {error && <p role="alert" className="text-sm text-rose-500">{error}</p>}

        <div className="flex justify-end gap-2 pt-2 border-t border-slate-800">
          <button type="button" onClick={onClose} className="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-sm">Later</button>
          <button
            type="submit"
            disabled={submitting || unanswered || venueMissing}
            className="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-semibold text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? 'Checking…' : 'Continue to review'}
          </button>
        </div>
      </form>
    </div>
  );
}
