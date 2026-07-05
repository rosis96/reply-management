# CRM Section — Structure & Build Plan (for approval, not yet built)

_A separate "mini-HubSpot" section inside Reply Manager. It does NOT touch the existing reply
system — new tables, new routes, new nav mode. The reply pipeline keeps running exactly as-is._

---

## 1. Principles

- **Zero risk to what works.** The reply engine, webhooks, dashboard, Calendly, and DB tables stay
  untouched. The CRM adds its own tables and its own `/crm` routes.
- **Modern & easy** (ClickUp / HubSpot / Instantly feel): a drag-and-drop board, column totals,
  fast card editing, tags with colors, search and filters.
- **Fed automatically.** When a lead is marked **Meeting Booked** in the reply system, a CRM
  opportunity is created automatically. Manual "Add opportunity" is also available.
- **Customizable.** Stages and tags are editable — rename, reorder, set colors, add/remove.

---

## 2. Navigation & layout

- A **CRM** button sits below Support in the sidebar.
- Clicking it enters **CRM mode**: the Replies nav collapses and a CRM nav appears
  (Pipeline, and later Reports / Settings). A "← Back to Replies" returns to the reply dashboard.
- **Pipeline view** = the Kanban board (default). Columns are stages; cards are opportunities.
- **List view** = the same data as a sortable table (toggle).
- **Card detail panel** = click any card to open a side panel with every field, tags, and notes.

---

## 3. Pipeline stages (customizable)

Seeded defaults (you can rename, recolor, reorder, add, or remove any of them):

| Order | Stage | Color | Notes |
|---|---|---|---|
| 1 | Opportunity | slate | Interested lead, not yet booked |
| 2 | Meeting Booked | blue | **Auto-created here** from the reply system |
| 3 | Meeting Completed | violet | Call happened |
| 4 | No Show | amber | Prospect didn't show |
| 5 | Follow-up | orange | Re-engaging / nurturing |
| 6 | Won | green | Closed-won (counts toward revenue) |
| 7 | Lost | red | Closed-lost |

Each stage has: name, color, order, and optional flags `is_won` / `is_lost` (used for reporting).
Dragging a card to a new column updates its stage instantly.

---

## 4. What's on an opportunity (fields)

- **Contact**: name, email, company (pulled from the lead automatically).
- **Workspace / client** (which client this belongs to — Ascendly, Webaholics, etc.).
- **Stage** (the column).
- **Lead intent** (e.g. simple_positive, pricing_question — carried from the reply system).
- **Deal value** ($) — column headers show totals per stage (e.g. "Meeting Booked — $15,000").
- **Status** (a short free label, separate from stage — e.g. "awaiting reply", "proposal sent").
- **Tags** (multiple, each with a color; fully customizable set).
- **Description / notes** (free text).
- **Owner** (who's handling it).
- **Next-action date** (for follow-up reminders later).
- **Linked lead** (one-click back to the full reply thread in the reply dashboard).
- **Timestamps** (created, last updated, stage-changed).

---

## 5. Data model (new tables only)

- **`crm_stages`** — `id, name, color, sort_order, is_won, is_lost`. Seeded with the defaults above.
- **`crm_tags`** — `id, name, color`.
- **`opportunities`** — `id, workspace_name, lead_id (link to existing leads, nullable),
  contact_name, email, company, stage_id, lead_intent, status, value (number), owner,
  description, next_action_date, tag_ids (JSON list), created_at, updated_at, stage_changed_at`.
- **(Phase 2) `opportunity_activity`** — a simple timeline log (stage changes, notes) per card.

No existing table is modified. `opportunities.lead_id` just points at the `leads` you already store.

---

## 6. How cards are created (auto)

- In the reply dashboard, when a lead's stage becomes **Meeting Booked** (the existing
  "Mark booked" action / stage set), the system **also** creates one `opportunities` row in the
  CRM "Meeting Booked" stage, copying name, email, company, workspace, and lead intent, and linking
  `lead_id`.
- **De-dupe:** one opportunity per lead — if it already exists, it's updated, not duplicated.
- Works for both Bison and Instantly leads (they both already produce lead rows).
- A manual **+ Add opportunity** button covers anything that didn't come through a booked reply.

This is a small, additive hook on the existing "booked" action — it does not change reply logic.

---

## 7. Board behavior (the "easy to use" part)

