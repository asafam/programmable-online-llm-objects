"""
Tests for generated test data quality validators.

All validators live in src/data/validate_test_cases.py so they can be
imported by the pipeline.  This file contains synthetic fixtures that
exercise each validator's detection logic and false-positive behaviour.

High impact issues (docs/FINDINGS.md):
    - Mock data field mismatches
    - Read/write service misclassification

Medium impact issues:
    - "After X confirms" sequential confirmation chains
    - Missing data in step text
    - Threshold evaluation in expectations

Structural / graph integrity:
    - Trigger & triggered_by reference integrity
    - Invalid peer declarations (dangling peer targets)
    - Peer graph dead-ends (entry-point with no peers)
    - Unreachable objects (orphan objects)
    - Missing mock tools for _data skills

Data quality:
    - Unnatural identifiers in mock data + expectations
"""
import pytest
from src.data.schema import (
    Ambiguity,
    Event,
    EventExpect,
    MockToolDef,
    Modification,
    ModType,
    ObjectDef,
    Step,
    Sample,
)
from src.data.validate_test_cases import (
    find_invalid_peer_declarations,
    find_missing_mock_tools,
    find_missing_step_data,
    find_mock_field_mismatches,
    find_peer_graph_dead_ends,
    find_read_write_misclassifications,
    find_sequential_confirmation_chains,
    find_threshold_evaluation_errors,
    find_trigger_reference_errors,
    find_unnatural_identifiers,
    find_unreachable_objects,
)


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _mod(target: str) -> Modification:
    return Modification(
        id="M001",
        target=target,
        when="W01-1T09:00",
        mod_type=ModType.contextual,
        intent="Prioritize enterprise customers",
        ambiguity=Ambiguity.precise,
    )


def _event(recipient: str, input_text: str, action: str) -> Event:
    return Event(
        id="E001",
        call_type="send_event",
        source="external",
        recipient=recipient,
        input=input_text,
        when="W01-2T10:00",
        expect=EventExpect(action=action, reason="routing rule"),
    )


# ── Tests: Mock Data Field Mismatches (High) ─────────────────────────────────

class TestMockDataFieldMismatches:
    """
    Mock tool response templates must use field values consistent with how
    behavior descriptions reference them.  A mismatch (e.g., mock returns
    status='captured' while behavior checks for status='new') causes objects
    to store wrong values, breaking downstream expectations.
    """

    def test_detects_status_field_mismatch(self):
        """Behavior expects status='new' but mock returns status='captured'."""
        obj = ObjectDef(
            object_id="ticket-router",
            role="Routes incoming support tickets",
            behavior="When a ticket arrives with status 'new', assign to the on-call agent.",
            event_sources=["zendesk"],
        )
        mock = MockToolDef(
            tool_name="zendesk.get_ticket",
            description="Fetch a Zendesk ticket by ID",
            arguments_schema={"type": "object", "properties": {"ticket_id": {"type": "string"}}},
            response_template='{"id": "TICKET-001", "status": "captured", "priority": "high"}',
        )
        tc = Sample(
            id="TC001", name="Ticket routing", domain="support", source_type="zapier",
            objects=[obj],
            steps=['New ticket received from customer'],
            modifications=[_mod("ticket-router")],
            events=[_event("ticket-router", "Ticket TICK-001 received", "Assign to on-call agent")],
            mock_tools=[mock],
        )
        issues = find_mock_field_mismatches(tc)
        assert len(issues) > 0
        assert any("status" in i and "new" in i for i in issues)

    def test_detects_stage_and_priority_mismatches(self):
        """Behavior expects stage='qualified' and priority='high' but mock differs on both."""
        obj = ObjectDef(
            object_id="crm-handler",
            role="Handles CRM lead processing",
            behavior="Process leads with stage 'qualified' and priority 'high'.",
            event_sources=["hubspot"],
        )
        mock = MockToolDef(
            tool_name="hubspot.get_contact",
            description="Fetch a HubSpot contact",
            arguments_schema={"type": "object", "properties": {"contact_id": {"type": "string"}}},
            response_template='{"id": "CONTACT-001", "stage": "prospect", "priority": "medium"}',
        )
        tc = Sample(
            id="TC002", name="Lead processing", domain="sales", source_type="zapier",
            objects=[obj],
            steps=['New lead from Acme Corp'],
            modifications=[_mod("crm-handler")],
            events=[_event("crm-handler", "New lead received", "Process lead")],
            mock_tools=[mock],
        )
        issues = find_mock_field_mismatches(tc)
        assert len(issues) >= 1

    def test_no_issues_when_fields_match(self):
        """Behavior expects status='new' and mock also returns status='new'."""
        obj = ObjectDef(
            object_id="ticket-router",
            role="Routes incoming support tickets",
            behavior="When a ticket arrives with status 'new', assign to the on-call agent.",
            event_sources=["zendesk"],
        )
        mock = MockToolDef(
            tool_name="zendesk.get_ticket",
            description="Fetch a Zendesk ticket",
            arguments_schema={"type": "object", "properties": {"ticket_id": {"type": "string"}}},
            response_template='{"id": "TICKET-001", "status": "new", "priority": "high"}',
        )
        tc = Sample(
            id="TC003", name="Ticket routing", domain="support", source_type="zapier",
            objects=[obj],
            steps=['New ticket received from customer'],
            modifications=[_mod("ticket-router")],
            events=[_event("ticket-router", "Ticket TICK-001 received", "Assign to on-call agent")],
            mock_tools=[mock],
        )
        assert find_mock_field_mismatches(tc) == []


