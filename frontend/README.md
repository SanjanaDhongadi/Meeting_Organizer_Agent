# Frontend: MEETING ORGANIZER AGENT

One React + Vite application with two views that share the same backend and database:

- **Meeting Organizer** — natural-language request, draft review (approve / edit / reject), waiting states,
  "Check for responses" (real Google Calendar / Gmail replies) and a clearly labelled simulated-response panel for testing.
- **Employees** — registration (persisted in the backend), profile list, and per-employee Google connection
  (OAuth with PKCE; the status shown is read back from the backend).

When the backend has no data the views show empty states; the frontend never stores employees or meetings itself.

## Development

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173 (proxies /api to http://127.0.0.1:8000)
npm run lint
npm run build
```

Use `#meetings` / `#employees` in the URL to open a view directly. Google OAuth returns to the Employees view.