- **Drag & drop** cards between columns → updates stage (and `stage_changed_at`).
- **Column headers** show count + summed deal value (like Instantly's "$140,000 · 28 deals").
- **Filter** by client/workspace, owner, and tag; **search** by name/company/email.
- **Quick add** card at the top of any column.
- One combined board with a client filter (matches the Instantly feel). If you later want fully
  separate boards per client, we can add a "pipeline" concept — but v1 = one board, filterable.

---

## 8. Build phases (so we ship safely, review each step)

1. **Data + read-only board.** Create the tables, seed default stages, render the Kanban board and
   list view from `opportunities`. Manual add. (No reply-system changes yet.)
2. **Auto-create on Meeting Booked.** Add the additive hook so booked leads flow in. De-dupe.
3. **Card detail + editing.** Full field editing, tags (with colors), status, value, notes, owner.
4. **Customize stages & tags.** UI to rename/recolor/reorder/add/remove stages and tags.
5. **Reports (optional).** Totals by stage, win rate, revenue, per client.

Each phase is independently shippable and reversible.

---

## 9. Still-open questions (small)

1. **Owners/users:** is it just you (single admin) for now, or multiple named owners to assign?
2. **Deal value default:** should new booked cards start at a default value (e.g. $5,000 like your
   Instantly setup), or blank until you set it?
3. **Stages: global or per-client?** Recommended v1 = one shared set of stages for all clients
   (simplest). Per-client pipelines can come later. OK to start shared?

---

## 11. Module 2 — Prospect Proposal Pages (structure only, build after CRM)

A post-meeting **personalized landing page** per prospect (not a document), e.g.
`proposal.ascendly.one/acme-co`. Feeds off the CRM.

**Flow**
1. When an opportunity is tagged **Meeting Completed** in the CRM, a **"Build proposal"** button
   appears on the card.
2. Clicking it opens a **premium deal form** (like the ClickUp "Deal Form"), **pre-filled** from the
   opportunity (contact, email, company, value, website) so you only add the few new fields
   (deal name, est. close date, description, etc.).
3. On submit, the system takes a **proposal template** (per client) full of `{{placeholders}}`,
   fills them with the form values, and **AI writes only the sections you mark** (e.g. exec summary,
   "why now", scope) — the rest stays exactly as entered.
4. It publishes a personalized page at a **custom link** `proposal.ascendly.one/{company-slug}`.
5. You **review** the page, tweak if needed, and **Approve**. For now, **sending is manual** — you
   copy the link and send it (later we can auto-send via the reply system or a Gmail account).

**Fields (from the form)**
Your name, Deal name, Deal amount, Est. close date, Description, Contact email / first / last,
Original source, Company, Deal stage, Company website URL — plus which template to use.

**AI scope:** AI fills ONLY placeholders you tag as `ai:` (e.g. `{{ai:exec_summary}}`); everything
else is literal form values. Predictable and on-brand.

**Data model (new tables)**
- `proposal_templates` — `id, client/workspace, name, html (with {{placeholders}}), ai_fields (list)`.
- `proposals` — `id, opportunity_id, template_id, slug (company-based, unique), field_values (JSON),
  generated_html, status (draft/ready/sent), public_url, created_at, updated_at`.

**Hosting — the one open decision (deferred):**
- **Serve from the existing app (Recommended):** add PUBLIC (no-login) routes that render a stored
  proposal by slug; point `proposal.ascendly.one` at the app. Custom links work instantly, fully
  dynamic, easy to edit/track, no external round-trip. Proposal pages are public; the dashboard
  stays private.
- **GitHub Pages (static):** the app pushes generated HTML to a GitHub repo via the GitHub API and
  GitHub Pages serves the subdomain. Matches "hosted in GitHub," but every edit is a commit/push
  with propagation delay and more moving parts.

_My recommendation: serve from the app (simplest, dynamic). We can still export to GitHub later if
you want static hosting._

**Build phases (after the CRM):**
1. Template storage + the deal form (pre-filled from the opportunity).
2. Merge engine: fill placeholders + AI-marked sections → generate the page HTML.
3. Public hosting at `proposal.ascendly.one/{slug}` + review/approve.
4. (Later) auto-send via reply system or a connected Gmail.

---

## 10. What this explicitly does NOT change

- The reply/follow-up engine, prompts, response types, Rules, Calendly, Test thread.
- The webhooks and Bison/Instantly sending.
- The existing `leads`, `workspaces`, `proposed_slots`, settings tables.
- The current dashboard pages (only an additive CRM nav button + the booked-hook).