# ── Tests: Read/Write Misclassification (High) ───────────────────────────────

class TestReadWriteMisclassification:
    """
    Objects that receive queries must be able to respond.  Modeling a data store
    as a write-only service causes the querying object to set PENDING and wait
    forever for a reply that never arrives.
    """

    def test_detects_queried_object_with_no_response_capability(self):
        """Object A queries B, but B has no event_sources and no _data skill."""
        obj_a = ObjectDef(
            object_id="hr-triage",
            role="Triages HR support requests",
            behavior="When a request arrives, query the FAQ knowledge base for a matching answer.",
            event_sources=["slack"],
            neighbors=['faq-knowledge-base'],
        )
        obj_b = ObjectDef(
            object_id="faq-knowledge-base",
            role="Stores FAQ articles for HR policies",
            behavior="Receive new FAQ articles and store them. Do not reply to incoming messages.",
        )
        tc = Sample(
            id="TC004", name="HR support", domain="hr", source_type="zapier",
            objects=[obj_a, obj_b],
            steps=['Slack message from Maya Chen: question about parental leave'],
            modifications=[_mod("hr-triage")],
            events=[_event("hr-triage", "HR question received", "Reply with FAQ answer")],
            mock_tools=[],
        )
        issues = find_read_write_misclassifications(tc)
        assert len(issues) > 0
        assert any("faq-knowledge-base" in i for i in issues)

    def test_detects_write_only_behavior_on_queried_object(self):
        """B is queried but has explicit 'do not reply' behavior."""
        obj_a = ObjectDef(
            object_id="policy-engine",
            role="Checks compliance policies",
            behavior="On each request, lookup applicable policy rules from the policy store.",
            event_sources=["webhook"],
            neighbors=['policy-store'],
        )
        obj_b = ObjectDef(
            object_id="policy-store",
            role="Stores compliance policy documents",
            behavior="Append new policy documents to the store. Do not reply to queries.",
            event_sources=["admin"],
        )
        tc = Sample(
            id="TC005", name="Policy check", domain="compliance", source_type="zapier",
            objects=[obj_a, obj_b],
            steps=['New vendor contract for review: Acme Corp MSA'],
            modifications=[_mod("policy-engine")],
            events=[_event("policy-engine", "Contract review requested", "Apply matching policy")],
            mock_tools=[],
        )
        issues = find_read_write_misclassifications(tc)
        assert len(issues) > 0

    def test_no_issues_when_queried_object_has_event_sources(self):
        """B is queried and has event_sources — correctly set up as a read service."""
        obj_a = ObjectDef(
            object_id="ticket-router",
            role="Routes support tickets",
            behavior="On each ticket, query the directory service to get the on-call agent.",
            event_sources=["zendesk"],
            neighbors=['org-directory'],
        )
        obj_b = ObjectDef(
            object_id="org-directory",
            role="Answers queries about organizational structure",
            behavior="When queried about a team, reply with the on-call agent name.",
            event_sources=["internal"],
        )
        tc = Sample(
            id="TC006", name="Ticket routing", domain="support", source_type="zapier",
            objects=[obj_a, obj_b],
            steps=['High-priority ticket from Acme Corp'],
            modifications=[_mod("ticket-router")],
            events=[_event("ticket-router", "Ticket arrived", "Route to on-call agent")],
            mock_tools=[],
        )
        assert find_read_write_misclassifications(tc) == []

    def test_no_issues_when_queried_object_has_data_skill(self):
        """B has a _data skill — can serve data lookups."""
        obj_a = ObjectDef(
            object_id="lead-scorer",
            role="Scores incoming leads",
            behavior="On each lead, retrieve account data and score based on firmographics.",
            event_sources=["hubspot"],
            neighbors=['crm-data-service'],
        )
        obj_b = ObjectDef(
            object_id="crm-data-service",
            role="Provides CRM account data on request",
            behavior="Return account details from state for any queried account ID.",
            skills=["crm_data"],
        )
        tc = Sample(
            id="TC007", name="Lead scoring", domain="sales", source_type="zapier",
            objects=[obj_a, obj_b],
            steps=['New lead: Jane Smith, jane@acme.com'],
            modifications=[_mod("lead-scorer")],
            events=[_event("lead-scorer", "Lead received", "Score lead and route")],
            mock_tools=[],
        )
        assert find_read_write_misclassifications(tc) == []


# ── Tests: Sequential Confirmation Chains (Medium) ───────────────────────────

