/**
 * openclaw-mock-external
 *
 * OpenClaw plugin that registers mock tools for external systems used in the
 * LNL baseline evaluation. All tool calls are forwarded to a local MockServer
 * (src/data/mock_server.py) which responds with scripted or LLM-generated
 * responses and optionally injects callbacks back into the agent session.
 *
 * Install:
 *   openclaw plugins install /path/to/plugins/openclaw-mock-external --link
 *
 * Configuration (environment variables):
 *   LNL_MOCK_SERVER_URL   — MockServer base URL (default: http://localhost:18888)
 */

import { Type, type TSchema, type Static } from "@sinclair/typebox";

// Inline definePluginEntry — mirrors openclaw/plugin-sdk/plugin-entry without
// requiring the openclaw package to be in the module resolution path at runtime.
type PluginApi = {
  registerTool: (tool: {
    name: string;
    label: string;
    description: string;
    parameters: TSchema;
    execute: (id: string, params: Static<TSchema>, signal?: AbortSignal) => Promise<{ content: Array<{ type: string; text: string }>; details: object }>;
  }) => void;
};
function definePluginEntry(def: {
  id: string;
  name: string;
  description?: string;
  configSchema?: object;
  register: (api: PluginApi) => void | Promise<void>;
}) {
  return { configSchema: {}, ...def };
}

const MOCK_SERVER_URL =
  process.env.LNL_MOCK_SERVER_URL ?? "http://localhost:18888";

// ── Forward a tool call to MockServer ─────────────────────────────────────────

