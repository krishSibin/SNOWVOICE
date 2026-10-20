# ServiceNow Ticket Resolution Agent

Researches a ServiceNow ticket the way an engineer would — reads its history,
finds past tickets about the same problem, checks what actually fixed them — and
writes a suggested resolution back onto the ticket as a work note.

Built to beat ServiceNow's built-in assistant on trust and cost:

- **Every claim is cited.** Each step points at the exact ticket and field it
  came from, and the code verifies those citations exist before the note goes out.
- **An explicit confidence score**, built from six measurable signals rather than
  the model's opinion of itself, with the breakdown printed in the note.
- **A self-correcting loop.** A low score sends it back to research with
  rewritten queries and a stronger model, not to a guess.
- **It says when it does not know.** No documented fix means no invented steps.
- **Cheap by default.** Small models do most of the work; the expensive one only
  appears when the loop escalates.

## What it tells you

Four outcomes, all handled explicitly:

| Situation | What the note says |
| --- | --- |
| Past tickets show how this was fixed | Root cause and numbered steps, each cited, with a confidence score |
| Same issue happened before but nobody wrote down the fix | Lists those tickets with who resolved them, so you can go ask |
| Another ticket is open for the same thing right now | Flags the duplicate, who has it, what they have already tried; warns about a possible major incident |
| Nothing similar exists | Says so, and suggests what to check instead |

Run `python scripts/demo_note.py` to see a sample note without any credentials.

## How it works

```
sweep, every minute: "which open tickets have no current agent note?"
        |
        v
priority queue (P1 first) --> 3 workers --> live search in ServiceNow
                                                   |
ServiceNow <-- work note (answer, confidence, citations)
```

Every connection is outbound — to ServiceNow and to the TCS GenAI Lab gateway — so there is no open
port for anyone to attack. An optional webhook can cut the pickup time from up
to a minute to seconds; see [servicenow/SETUP.md](servicenow/SETUP.md).

**No copy of ticket data is kept anywhere.** There is no database and no index:
every run searches ServiceNow directly and holds the results only for the length
of the run. The work note is the record — it carries the citations, the
confidence and a fingerprint of the ticket text it answered, which is how a
re-run knows it has already been here.

The agent loop: extract the facts from the ticket -> plan several search queries
-> search ServiceNow for past tickets and for open ones -> grade and bucket the
evidence -> draft an answer -> verify every citation -> score confidence ->
send, or research again.

Retrieval combines two passes, because neither is enough alone:

- **Full-text search** (`123TEXTQUERY321`, ServiceNow's own search engine) for
  each query the planner writes. Excellent on error codes, blind when the
  wording differs completely.
- **Scoped sweeps** by configuration item, business service and category, which
  catch the same issue described in entirely different words.

Results from every route are merged by rank, so a ticket found several ways
rises to the top.

Evidence is sorted into three buckets, and the rules are enforced in code, not
left to the model:

- **A** — resolved, with a real write-up. Only these can support a fix step.
- **B** — resolved, but nothing usable recorded. Reported, never cited for a fix.
- **C** — open right now. Used for duplicate warnings and "already tried this".

## Setup

Requires Python 3.11+, a TCS GenAI Lab MaaS key (issued during the AI Fridays
event — see the Participant Handbook, section 13), and a ServiceNow
integration user. No database, no other infrastructure. Every LLM call goes
through the TCS GenAI Lab gateway at `https://genailab.tcs.in`; no other model
provider or external API is used, per the handbook's rules.

```bash
cp .env.example .env          # fill in ServiceNow + TCS_GENAI_API_KEY
pip install -e ".[dev]"
```

Then create the agent's own restricted ServiceNow account:

```bash
python scripts/create_agent_user.py   # needs an admin login in .env, once
```

It can read tickets and add work notes, and ServiceNow itself blocks it from
anything else; the script proves each rule before you rely on it. Details in
[servicenow/SETUP.md](servicenow/SETUP.md).

## Demo data

No instance yet? Create a free
[Personal Developer Instance](https://developer.servicenow.com/dev.do), put its
URL and admin credentials in `.env`, then:

```bash
python scripts/seed_demo_data.py --dry-run   # show what it would create
python scripts/seed_demo_data.py             # create ~26 demo records
python scripts/seed_demo_data.py --cleanup --yes   # remove them again
```

It creates ticket histories built to exercise each behaviour — a documented fix,
an issue nobody wrote up, a live outage with duplicates, something entirely new,
and a near-miss that should *not* produce confident steps — then prints the
ticket numbers to run the agent against. Everything it creates is tagged
`correlation_id = sn-agent-demo`, and cleanup only touches that tag.

## Use

```bash
sn-agent resolve INC0012345 --dry-run  # research one ticket, print the note, write nothing
sn-agent resolve INC0012345            # same, but post the work note
sn-agent resolve INC0012345 --force    # answer again even if a note already exists
sn-agent sweep --list                  # show the queue: open tickets still needing a note
sn-agent sweep                         # work through that queue once, most urgent first
sn-agent serve                         # run continuously: 3 workers, sweep every minute
```

Every command prints each step to the terminal as it happens, tagged with the
ticket it belongs to, with warnings in yellow and errors in red. Add `-v` to any
command for full detail: every ServiceNow query, every search's hit count, and
every TCS GenAI Lab call with its model, timing, tokens and cost.

There is nothing to sync or schedule. A run takes 20 seconds to a minute and a
half, most of it waiting on the model. The sweep only looks at
tickets opened in the last 3 days, so a large backlog can't run up a bill, and a
ticket that fails three times is set aside rather than retried forever.

## Pre-ticket assistant (chat and voice)

Before anyone raises a ticket, an employee can talk or type to an assistant. It
asks what is wrong, searches past resolved tickets for how the same problem was
fixed, and walks the employee through the steps that a person can do on their
own. If nothing helps, or the employee asks for a ticket, it raises an incident
with a priority and an assignment group it chose itself, and the agent above
then picks that ticket up exactly as it does any other.

- Fix steps come only from resolved tickets with a written-up fix (bucket A),
  are checked against those tickets before they are spoken, and anything risky
  (admin rights, registry, firewall, restarting servers) is left to a technician.
- The assistant's own work notes are never treated as evidence of a fix.
- Priority is set the ServiceNow way, from impact (how many people) and urgency
  (can they work), with security issues and major incidents raised to P1 or P2.
- The group comes from the teams that fixed the similar tickets, then from which
  team usually takes that category, then from the model picking from the real
  group list, and finally `INTAKE_DEFAULT_GROUP`.
- Incidents are created through a separate `ai.intake.agent` account that can
  only create incidents and add work notes. Until `INTAKE_CREATE_TICKETS=true`
  the assistant drafts the incident and shows what it would have sent instead.

Voice runs through LiveKit with local, free speech models: faster-whisper for
listening and Kokoro for speaking. Nothing leaves your network except the chat
model call to TCS GenAI Lab. Replies are streamed, the assistant starts thinking
before you finish talking, says "let me check how this was fixed before" while it
searches, and stops talking as soon as you talk over it, type, or press Stop
(or Esc) on the page.

### Setting it up

```bash
pip install -e ".[voice]"                        # LiveKit agents, whisper, kokoro
python scripts/download_voice_models.py          # kokoro + whisper into models/
sn-agent voice download-files                    # voice-activity detector weights
python scripts/create_agent_user.py --intake     # the incident-only account (admin login)
```

Download the LiveKit server binary from
https://github.com/livekit/livekit/releases and run it locally in dev mode; the
defaults (`LIVEKIT_URL=ws://127.0.0.1:7880`, `devkey`/`secret`) match it. Start
it with `--node-ip` set to the machine's own IPv4 address (from `ipconfig`):
the browser sends its audio there, and Chrome will not send it to 127.0.0.1.
Set `APP_SECRET_KEY` to a long random string.

Then add the people who can sign in. Employees only ever see the chatbot; `ops`
users also get the ops console.

```bash
sn-agent users add ana@company.com --name "Ana" --role user
sn-agent users add lead@company.com --name "Ops lead" --role ops
sn-agent users list
```

### Running it

Three processes, each in its own terminal:

```bash
livekit-server --dev --node-ip <this PC's IPv4>   # the voice server
sn-agent voice dev              # the voice agent (use "start" in production)
sn-agent web                    # the web app on http://127.0.0.1:8080
```

`sn-agent intake` runs the same assistant in the terminal, with no browser or
LiveKit, which is the quickest way to try it. If the LiveKit server is down the
web chatbot still works by typing.

### The ops console

Sign in as an `ops` user and open `/ops`:

- **Dashboard**: chats solved without a ticket, tickets the chatbot raised by
  priority and team, tickets the agent answered per day, its outcomes and
  confidence, chatbot reply time and agent run time.
- **Tickets answered**: every ticket the agent wrote a note on, with its
  confidence breakdown, the steps it took with timestamps, the evidence it cited
  and the note itself.
- **Chatbot conversations**: every chat and call, what the assistant searched,
  what it decided and the transcript.

The agent records its runs for the console as it posts notes (turn off with
`RECORD_AGENT_RUNS=false`); what it does on a ticket is unchanged. Everything is
kept in a local SQLite file, `data/sn_agent.db`.

## Measuring it

The point of the evaluation is to make "better than the built-in assistant" a
number rather than a claim:

```bash
python eval/run_eval.py build --opened-after 2026-01-01 --limit 100
python eval/run_eval.py run
```

Each case is a past ticket with its resolution hidden and retrieval cut off at
the moment it was opened, so nothing written later can leak in. Reported:
resolution-match rate, citation precision, faithfulness, calibration error (is
80% confidence right 80% of the time?), abstention rate, cost per ticket and p95
latency. Run the same tickets through Now Assist to compare like for like.

## Safety

- The ticket agent's only write is a work note. No state changes, no reassignment,
  no auto-close. The pre-ticket assistant can also create incidents, through its
  own account, and only once `INTAKE_CREATE_TICKETS=true`.
- Ticket text is treated as data, never as instructions — prompt injection through
  a ticket description cannot redirect the agent.
- Emails, phone numbers and secrets are redacted before any ticket text reaches
  the model.
- The agent keeps no copy of the tickets it reads. The ops console stores each
  run's result, note and step log, and chatbot transcripts, in a local SQLite file.
- Every run has a token and cost budget, and stops cleanly when it is spent.
- Each note carries a run ID, and the full trace (queries, evidence, grades,
  scores) is emitted as tracing spans and structured logs.

## Later

The design leaves room for these without restructuring:

- More knowledge sources (KB articles, Confluence, vendor docs, web) through the
  `KnowledgeSource` interface.
- Email to the assignment group and Microsoft Teams through the `Notifier`
  interface.
- Linking duplicates automatically, and pushing a resolution to the open tickets
  that were waiting on it.
- Feedback capture and confidence calibration, if a custom ServiceNow table is
  ever allowed to hold run history.
- Guarded auto-resolve, once that calibration says the confidence score deserves
  the trust.

## Layout

```
src/sn_agent/
  servicenow/   REST client: search, reads, and one write (work_notes)
  sources/      KnowledgeSource implementations; today, live ServiceNow search
  ingest/       redaction, resolution-level classification, chunking
  agent/        pipeline, prompts, bucketing, verification, confidence
  notify/       note formatting and the ServiceNow notifier
  api/          webhook and its in-process worker
  intake/       pre-ticket assistant: fix search, priority and group, incident,
                text chat, LiveKit voice agent, local speech models
  web/          web app: sign-in, employee chatbot, ops console
  store.py      SQLite store for users, conversations and agent runs
  recorder.py   records the agent's runs and steps for the console
eval/           offline evaluation harness
servicenow/     instance-side setup and Business Rule
```