class TestSequentialConfirmationChains:
    """
    Behavior must not wait for confirmation from fire-and-forget write services.
    These peers never reply, so the chain stalls permanently after the first send.
    """

    def test_detects_after_confirms_pattern(self):
        """Behavior uses 'after gmail-drafts confirms'."""
        obj = ObjectDef(
            object_id="follow-up-composer",
            role="Composes and dispatches sales follow-up emails",
            behavior=(
                "On each call summary, draft an email and send to gmail-drafts. "
                "After gmail-drafts confirms, send task details to hubspot-tasks. "
                "After hubspot-tasks confirms, send summary to slack-notifications."
            ),
            event_sources=["gong"],
            neighbors=['gmail-drafts', 'hubspot-tasks', 'slack-notifications'],
        )
        tc = Sample(
            id="TC008", name="Sales follow-up", domain="sales", source_type="zapier",
            objects=[obj],
            steps=['Gong call summary: rep Alice, Acme Corp'],
            modifications=[_mod("follow-up-composer")],
            events=[_event("follow-up-composer", "Call summary received", "Send email draft, HubSpot task, and Slack notification")],
            mock_tools=[],
        )
        issues = find_sequential_confirmation_chains(tc)
        assert len(issues) > 0
        assert any("follow-up-composer" in i for i in issues)

    def test_detects_when_responds_pattern(self):
        """Behavior uses 'when finance-tracker responds'."""
        obj = ObjectDef(
            object_id="expense-policy",
            role="Processes expense submissions",
            behavior=(
                "When an expense is submitted, record it in finance-tracker. "
                "When finance-tracker responds with the record ID, notify slack-finance."
            ),
            event_sources=["expensify"],
            neighbors=['finance-tracker', 'slack-finance'],
        )
        tc = Sample(
            id="TC009", name="Expense processing", domain="finance", source_type="zapier",
            objects=[obj],
            steps=['Expense submitted by Alice: $450, client dinner'],
            modifications=[_mod("expense-policy")],
            events=[_event("expense-policy", "Expense received", "Record and notify")],
            mock_tools=[],
        )
        issues = find_sequential_confirmation_chains(tc)
        assert len(issues) > 0

    def test_no_issues_for_simultaneous_fanout(self):
        """Behavior uses flat simultaneous fan-out — no confirmation language."""
        obj = ObjectDef(
            object_id="lead-dispatcher",
            role="Dispatches leads simultaneously to CRM, task manager, and Slack",
            behavior=(
                "When a new lead arrives, simultaneously send to hubspot-crm, "
                "asana-tasks, and slack-sales. Include all lead details in each message."
            ),
            event_sources=["typeform"],
            neighbors=['hubspot-crm', 'asana-tasks', 'slack-sales'],
        )
        tc = Sample(
            id="TC010", name="Lead dispatch", domain="sales", source_type="zapier",
            objects=[obj],
            steps=['New lead: John Doe, john@acme.com, Acme Corp, Enterprise'],
            modifications=[_mod("lead-dispatcher")],
            events=[_event("lead-dispatcher", "Lead form submitted", "CRM contact, task, and Slack notification")],
            mock_tools=[],
        )
        assert find_sequential_confirmation_chains(tc) == []


# ── Tests: Missing Data in Step Text (Medium) ────────────────────────────────

