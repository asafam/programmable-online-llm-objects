# TCs with peer-behavior mismatches

**Definition:** an object's `behavior` text references another object's `object_id` by exact name, but that object_id is NOT in its `peers` list. The model cannot dispatch to peers it doesn't have declared, so these TCs are partially or fully unsolvable by any prompt or runtime change.

## Magnitude

- **Evaluated subset (83 TCs):** 37 flagged (45%)
- **Full workflows-mods.jsonl (498 TCs):** 227 flagged (46%)

## By pass-rate band (evaluated, 2-run mean)

| Band | Flagged | Total | % flagged |
|---|---|---|---|
| full_pass | 14 | 37 | 37.83783783783784% |
| partial | 17 | 27 | 62.96296296296296% |
| hard_fail | 6 | 19 | 31.57894736842105% |

The partial band has the strongest correlation (63%) — these are TCs where the model partially works around the missing peer but loses some events.

## Flagged TCs (evaluated only, sorted by pass_rate ascending)


### hard_fail

**`linkedin-conversion-tracking-for-physical-stores-contextual-TC001`** (pr=0.00, 3 mismatches)
  - `[lead-conversion-tracking]` behavior references `linkedin-leads` but it is NOT in peers
  - `[lead-conversion-tracking]` behavior references `zapier-interface` but it is NOT in peers
  - `[zapier-interface]` behavior references `zapier-tables` but it is NOT in peers

**`automated-blog-content-generator-claude-ai-temporal-TC001`** (pr=0.00, 2 mismatches)
  - `[blog-publisher]` behavior references `content-generation` but it is NOT in peers
  - `[blog-publisher]` behavior references `image-generation` but it is NOT in peers

**`automate-github-issues-from-slack-temporal-TC001`** (pr=0.00, 2 mismatches)
  - `[issue-formatter]` behavior references `github-issues` but it is NOT in peers
  - `[issue-formatter]` behavior references `slack-notifications` but it is NOT in peers

**`ai-agent-marketing-campaign-tracker-temporal-TC001`** (pr=0.00, 2 mismatches)
  - `[campaign-performance]` behavior references `facebook-ads` but it is NOT in peers
  - `[campaign-performance]` behavior references `linkedin-ads` but it is NOT in peers

**`engineering-work-intake-slack-jira-exception-TC001`** (pr=0.00, 1 mismatch)
  - `[request-triage]` behavior references `slack-requests` but it is NOT in peers

**`employment-verification-letter-automation-business-travel-temporal-TC001`** (pr=0.00, 1 mismatch)
  - `[verification-letter-policy]` behavior references `hr-portal` but it is NOT in peers


### partial

**`document-approval-temporal-TC001`** (pr=0.25, 1 mismatch)
  - `[approval-policy]` behavior references `approval-action-receiver` but it is NOT in peers

**`project-management-stakeholder-communications-contextual-TC001`** (pr=0.25, 1 mismatch)
  - `[stakeholder-routing]` behavior references `slack-approval` but it is NOT in peers

**`automate-hr-support-ai-helpdesk-assistant-temporal-TC001`** (pr=0.50, 2 mismatches)
  - `[hr-triage]` behavior references `hr-slack-channel` but it is NOT in peers
  - `[faq-knowledge-base]` behavior references `hr-triage` but it is NOT in peers

**`it-helpdesk-contextual-TC001`** (pr=0.50, 2 mismatches)
  - `[helpdesk-triage]` behavior references `it-helpdesk-channel` but it is NOT in peers
  - `[helpdesk-triage]` behavior references `jira-tickets` but it is NOT in peers

**`automate-employment-verification-letters-temporal-TC001`** (pr=0.50, 2 mismatches)
  - `[verification-letter-policy]` behavior references `hr-portal` but it is NOT in peers
  - `[verification-letter-policy]` behavior references `bamboohr-approval` but it is NOT in peers

**`ai-voice-generator-temporal-TC001`** (pr=0.50, 1 mismatch)
  - `[audio-generation-pipeline]` behavior references `typeform-intake` but it is NOT in peers

**`applicant-tracker-temporal-TC001`** (pr=0.50, 1 mismatch)
  - `[applicant-tracker]` behavior references `status-change-events` but it is NOT in peers

**`expenses-tracker-exception-TC001`** (pr=0.50, 1 mismatch)
  - `[expense-processing]` behavior references `expense-tracker` but it is NOT in peers

**`user-research-customer-interview-signup-temporal-TC001`** (pr=0.50, 1 mismatch)
  - `[interview-signup-policy]` behavior references `interview-sessions` but it is NOT in peers

**`product-feedback-contextual-TC001`** (pr=0.50, 1 mismatch)
  - `[feedback-processor]` behavior references `product-feedback-table` but it is NOT in peers

**`employee-onboarding-custom-ai-chatbot-contextual-TC001`** (pr=0.50, 1 mismatch)
  - `[onboarding-knowledge]` behavior references `intranet-chatbot` but it is NOT in peers

