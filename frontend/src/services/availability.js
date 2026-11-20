// The only three availability states shown in the UI. The backend stores AVAILABLE / BUSY / UNVERIFIED
// (backend/skills/scheduling_skill.py); anything else (including old "Email added" values) is not verified.
const LABELS = {
  AVAILABLE: 'Available',
  BUSY: 'Busy',
  UNVERIFIED: 'Availability not verified',
};

const STYLES = {
  AVAILABLE: 'text-emerald-400 bg-emerald-950/40 border-emerald-800/40',
  BUSY: 'text-red-400 bg-red-950/40 border-red-800/40',
  UNVERIFIED: 'text-amber-300 bg-amber-950/40 border-amber-800/40',
};

export function availabilityKey(status) {
  const key = String(status || '').toUpperCase();
  return key === 'AVAILABLE' || key === 'BUSY' ? key : 'UNVERIFIED';
}

export const availabilityLabel = (status) => LABELS[availabilityKey(status)];
export const availabilityStyle = (status) => STYLES[availabilityKey(status)];