class TestMissingStepData:
    """
    Step text must carry all data downstream objects need.  Missing channel names
    or ticket IDs cause objects to invent values that won't match expectations.
    """

    def test_detects_missing_slack_channel(self):
        """Expectation references #support-queue but no step or mock includes it."""
        obj = ObjectDef(
            object_id="helpdesk-router",
            role="Routes support tickets to Slack channels",
            behavior="Post notification to the appropriate channel.",
            event_sources=["zendesk"],
        )
        tc = Sample(
            id="TC011", name="Helpdesk routing", domain="support", source_type="zapier",
            objects=[obj],
            steps=['New support ticket from priya.nair'],
            modifications=[_mod("helpdesk-router")],
            events=[Event(
                id="E001", call_type="send_event", source="zendesk",
                recipient="helpdesk-router",
                input="Ticket TICK-001 from priya.nair: cannot login",
                when="W01-2T10:00",
                expect=EventExpect(
                    action="Post notification to #support-queue assigning ticket to Jordan Reyes",
                    reason="New tickets go to #support-queue",
                ),
            )],
            mock_tools=[],
        )
        issues = find_missing_step_data(tc)
        assert len(issues) > 0
        assert any("support-queue" in i for i in issues)

    def test_detects_missing_ticket_id(self):
        """Expectation references 'PROJ-1042' absent from steps and mock data."""
        obj = ObjectDef(
            object_id="jira-handler",
            role="Handles Jira ticket creation",
            behavior="Create a Jira ticket for each form submission.",
            event_sources=["form"],
        )
        tc = Sample(
            id="TC012", name="Engineering intake", domain="engineering", source_type="zapier",
            objects=[obj],
            steps=['Engineering intake form submitted: need dashboard feature'],
            modifications=[_mod("jira-handler")],
            events=[Event(
                id="E001", call_type="send_event", source="form",
                recipient="jira-handler",
                input="Form submission: new feature request",
                when="W01-2T10:00",
                expect=EventExpect(
                    action='Jira ticket "PROJ-1042" created and assigned to lead engineer',
                    reason="All form submissions create Jira tickets",
                ),
            )],
            mock_tools=[],
        )
        issues = find_missing_step_data(tc)
        assert len(issues) > 0

    def test_no_issues_when_channel_in_step_text(self):
        """Channel referenced in expectation also appears in step text."""
        obj = ObjectDef(
            object_id="helpdesk-router",
            role="Routes support tickets",
            behavior="Post to the channel specified in the trigger.",
            event_sources=["zendesk"],
        )
        tc = Sample(
            id="TC013", name="Helpdesk routing", domain="support", source_type="zapier",
            objects=[obj],
            steps=['New ticket from priya.nair, route to #support-queue'],
            modifications=[_mod("helpdesk-router")],
            events=[Event(
                id="E001", call_type="send_event", source="zendesk",
                recipient="helpdesk-router",
                input="Ticket from priya.nair",
                when="W01-2T10:00",
                expect=EventExpect(action="Post notification to #support-queue", reason="ticket routing"),
            )],
            mock_tools=[],
        )
        assert find_missing_step_data(tc) == []

    def test_no_issues_when_channel_in_mock_data(self):
        """Channel referenced in expectation is supplied by mock tool response."""
        obj = ObjectDef(
            object_id="helpdesk-router",
            role="Routes tickets using channel from config",
            behavior="Fetch ticket data and route to the channel from the config.",
            event_sources=["zendesk"],
        )
        mock = MockToolDef(
            tool_name="zendesk.get_ticket",
            description="Fetch Zendesk ticket data",
            arguments_schema={"type": "object", "properties": {"id": {"type": "string"}}},
            response_template='{"id": "TICK-001", "channel": "#support-queue", "assignee": "Jordan Reyes"}',
        )
        tc = Sample(
            id="TC014", name="Helpdesk routing", domain="support", source_type="zapier",
            objects=[obj],
            steps=['New ticket TICK-001 from priya.nair'],
            modifications=[_mod("helpdesk-router")],
            events=[Event(
                id="E001", call_type="send_event", source="zendesk",
                recipient="helpdesk-router",
                input="Ticket TICK-001 received",
                when="W01-2T10:00",
                expect=EventExpect(action="Post notification to #support-queue", reason="channel from config"),
            )],
            mock_tools=[mock],
        )
        assert find_missing_step_data(tc) == []


# ── Tests: Threshold Evaluation (Medium) ─────────────────────────────────────

class TestThresholdEvaluationInExpectations:
    """
    Expectations must only include conditional outputs when the threshold
    condition is actually satisfied by the event's input data.
    """

    def test_detects_escalation_when_score_above_threshold(self):
        """Behavior: 'score < 15 → escalate'. Input has score 23. Expectation wrongly says escalate."""
        obj = ObjectDef(
            object_id="call-coach",
            role="Evaluates sales call quality",
            behavior=(
                "Evaluate each call recording. "
                "If score < 15 → escalate to senior coach for review. "
                "Otherwise, send standard feedback to the rep."
            ),
            event_sources=["gong"],
        )
        tc = Sample(
            id="TC015", name="Call coaching", domain="sales", source_type="zapier",
            objects=[obj],
            steps=['Call recording from rep Alice available'],
            modifications=[_mod("call-coach")],
            events=[Event(
                id="E001", call_type="send_event", source="gong",
                recipient="call-coach",
                input="Call completed: rep Alice, score 23, topics: pricing",
                when="W01-2T10:00",
                expect=EventExpect(
                    action="Escalate call to senior coach for review",
                    reason="Score is below threshold of 15",  # wrong — 23 > 15
                ),
            )],
            mock_tools=[],
        )
        issues = find_threshold_evaluation_errors(tc)
        assert len(issues) > 0
        assert any("escalate" in i.lower() for i in issues)

    def test_no_issue_when_threshold_is_met(self):
        """Behavior: 'score < 15 → escalate'. Input has score 10. Expectation is correct."""
        obj = ObjectDef(
            object_id="call-coach",
            role="Evaluates sales call quality",
            behavior=(
                "Evaluate each call recording. "
                "If score < 15 → escalate to senior coach for review. "
                "Otherwise, send standard feedback to the rep."
            ),
            event_sources=["gong"],
        )
        tc = Sample(
            id="TC016", name="Call coaching", domain="sales", source_type="zapier",
            objects=[obj],
            steps=['Call recording from rep Bob available'],
            modifications=[_mod("call-coach")],
            events=[Event(
                id="E001", call_type="send_event", source="gong",
                recipient="call-coach",
                input="Call completed: rep Bob, score 10, topics: pricing only",
                when="W01-2T10:00",
                expect=EventExpect(
                    action="Escalate call to senior coach for review",
                    reason="Score 10 is below threshold of 15",  # correct
                ),
            )],
            mock_tools=[],
        )
        assert find_threshold_evaluation_errors(tc) == []

    def test_no_issue_when_conditional_action_absent_and_threshold_not_met(self):
        """Behavior: 'amount > 1000 → flag'. Input 500. Expectation says auto-approve — correct."""
        obj = ObjectDef(
            object_id="expense-monitor",
            role="Monitors expense submissions",
            behavior=(
                "If amount > 1000 → flag for manager review and notify compliance. "
                "Otherwise, auto-approve and record."
            ),
            event_sources=["expensify"],
        )
        tc = Sample(
            id="TC017", name="Expense monitoring", domain="finance", source_type="zapier",
            objects=[obj],
            steps=['Expense from Alice for team lunch'],
            modifications=[_mod("expense-monitor")],
            events=[Event(
                id="E001", call_type="send_event", source="expensify",
                recipient="expense-monitor",
                input="Expense submitted by Alice: amount 500 for team lunch",
                when="W01-2T10:00",
                expect=EventExpect(
                    action="Auto-approve expense and record in finance system",
                    reason="Amount $500 is below $1000 threshold",
                ),
            )],
            mock_tools=[],
        )
        assert find_threshold_evaluation_errors(tc) == []


