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
  },
});
