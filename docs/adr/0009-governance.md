# 9. Row security, column masking and the audit trail

- Status: Accepted
- Date: 2026-09-19

## Context

Warp turns a database into an API with very little configuration, which is the
point — and the risk. The three questions an operator in a regulated setting
(KVKK, BDDK) has to answer about such an API are:

1. Can a caller read rows that are not theirs?
2. Can a caller read column values they should not see?
3. Can anyone show, afterwards, what a given caller actually saw?

Before this change the answers were "yes", "yes" and "no". Permissions existed,
but only as coarse verbs (`read`, `create`, …) with no notion of *which* rows or
*which* columns, and nothing was recorded beyond ordinary request logs.

An adjacent constraint shaped the design: the identity built during
authentication never reached a route. Every router registers auth as
`dependencies=[Depends(auth.require(...))]`, and FastAPI discards what such a
dependency returns, so the `AuthenticatedUser` was constructed and thrown away.

## Decision

### Identity reaches the handler

`permission_checker` records the caller on `request.state` before returning it,
and `caller_of` / `policy_of` / `roles_of` read it back. Nothing else changes —
no route signature had to move. `None` means "nobody was identified" (auth off,
or a public path) and is never treated as "permitted".

### Row-level security is a filter, not a check

An API key carries `tenant`, `roles` and per-table `row_filters`, written in the
same vocabulary as user filters so they can simply be ANDed onto a query. Two
enforcement paths, because there are two kinds of access:

- **Reads with a `WHERE` clause** (list, count, export, stream) push the
  conditions into SQL. The database never returns a row the caller may not see,
  and the caller's own filters can only narrow further — policy conditions are
  appended last.
- **Reads and writes addressed by primary key** have no `WHERE` to push into,
  so the row is fetched and matched in memory. A row outside the policy is
  reported as **404, not 403**: telling a caller that a record exists but is
  not theirs is itself a disclosure.

Writes are checked in both directions. A caller cannot create a row into a
scope they cannot read, cannot move a row there with an update, and cannot omit
the scope column and let a database default decide it.

Raw SQL is denied to any key with row rules, even one holding `all`. Conditions
cannot be pushed into a statement the caller wrote, and one `SELECT` would make
the rules irrelevant.

### Masking is keyed on meaning, not on names

Masking rules attach to the catalog's **semantic types** (`email`, `phone`,
`address`, …), not to column names. This is the whole reason the LLM catalog
earns its place here: when it labels a newly discovered `contact_email`, that
column is masked from that moment, rather than leaking until somebody remembers
to write a rule for it.

Only an **approved** catalog is used. A draft's labels have not been reviewed,
and masking the wrong columns is as damaging as masking none.

A mask must produce a value the response can still carry, so the text
strategies require a text column and `null` requires a nullable one. Anything
that does not fit is reported at startup, naming the column, rather than
failing response validation on the first row that reaches it.

### The audit trail records restriction, not data

One event per data-touching request: who, when, which database and table, which
action, how many rows, the request id — and, crucially, **whether a row filter
applied and which columns were masked**. An audit log that cannot distinguish
"read the table" from "read their own tenant, with the email column masked"
cannot answer the only question it is ever asked.

What an event never carries is the data: no row values, no filter literals, no
SQL parameters. Filter *column names* are recorded; the values compared against
them are not. An audit trail that quotes the data it audits becomes a second
copy of that data, in a file with weaker access controls and longer retention
than the database it came from.

Sinks must never raise. An audit sink that can fail a request turns a logging
problem into an outage.

## Consequences

- Row rules mean nothing without authentication, so `auth.enabled: false` with
  `row_filters` configured refuses startup in production. The same hazard
  exists for a data path listed in `auth.public_paths`, which the README warns
  about but cannot detect generically.
- The `CRUDOperations` instance is shared across requests, so the policy is
  bound per request through `with_policy` rather than stored on the instance —
  a cached instance carrying one tenant's rules is precisely the bug this
  feature exists to prevent.
- Masking and auditing travel together as a `Governance` bundle: every route
  that withholds something should also record that it did.
- The audit sink is built in the composition root and reaches the HTTP layer
  through the `Container`, because an inbound adapter may not import an
  outbound one (ADR-0007).
- Writing this exposed a gap in the test harness: `MockDatabaseAdapter.select`
  ignored its `filters` argument, so every route test that exercised
  `filter[column][op]` only proved the request did not error. It now filters as
  SQL would — which is what lets a test notice a *missing* condition.
