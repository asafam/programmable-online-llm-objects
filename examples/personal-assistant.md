# Example: Personal Assistant Automation

## Problem statement

When I book a morning ClassPass class, set my alarm based on the class time. If I snooze twice, cancel the class. Block the time on my calendar—if it conflicts with standup, ping Slack. Plan my commute from the gym to the office and pre-order my usual coffee timed for pickup on the way.

## Grounded steps

1. When I book a morning ClassPass class, block that time on my calendar
1. Set my alarm based on the class time
1. If I snooze my alarm twice, cancel the ClassPass class
1. If rain is expected, book an Uber from the gym to the office, otherwise I'll walk 
1. If walking, pre-order my usual coffee timed for pickup on the way. If taking a ride, skip the coffee order.
1. If the class time conflicts with my 9am standup, ping #team on Slack to let them know I'll be late

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

### Base scenario (no modifications)

```mermaid
sequenceDiagram
    participant ClassPass
    participant Alarm
    participant Calendar
    participant Transportation
    participant CoffeeApp
    participant Slack

    Note over ClassPass: Class booked: 7am at Brooklyn Gym
    ClassPass->>Alarm: Set alarm for 6:15am
    alt Snoozes twice
        Alarm->>ClassPass: Cancel class
    else No snooze or less than 2 snoozes
        ClassPass->>Calendar: Block 7am-8am
        Calendar->>Calendar: Check for conflicts
        Calendar->>Slack: Ping "might be 5 min late to standup"
        ClassPass->>Transport: Class ends 8am at Brooklyn Gym
        Transport->>Transport: Check weather, calculate ETA
        alt Rain expected
            Transport->>Transport: Book a ride to office
        else No rain
            Transport->>CoffeeApp: Order usual, pickup 8:20am
        end
    end
```

### Scenario with modification: "If the class is rescheduled to 8am, update my calendar and alarm accordingly, and skip the coffee order since I'll be leaving later"

```mermaid
sequenceDiagram
    participant Operator
    participant ClassPass
    participant Alarm
    participant Calendar
    participant Transportation
    participant CoffeeApp

    Operator->>ClassPass: If the class is rescheduled, update calendar and alarm, and skip coffee order
    Note over ClassPass: [5:50am] Class rescheduled: 8am at Brooklyn Gym
    ClassPass->>Alarm: Update alarm to 6:45am
    Calendar->>CoffeeApp: Skip coffee order today
    ClassPass->>Calendar: Update calendar to 7:30am-8:30am
    Calendar->>Calendar: Check for conflicts
    Calendar->>Slack: Ping "I will be 15 mins late to standup"
    ClassPass->>Transport: Class ends 8:30am at Brooklyn Gym
    Transport->>Transport: Check weather, calculate ETA
    alt Rain expected
        Transport->>Transport: Book a ride to office
    else No rain
        Transport->>CoffeeApp: Order usual, pickup 8:50am
        CoffeeApp->>CoffeeApp: Skip order due to class reschedule
    end
```

### Scenario with modification: "It's too hot this week, book a ride anyhow"

```mermaid
sequenceDiagram
    participant ClassPass
    participant Alarm
    participant Calendar
    participant Transportation
    participant CoffeeApp
    participant Slack

    Note over ClassPass: Class booked: 7am at Brooklyn Gym
    ClassPass->>Alarm: Set alarm for 6:15am
    alt Snoozes twice
        Alarm->>ClassPass: Cancel class
    else No snooze or less than 2 snoozes
        ClassPass->>Calendar: Block 7am-8am
        Calendar->>Calendar: Check for conflicts
        Calendar->>Slack: Ping "might be 5 min late to standup"
        ClassPass->>Transport: Class ends 8am at Brooklyn Gym
        Transport->>Transport: Book a ride to office
    end
```