async function forwardToMockServer(
  method: string,
  args: Record<string, unknown>,
  toolCallId: string,
): Promise<string> {
  const body = JSON.stringify({ ...args, __tool_call_id__: toolCallId });

  let resp: Response;
  try {
    resp = await fetch(`${MOCK_SERVER_URL}/tool/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch {
    // MockServer not running — return a safe fallback so the agent can continue
    return `(mock unavailable) ${method} called`;
  }

  if (!resp.ok) {
    return `(mock error ${resp.status}) ${method}`;
  }

  const data = (await resp.json()) as { status: string; result: string };
  return data.result ?? "(no result)";
}

// ── Plugin entry point ────────────────────────────────────────────────────────

export default definePluginEntry({
  id: "lnl-mock-external",
  name: "LNL Mock External Systems",
  description: "Mock tools for Slack, Email, Jira, and Webhook — forwards to local MockServer for LNL baseline evaluation.",

  register(api) {
    // ── Slack ────────────────────────────────────────────────────────────────
    api.registerTool({
      name: "slack_send_message",
      label: "Slack: Send Message",
      description: "Send a message to a Slack channel or user.",
      parameters: Type.Object({
        channel: Type.String({ description: "Channel name (e.g. #deal-desk) or user ID" }),
        message: Type.String({ description: "Message text to send" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("slack_send_message", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "slack_list_channels",
      label: "Slack: List Channels",
      description: "List available Slack channels.",
      parameters: Type.Object({}),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("slack_list_channels", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "slack_add_reaction",
      label: "Slack: Add Reaction",
      description: "Add an emoji reaction to a Slack message.",
      parameters: Type.Object({
        message_id: Type.String({ description: "Slack message ID" }),
        emoji: Type.String({ description: "Emoji name without colons (e.g. white_check_mark)" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("slack_add_reaction", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "slack_get_user",
      label: "Slack: Get User",
      description: "Get Slack user profile information.",
      parameters: Type.Object({
        user: Type.String({ description: "Slack user ID or display name" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("slack_get_user", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Email ────────────────────────────────────────────────────────────────
    api.registerTool({
      name: "email_send",
      label: "Email: Send",
      description: "Send an email to one or more recipients.",
      parameters: Type.Object({
        to: Type.String({ description: "Recipient email address or name" }),
        subject: Type.String({ description: "Email subject line" }),
        body: Type.String({ description: "Email body text" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("email_send", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "email_list_inbox",
      label: "Email: List Inbox",
      description: "List emails in an inbox folder.",
      parameters: Type.Object({
        folder: Type.Optional(Type.String({ description: "Folder name (default: inbox)" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("email_list_inbox", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "email_read",
      label: "Email: Read",
      description: "Read an email by its message ID.",
      parameters: Type.Object({
        message_id: Type.String({ description: "Email message ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("email_read", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Jira ─────────────────────────────────────────────────────────────────
    api.registerTool({
      name: "jira_create_issue",
      label: "Jira: Create Issue",
      description: "Create a new Jira issue.",
      parameters: Type.Object({
        project: Type.String({ description: "Jira project key (e.g. PROJ)" }),
        summary: Type.String({ description: "Issue summary / title" }),
        description: Type.Optional(Type.String({ description: "Issue description" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("jira_create_issue", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "jira_update_issue",
      label: "Jira: Update Issue",
      description: "Update the status of an existing Jira issue.",
      parameters: Type.Object({
        issue_id: Type.String({ description: "Jira issue ID (e.g. PROJ-123)" }),
        status: Type.String({ description: "New status (e.g. In Progress, Done)" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("jira_update_issue", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "jira_get_issue",
      label: "Jira: Get Issue",
      description: "Get details of a Jira issue.",
      parameters: Type.Object({
        issue_id: Type.String({ description: "Jira issue ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("jira_get_issue", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "jira_list_issues",
      label: "Jira: List Issues",
      description: "List Jira issues matching a query.",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "Filter by project key" })),
        status: Type.Optional(Type.String({ description: "Filter by status" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("jira_list_issues", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Zapier Tables ────────────────────────────────────────────────────────
    api.registerTool({
      name: "zapier_tables_create_record",
      label: "Zapier Tables: Create Record",
      description: "Write a new record to a Zapier Table.",
      parameters: Type.Object({
        table: Type.String({ description: "Table name or ID" }),
        data: Type.String({ description: "JSON object of field values to write" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("zapier_tables_create_record", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "zapier_tables_list_records",
      label: "Zapier Tables: List Records",
      description: "List records from a Zapier Table, optionally filtered.",
      parameters: Type.Object({
        table: Type.String({ description: "Table name or ID" }),
        filter: Type.Optional(Type.String({ description: "Filter expression" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("zapier_tables_list_records", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Google Calendar ──────────────────────────────────────────────────────
    api.registerTool({
      name: "calendar_create_event",
      label: "Google Calendar: Create Event",
      description: "Create a new calendar event.",
      parameters: Type.Object({
        title: Type.String({ description: "Event title" }),
        start: Type.String({ description: "Start datetime (ISO 8601)" }),
        end: Type.String({ description: "End datetime (ISO 8601)" }),
        attendees: Type.Optional(Type.String({ description: "Comma-separated attendee emails" })),
        description: Type.Optional(Type.String({ description: "Event description" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("calendar_create_event", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "calendar_update_event",
      label: "Google Calendar: Update Event",
      description: "Update an existing calendar event.",
      parameters: Type.Object({
        event_id: Type.String({ description: "Calendar event ID" }),
        title: Type.Optional(Type.String({ description: "New event title" })),
        start: Type.Optional(Type.String({ description: "New start datetime (ISO 8601)" })),
        end: Type.Optional(Type.String({ description: "New end datetime (ISO 8601)" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("calendar_update_event", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "calendar_get_event",
      label: "Google Calendar: Get Event",
      description: "Get details of a calendar event.",
      parameters: Type.Object({
        event_id: Type.String({ description: "Calendar event ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("calendar_get_event", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "calendar_list_events",
      label: "Google Calendar: List Events",
      description: "List upcoming calendar events.",
      parameters: Type.Object({
        calendar_id: Type.Optional(Type.String({ description: "Calendar ID (default: primary)" })),
        time_min: Type.Optional(Type.String({ description: "Start of time range (ISO 8601)" })),
        time_max: Type.Optional(Type.String({ description: "End of time range (ISO 8601)" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("calendar_list_events", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Stripe ────────────────────────────────────────────────────────────────
    api.registerTool({
      name: "stripe_create_charge",
      label: "Stripe: Create Charge",
      description: "Create a new Stripe charge.",
      parameters: Type.Object({
        amount: Type.Number({ description: "Amount in cents" }),
        currency: Type.String({ description: "Currency code (e.g. usd)" }),
        customer: Type.Optional(Type.String({ description: "Customer ID" })),
        description: Type.Optional(Type.String({ description: "Charge description" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("stripe_create_charge", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "stripe_get_charge",
      label: "Stripe: Get Charge",
      description: "Get details of a Stripe charge.",
      parameters: Type.Object({
        charge_id: Type.String({ description: "Stripe charge ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("stripe_get_charge", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "stripe_list_charges",
      label: "Stripe: List Charges",
      description: "List recent Stripe charges.",
      parameters: Type.Object({
        customer: Type.Optional(Type.String({ description: "Filter by customer ID" })),
        limit: Type.Optional(Type.Number({ description: "Max number of results" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("stripe_list_charges", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "stripe_refund_charge",
      label: "Stripe: Refund Charge",
      description: "Refund a Stripe charge.",
      parameters: Type.Object({
        charge_id: Type.String({ description: "Stripe charge ID to refund" }),
        amount: Type.Optional(Type.Number({ description: "Amount to refund in cents (default: full)" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("stripe_refund_charge", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Monday.com ───────────────────────────────────────────────────────────
    api.registerTool({
      name: "monday_create_item",
      label: "Monday.com: Create Item",
      description: "Create a new item on a Monday.com board.",
      parameters: Type.Object({
        board_id: Type.String({ description: "Monday.com board ID" }),
        item_name: Type.String({ description: "Item name" }),
        column_values: Type.Optional(Type.String({ description: "JSON object of column values" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("monday_create_item", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "monday_update_item",
      label: "Monday.com: Update Item",
      description: "Update an item on a Monday.com board.",
      parameters: Type.Object({
        item_id: Type.String({ description: "Monday.com item ID" }),
        column_values: Type.String({ description: "JSON object of column values to update" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("monday_update_item", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "monday_get_item",
      label: "Monday.com: Get Item",
      description: "Get details of a Monday.com item.",
      parameters: Type.Object({
        item_id: Type.String({ description: "Monday.com item ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("monday_get_item", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "monday_list_items",
      label: "Monday.com: List Items",
      description: "List items on a Monday.com board.",
      parameters: Type.Object({
        board_id: Type.String({ description: "Monday.com board ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("monday_list_items", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Salesforce ───────────────────────────────────────────────────────────
    api.registerTool({
      name: "salesforce_create_record",
      label: "Salesforce: Create Record",
      description: "Create a new Salesforce record.",
      parameters: Type.Object({
        object_type: Type.String({ description: "Salesforce object type (e.g. Lead, Contact, Opportunity)" }),
        fields: Type.String({ description: "JSON object of field values" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("salesforce_create_record", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "salesforce_update_record",
      label: "Salesforce: Update Record",
      description: "Update an existing Salesforce record.",
      parameters: Type.Object({
        object_type: Type.String({ description: "Salesforce object type" }),
        record_id: Type.String({ description: "Salesforce record ID" }),
        fields: Type.String({ description: "JSON object of fields to update" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("salesforce_update_record", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "salesforce_get_record",
      label: "Salesforce: Get Record",
      description: "Get a Salesforce record by ID.",
      parameters: Type.Object({
        object_type: Type.String({ description: "Salesforce object type" }),
        record_id: Type.String({ description: "Salesforce record ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("salesforce_get_record", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "salesforce_list_records",
      label: "Salesforce: List Records",
      description: "Query Salesforce records.",
      parameters: Type.Object({
        object_type: Type.String({ description: "Salesforce object type" }),
        filter: Type.Optional(Type.String({ description: "SOQL WHERE clause" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("salesforce_list_records", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Airtable ─────────────────────────────────────────────────────────────
    api.registerTool({
      name: "airtable_create_record",
      label: "Airtable: Create Record",
      description: "Create a new Airtable record.",
      parameters: Type.Object({
        base_id: Type.String({ description: "Airtable base ID" }),
        table: Type.String({ description: "Table name" }),
        fields: Type.String({ description: "JSON object of field values" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("airtable_create_record", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "airtable_update_record",
      label: "Airtable: Update Record",
      description: "Update an existing Airtable record.",
      parameters: Type.Object({
        base_id: Type.String({ description: "Airtable base ID" }),
        table: Type.String({ description: "Table name" }),
        record_id: Type.String({ description: "Airtable record ID" }),
        fields: Type.String({ description: "JSON object of fields to update" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("airtable_update_record", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "airtable_get_record",
      label: "Airtable: Get Record",
      description: "Get an Airtable record by ID.",
      parameters: Type.Object({
        base_id: Type.String({ description: "Airtable base ID" }),
        table: Type.String({ description: "Table name" }),
        record_id: Type.String({ description: "Airtable record ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("airtable_get_record", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "airtable_list_records",
      label: "Airtable: List Records",
      description: "List records from an Airtable table.",
      parameters: Type.Object({
        base_id: Type.String({ description: "Airtable base ID" }),
        table: Type.String({ description: "Table name" }),
        filter: Type.Optional(Type.String({ description: "Filter formula" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("airtable_list_records", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── HubSpot ───────────────────────────────────────────────────────────────
    api.registerTool({
      name: "hubspot_create_contact",
      label: "HubSpot: Create Contact",
      description: "Create a new HubSpot contact.",
      parameters: Type.Object({
        email: Type.String({ description: "Contact email" }),
        first_name: Type.Optional(Type.String({ description: "First name" })),
        last_name: Type.Optional(Type.String({ description: "Last name" })),
        properties: Type.Optional(Type.String({ description: "JSON object of additional properties" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("hubspot_create_contact", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "hubspot_update_contact",
      label: "HubSpot: Update Contact",
      description: "Update a HubSpot contact.",
      parameters: Type.Object({
        contact_id: Type.String({ description: "HubSpot contact ID" }),
        properties: Type.String({ description: "JSON object of properties to update" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("hubspot_update_contact", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "hubspot_create_deal",
      label: "HubSpot: Create Deal",
      description: "Create a new HubSpot deal.",
      parameters: Type.Object({
        deal_name: Type.String({ description: "Deal name" }),
        amount: Type.Optional(Type.Number({ description: "Deal amount" })),
        stage: Type.Optional(Type.String({ description: "Deal stage" })),
        contact_id: Type.Optional(Type.String({ description: "Associated contact ID" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("hubspot_create_deal", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "hubspot_update_deal",
      label: "HubSpot: Update Deal",
      description: "Update a HubSpot deal.",
      parameters: Type.Object({
        deal_id: Type.String({ description: "HubSpot deal ID" }),
        properties: Type.String({ description: "JSON object of properties to update" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("hubspot_update_deal", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "hubspot_get_deal",
      label: "HubSpot: Get Deal",
      description: "Get details of a HubSpot deal.",
      parameters: Type.Object({
        deal_id: Type.String({ description: "HubSpot deal ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("hubspot_get_deal", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── GitHub ───────────────────────────────────────────────────────────────
    api.registerTool({
      name: "github_create_issue",
      label: "GitHub: Create Issue",
      description: "Create a new GitHub issue.",
      parameters: Type.Object({
        repo: Type.String({ description: "Repository in owner/repo format" }),
        title: Type.String({ description: "Issue title" }),
        body: Type.Optional(Type.String({ description: "Issue body" })),
        labels: Type.Optional(Type.String({ description: "Comma-separated labels" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("github_create_issue", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "github_update_issue",
      label: "GitHub: Update Issue",
      description: "Update a GitHub issue.",
      parameters: Type.Object({
        repo: Type.String({ description: "Repository in owner/repo format" }),
        issue_number: Type.Number({ description: "Issue number" }),
        title: Type.Optional(Type.String({ description: "New title" })),
        state: Type.Optional(Type.String({ description: "open or closed" })),
        body: Type.Optional(Type.String({ description: "New body" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("github_update_issue", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "github_get_issue",
      label: "GitHub: Get Issue",
      description: "Get details of a GitHub issue.",
      parameters: Type.Object({
        repo: Type.String({ description: "Repository in owner/repo format" }),
        issue_number: Type.Number({ description: "Issue number" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("github_get_issue", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "github_list_issues",
      label: "GitHub: List Issues",
      description: "List GitHub issues.",
      parameters: Type.Object({
        repo: Type.String({ description: "Repository in owner/repo format" }),
        state: Type.Optional(Type.String({ description: "Filter by state: open, closed, all" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("github_list_issues", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Google Sheets ─────────────────────────────────────────────────────────
    api.registerTool({
      name: "sheets_create_row",
      label: "Google Sheets: Create Row",
      description: "Append a new row to a Google Sheets spreadsheet.",
      parameters: Type.Object({
        spreadsheet_id: Type.String({ description: "Google Sheets spreadsheet ID" }),
        sheet: Type.Optional(Type.String({ description: "Sheet name (default: Sheet1)" })),
        values: Type.String({ description: "JSON array of cell values for the row" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("sheets_create_row", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "sheets_update_row",
      label: "Google Sheets: Update Row",
      description: "Update a row in a Google Sheets spreadsheet.",
      parameters: Type.Object({
        spreadsheet_id: Type.String({ description: "Google Sheets spreadsheet ID" }),
        row: Type.Number({ description: "Row number (1-indexed)" }),
        values: Type.String({ description: "JSON array of new cell values" }),
        sheet: Type.Optional(Type.String({ description: "Sheet name" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("sheets_update_row", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "sheets_get_row",
      label: "Google Sheets: Get Row",
      description: "Get a row from a Google Sheets spreadsheet.",
      parameters: Type.Object({
        spreadsheet_id: Type.String({ description: "Google Sheets spreadsheet ID" }),
        row: Type.Number({ description: "Row number (1-indexed)" }),
        sheet: Type.Optional(Type.String({ description: "Sheet name" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("sheets_get_row", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "sheets_list_rows",
      label: "Google Sheets: List Rows",
      description: "List rows from a Google Sheets spreadsheet.",
      parameters: Type.Object({
        spreadsheet_id: Type.String({ description: "Google Sheets spreadsheet ID" }),
        sheet: Type.Optional(Type.String({ description: "Sheet name" })),
        max_rows: Type.Optional(Type.Number({ description: "Maximum number of rows to return" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("sheets_list_rows", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Asana ────────────────────────────────────────────────────────────────
    api.registerTool({
      name: "asana_create_task",
      label: "Asana: Create Task",
      description: "Create a new Asana task.",
      parameters: Type.Object({
        project_id: Type.String({ description: "Asana project ID" }),
        name: Type.String({ description: "Task name" }),
        notes: Type.Optional(Type.String({ description: "Task description" })),
        assignee: Type.Optional(Type.String({ description: "Assignee user ID or email" })),
        due_on: Type.Optional(Type.String({ description: "Due date (YYYY-MM-DD)" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("asana_create_task", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "asana_update_task",
      label: "Asana: Update Task",
      description: "Update an Asana task.",
      parameters: Type.Object({
        task_id: Type.String({ description: "Asana task ID" }),
        name: Type.Optional(Type.String({ description: "New task name" })),
        completed: Type.Optional(Type.Boolean({ description: "Mark as completed" })),
        notes: Type.Optional(Type.String({ description: "New description" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("asana_update_task", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "asana_get_task",
      label: "Asana: Get Task",
      description: "Get details of an Asana task.",
      parameters: Type.Object({
        task_id: Type.String({ description: "Asana task ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("asana_get_task", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "asana_list_tasks",
      label: "Asana: List Tasks",
      description: "List tasks in an Asana project.",
      parameters: Type.Object({
        project_id: Type.String({ description: "Asana project ID" }),
        completed: Type.Optional(Type.Boolean({ description: "Filter by completion status" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("asana_list_tasks", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Notion ───────────────────────────────────────────────────────────────
    api.registerTool({
      name: "notion_create_page",
      label: "Notion: Create Page",
      description: "Create a new Notion page.",
      parameters: Type.Object({
        parent_id: Type.String({ description: "Parent page or database ID" }),
        title: Type.String({ description: "Page title" }),
        content: Type.Optional(Type.String({ description: "Page content (markdown)" })),
        properties: Type.Optional(Type.String({ description: "JSON object of database properties" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("notion_create_page", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "notion_update_page",
      label: "Notion: Update Page",
      description: "Update a Notion page.",
      parameters: Type.Object({
        page_id: Type.String({ description: "Notion page ID" }),
        title: Type.Optional(Type.String({ description: "New page title" })),
        properties: Type.Optional(Type.String({ description: "JSON object of properties to update" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("notion_update_page", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "notion_get_page",
      label: "Notion: Get Page",
      description: "Get a Notion page by ID.",
      parameters: Type.Object({
        page_id: Type.String({ description: "Notion page ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("notion_get_page", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "notion_query_database",
      label: "Notion: Query Database",
      description: "Query a Notion database.",
      parameters: Type.Object({
        database_id: Type.String({ description: "Notion database ID" }),
        filter: Type.Optional(Type.String({ description: "JSON filter object" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("notion_query_database", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Twilio ───────────────────────────────────────────────────────────────
    api.registerTool({
      name: "twilio_send_sms",
      label: "Twilio: Send SMS",
      description: "Send an SMS message via Twilio.",
      parameters: Type.Object({
        to: Type.String({ description: "Recipient phone number (E.164 format)" }),
        message: Type.String({ description: "SMS message text" }),
        from: Type.Optional(Type.String({ description: "Sender phone number" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("twilio_send_sms", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "twilio_send_message",
      label: "Twilio: Send Message",
      description: "Send a message via Twilio (SMS or WhatsApp).",
      parameters: Type.Object({
        to: Type.String({ description: "Recipient phone number or WhatsApp address" }),
        message: Type.String({ description: "Message text" }),
        channel: Type.Optional(Type.String({ description: "Channel: sms or whatsapp" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("twilio_send_message", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Webhook ──────────────────────────────────────────────────────────────
    api.registerTool({
      name: "webhook_post",
      label: "Webhook: POST",
      description: "Send an HTTP POST to an external webhook URL.",
      parameters: Type.Object({
        url: Type.String({ description: "Webhook destination URL" }),
        payload: Type.Optional(Type.String({ description: "JSON payload body" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("webhook_post", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── OpenAI ───────────────────────────────────────────────────────────────
    api.registerTool({
      name: "openai_chat_completion",
      label: "OpenAI: Chat Completion",
      description: "Invoke the OpenAI/ChatGPT API to generate a text completion.",
      parameters: Type.Object({
        prompt: Type.String({ description: "The prompt or user message to send" }),
        model: Type.Optional(Type.String({ description: "Model name (default: gpt-4o)" })),
        system: Type.Optional(Type.String({ description: "System prompt" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("openai_chat_completion", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "openai_generate_audio",
      label: "OpenAI: Generate Audio",
      description: "Invoke the OpenAI voice/TTS API to generate an audio file from text.",
      parameters: Type.Object({
        text: Type.String({ description: "The text to convert to speech" }),
        voice: Type.Optional(Type.String({ description: "Voice name (e.g. alloy, echo, nova)" })),
        format: Type.Optional(Type.String({ description: "Output format: mp3, wav (default: mp3)" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("openai_generate_audio", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "openai_text_to_speech",
      label: "OpenAI: Text to Speech",
      description: "Convert text to speech using OpenAI TTS.",
      parameters: Type.Object({
        input: Type.String({ description: "Text to synthesize" }),
        voice: Type.Optional(Type.String({ description: "Voice name" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("openai_text_to_speech", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Google Drive ─────────────────────────────────────────────────────────
    api.registerTool({
      name: "drive_upload_file",
      label: "Google Drive: Upload File",
      description: "Upload a file to Google Drive.",
      parameters: Type.Object({
        filename: Type.String({ description: "File name including extension" }),
        content: Type.Optional(Type.String({ description: "File content or base64-encoded binary" })),
        folder_id: Type.Optional(Type.String({ description: "Destination folder ID" })),
        mime_type: Type.Optional(Type.String({ description: "MIME type of the file" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("drive_upload_file", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "drive_create_file",
      label: "Google Drive: Create File",
      description: "Create a new file in Google Drive.",
      parameters: Type.Object({
        filename: Type.String({ description: "File name" }),
        content: Type.Optional(Type.String({ description: "File content" })),
        folder_id: Type.Optional(Type.String({ description: "Parent folder ID" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("drive_create_file", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "drive_get_file",
      label: "Google Drive: Get File",
      description: "Get metadata for a Google Drive file.",
      parameters: Type.Object({
        file_id: Type.String({ description: "Google Drive file ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("drive_get_file", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "drive_list_files",
      label: "Google Drive: List Files",
      description: "List files in a Google Drive folder.",
      parameters: Type.Object({
        folder_id: Type.Optional(Type.String({ description: "Folder ID to list (default: root)" })),
        query: Type.Optional(Type.String({ description: "Search query" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("drive_list_files", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Zendesk ──────────────────────────────────────────────────────────────
    api.registerTool({
      name: "zendesk_create_ticket",
      label: "Zendesk: Create Ticket",
      description: "Create a new Zendesk support ticket.",
      parameters: Type.Object({
        subject: Type.String({ description: "Ticket subject" }),
        description: Type.String({ description: "Ticket description" }),
        priority: Type.Optional(Type.String({ description: "Priority: low, normal, high, urgent" })),
        requester_email: Type.Optional(Type.String({ description: "Requester email" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("zendesk_create_ticket", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "zendesk_update_ticket",
      label: "Zendesk: Update Ticket",
      description: "Update the status or fields of a Zendesk ticket.",
      parameters: Type.Object({
        ticket_id: Type.String({ description: "Zendesk ticket ID" }),
        status: Type.Optional(Type.String({ description: "New status: open, pending, solved, closed" })),
        comment: Type.Optional(Type.String({ description: "Comment to add" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("zendesk_update_ticket", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "zendesk_add_note",
      label: "Zendesk: Add Internal Note",
      description: "Add an internal note to a Zendesk ticket.",
      parameters: Type.Object({
        ticket_id: Type.String({ description: "Zendesk ticket ID" }),
        note: Type.String({ description: "Internal note text" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("zendesk_add_note", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "zendesk_get_ticket",
      label: "Zendesk: Get Ticket",
      description: "Get details of a Zendesk ticket.",
      parameters: Type.Object({
        ticket_id: Type.String({ description: "Zendesk ticket ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("zendesk_get_ticket", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Intercom ─────────────────────────────────────────────────────────────
    api.registerTool({
      name: "intercom_send_message",
      label: "Intercom: Send Message",
      description: "Send a message in an Intercom conversation.",
      parameters: Type.Object({
        conversation_id: Type.String({ description: "Intercom conversation ID" }),
        message: Type.String({ description: "Message text to send" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("intercom_send_message", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "intercom_reply_conversation",
      label: "Intercom: Reply to Conversation",
      description: "Reply to an Intercom conversation.",
      parameters: Type.Object({
        conversation_id: Type.String({ description: "Intercom conversation ID" }),
        reply: Type.String({ description: "Reply text" }),
        reply_type: Type.Optional(Type.String({ description: "Reply type: comment or note" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("intercom_reply_conversation", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "intercom_get_conversation",
      label: "Intercom: Get Conversation",
      description: "Get details of an Intercom conversation.",
      parameters: Type.Object({
        conversation_id: Type.String({ description: "Intercom conversation ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("intercom_get_conversation", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Pipedrive ─────────────────────────────────────────────────────────────
    api.registerTool({
      name: "pipedrive_create_deal",
      label: "Pipedrive: Create Deal",
      description: "Create a new deal in Pipedrive.",
      parameters: Type.Object({
        title: Type.String({ description: "Deal title" }),
        value: Type.Optional(Type.Number({ description: "Deal value" })),
        currency: Type.Optional(Type.String({ description: "Currency code (e.g. USD)" })),
        stage: Type.Optional(Type.String({ description: "Pipeline stage name" })),
        person_name: Type.Optional(Type.String({ description: "Associated person name" })),
        organization: Type.Optional(Type.String({ description: "Associated organization name" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("pipedrive_create_deal", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "pipedrive_update_deal",
      label: "Pipedrive: Update Deal",
      description: "Update an existing Pipedrive deal.",
      parameters: Type.Object({
        deal_id: Type.String({ description: "Pipedrive deal ID" }),
        title: Type.Optional(Type.String({ description: "New deal title" })),
        stage: Type.Optional(Type.String({ description: "New stage" })),
        status: Type.Optional(Type.String({ description: "New status" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("pipedrive_update_deal", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "pipedrive_get_deal",
      label: "Pipedrive: Get Deal",
      description: "Get details of a Pipedrive deal.",
      parameters: Type.Object({
        deal_id: Type.String({ description: "Pipedrive deal ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("pipedrive_get_deal", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── Google Contacts ───────────────────────────────────────────────────────
    api.registerTool({
      name: "google_contacts_create",
      label: "Google Contacts: Create Contact",
      description: "Create a new Google Contact.",
      parameters: Type.Object({
        name: Type.String({ description: "Contact full name" }),
        email: Type.Optional(Type.String({ description: "Email address" })),
        phone: Type.Optional(Type.String({ description: "Phone number" })),
        notes: Type.Optional(Type.String({ description: "Additional notes" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("google_contacts_create", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "google_contacts_update",
      label: "Google Contacts: Update Contact",
      description: "Update an existing Google Contact.",
      parameters: Type.Object({
        resource_name: Type.String({ description: "Contact resource name (e.g. people/123)" }),
        name: Type.Optional(Type.String({ description: "New display name" })),
        email: Type.Optional(Type.String({ description: "New email address" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("google_contacts_update", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "google_contacts_get",
      label: "Google Contacts: Get Contact",
      description: "Get a Google Contact by resource name.",
      parameters: Type.Object({
        resource_name: Type.String({ description: "Contact resource name" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("google_contacts_get", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── ClickUp ───────────────────────────────────────────────────────────────
    api.registerTool({
      name: "clickup_create_task",
      label: "ClickUp: Create Task",
      description: "Create a new task in ClickUp.",
      parameters: Type.Object({
        name: Type.String({ description: "Task name" }),
        space: Type.Optional(Type.String({ description: "Space or list name" })),
        description: Type.Optional(Type.String({ description: "Task description" })),
        priority: Type.Optional(Type.String({ description: "Priority: urgent, high, normal, low" })),
        assignees: Type.Optional(Type.String({ description: "Comma-separated assignee names or IDs" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("clickup_create_task", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "clickup_update_task",
      label: "ClickUp: Update Task",
      description: "Update an existing ClickUp task.",
      parameters: Type.Object({
        task_id: Type.String({ description: "ClickUp task ID" }),
        status: Type.Optional(Type.String({ description: "New status" })),
        name: Type.Optional(Type.String({ description: "New task name" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("clickup_update_task", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "clickup_get_task",
      label: "ClickUp: Get Task",
      description: "Get details of a ClickUp task.",
      parameters: Type.Object({
        task_id: Type.String({ description: "ClickUp task ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("clickup_get_task", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "clickup_add_comment",
      label: "ClickUp: Add Comment",
      description: "Add a comment to a ClickUp task.",
      parameters: Type.Object({
        task_id: Type.String({ description: "ClickUp task ID" }),
        comment: Type.String({ description: "Comment text" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("clickup_add_comment", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    // ── DocuSign ──────────────────────────────────────────────────────────────
    api.registerTool({
      name: "docusign_create_envelope",
      label: "DocuSign: Create Envelope",
      description: "Create and send a DocuSign envelope for signature.",
      parameters: Type.Object({
        subject: Type.String({ description: "Email subject for the envelope" }),
        document_name: Type.Optional(Type.String({ description: "Document name" })),
        signer_email: Type.Optional(Type.String({ description: "Signer email address" })),
        signer_name: Type.Optional(Type.String({ description: "Signer name" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("docusign_create_envelope", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "docusign_generate_document",
      label: "DocuSign: Generate Document",
      description: "Generate a formatted document via DocuSign.",
      parameters: Type.Object({
        template: Type.String({ description: "Document template name" }),
        fields: Type.Optional(Type.String({ description: "JSON object of field values to fill in" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("docusign_generate_document", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "docusign_create_review",
      label: "DocuSign: Create Review Item",
      description: "Create a review/approval item in DocuSign for a document.",
      parameters: Type.Object({
        envelope_id: Type.String({ description: "DocuSign envelope ID" }),
        reviewer_email: Type.Optional(Type.String({ description: "Reviewer email address" })),
        reviewer_name: Type.Optional(Type.String({ description: "Reviewer name" })),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("docusign_create_review", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });

    api.registerTool({
      name: "docusign_get_envelope",
      label: "DocuSign: Get Envelope",
      description: "Get the status and details of a DocuSign envelope.",
      parameters: Type.Object({
        envelope_id: Type.String({ description: "DocuSign envelope ID" }),
      }),
      async execute(toolCallId, params) {
        const result = await forwardToMockServer("docusign_get_envelope", params, toolCallId);
        return { content: [{ type: "text", text: result }], details: {} };
      },
    });
  },
});