**`out-of-office-plan-temporal-TC001`** (pr=0.50, 1 mismatch)
  - `[task-tracker]` behavior references `ooo-dashboard` but it is NOT in peers

**`target-account-engagement-alert-rep-outreach-kit-temporal-TC001`** (pr=0.50, 1 mismatch)
  - `[email-draft-generator]` behavior references `lead-enrichment` but it is NOT in peers

**`facebook-lead-tracker-temporal-TC001`** (pr=0.75, 2 mismatches)
  - `[lead-tracker]` behavior references `facebook-leads` but it is NOT in peers
  - `[lead-tracker]` behavior references `zapier-tables` but it is NOT in peers

**`deal-desk-manage-hubspot-quote-approvals-slack-expansion-TC001`** (pr=0.75, 2 mismatches)
  - `[approval-policy]` behavior references `hubspot-quotes` but it is NOT in peers
  - `[approval-policy]` behavior references `deal-approvals-slack` but it is NOT in peers

**`canaries-employee-attrition-risk-prediction-mitigation-temporal-TC001`** (pr=0.75, 1 mismatch)
  - `[attrition-risk-engine]` behavior references `employee-data-store` but it is NOT in peers

**`employee-offboarding-temporal-TC001`** (pr=0.75, 1 mismatch)
  - `[offboarding-policy]` behavior references `talent-tracking-channel` but it is NOT in peers


### full_pass

**`automate-brand-monitoring-news-mentions-tracker-temporal-TC001`** (pr=1.00, 7 mismatches)
  - `[mention-analysis]` behavior references `rss-feed` but it is NOT in peers
  - `[mention-analysis]` behavior references `weekly-digest` but it is NOT in peers
  - `[weekly-digest]` behavior references `mention-store` but it is NOT in peers
  - `[weekly-digest]` behavior references `weekly-digest-trigger` but it is NOT in peers
  - `[digest-channel]` behavior references `weekly-digest` but it is NOT in peers
  - `[chatbot-query-handler]` behavior references `mention-store` but it is NOT in peers
  - `[chatbot-query-handler]` behavior references `chatbot-interface` but it is NOT in peers

**`automated-incident-postmortem-reviews-temporal-TC001`** (pr=1.00, 3 mismatches)
  - `[postmortem-analysis]` behavior references `incident-intake` but it is NOT in peers
  - `[postmortem-analysis]` behavior references `confluence-watcher` but it is NOT in peers
  - `[postmortem-analysis]` behavior references `weekly-scheduler` but it is NOT in peers

**`web-clipper-removal-TC001`** (pr=1.00, 2 mismatches)
  - `[content-curator]` behavior references `chrome-extension` but it is NOT in peers
  - `[content-curator]` behavior references `zapier-tables` but it is NOT in peers

**`inventory-temporal-TC001`** (pr=1.00, 2 mismatches)
  - `[reorder-policy]` behavior references `inventory-table` but it is NOT in peers
  - `[reorder-policy]` behavior references `email-sender` but it is NOT in peers

**`instagram-content-calendar-temporal-TC001`** (pr=1.00, 2 mismatches)
  - `[scheduling-policy]` behavior references `content-calendar` but it is NOT in peers
  - `[instagram-publisher]` behavior references `scheduling-policy` but it is NOT in peers

**`helpdesk-automation-template-slack-clickup-temporal-TC001`** (pr=1.00, 2 mismatches)
  - `[support-triage]` behavior references `slack-commands` but it is NOT in peers
  - `[support-triage]` behavior references `slack-ticket-actions` but it is NOT in peers

**`lead-capture-temporal-TC001`** (pr=1.00, 1 mismatch)
  - `[lead-capture-policy]` behavior references `contact-form` but it is NOT in peers

**`team-meeting-notes-automation-fathom-ai-slack-contextual-TC001`** (pr=1.00, 1 mismatch)
  - `[meeting-summary-processor]` behavior references `fathom-meetings` but it is NOT in peers

**`connect-databricks-engagement-data-salesloft-signals-exception-TC001`** (pr=1.00, 1 mismatch)
  - `[engagement-signal-analyzer]` behavior references `databricks` but it is NOT in peers

**`facebook-conversion-tracking-for-physical-stores-contextual-TC001`** (pr=1.00, 1 mismatch)
  - `[sales-dashboard]` behavior references `lead-conversion-tracker` but it is NOT in peers

**`landing-page-temporal-TC001`** (pr=1.00, 1 mismatch)
  - `[lead-intake-pipeline]` behavior references `zapier-table` but it is NOT in peers

**`slack-changelog-automation-removal-TC001`** (pr=1.00, 1 mismatch)
  - `[changelog-policy]` behavior references `slack-channel` but it is NOT in peers

**`email-campaign-portal-contextual-TC001`** (pr=1.00, 1 mismatch)
  - `[campaign-sequence]` behavior references `intake-form` but it is NOT in peers

**`call-prep-guide-exception-TC001`** (pr=1.00, 1 mismatch)
  - `[meeting-brief-builder]` behavior references `calendly` but it is NOT in peers

