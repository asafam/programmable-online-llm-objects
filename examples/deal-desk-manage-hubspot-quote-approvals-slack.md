# Example: HubSpot Automated Quote Approval

## Problem statement

Automate your HubSpot quote approval workflow to close deals faster and kick up your sales efficiency.

https://zapier.com/templates/details/deal-desk-manage-hubspot-quote-approvals-slack

## Template

1. A sales rep submits a new quote for approval in HubSpot Quotes
1. The system identifies approvers based on the specific concessions asked for and the rep's reporting chain
1. An email approval request gets sent to the designated approvers
1. The approver reviews the quote details and takes action—approve, reject, or request changes—in Slack
1. If approved, the quote is marked as such in HubSpot, and the rep is free to send it
1. If concessions aren't approved, the quote is marked rejected, and reps can resubmit
1. If quotes aren't approved in 24 hours, stakeholders are tagged in the thread as a reminder

## Grounded steps

1. A sales rep submits a new quote for approval in HubSpot Quotes
1. The system identifies approvers based on the specific concessions asked for and the rep's reporting chain
1. An email approval request gets sent to the designated approvers
1. The approver reviews the quote details and takes action—approve, reject, or request changes—in the #quote-approvals channel on Slack
1. If approved, the quote is marked as such in HubSpot, and the rep is free to send it
1. If concessions aren't approved, the quote is marked rejected, and reps can resubmit
1. If quotes aren't approved in 24 hours, the Deal Desk Team is tagged in the thread as a reminder

## System objects and relationships

```mermaid
graph TD
  HubSpot -->|notifies| QuoteApprovals
  QuoteApprovals -->|identifies approvers| OrganizationDirectory
  OrganizationDirectory -->|provides approver info| QuoteApprovals
  QuoteApprovals -->|sends approval request| Email
  Slack -->|approver action| QuoteApprovals
  QuoteApprovals -->|updates quote status| HubSpot
  QuoteApprovals -->|sends reminders| Slack
  
```

## Sequence diagrams

### Base scenario (no modifications)

```mermaid
sequenceDiagram
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  HubSpot->>QuoteApprovals: New quote submitted
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info
  QuoteApprovals->>Email: Send approval request to approvers
  Slack->>QuoteApprovals: Approver takes action (approve/reject/request changes)
  alt Approved
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Rejected
    QuoteApprovals->>HubSpot: Mark quote as rejected
  end
  QuoteApprovals->>Slack: Send reminder if not approved in 24 hours
```

### Scenario with modification: "Concessions involving discounts over 20% require CFO approval and a secondary notification to the finance team"

```mermaid
sequenceDiagram
  participant Operator
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  Operator->>HubSpot: If quote includes concessions with >20% discount, flag for CFO approval
  HubSpot->>QuoteApprovals: New quote submitted with concessions
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info
  QuoteApprovals->>Email: Send approval request to approvers
  Slack->>QuoteApprovals: Approver takes action (approve/reject/request changes)
  alt Approved with >20% discount
    QuoteApprovals->>HubSpot: Mark quote as approved with CFO approval required
    QuoteApprovals->>Email: Send notification to finance team for CFO review
  else Approved with <=20% discount
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Rejected
    QuoteApprovals->>HubSpot: Mark quote as rejected
  end
  QuoteApprovals->>Slack: Send reminder if not approved in 24 hours
```

### Scenario with modification: "Alice is on vacation this week, reassign any approvals that would have gone to her to Bob"

```mermaid
sequenceDiagram
  participant Operator
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  Operator->>OrganizationDirectory: If approver is Alice, reassign to Bob for this week
  HubSpot->>QuoteApprovals: New quote submitted
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info with Bob instead of Alice
  QuoteApprovals->>Email: Send approval request to approvers (including Bob instead of Alice)
  Slack->>QuoteApprovals: Approver takes action (approve/reject/request changes)
  alt Approved
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Rejected
    QuoteApprovals->>HubSpot: Mark quote as rejected
  end
  QuoteApprovals->>Slack: Send reminder if not approved in 24 hours
```

### Scenario with modification: "For quotes approval requests sent after 6pm, send the email notification the next morning at 9am instead of immediately"

```mermaid
sequenceDiagram
  participant Operator
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  Operator->>Email: If approval request generated after 6pm, delay email notification until next morning at 9am
  HubSpot->>QuoteApprovals: New quote submitted
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info
  alt After 6pm
    QuoteApprovals->>Email: Schedule approval request email for next morning at 9am
  else Before 6pm
    QuoteApprovals->>Email: Send approval request to approvers immediately
  end
  Slack->>QuoteApprovals: Approver takes action (approve/reject/request changes)
  alt Approved
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Rejected
    QuoteApprovals->>HubSpot: Mark quote as rejected
  end
  QuoteApprovals->>Slack: Send reminder if not approved in 24 hours
```

### Scenario with modification: "All quotes for the Acme Corporation should be automatically approved without sending email notifications or requiring approver action"

```mermaid
sequenceDiagram
  participant Operator
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  Operator->>QuoteApprovals: If quote is for Acme Corporation, auto-approve without email notification or approver action
  HubSpot->>QuoteApprovals: New quote submitted for Acme Corporation
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info
  QuoteApprovals->>HubSpot: Mark quote as approved without sending email or requiring approver action
  QuoteApprovals->>Slack: Send notification of auto-approval to sales rep
  QuoteApprovals->>Slack: Send reminder if not approved in 24 hours (should not trigger since it's auto-approved)
```

### Scenario with modification: "Tag the regional manager in Slack on all quotes approvals from their region until the end of the quarter for additional visibility"

```mermaid
sequenceDiagram
  participant Operator
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  Operator->>Slack: Tag regional manager in Slack for all quotes approvals from their region until end of quarter
  HubSpot->>QuoteApprovals: New quote submitted
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info
  QuoteApprovals->>Email: Send approval request to approvers
  Slack->>QuoteApprovals: Approver takes action (approve/reject/request changes)
  alt Approved
    alt Before the end of the quarter
      Slack->>QuoteApprovals: Identify regional manager based on quote region
      QuoteApprovals->>OrganizationDirectory: Get regional manager info
      OrganizationDirectory-->>QuoteApprovals: Return regional manager info
      QuoteApprovals-->>Slack: Regional manager
      Slack->>Slack: Tag regional manager in approval request thread for visibility
    end
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Rejected
    QuoteApprovals->>HubSpot: Mark quote as rejected
  end
  QuoteApprovals->>Slack: Send reminder if not approved in 24 hours
```