# ── Tests: Trigger & triggered_by reference integrity ────────────────────────

class TestTriggerDataQuality:
    """
    Structural integrity of MockToolDef triggers and Event.triggered_by chains.
    """

    def _base_objects(self):
        return [
            ObjectDef(
                object_id="email-handler",
                role="Processes outbound email requests",
                behavior="Send emails and notify downstream handlers.",
                event_sources=["crm"],
            ),
            ObjectDef(
                object_id="slack-handler",
                role="Receives Slack messages and routes them",
                behavior="Route Slack messages to the relevant team.",
                event_sources=["slack"],
            ),
        ]

    def _base_mock_tool(self, triggers=None):
        return MockToolDef(
            tool_name="email.send",
            description="Send an email",
            arguments_schema={"type": "object", "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
            }},
            response_template="email_id: {call_index}",
            triggers=triggers or [],
        )

    def test_detects_trigger_targeting_nonexistent_object(self):
        """Trigger target_object_id references an object not in tc.objects."""
        from src.data.schema import MockToolTrigger
        mock = self._base_mock_tool(triggers=[
            MockToolTrigger(target_object_id="nonexistent-handler", message_template="Email to {to}", source="slack"),
        ])
        tc = Sample(
            id="TC018", name="Email chain", domain="sales", source_type="zapier",
            objects=self._base_objects(),
            steps=['Send follow-up email to alice@company.com'],
            modifications=[_mod("email-handler")],
            events=[_event("email-handler", "Email send requested", "Email dispatched")],
            mock_tools=[mock],
        )
        issues = find_trigger_reference_errors(tc)
        assert any("nonexistent-handler" in i for i in issues)

    def test_detects_trigger_template_with_undefined_placeholder(self):
        """
        Template uses {recipient} but schema only declares 'to' and 'subject'.
        Validator catches {ticket_id} vs {id} style mismatches too.
        """
        from src.data.schema import MockToolTrigger
        mock = self._base_mock_tool(triggers=[
            MockToolTrigger(
                target_object_id="slack-handler",
                message_template="Email sent to {recipient} about {subject}",  # {recipient} undefined
                source="slack",
            ),
        ])
        tc = Sample(
            id="TC019", name="Email chain", domain="sales", source_type="zapier",
            objects=self._base_objects(),
            steps=['Send follow-up email'],
            modifications=[_mod("email-handler")],
            events=[_event("email-handler", "Email send requested", "Email dispatched")],
            mock_tools=[mock],
        )
        issues = find_trigger_reference_errors(tc)
        assert any("recipient" in i for i in issues)

    def test_detects_ticket_id_vs_id_placeholder_mismatch(self):
        """
        Template uses {ticket_id} but the tool schema only declares {id}.
        This is the canonical false-name placeholder error.
        """
        from src.data.schema import MockToolTrigger
        mock = MockToolDef(
            tool_name="zendesk.create_ticket",
            description="Create a Zendesk ticket",
            arguments_schema={"type": "object", "properties": {"id": {"type": "string"}, "subject": {"type": "string"}}},
            response_template="Created ticket {id}",
            triggers=[
                MockToolTrigger(
                    target_object_id="slack-handler",
                    message_template="Ticket {ticket_id} created for {subject}",  # {ticket_id} wrong, should be {id}
                    source="slack",
                ),
            ],
        )
        tc = Sample(
            id="TC020", name="Ticket creation", domain="support", source_type="zapier",
            objects=self._base_objects(),
            steps=['Create ticket for login issue'],
            modifications=[_mod("email-handler")],
            events=[_event("email-handler", "Ticket created", "Slack notified")],
            mock_tools=[mock],
        )
        issues = find_trigger_reference_errors(tc)
        assert any("ticket_id" in i for i in issues)

    def test_detects_triggered_by_referencing_nonexistent_event(self):
        """Event.triggered_by references an event ID that does not exist."""
        tc = Sample(
            id="TC021", name="Trigger chain", domain="sales", source_type="zapier",
            objects=self._base_objects(),
            steps=['Send email to alice@company.com'],
            modifications=[_mod("email-handler")],
            events=[
                Event(
                    id="E001", call_type="send_event", source="crm",
                    recipient="email-handler", input="Send email to Alice",
                    when="W01-1T09:00",
                    expect=EventExpect(action="Email dispatched", reason="CRM trigger"),
                ),
                Event(
                    id="E002", call_type="send_event", source="slack",
                    recipient="slack-handler",
                    input="New Slack message: email sent to Alice",
                    when="W01-1T09:05",
                    triggered_by="E999",  # does not exist
                    expect=EventExpect(action="Route Slack message to deals team", reason="Triggered by email"),
                ),
            ],
            mock_tools=[],
        )
        issues = find_trigger_reference_errors(tc)
        assert any("E999" in i for i in issues)

    def test_no_issues_for_valid_trigger_chain(self):
        """Valid trigger: target exists, template uses defined args, triggered_by references real event."""
        from src.data.schema import MockToolTrigger
        mock = self._base_mock_tool(triggers=[
            MockToolTrigger(
                target_object_id="slack-handler",
                message_template="Email sent to {to} — subject: {subject}",
                source="slack",
            ),
        ])
        tc = Sample(
            id="TC022", name="Email to Slack chain", domain="sales", source_type="zapier",
            objects=self._base_objects(),
            steps=['Send follow-up to alice@company.com, subject: Q2 deal'],
            modifications=[_mod("email-handler")],
            events=[
                Event(
                    id="E001", call_type="send_event", source="crm",
                    recipient="email-handler", input="Send email to alice@company.com",
                    when="W01-1T09:00",
                    expect=EventExpect(action="Email dispatched", reason="CRM trigger"),
                ),
                Event(
                    id="E002", call_type="send_event", source="slack",
                    recipient="slack-handler",
                    input="New Slack message: email sent to alice@company.com — subject: Q2 deal",
                    when="W01-1T09:05",
                    triggered_by="E001",
                    expect=EventExpect(action="Route Slack message to deals team", reason="Triggered by email"),
                ),
            ],
            mock_tools=[mock],
        )
        assert find_trigger_reference_errors(tc) == []


