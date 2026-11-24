# Orbit — Vision

Orbit is growing from a personal CRM into a **personal operating system**: one self-hosted app that brings the parts of
my life together. These parts include people, money, calendar, commitments, goals, habits, fitness, nutrition, school
and more. Every module feeds one **shared personal context**. AI agents read that context to understand what is
happening, find priorities and tradeoffs, make plans, and, step by step, act for me.

The goal is not better tracking. The goal is a system that **understands my life and helps me run it**.

This file is the north star. `SPEC.md` describes what is built (or specified and next to build). `ROADMAP.md` says in
which order. `DECISIONS.md` records why. When a request or a design does not fit this file, say so before building.

---

## 1. What makes this different from many trackers

Ten trackers in one app are still ten trackers. All the value is in the connections between modules:

- *"Exam on Thursday, 5 gym sessions planned, money tight this month, Peter's birthday on Saturday"* becomes one plan with
  explicit tradeoffs, not four separate reminders.
- A meeting brief knows the person, what we owe each other, what I promised, and which of my goals the meeting serves.
- A weekly review sees the whole week across every domain and proposes next week.

Each new module is judged first by what it adds to the shared context, and only then by its own screens.

## 2. Principles

1. **Shared context over silos.** Every module writes its data in a form the rest of the system can read: typed
   models, links to other entities (people, goals, events), and a context provider for agents (see §4). A module that
   only serves its own screens is incomplete.
2. **Passive capture first.** Manual logging stops after two weeks. Prefer sources that need no typing: bank exports,
   calendar, contacts, Apple Health via Shortcuts, school systems, email. Dictation via Shortcuts is the fallback, and
   forms are the last option.
3. **Goals are the spine.** An agent can only weigh tradeoffs against explicit priorities. Goals and their ranking are
   first-class data. Other modules link to them.
4. **Claude proposes, I confirm.** This is the default for every agent output. See the autonomy ladder in §3.
5. **Depth before breadth.** Make one cross-domain loop work end to end before adding the next domain. A new module
   ships only with a real data source and at least one agent use.
6. **Self-hosted, and generous with Claude.** It is single-user and holds my whole life. The data stays on my own server,
   but agents get rich context: the better the information, the better the decisions (`DECISIONS.md` #7). Only
   identifiers with no decision value (IBANs, account numbers, full phone numbers, emails) are masked.
7. **Simple and boring tech.** Use the stack in `CLAUDE.md`. Build a shared abstraction only when a second real
   consumer needs it, never speculatively.

## 3. Autonomy ladder

Every agent capability sits on one level. **L2 is the default.** Raising a capability is my decision. Record it in
`DECISIONS.md` for that specific kind of action, never as a global switch.

| Level | Name | What the system does |
|---|---|---|
| L0 | Observe | Reads and stores data. No output. |
| L1 | Inform | Briefs, digests, reviews, insights. Changes nothing. |
| L2 | Propose | Writes a `Proposal` to the Inbox. Nothing changes until I confirm. **Default.** |
| L3 | Act with Undo | Applies the change right away, notifies me, and logs it with one-tap Undo. Only for allowlisted, reversible, internal actions. |
| L4 | Act | Acts without asking, inside a narrow allowlisted scope with limits. Logged. |

Hard rules that do not change with the level:
- Nothing outward-facing (sending messages, paying, booking, posting) goes above L2 unless `DECISIONS.md` says so for
  that exact action.
- Money never moves automatically. Debts are never settled automatically.
- Every L3 or L4 action is reversible or explicitly logged, and is visible in one audit view.

## 4. Architecture direction

Most of this is not built yet. Treat it as the target shape, and build each piece in its roadmap phase.

**Modules.** A module is one domain, for example people, ledger, calendar, goals or health. It follows the existing
layout: models in `orbit/domain/`, logic in `orbit/services/`, JSON API in `orbit/api/`, pages in `orbit/web/`, and
external sources in `orbit/integrations/`. Every module must provide:
1. Typed models with links to shared entities (people, goals, meetings) through foreign keys, not free text.
2. Ingest from a real source, with idempotent dedupe (see `CLAUDE.md` rule 6).
3. A **context provider**: a function that returns a typed, bounded, masked Pydantic summary of the module's state for a
   time window and an optional focus (a person, a goal). Agents get module data only through providers.
4. Its proposal kinds (payload model + apply function) when automation can change its data.
5. Tests, as for every service.

**Shared context layer.** This is `orbit/services/context/` (`snapshot.py` builds it; built in Phase 14). It builds a snapshot for agents from all
providers: "the week ahead", "everything about Peter", "everything that touches goal X". It enforces size limits and
masking in one place.

**Agents.** An agent is a service that does four things: it collects context through the context layer, calls Claude
through `orbit/integrations/claude.py`, validates the structured output, and writes proposals or L1 output. Scheduled
agents run from `orbit/jobs/`. Agents never write domain tables directly at L2. Existing examples are capture,
transaction classification, meeting linking and briefs.

**Inbox.** This is the one place where proposals from all modules and agents meet. As proposal kinds grow, the
`if/elif` dispatch in `orbit/services/inbox.py` becomes a registry: kind → payload model, apply, reopen rule.

**Interfaces.** These are the iPhone (Shortcuts, ntfy push with action buttons), the ICS feed, and the mobile-first web
UI. An MCP server for Claude desktop is in the backlog.

## 5. Domains

Current status and intent. The order after Goals is my choice, made per phase.

| Domain | Status | Likely source | Context it adds |
|---|---|---|---|
| People & relationships | Built (v1) | iCloud Contacts, dictation | Who matters, last contact, birthdays, notes |
| Ledger (debts, items, favors) | Built (v1) | Bank CSV, dictation | Who owes what |
| Calendar & meetings | Built (v1) | iCloud CalDAV | Time commitments, who I meet |
| Promises | Built (v1) | Dictation, post-meeting capture | What I committed to |
| Goals & priorities | Next | Me (weekly review) | What matters and in which order |
| Personal finance (budget, spending) | Later | Bank CSV (already imported) | Money pressure, spending against goals |
| Fitness & health | Later | Apple Health via Shortcuts | Energy, training load, sleep |
| Habits | Later | Shortcuts, Health | Routines and streaks |
| School & learning | Specified (Phase 18, `SPEC §2.20`; waits for my OK) | SIS and ReCodEx calendar feeds (token), public SIS pages; later a browser helper (SIS, Owl), course websites, school Outlook/Teams | Lessons, exams, deadlines, workload |
| Nutrition | Later | To be decided | Intake against health goals |

## 6. Non-goals

- Multi-user, sharing, or a product for other people.
- A native app or SPA. iPhone integration stays Shortcuts + push + web.
- Gamified tracking for its own sake.
- Autonomy that I cannot see, undo, or turn off.

## 7. First proof point

The concept works when a **weekly review agent** reads people, money, calendar, promises and goals together and
produces priorities, conflicts and a proposed plan that I would not have noticed myself. Until that works, do not
add more domains.
