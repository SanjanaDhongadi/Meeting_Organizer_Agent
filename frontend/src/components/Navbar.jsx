import React from 'react';
import { Users, Calendar } from 'lucide-react';

const TABS = [
  { id: 'meetings', label: 'Meeting Organizer', hint: 'Schedule · Review · Confirm', icon: Calendar, color: '#176b63' },
  { id: 'employees', label: 'Employees', hint: 'Register · Manage · Connect Google', icon: Users, color: '#1e5799' },
];

export default function Navbar({ activeTab, onChange }) {
  return (
    <header style={{
      position: 'sticky', top: 0, zIndex: 40, width: '100%',
      background: '#ffffff', borderBottom: '1px solid #e5e7eb',
      boxShadow: '0 1px 3px rgba(0,0,0,0.05)'
    }}>
      <div style={{
        maxWidth: 1160, margin: '0 auto', padding: '10px 24px', minHeight: 66,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap'
      }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 15, color: '#111827', lineHeight: 1.2 }}>Meeting Organizer Agent</div>
          <div style={{ fontSize: 11, color: '#6b7280' }}>
            {TABS.find((t) => t.id === activeTab)?.hint}
          </div>
        </div>

        <nav aria-label="Main" style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {TABS.map(({ id, label, icon: Icon, color }) => {
            const active = id === activeTab;
            return (
              <button
                key={id}
                type="button"
                onClick={() => onChange(id)}
                aria-current={active ? 'page' : undefined}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, padding: '8px 14px', borderRadius: 8, cursor: 'pointer',
                  fontSize: 13, fontWeight: 600,
                  background: active ? color : '#f9fafb',
                  color: active ? '#ffffff' : '#374151',
                  border: `1px solid ${active ? color : '#e5e7eb'}`,
                }}
              >
                <Icon size={15} />
                {label}
              </button>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