# ── Tests: Invalid Peer Declarations ─────────────────────────────────────────

class TestInvalidPeerDeclarations:
    """
    Every PeerDecl.object_id must exist in tc.objects.  Dangling peer references
    cause sends to be silently dropped at runtime.

    Note: this validates declared-peer existence, not fan-out completeness
    (behavior implying more targets than declared peers).  Fan-out completeness
    cannot be reliably detected from generated output — it is enforced at the
    prompt level via the Fan-out rule in identify_objects.yaml.
    """

    def test_detects_peer_referencing_nonexistent_object(self):
        """PeerDecl references 'missing-service' which is not in tc.objects."""
        obj = ObjectDef(
            object_id="lead-dispatcher",
            role="Dispatches new leads",
            behavior="Send to hubspot-crm and missing-service.",
            event_sources=["typeform"],
            neighbors=['hubspot-crm', 'missing-service'],
        )
        tc = Sample(
            id="TC023", name="Lead dispatch", domain="sales", source_type="zapier",
            objects=[
                obj,
                ObjectDef(object_id="hubspot-crm", role="HubSpot CRM", behavior="Create contact."),
                # missing-service is NOT in objects
            ],
            steps=['New lead: Jane Smith'],
            modifications=[_mod("lead-dispatcher")],
            events=[_event("lead-dispatcher", "Lead received", "CRM contact created")],
            mock_tools=[],
        )
        issues = find_invalid_peer_declarations(tc)
        assert any("missing-service" in i for i in issues)

    def test_no_issues_when_all_peers_exist(self):
        """All declared peers exist as objects in the test case."""
        obj = ObjectDef(
            object_id="lead-dispatcher",
            role="Dispatches leads",
            behavior="Send to hubspot-crm and slack-sales.",
            event_sources=["typeform"],
            neighbors=['hubspot-crm', 'slack-sales'],
        )
        tc = Sample(
            id="TC024", name="Lead dispatch", domain="sales", source_type="zapier",
            objects=[
                obj,
                ObjectDef(object_id="hubspot-crm", role="HubSpot CRM",         behavior="Create contact."),
                ObjectDef(object_id="slack-sales",  role="Slack sales channel", behavior="Post message."),
            ],
            steps=['New lead: Jane Smith, jane@acme.com'],
            modifications=[_mod("lead-dispatcher")],
            events=[_event("lead-dispatcher", "Lead received", "CRM and Slack notified")],
            mock_tools=[],
        )
        assert find_invalid_peer_declarations(tc) == []


# ── Tests: Peer Graph Dead-Ends ───────────────────────────────────────────────

