"""Enrich tool response_template for chronic-failure mock tools.

The default response_template for most tools is {"status":"success"} with
no values — when downstream steps need to thread a returned id/url/key,
the agent has no real value and resorts to fabrication. This script
replaces those stub templates with realistic payloads containing the
fields downstream steps actually consume.

The mock-engine interpolates {call_index} and tool-arg names into
templates (see src/lnl/tools.py). We use only {call_index} so the
templates are robust regardless of which args a particular call carries.

Only tools whose responses are CONSUMED by downstream steps are
enriched. Tools whose returns are ignored (e.g. send_email, post_slack_message
as terminal actions) get richer responses too, since enriched responses
add no risk for those.

Modifies data/zapier/workflows-mods.jsonl in place (the file under the
symlink, outputs/data/zapier/20260522_rev/workflows-mods.jsonl). Backup
already at workflows-mods.jsonl.bak_pre_mock_enrich.
"""
import json
import os
import sys

# tool_name → enriched response_template. Templates may use {call_index}
# (always available; integer). Keys here are exact tool_name matches.
# Order matters only for the rare case where a tool_name appears under
# two different shapes — last write wins.
ENRICHMENTS: dict[str, str] = {
    # Jira ─────────────────────────────────────────────────────────────
    "create_jira_issue": (
        '{{"status":"success","issue_key":"ITHELP-{call_index:04d}",'
        '"issue_url":"https://jira.example.com/browse/ITHELP-{call_index:04d}",'
        '"issue_id":"jira-{call_index:08d}"}}'
    ),
    "route_jira_issue_to_board": (
        '{{"status":"success","board_id":"board-{call_index:04d}",'
        '"routed_at":"2024-01-01T00:00:00Z"}}'
    ),
    # Google Drive ─────────────────────────────────────────────────────
    "upload_google_drive_file": (
        '{{"status":"success","file_id":"1Drv{call_index:08d}",'
        '"file_url":"https://drive.google.com/file/d/1Drv{call_index:08d}/view",'
        '"upload_timestamp":"2024-01-01T00:00:00Z"}}'
    ),
    "create_google_doc": (
        '{{"status":"success","doc_id":"1Doc{call_index:08d}",'
        '"doc_url":"https://docs.google.com/document/d/1Doc{call_index:08d}/edit"}}'
    ),
    # Google Sheets ────────────────────────────────────────────────────
    "append_sheet_row": (
        '{{"status":"success","row_index":{call_index},'
        '"row_id":"row-{call_index:04d}"}}'
    ),
    "update_active_leads_sheet": (
        '{{"status":"success","row_id":"row-{call_index:04d}",'
        '"updated_at":"2024-01-01T00:00:00Z"}}'
    ),
    "update_sheet_row": (
        '{{"status":"success","row_id":"row-{call_index:04d}",'
        '"updated_at":"2024-01-01T00:00:00Z"}}'
    ),
    "update_google_sheet": (
        '{{"status":"success","row_id":"row-{call_index:04d}",'
        '"updated_at":"2024-01-01T00:00:00Z"}}'
    ),
    # Airtable ─────────────────────────────────────────────────────────
    "create_airtable_record": (
        '{{"status":"success","record_id":"rec{call_index:08d}",'
        '"created_at":"2024-01-01T00:00:00Z"}}'
    ),
    "update_airtable_record": (
        '{{"status":"success","record_id":"rec{call_index:08d}",'
        '"updated_at":"2024-01-01T00:00:00Z"}}'
    ),
    "write_airtable_record": (
        '{{"status":"success","record_id":"rec{call_index:08d}",'
        '"created_at":"2024-01-01T00:00:00Z"}}'
    ),
    "create_airtable_generated_audio_record": (
        '{{"status":"success","record_id":"recAudio{call_index:08d}",'
        '"created_at":"2024-01-01T00:00:00Z"}}'
    ),
    "store_leave_request": (
        '{{"status":"success","record_id":"recLeave{call_index:08d}"}}'
    ),
    "create_pending_submission_record": (
        '{{"status":"success","record_id":"recPend{call_index:08d}"}}'
    ),
    "write_blog_post": (
        '{{"status":"success","record_id":"recPost{call_index:08d}",'
        '"post_url":"https://supabase.example.com/post/{call_index:08d}"}}'
    ),
    # Asana ────────────────────────────────────────────────────────────
    "create_asana_task": (
        '{{"status":"success","task_id":"asana-{call_index:08d}",'
        '"task_url":"https://app.asana.com/0/0/asana-{call_index:08d}"}}'
    ),
    "attach_asana_file": (
        '{{"status":"success","attachment_id":"att-{call_index:08d}"}}'
    ),
    "update_asana_project": (
        '{{"status":"success","project_update_id":"upd-{call_index:04d}",'
        '"updated_at":"2024-01-01T00:00:00Z"}}'
    ),
    "create_review_task": (
        '{{"status":"success","task_id":"task-{call_index:04d}"}}'
    ),
    # Notion ───────────────────────────────────────────────────────────
    "create_notion_database_item": (
        '{{"status":"success","page_id":"notion-{call_index:08d}",'
        '"page_url":"https://notion.so/notion-{call_index:08d}"}}'
    ),
    "write_notion_record": (
        '{{"status":"success","page_id":"notion-{call_index:08d}",'
        '"page_url":"https://notion.so/notion-{call_index:08d}"}}'
    ),
    # HubSpot ──────────────────────────────────────────────────────────
    "create_hubspot_contact": (
        '{{"status":"success","contact_id":"hs-contact-{call_index:08d}"}}'
    ),
    "update_hubspot_record": (
        '{{"status":"success","record_id":"hs-{call_index:08d}",'
        '"updated_at":"2024-01-01T00:00:00Z"}}'
    ),
    "create_pipedrive_deal": (
        '{{"status":"success","deal_id":"pd-deal-{call_index:08d}"}}'
    ),
    # GitHub ───────────────────────────────────────────────────────────
    "create_github_issue": (
        '{{"status":"success","issue_number":{call_index},'
        '"issue_url":"https://github.com/example/repo/issues/{call_index}"}}'
    ),
    # Other ────────────────────────────────────────────────────────────
    "publish_claim_link": (
        '{{"status":"success","claim_link_id":"claim-{call_index:08d}"}}'
    ),
    "create_openai_voice_generation": (
        '{{"status":"success","generation_id":"gen-{call_index:08d}",'
        '"audio_url":"https://openai.example.com/audio/{call_index:08d}.mp3"}}'
    ),
    "record_jotform_submission": (
        '{{"status":"success","audit_id":"audit-{call_index:08d}"}}'
    ),
    "insert_support_ticket": (
        '{{"status":"success","ticket_id":"TKT-{call_index:04d}",'
        '"created_at":"2024-01-01T00:00:00Z"}}'
    ),
    "record_audit_entry": (
        '{{"status":"success","audit_id":"audit-{call_index:08d}"}}'
    ),
    "log_audit_event": (
        '{{"status":"success","audit_id":"audit-{call_index:08d}"}}'
    ),
    "append_or_update_request_record": (
        '{{"status":"success","record_id":"req-{call_index:08d}",'
        '"updated_at":"2024-01-01T00:00:00Z"}}'
    ),
    "create_pending_submission_record": (
        '{{"status":"success","record_id":"recPend{call_index:08d}"}}'
    ),
    "send_telegram_message": (
        '{{"status":"success","message_id":"tg-{call_index:08d}"}}'
    ),
    # Generation tools (R2 — Cat 2: stub generation responses left
    # downstream Slack/email steps with no content to share, causing
    # judge failures even though the workflow ran end-to-end). Embed
    # realistic synthesized content keyed off {call_index} so each call
    # returns a distinct payload.
    "generate_meeting_brief": (
        '{{"status":"success","brief_id":"brief-{call_index:08d}",'
        '"brief":"Meeting brief #{call_index}: Account overview — recent '
        'engagement strong with quarterly product usage up ~12%. '
        'Stakeholders to engage: primary contact + executive sponsor. '
        'Recommended discussion: review current quarter priorities, '
        'surface blockers, validate renewal timeline. Key questions: '
        '(1) What are the top three priorities this quarter? '
        '(2) Are there outstanding integration or support issues? '
        '(3) When does the procurement cycle re-open? '
        'Recent notes from Salesforce and Clearbit are attached as '
        'context. Suggested next step: schedule a 30-minute follow-up '
        'within the next 7 business days.",'
        '"prompt_tokens":520,"completion_tokens":210}}'
    ),
    "call_gpt_4o_classify_message": (
        '{{"status":"success","classification_id":"cls-{call_index:08d}",'
        '"category":"actionable","subcategory":"customer-support",'
        '"confidence":0.86,"reasoning":"Message references a specific '
        'product issue and requests a follow-up action, matching the '
        '\\"actionable\\" rubric definition.",'
        '"prompt_tokens":180,"completion_tokens":40}}'
    ),
    "call_gpt_4o_summarize_message": (
        '{{"status":"success","summary_id":"sum-{call_index:08d}",'
        '"summary":"Sender raised a concrete request needing follow-up '
        'and provided context to act on; the recipient should respond '
        'with next steps and an ETA.",'
        '"prompt_tokens":210,"completion_tokens":55}}'
    ),
    "generate_image": (
        '{{"status":"success","image_id":"img-{call_index:08d}",'
        '"image_url":"https://cdn.example.com/generated/{call_index:08d}.png",'
        '"thumbnail_url":"https://cdn.example.com/generated/{call_index:08d}_thumb.png",'
        '"width":1024,"height":1024}}'
    ),
}

