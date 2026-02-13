# Example: Slack-ClickUp Support Automation

## Problem statement

Transform chaotic requests and questions into an easy intake process and a knowledge base that grows itself based on your teams expertise.

https://zapier.com/templates/details/helpdesk-automation-template-slack-clickup

## Template

1. An employee posts a question in a designated Slack channel
1. AI automatically searches your FAQ knowledge base (stored in a Zapier table) for relevant answers
1. If AI finds a suitable answer, an AI chatbot replies to the employee directly in Slack
1. If AI can't find a good answer—or if the issue requires human attention—the request gets escalated to an IT team member
1. Employees can mark the urgency of their requests using an emoji that corresponds to predefined priority options
1. The system automatically creates tickets in Jira or ClickUp and sends status updates back to Slack
1. After each ticket gets closed out, the system summarizes the Slack thread and adds it to your FAQ database
1. Next time someone has the same question, AI will have the info to respond automatically

## Grounded steps
1. An employee posts a question in the #support-tickets Slack channel
1. AI automatically searches the IT Support FAQ (stored in a Zapier table) for relevant answers
1. If AI finds a suitable answer, an AI chatbot replies to the employee directly in Slack
1. If AI can't find a good answer—or if the issue requires human attention—the request gets escalated to an IT Support Team member
1. Employees can mark the urgency of their requests using an emoji that corresponds to predefined priority options: :red_circle: high, :yellow_circle: medium, :green_circle: low
1. The system automatically creates tickets in Jira and sends status updates back to Slack
1. After each ticket gets closed out, the system summarizes the Slack thread and adds it to the IT Support FAQ
1. Next time someone has the same question, AI will have the info to respond automatically

## System objects and relationships

```mermaid
graph TD
  Slack --> |notifies| QuestionResolution
  QuestionResolution --> |queries| KnowledgeBase
  QuestionResolution --> |replies to user| Slack
  QuestionResolution --> |creates| Ticket
  Slack --> |updates urgency| Ticket
  Ticket --> |sends status updates| Slack
  Slack --> |sends summary| KnowledgeBase
  KnowledgeBase --> |Updates FAQ| KnowledgeBase
```

## Sequence diagram

### Base scenario (no modifications)

```mermaid
sequenceDiagram
  participant Slack
  participant QuestionResolution
  participant KnowledgeBase
  participant Ticket

  Slack->>QuestionResolution: Receives new question
  QuestionResolution->>KnowledgeBase: Search for answer
  alt Answer found
    KnowledgeBase-->>QuestionResolution: Return answer
    QuestionResolution->>Slack: Reply with answer
  else No good answer
    KnowledgeBase-->>QuestionResolution: Return no good answer
    QuestionResolution->>Ticket: Create support ticket
    Ticket->>Slack: Notify user of ticket creation
    Slack->>Ticket: User updates urgency with emoji
    Ticket->>Slack: Send status updates
    Ticket->>QuestionResolution: Ticket closed
    QuestionResolution->>Slack: Summarize thread
    Slack->>KnowledgeBase: Add summary to FAQ
  end
```

### Scenario with modification: "If user marks request as high urgency, assign to senior support team member and escalate if not resolved in 1 hour"

```mermaid
sequenceDiagram
  participant Operator
  participant Slack
  participant QuestionResolution
  participant KnowledgeBase
  participant Ticket

  Operator->>Slack: If :red_circle: emoji received, assign to senior support team member and set 1 hour escalation timer
  Slack->>QuestionResolution: Receives new question
  QuestionResolution->>KnowledgeBase: Search for answer
  alt Answer found
    KnowledgeBase-->>QuestionResolution: Return answer
    QuestionResolution->>Slack: Reply with answer
  else No good answer
    KnowledgeBase-->>QuestionResolution: Return no good answer
    QuestionResolution->>Ticket: Create support ticket
    Ticket->>Slack: Notify user of ticket creation
    Slack->>Ticket: User updates urgency with :red_circle: emoji
    Ticket->>Ticket: Assign senior support team member
    alt Ticket resolved within 1 hour
      Ticket->>Slack: Send status updates
      Ticket->>QuestionResolution: Ticket closed
      QuestionResolution->>Slack: Summarize thread
      Slack->>KnowledgeBase: Add summary to FAQ
    else Not resolved in 1 hour
      Ticket->>Slack: Escalate ticket and notify stakeholders
    end
  end
```

