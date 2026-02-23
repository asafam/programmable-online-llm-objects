# Example: Personal Assistant Automation

## Problem statement

When I book a morning ClassPass class, set my alarm based on the class time. Block the time on my calendar—if it conflicts with standup, ping Slack. Plan my commute from the gym to the office and pre-order my usual coffee timed for pickup on the way.

## Grounded steps

1. When I book a morning ClassPass class, block that time on my calendar
1. Set my alarm based on the class time
1. If rain is expected, book an Uber from the gym to the office, otherwise I'll walk 
1. If walking, pre-order my usual coffee timed for pickup on the way. If taking a ride, skip the coffee order.
1. If the class time conflicts with my 9am standup meeting, ping #team on Slack to let them know I'll be late

## System objects and relationships

```mermaid
graph TD
    ClassPass --> |Blocks time| Calendar
    ClassPass --> |Sets alarm| Alarm
    Alarm --> |Cancels class| ClassPass
    ClassPass --> |Provides class info| Transportation
    Transportation --> |Checks weather| CoffeeApp
    Calendar --> |Pings if conflicts| Slack
```

## Sequence diagram

### Base scenario

A user books a class, sets an alarm, and plans their commute and coffee order accordingly.

```mermaid
sequenceDiagram
    participant ClassPass
    participant Alarm
    participant Calendar
    participant Transportation
    participant CoffeeApp
    participant Slack

    User->>ClassPass: Books me a morning yoga classes at the closeby gym
    ClassPass-->>User: Confirms you're booked for 7am at Brooklyn Gym
    ClassPass->>Alarm: I see she usually wakes up at 6:15am: Set an alarm for 6:15am
    ClassPass->>Calendar: Block 7am-8am
    ClassPass->>Transport: Class ends 8am at Brooklyn Gym
    Transport->>Transport: Check weather, calculate ETA
    alt Rain expected
        Transport->>Transport: Book a ride to office
    else No rain
        Transport->>CoffeeApp: Order usual, pickup 8:20am
    end
    Transport->>Calendar: Update commute time on calendar
    Calendar->>Calendar: No conflicts. Standup got rescheduled to 8:30am today.
    Calendar->>Slack: Ping "might be 5 min late to standup"
```

### Scenario: Simple modification

**Modification (to Calendar):**

```
If I snooze my alarm twice, cancel the ClassPass class
```

In this example, the user wants to add a new requirement that if they snooze their alarm twice, the system should automatically cancel their ClassPass class. This requires the Calendar object to communicate with the Alarm object to track snooze events and with the ClassPass object to cancel the class if the condition is met.
With traditional programming, this would require additional code to check the timing and conditionally execute the coffee order, whereas with natural language programming, you can simply state the new requirement and let the system handle the implementation details.

#### Modification sequence

```mermaid
sequenceDiagram
    participant Operator
    participant Alarm

    Operator->>Alarm: If I snooze twice, cancel the ClassPass class
```

#### New event sequence

```mermaid
sequenceDiagram
    participant ClassPass
    participant Alarm
    participant Calendar
    participant Transportation
    participant CoffeeApp

    Note over ClassPass: User has 7am class and snoozes alarm at 6:15am and 6:30am
    Alarm->>ClassPass: User snoozed twice, cancel class
    ClassPass->>Calendar: Unblock 7am-8am
    ClassPass->>Transportation: No class, no commute needed
    Transportation->>CoffeeApp: No commute, cancel coffee order
```

### Scenario: Smeantic Polymorphism

**Modification:**

```
Delay by 30 minutes
```

In this example, the user passes the same instruction to different llm-objects. Same instruction, different behavior based on context. 
In a traditional programming paradigm, this would likely require additional code to handle the new instruction for each object, and the system might not be able to generalize the instruction across different contexts without explicit programming. With natural language programming, you can simply state the new instruction once, and the system can interpret and apply it appropriately across all relevant objects based on their responsibilities and relationships.

#### New event sequence

```mermaid
sequenceDiagram
  participant User
  participant ClassPass
  participant Alarm
  participant Calendar
  participant Transport
  participant CoffeeApp

  User->>ClassPass: Delay by 30 minutes
  ClassPass-->>ClassPass: Rebook class from 7am to 7:30am
  
  User->>Alarm: Delay by 30 minutes
  Alarm-->>Alarm: Move alarm from 5:30am to 6:00am
  
  User->>Calendar: Delay by 30 minutes
  Calendar-->>Calendar: Shift event from 7-8am to 7:30-8:30am 
  
  User->>Transport: Delay by 30 minutes
  Transport-->>Transport: Recalculate ETA, adjust pickup time
  
  User->>CoffeeApp: Delay by 30 minutes
  CoffeeApp-->>CoffeeApp: Push order pickup from 8:20am to 8:50am
```