class TestPeerGraphDeadEnds:
    """
    Entry-point objects must have at least one peer so the chain can propagate.
    """

    def test_detects_entry_point_with_no_peers(self):
        """Entry-point has event_sources but zero peers — chain dead-ends."""
        entry = ObjectDef(
            object_id="ticket-router",
            role="Routes incoming support tickets",
            behavior="Process and route tickets to the support team.",
            event_sources=["zendesk"],
            neighbors=[],
        )
        tc = Sample(
            id="TC025", name="Ticket routing", domain="support", source_type="zapier",
            objects=[entry, ObjectDef(object_id="support-team", role="Handles tickets", behavior="Respond.")],
            steps=['New ticket from customer'],
            modifications=[_mod("ticket-router")],
            events=[_event("ticket-router", "Ticket received", "Ticket routed")],
            mock_tools=[],
        )
        issues = find_peer_graph_dead_ends(tc)
        assert any("ticket-router" in i for i in issues)

    def test_no_issues_when_entry_point_has_peers(self):
        entry = ObjectDef(
            object_id="ticket-router",
            role="Routes incoming support tickets",
            behavior="Send each ticket to support-team.",
            event_sources=["zendesk"],
            neighbors=['support-team'],
        )
        tc = Sample(
            id="TC026", name="Ticket routing", domain="support", source_type="zapier",
            objects=[entry, ObjectDef(object_id="support-team", role="Handles tickets", behavior="Respond.")],
            steps=['New ticket from customer'],
            modifications=[_mod("ticket-router")],
            events=[_event("ticket-router", "Ticket received", "Ticket routed")],
            mock_tools=[],
        )
        assert find_peer_graph_dead_ends(tc) == []

    def test_single_object_not_flagged(self):
        """Solo entry-point with no peers is intentionally self-contained."""
        solo = ObjectDef(
            object_id="notification-sender",
            role="Sends outbound notifications directly",
            behavior="Send the notification email.",
            event_sources=["webhook"],
            neighbors=[],
        )
        tc = Sample(
            id="TC027", name="Notification", domain="comms", source_type="zapier",
            objects=[solo],
            steps=['Send notification to alice@company.com'],
            modifications=[_mod("notification-sender")],
            events=[_event("notification-sender", "Notification triggered", "Email sent")],
            mock_tools=[],
        )
        assert find_peer_graph_dead_ends(tc) == []


# ── Tests: Unreachable Objects ────────────────────────────────────────────────

class TestUnreachableObjects:
    """
    Every non-entry-point object must be reachable from some entry point via
    the peer graph.  Orphaned objects will never receive messages.
    """

    def test_detects_orphan_object(self):
        """'audit-logger' is never a peer of any object — it will never be called."""
        entry = ObjectDef(
            object_id="ticket-router",
            role="Routes tickets",
            behavior="Send to support-team.",
            event_sources=["zendesk"],
            neighbors=['support-team'],
        )
        orphan = ObjectDef(
            object_id="audit-logger",
            role="Logs all ticket events",
            behavior="Record event in audit log.",
        )
        tc = Sample(
            id="TC028", name="Ticket routing", domain="support", source_type="zapier",
            objects=[
                entry,
                ObjectDef(object_id="support-team", role="Handles tickets", behavior="Respond."),
                orphan,
            ],
            steps=['New ticket from customer'],
            modifications=[_mod("ticket-router")],
            events=[_event("ticket-router", "Ticket received", "Ticket handled")],
            mock_tools=[],
        )
        issues = find_unreachable_objects(tc)
        assert any("audit-logger" in i for i in issues)

    def test_no_issues_when_all_objects_reachable(self):
        """All objects are connected via the peer graph."""
        entry = ObjectDef(
            object_id="ticket-router",
            role="Routes tickets",
            behavior="Send to support-team.",
            event_sources=["zendesk"],
            neighbors=['support-team', 'audit-logger'],
        )
        tc = Sample(
            id="TC029", name="Ticket routing", domain="support", source_type="zapier",
            objects=[
                entry,
                ObjectDef(object_id="support-team", role="Handles tickets", behavior="Respond."),
                ObjectDef(object_id="audit-logger",  role="Logs events",     behavior="Record."),
            ],
            steps=['New ticket from customer'],
            modifications=[_mod("ticket-router")],
            events=[_event("ticket-router", "Ticket received", "Ticket handled and logged")],
            mock_tools=[],
        )
        assert find_unreachable_objects(tc) == []

    def test_single_object_not_flagged(self):
        solo = ObjectDef(object_id="sender", role="Sends emails", behavior="Send.", event_sources=["webhook"])
        tc = Sample(
            id="TC030", name="Send", domain="comms", source_type="zapier",
            objects=[solo],
            steps=['Send email'],
            modifications=[_mod("sender")],
            events=[_event("sender", "Triggered", "Email sent")],
            mock_tools=[],
        )
        assert find_unreachable_objects(tc) == []


# ── Tests: Missing Mock Tools ─────────────────────────────────────────────────

