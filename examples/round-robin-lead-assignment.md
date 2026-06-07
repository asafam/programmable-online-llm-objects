# Example: Round Robin Lead Assignment

## Problem statement

Automatically distribute incoming leads evenly across your sales team by rotating assignments through a round-robin queue.

https://zapier.com/templates/details/round-robin-lead-assignment

## Template

1. Capture lead information through a customizable form on a lead capture page.
1. The sales reps table maintains current representatives and their positions; positions are automatically updated when a new rep is added or an existing rep is removed.
1. Retrieve the sales rep currently in position 1 when a new lead is submitted.
1. Assign the new lead to the sales rep in position 1.
1. Move the assigned sales rep to the back of the line to rotate positions.

## Grounded steps

1. A prospect submits lead information through the lead capture form (or a Facebook Lead Ad).
1. The sales reps table holds the current representatives in ordered queue positions; positions shift when a rep is added or removed.
1. When a new lead arrives, retrieve the sales rep currently in position 1.
1. Assign the new lead to that rep, store the lead record, and notify the rep in Slack with the lead details and a claim link.
1. Move the assigned rep to the back of the line so the next lead goes to the following rep.

## System objects and relationships

```mermaid
graph TD
  LeadForm -->|forwards lead| LeadAssignment
  FacebookLeadAds -->|forwards lead| LeadAssignment
  LeadAssignment -->|get rep in position 1| SalesRepsTable
  SalesRepsTable -->|returns current rep| LeadAssignment
  LeadAssignment -->|rotate rep to back of line| SalesRepsTable
  LeadAssignment -->|store lead + assignment| LeadRecordStore
  LeadAssignment -->|notify assigned rep| SlackNotifications
```

## Sequence diagrams

### Base scenario

A lead is submitted; the round-robin logic assigns it to the rep currently in position 1, stores the record, notifies that rep, and rotates them to the back of the line.

```mermaid
sequenceDiagram
  participant User
  participant LeadForm
  participant LeadAssignment
  participant SalesRepsTable
  participant LeadRecordStore
  participant SlackNotifications

  User->>LeadForm: Submit lead capture form
  LeadForm->>LeadAssignment: Forward new lead
  LeadAssignment->>SalesRepsTable: Get rep currently in position 1
  SalesRepsTable-->>LeadAssignment: Return rep (position 1)
  LeadAssignment->>LeadRecordStore: Store lead with assigned rep
  LeadAssignment->>SlackNotifications: Notify assigned rep with lead details + claim link
  LeadAssignment->>SalesRepsTable: Move assigned rep to the back of the line
```

### Scenario: State-dependent rule — per-rep daily cap

**Workflow rule (to LeadAssignment):**

```
Assign leads round-robin, but no rep may receive more than 2 leads per day.
When the rep in position 1 has already been assigned 2 leads today, skip them
(rotate to the back without assigning) and assign the next eligible rep. If every
rep has hit the daily cap, do not auto-assign — hold the lead and alert the sales
manager. Reset the per-rep daily counts at the start of each day.
```

Unlike the base round-robin (which depends only on *who* is in position 1), this rule is **value-dependent**: the correct action depends on a running count of leads each rep has received *today*. The object must carry that counter as state across every lead it handles, compare it against the cap, and change behavior at the threshold — skipping a capped rep, and eventually holding when all reps are exhausted. The counter is not in any single lead's payload; it only exists in the object's accumulated state.

With traditional programming this would require an explicit per-rep daily counter, a reset job at day boundaries, and branch logic for "skip" vs. "hold all-capped" — each an edge case that must be hand-coded. Here the rule is stated once and the object maintains the state itself.

#### Event sequence (two reps, Ana and Ben; cap = 2/day)

```mermaid
sequenceDiagram
  participant User
  participant LeadAssignment
  participant SalesRepsTable
  participant SlackNotifications

  Note over LeadAssignment: Earlier today Ana already took one lead. Counts Ana=1, Ben=0. Queue order Ana, Ben

  User->>LeadAssignment: New lead L1
  LeadAssignment->>SalesRepsTable: Rep in position 1?
  SalesRepsTable-->>LeadAssignment: Ana
  Note over LeadAssignment: Ana today=1, under cap. Assign. Now Ana=2, Ben=0
  LeadAssignment->>SlackNotifications: Notify Ana (L1)
  LeadAssignment->>SalesRepsTable: Rotate Ana to back (queue Ben, Ana)

  User->>LeadAssignment: New lead L2
  LeadAssignment->>SalesRepsTable: Rep in position 1?
  SalesRepsTable-->>LeadAssignment: Ben
  Note over LeadAssignment: Ben today=0, under cap. Assign. Now Ana=2, Ben=1
  LeadAssignment->>SlackNotifications: Notify Ben (L2)
  LeadAssignment->>SalesRepsTable: Rotate Ben to back (queue Ana, Ben)

  User->>LeadAssignment: New lead L3
  LeadAssignment->>SalesRepsTable: Rep in position 1?
  SalesRepsTable-->>LeadAssignment: Ana
  Note over LeadAssignment: Ana today=2, at cap. SKIP Ana, rotate without assigning
  LeadAssignment->>SalesRepsTable: Rotate Ana to back, no assignment (queue Ben, Ana)
  LeadAssignment->>SalesRepsTable: Rep in position 1?
  SalesRepsTable-->>LeadAssignment: Ben
  Note over LeadAssignment: Ben today=1, under cap. Assign. Now Ana=2, Ben=2
  LeadAssignment->>SlackNotifications: Notify Ben (L3)
  LeadAssignment->>SalesRepsTable: Rotate Ben to back (queue Ana, Ben)

  User->>LeadAssignment: New lead L4
  Note over LeadAssignment: Ana=2, Ben=2 — all reps at daily cap
  LeadAssignment->>SlackNotifications: Hold L4 and alert sales manager (no eligible rep today)
```