STUB_RE_FULL = '{"status": "success", "tool":'  # marker for stub responses


def main():
    target = os.path.realpath("data/zapier/workflows-mods.jsonl")
    print(f"Loading {target}")
    lines_in = []
    with open(target) as f:
        for line in f:
            lines_in.append(line.rstrip("\n"))
    print(f"  {len(lines_in)} TCs loaded")

    tool_hits = 0
    tc_hits = 0
    lines_out = []
    for raw in lines_in:
        d = json.loads(raw)
        changed = False
        for t in d.get("tools", []):
            name = t.get("tool_name")
            tmpl = t.get("response_template", "")
            if name in ENRICHMENTS and STUB_RE_FULL in tmpl:
                t["response_template"] = ENRICHMENTS[name]
                tool_hits += 1
                changed = True
        if changed:
            tc_hits += 1
        lines_out.append(json.dumps(d, ensure_ascii=False))

    with open(target, "w") as f:
        for line in lines_out:
            f.write(line + "\n")

    print(f"Enriched {tool_hits} tool response_templates across {tc_hits} TCs.")
    print(f"Tools that got enriched (occurrence counts):")
    # Recount by tool to verify
    counts = {}
    with open(target) as f:
        for line in f:
            d = json.loads(line)
            for t in d.get("tools", []):
                n = t.get("tool_name")
                if n in ENRICHMENTS and t.get("response_template") == ENRICHMENTS[n]:
                    counts[n] = counts.get(n, 0) + 1
    for n in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {n}: {counts[n]} TCs")


if __name__ == "__main__":
    main()