class TestMissingMockTools:
    """
    Every _data skill must have a corresponding MockToolDef.  Without it,
    the _data call falls through to PassthroughExecutor which returns '{}',
    stalling request-reply chains.
    """

    def test_detects_missing_data_tool(self):
        """Object has skill 'faq_knowledge_base_data' but no mock tool for it."""
        obj = ObjectDef(
            object_id="faq-knowledge-base",
            role="Answers HR FAQ questions",
            behavior="Use faq_knowledge_base_data to look up answers.",
            event_sources=["slack"],
            skills=["faq_knowledge_base_data"],
        )
        tc = Sample(
            id="TC031", name="FAQ lookup", domain="hr", source_type="zapier",
            objects=[obj],
            steps=['Question about parental leave from Maya Chen'],
            modifications=[_mod("faq-knowledge-base")],
            events=[_event("faq-knowledge-base", "FAQ question received", "Reply with answer")],
            mock_tools=[],  # no mock tool for faq_knowledge_base_data
        )
        issues = find_missing_mock_tools(tc)
        assert any("faq_knowledge_base_data" in i for i in issues)

    def test_no_issues_when_data_tool_is_mocked(self):
        """The _data skill has a corresponding MockToolDef."""
        obj = ObjectDef(
            object_id="faq-knowledge-base",
            role="Answers HR FAQ questions",
            behavior="Use faq_knowledge_base_data to look up answers.",
            event_sources=["slack"],
            skills=["faq_knowledge_base_data"],
        )
        mock = MockToolDef(
            tool_name="faq_knowledge_base_data",
            description="Look up FAQ articles",
            arguments_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            response_template='{"article": "Parental leave is 16 weeks at full pay."}',
        )
        tc = Sample(
            id="TC032", name="FAQ lookup", domain="hr", source_type="zapier",
            objects=[obj],
            steps=['Question about parental leave from Maya Chen'],
            modifications=[_mod("faq-knowledge-base")],
            events=[_event("faq-knowledge-base", "FAQ question received", "Reply with parental leave policy")],
            mock_tools=[mock],
        )
        assert find_missing_mock_tools(tc) == []

    def test_no_issues_for_non_data_skills(self):
        """Skills not containing '_data' are not checked."""
        obj = ObjectDef(
            object_id="email-sender",
            role="Sends emails",
            behavior="Compose and send emails.",
            event_sources=["crm"],
            skills=["compose_email", "validate_address"],
        )
        tc = Sample(
            id="TC033", name="Email sending", domain="comms", source_type="zapier",
            objects=[obj],
            steps=['Send email to alice@company.com'],
            modifications=[_mod("email-sender")],
            events=[_event("email-sender", "Email triggered", "Email sent")],
            mock_tools=[],
        )
        assert find_missing_mock_tools(tc) == []


# ── Tests: Unnatural Identifiers ─────────────────────────────────────────────

class TestUnnaturalIdentifiers:
    """
    Short identifiers are only flagged when they appear in BOTH mock data AND
    event expectations — where judge false-failures are most likely.
    IDs present only in mock data (not in any expectation) are not flagged.
    """

    def _make_mock(self, response_template: str):
        return MockToolDef(
            tool_name="zendesk.get_ticket",
            description="Fetch Zendesk ticket",
            arguments_schema={"type": "object", "properties": {"id": {"type": "string"}}},
            response_template=response_template,
        )

    def _base_tc(self, mock, event_action: str):
        obj = ObjectDef(
            object_id="helpdesk-router",
            role="Routes support tickets",
            behavior="Route ticket to the assigned agent.",
            event_sources=["zendesk"],
        )
        return Sample(
            id="TC034", name="Helpdesk", domain="support", source_type="zapier",
            objects=[obj],
            steps=['New ticket TICK-001'],
            modifications=[_mod("helpdesk-router")],
            events=[Event(
                id="E001", call_type="send_event", source="zendesk",
                recipient="helpdesk-router",
                input="Ticket received",
                when="W01-2T10:00",
                expect=EventExpect(action=event_action, reason="routing rule"),
            )],
            mock_tools=[mock],
        )

    def test_detects_short_slack_id_when_in_expectations(self):
        """U4821 in mock data AND referenced in expectation → flagged."""
        mock = self._make_mock('{"assignee_id": "U4821", "status": "open"}')
        tc   = self._base_tc(mock, "Send DM to U4821 with resolution steps")
        issues = find_unnatural_identifiers(tc)
        assert any("U4821" in i for i in issues)

    def test_detects_short_ticket_id_when_in_expectations(self):
        """PROJ-42 in mock data AND referenced in expectation → flagged."""
        mock = self._make_mock('{"ticket_id": "PROJ-42", "status": "open"}')
        tc   = self._base_tc(mock, "Update ticket PROJ-42 status to resolved")
        issues = find_unnatural_identifiers(tc)
        assert any("PROJ-42" in i for i in issues)

    def test_no_issue_when_short_id_not_in_expectations(self):
        """U4821 in mock data but NOT in any expectation → not flagged (lower risk)."""
        mock = self._make_mock('{"assignee_id": "U4821", "status": "open"}')
        tc   = self._base_tc(mock, "Assign ticket to Jordan Reyes")  # no U4821 here
        assert find_unnatural_identifiers(tc) == []

    def test_no_issue_for_realistic_slack_id(self):
        """Properly formatted Slack user ID (9+ chars) is never flagged."""
        mock = self._make_mock('{"assignee_id": "U01ABCDEF", "status": "open"}')
        tc   = self._base_tc(mock, "Send DM to U01ABCDEF with resolution steps")
        assert find_unnatural_identifiers(tc) == []

    def test_no_issue_for_realistic_ticket_id(self):
        """Properly formatted ticket ID (3+ digit number) is never flagged."""
        mock = self._make_mock('{"ticket_id": "PROJ-1042", "status": "open"}')
        tc   = self._base_tc(mock, "Update ticket PROJ-1042 status to resolved")
        assert find_unnatural_identifiers(tc) == []
