import React from 'react';
import { Plus, X } from 'lucide-react';

/**
 * Optional, expandable agenda: starts with a single "+" button; each "+" adds the next point.
 * Points can be removed individually; order is preserved. Empty points are dropped by the caller on submit.
 */
export default function AgendaPointsEditor({ points, onChange, label = 'Agenda (optional)' }) {
  const update = (index, value) => onChange(points.map((p, i) => (i === index ? value : p)));
  const remove = (index) => onChange(points.filter((_, i) => i !== index));
  const add = () => onChange([...points, '']);

  return (
    <div className="space-y-1.5">
      <div className="text-xs font-semibold text-slate-300">{label}</div>
      {points.map((point, index) => (
        <div key={index} className="flex items-center gap-2">
          <label htmlFor={`agenda-point-${index}`} className="text-[11px] text-slate-500 w-14 shrink-0">Point {index + 1}</label>
          <input
            id={`agenda-point-${index}`}
            type="text"
            value={point}
            autoFocus={index === points.length - 1 && point === ''}
            onChange={(e) => update(index, e.target.value)}
            placeholder="Topic to discuss"
            className="flex-1 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1.5 text-white text-sm"
          />
          <button type="button" onClick={() => remove(index)} aria-label={`Remove agenda point ${index + 1}`}
            className="p-1 rounded-md text-slate-400 hover:text-rose-400">
            <X className="w-4 h-4" />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={add}
        aria-label={points.length ? 'Add another agenda point' : 'Add an agenda point'}
        className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-slate-700 text-slate-300 hover:bg-slate-700 text-xs"
        style={{ fontSize: 12 }}
      >
        <Plus className="w-3.5 h-3.5" />
        {points.length === 0 && <span>Add agenda point</span>}
      </button>
    </div>
  );
}
