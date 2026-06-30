# Reply Format Schema — the JSON your GPT should produce

This is the canonical structure for a workspace's **Reply Format**. It replaces the old single
free-form JSON with a clear, category-based layout the reply engine can follow strictly.

Train your GPT to output **exactly this shape**. When you paste it into a workspace, every section
maps 1:1 to how the system writes replies and follow-ups.

---

## The one rule that never changes (the system contract)

The system writes these **exact** custom-variable names to Bison/Instantly. Your JSON's keys must
match them:

- `main_reply` — the immediate reply (auto-sent only for a confident simple-positive match).
- `followup_1` … `followup_6` — the drip follow-ups (in order).
- `reply_intent`, `reply_confidence` — metadata (optional to create in Bison).

So the **followups array maps in order**: the 1st item → `followup_1`, the 2nd → `followup_2`, etc.
Keep at most 6. A 7th is allowed in the JSON but won't be sent (the engine caps at 6, and the
system safely ignores extras).

---

## Top-level structure

```json
{
  "format_name": "Ascendly Goal-Based Positive Reply + Follow-up Format",
  "client": "Ascendly",
  "mode": "reply",

  "response_types": [ ... ],
  "followups": [ ... ],

  "placeholders": { ... },
  "scheduling_rules": { ... },
  "content_rules": { ... },
  "global_rules": [ ... ],
  "signature_rule": "...",
  "formatting_rules": { ... }
}
```

| Field | Type | Purpose |
|---|---|---|
| `format_name` | string | A human label for this format. |
| `client` | string | Client/brand name. |
| `mode` | `"reply"` or `"followup"` | Which engine path this format feeds. |
| `response_types` | array | **The core.** Each category of incoming reply + how to answer it. |
| `followups` | array | The 6 follow-ups (FUP1–FUP6), in send order. |
| `placeholders` | object | What each `{{placeholder}}` means. |
| `scheduling_rules` | object | Time window, notice, weekday rules. |
| `content_rules` | object | `focus_on`, `may_reference`, `avoid` word/topic lists. |
| `global_rules` | array of strings | Rules that apply to every reply. |
| `signature_rule` | string | How signatures/initials are handled. |
| `formatting_rules` | object | Line breaks, no em dash, tone, etc. |

---

## `response_types` — the categories (this is what was missing)

Instead of one `main_reply`, you define **each type of reply you receive** and the exact format for
each. The engine reads the incoming message, picks the best-matching type, and writes `main_reply`
using that type's template + rules.

Each item:

| Field | Type | Purpose |
|---|---|---|
| `type` | string (snake_case) | The category id. This is also reported back as `reply_intent`. |
| `intent` | string | Plain-English description of when this category applies. |
| `examples` | array of strings | Sample replies that fall in this category (your "comma list"). |
| `auto_send` | boolean | `true` = a confident match may be auto-sent. `false` = draft only, route to a human. |
| `template` | string | The response template, with `{{placeholders}}`. |
| `rules` | array of strings | Conditions/rules specific to this type (e.g. "don't quote a price"). |

Example:

```json
"response_types": [
  {
    "type": "simple_positive",
    "intent": "A short, clearly interested reply with no hard questions.",
    "examples": ["yes", "sure", "interested", "tell me more", "send more info", "sounds good", "open to it"],
    "auto_send": true,
    "template": "Hey {{FirstName}}, thanks for the response.\n\nThe goal is to help {{CompanyName}} {{desired_outcome}}.\n\nWe do that through {{ascendly_mechanism}}, so your team can stay focused on closing the right opportunities.\n\nWould next week on {{Day}} at {{Time Option 1}}, {{Time Option 2}}, or {{Time Option 3}} work? Let me know and I'll send the invite.",
    "rules": [
      "Propose times for two days, between 11 AM and 3 PM.",
      "Never quote a price.",
      "End with one clear question."
    ]
  },
  {
    "type": "pricing_question",
    "intent": "Prospect asks about cost, price, or budget.",
    "examples": ["how much", "what's the cost", "pricing?", "what are your rates"],
    "auto_send": false,
    "template": "Hey {{FirstName}}, great question.\n\nPricing depends on {{pricing_factor}}, so the honest answer is it's best to show you on a quick call where I can tailor it to {{CompanyName}}.\n\nWould {{Day}} at {{Time Option 1}} or {{Time Option 2}} work?",
    "rules": ["Do not state a number.", "Pivot to a short call."]
  },
  {
    "type": "out_of_context_question",
    "intent": "Any unexpected or clarifying question that doesn't fit another type.",
    "examples": ["are you referring to website design?", "who is this?", "what exactly do you do?"],
    "auto_send": false,
    "template": "Hey {{FirstName}}, {{direct_answer_to_their_question}}.\n\nIn short, we {{one_line_what_we_do}}.\n\nWorth a quick call to see if it fits {{CompanyName}}? {{Day}} at {{Time Option 1}} or {{Time Option 2}}?",
    "rules": ["Answer their exact question first.", "Never leave main_reply blank."]
  }
]
```

