# ServiceNow side of the setup

One thing is required: the agent's own restricted account. The webhook
(section 3) is optional and off by default.

## 1. The agent's account

The agent must never run as admin. Run this once, with an admin login filled in
as `SN_SEED_USERNAME` / `SN_SEED_PASSWORD` in `.env`:

```bash
python scripts/create_agent_user.py
```

It creates `ai.resolution.agent` and writes its credentials into `.env`. Blank
the admin password again afterwards; the agent never uses it.

What the account can do, and why:

- **Role `itil`** — the standard service-desk role. Lets it read incidents,
  problems, changes and their notes, and use full-text search. It cannot delete
  records or touch configuration, users or security.
- **API only** — it cannot log in to the web interface.
- **Guard rule "AI agent: work notes only"** — a Business Rule on `task` (so it
  covers incidents, problems and changes) that blocks this account from changing
  anything except adding work notes: no field edits, no new records, no deletes.
  `itil` alone would allow status changes; this rule is what narrows it to notes,
  and because ServiceNow enforces it, a stolen password can't get round it.
- **Timezone GMT** — note timestamps come back in the account's own timezone.

The script then logs in as the new account and checks each of these, including
that the notes it writes can actually be read back. ServiceNow hides rows an
account may not see instead of refusing, so a success status alone proves
nothing.

On a production instance your ServiceNow admins may prefer a custom role with
ACLs instead of `itil` plus the guard rule. Either works, as long as the account
can read `work_notes` through the ticket, which is how the agent reads notes.

## 2. Search access

Retrieval relies on ServiceNow's own full-text search (`123TEXTQUERY321`), which
needs the text index on `incident` and `problem`. It is there by default. If a
search returns nothing for words you can see in a ticket, check System
Definition > Tables > Text Indexes.

Nothing is copied out of the instance: the agent queries it per run and keeps no
database of its own.

## 3. Optional: the webhook

By default the agent only makes outbound calls — the sweep asks ServiceNow for
new tickets every minute — so nothing on the network can connect to it. That is
the recommended setup.

The webhook lets ServiceNow notify the agent the instant a ticket is created,
saving up to a minute. It means opening a port, so only enable it when the agent
is hosted behind HTTPS with an allowlist of your instance's IP addresses:

1. Set `WEBHOOK_ENABLED=true` and a long random `WEBHOOK_SECRET` in `.env`.
2. In ServiceNow, store the same secret in a System Property
   `x_ai_agent.webhook_secret`, and the agent's URL in `x_ai_agent.endpoint`.
3. System Definition > Business Rules > New: table Incident, `after` Insert and
   Update, condition `current.assignment_group.changes() || current.sys_created_on == current.sys_updated_on`,
   script from `business_rule.js` in this folder.

Every request is signed with the secret, and anything unsigned or mis-signed is
rejected.
