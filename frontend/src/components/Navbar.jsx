import React from 'react';
import { Users, Calendar } from 'lucide-react';

export default function Navbar({ activeTab }) {
  const isEmployee = activeTab === 'employees';

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

        {/* Brand — changes per dashboard */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{
            width: 34, height: 34, borderRadius: 8,
            background: isEmployee ? '#1e5799' : '#176b63',
            display: 'flex', alignItems: 'center', justifyContent: 'center'
          }}>
            {isEmployee ? <Users size={17} color="#fff" /> : <Calendar size={17} color="#fff" />}
          </div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 15, color: '#111827', lineHeight: 1.2 }}>
              {isEmployee ? 'Employee Dashboard' : 'Meeting Organizer'}
            </div>
            <div style={{ fontSize: 11, color: '#6b7280' }}>
              {isEmployee ? 'Register · Manage · Connect Calendar' : 'Schedule · Review · Confirm'}
            </div>
          </div>
        </div>

        {/* Right side: port indicator only — no cross-dashboard navigation */}
        <div style={{
          fontSize: 11, color: '#9ca3af', fontFamily: 'monospace',
          background: '#f9fafb', border: '1px solid #e5e7eb',
          borderRadius: 6, padding: '4px 10px'
        }}>
          {isEmployee ? 'localhost:5173' : 'localhost:5174'}
        </div>

      </div>
    </header>
  );
}