**Recommended types to always include:** `simple_positive` (auto_send true), `pricing_question`,
`technical_question`, `objection`, `case_study_request`, `out_of_context_question`,
`not_interested`, `wrong_person`, `out_of_office`, `unsubscribe`. Only `simple_positive` should be
`auto_send: true`; everything else drafts for a human.

---

## `followups` — the FUP sequence (FUP1–FUP6)

A list, in order. Item N becomes `followup_N`.

| Field | Type | Purpose |
|---|---|---|
| `key` | string | `"followup_1"` … `"followup_6"` (must match). |
| `label` | string | Human label, e.g. `"FUP1"`. |
| `intent` | string | What this follow-up is for. |
| `max_words` | number | Length cap. |
| `template` | string | The follow-up body with placeholders. No signature inside. |

```json
"followups": [
  { "key": "followup_1", "label": "FUP1", "intent": "Scheduling check on the times proposed in the main reply.", "max_words": 55,
    "template": "Hey {{FirstName}},\n\nJust checking if the times I proposed work. If not, happy to send a few new options.\n\nWhat works best?" },
  { "key": "followup_2", "label": "FUP2", "intent": "Add one new value point + calendar link.", "max_words": 110,
    "template": "Hey {{FirstName}},\n\n{{pick_one_value_point}}.\n\nHere's my calendar: {{calendar_link}} — grab a time or share yours." }
]
```

---

## Supporting blocks

```json
"placeholders": {
  "{{FirstName}}": "Prospect first name, or 'there' if unknown.",
  "{{CompanyName}}": "Prospect company, or rewrite the line if unknown.",
  "{{desired_outcome}}": "A specific outbound outcome (more qualified meetings, predictable pipeline...).",
  "{{ascendly_mechanism}}": "Managed outbound meeting generation: targeted campaigns, reply management, qualification, booking.",
  "{{Day}}": "A working day at least 2 days out, no weekends.",
  "{{Time Option 1}}": "~11 AM", "{{Time Option 2}}": "~noon", "{{Time Option 3}}": "~1 PM",
  "calendar_link": "Always exactly: https://calendly.com/rosis/new-meeting"
},
"scheduling_rules": {
  "preferred_time_window": "11 AM to 3 PM",
  "minimum_notice_days": 2,
  "working_days_only": true,
  "avoid_weekends": true,
  "proposal_style": "Propose two days with natural time options."
},
"content_rules": {
  "focus_on": ["qualified sales conversations", "meeting flow", "pipeline growth"],
  "may_reference": ["targeted outbound", "reply management", "lead qualification"],
  "avoid": ["AI visibility", "GEO", "website visitor identification", "retargeting"]
},
"global_rules": [
  "No em dashes. No buzzwords or hype.",
  "Match the prospect's energy and length.",
  "No sign-offs or names (the system appends the signature)."
],
"signature_rule": "main_reply may end with {{Initials}} only — or omit it to avoid a double signature.",
"formatting_rules": {
  "line_breaks": "One blank line between paragraphs; day/time lists on their own lines.",
  "no_em_dash": true, "no_markdown": true, "no_bullets_in_email": true,
  "tone": "Simple, human, direct, consultative."
}
```

---

## Drop-in GPT system prompt (paste into your custom GPT)

> You generate **Reply Format JSON** for a cold-email reply system. Given a client's offer, ICP,
> and tone notes, output **only** valid JSON matching this schema: top-level keys `format_name`,
> `client`, `mode` ("reply"), `response_types`, `followups`, `placeholders`, `scheduling_rules`,
> `content_rules`, `global_rules`, `signature_rule`, `formatting_rules`.
>
> `response_types` is an array; each has `type` (snake_case), `intent`, `examples` (array),
> `auto_send` (boolean — only `simple_positive` is true), `template` (with `{{placeholders}}`),
> and `rules` (array). Always include these types: simple_positive, pricing_question,
> technical_question, objection, case_study_request, out_of_context_question, not_interested,
> wrong_person, out_of_office, unsubscribe.
>
> `followups` is an array of up to 6 items, each with `key` ("followup_1".."followup_6"), `label`,
> `intent`, `max_words`, `template`. Never put a signature inside a follow-up.
>
> Rules: no em dashes; no sign-offs or sender names in templates (the system adds the signature);
> never leave a `{{placeholder}}` undefined in `placeholders`; output JSON only, no commentary.

---

## How the system uses it (so the format is actually followed)

1. A reply comes in → the engine matches it to one `response_types` entry by `examples` + `intent`.
2. It writes `main_reply` using that type's `template` + `rules`, and reports `type` as `reply_intent`.
3. If the matched type has `auto_send: true` and the match is confident → the reply is sent
   automatically. Otherwise it's drafted and routed to a human (Needs Review).
4. `followups[0..5]` are written to `followup_1..6` and pushed into the follow-up campaign.
5. Anything in the dashboard **Rules** page is layered on top as mandatory overrides.