### Scenario with modification: "If AI finds an answer with low confidence, escalate to human support agent"

```mermaid
sequenceDiagram
  participant QuestionResolution
  participant KnowledgeBase
  participant Slack

  Operator->>KnowledgeBase: Return low confidence result if answer is uncertain
  Operator->>QuestionResolution: If low confidence result received, escalate to human support agent
  QuestionResolution->>KnowledgeBase: Search for answer
  alt Low confidence answer
    KnowledgeBase-->>QuestionResolution: Return low confidence result
    QuestionResolution->>Ticket: Create support ticket
  else High confidence answer
    KnowledgeBase-->>QuestionResolution: Return answer
    QuestionResolution->>Slack: Reply with answer
  end
```

### Scenario with modification: "When a question arrive from a VIP employee, prioritize it and notify the executive support team"

```mermaid
sequenceDiagram
  participant Operator
  participant Slack
  participant QuestionResolution
  participant KnowledgeBase
  participant Ticket

  Operator->>Slack: If question received from VIP employee, prioritize and notify executive support team
  Slack->>QuestionResolution: Receives new question
  QuestionResolution->>KnowledgeBase: Search for answer
  alt Answer found
    KnowledgeBase-->>QuestionResolution: Return answer
    QuestionResolution->>Slack: Reply with answer and notify executive support team
  else No good answer
    QuestionResolution->>Ticket: Create high priority support ticket
    Ticket->>Slack: Notify user of ticket creation and executive support team
  end
```

### Scenario with modification: "If a question has been asked more than 3 times, and has no good answer, automatically escalate to support manager"

```mermaid
sequenceDiagram
  participant Operator
  participant Slack
  participant QuestionResolution
  participant KnowledgeBase
  participant Ticket

  Operator->>QuestionResolution: If same question asked >3 times with no good answer, escalate to support manager
  Slack->>QuestionResolution: Receives new question
  QuestionResolution->>KnowledgeBase: Search for answer and check question frequency
  alt Answer found
    KnowledgeBase-->>QuestionResolution: Return answer
    QuestionResolution->>Slack: Reply with answer
  else No good answer and frequency >3
    KnowledgeBase-->>QuestionResolution: Return no good answer
    QuestionResolution->>Ticket: Create support ticket and escalate to support manager
    Ticket->>Slack: Notify user of ticket creation and escalation
  else No good answer and frequency <=3
    KnowledgeBase-->>QuestionResolution: Return no good answer
    QuestionResolution->>Ticket: Create support ticket without escalation
    Ticket->>Slack: Notify user of ticket creation
  end
```

### Scenario with modification: "For the temporal period of the upcoming product launch (2 weeks), all answers to new questions related to the new product should be automatically classified as pending archival"

```mermaid
sequenceDiagram
  participant Operator
  participant Slack
  participant QuestionResolution
  participant KnowledgeBase
  participant Ticket

  Operator->>KnowledgeBase: For the next 2 weeks, classify answers to questions about new product as pending archival
  Slack->>QuestionResolution: Receives new question
  QuestionResolution->>KnowledgeBase: Search for answer
  alt Answer found
    KnowledgeBase-->>QuestionResolution: Return answer
    QuestionResolution->>Slack: Reply with answer
  else No good answer
    KnowledgeBase-->>QuestionResolution: Return no good answer
    QuestionResolution->>Ticket: Create support ticket
    Ticket->>Slack: Notify user of ticket creation
    Slack->>Ticket: User updates urgency with emoji
    Ticket->>Slack: Send status updates
    Ticket->>QuestionResolution: Ticket closed
    QuestionResolution->>Slack: Summarize thread
    alt Question about new product during launch period
      KnowledgeBase->>KnowledgeBase: Update FAQ with pending archival flag
    else Other questions or outside launch period
      KnowledgeBase->>KnowledgeBase: Update FAQ without pending archival flag
    end
  end
```

  