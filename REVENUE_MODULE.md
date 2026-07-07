# Revenue Module (Blueprints & Agreements)

The Reply Manager is the cockpit; the `ascendly-client-portals` service
(blueprint.ascendly.one / agreement.ascendly.one) is the document engine.
`revenue.py` connects them over the portals Service API. Codebases stay separate.

## Env vars (add on Railway for THIS app)
```
PORTALS_API_URL=https://ascendly-client-portals-production.up.railway.app
PORTALS_API_KEY=<same value as SERVICE_API_KEY on the portals service>
OPENAI_API_KEY=<for the AI assistant>   # optional; OPENAI_MODEL defaults to gpt-4o-mini
```
On the PORTALS service add: `SERVICE_API_KEY=<generate a long random string>`

## What you get
- Sidebar → **Revenue**: Clients / Blueprints / Agreements / AI Assistant
- `/revenue` — all clients with status (Draft → Sent/Live → Client signed → Executed)
- `/revenue/client/{slug}` — workspace: blueprint card (preview, copy link,
  publish/unpublish, regenerate AI), agreement card (open, countersign link,
  download executed PDF, resend email), overview, intake, audit timeline
- `/revenue/new` — create portal (form or paste-JSON); edit preserves signatures
- `/revenue/assistant` — chat: "Build a blueprint for Northstar" (paste transcript),
  "Change pricing to $4,500", "Publish acme-co", "Delete lima-llc" (asks to confirm).
  The AI calls the portals Service API through tools.

## Flow
Reply → Meeting → create client here → review draft (preview link) → Publish →
send blueprint link → client clicks "Let's Get Started" → agreement → signs →
you get countersign email → countersign → executed PDF emailed → mark Active.

## Notes
- New portals start **published**. Unpublish to work in draft; client links 404
  while draft, but Preview links (with internal key) always work.
- Blueprint "publish" gates BOTH public pages (blueprint + agreement).
