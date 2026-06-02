"""
Structural validators for generated samples and test cases.

Called automatically after each pipeline stage to catch common data-gen
failures before they reach evaluation.  Each validator returns a list of
issue strings; an empty list means no problems detected.

Design philosophy
-----------------
Validators here are STRUCTURAL — they check graph integrity and schema
invariants that can be verified deterministically from the generated JSON.
They deliberately avoid heuristic NLP on behavior descriptions (too fragile)
except where explicitly noted, with known limitations documented per function.

Running validators
------------------
    from src.data.validate_test_cases import validate_test_case, print_validation_report

    issues = validate_test_case(tc)       # {validator_name: [issue_str, ...]}
    print_validation_report({"TC001": issues})
"""
from __future__ import annotations

import re
import string as _string
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.schema import Workflow, Sample


# ── Validator: mock data field mismatches ─────────────────────────────────────

def find_mock_field_mismatches(tc: "Sample") -> list[str]:
    """
    Return warnings where behavior text references a specific categorical-field
    value that differs from what the mock tool returns.

    Example: behavior says status='new' but mock returns status='captured'.
    The LLM stores 'captured', downstream expectations check for 'new' → mismatch.

    Checked fields: status, priority, stage, type, state.

    Limitations: only catches quoted value references matching
    '<field> "<value>"' or "<field> '<value>'" patterns. Field names expressed
    differently (e.g. "is_new: true", "must be in new state") are not detected.
    """
    CHECKED_FIELDS = ("status", "priority", "stage", "type", "state")

    mock_field_values: dict[str, set[str]] = {}
    for tool in tc.tools:
        for field in CHECKED_FIELDS:
            for m in re.finditer(rf'"{field}"\s*:\s*"([^"]+)"', tool.response_template, re.IGNORECASE):
                mock_field_values.setdefault(field, set()).add(m.group(1).lower())

    issues = []
    for obj in tc.objects:
        for field, mock_values in mock_field_values.items():
            behavior_values = re.findall(
                rf"\b{field}\b[^\"']*[\"']([^\"']+)[\"']",
                obj.behavior,
                re.IGNORECASE,
            )
            for expected_val in behavior_values:
                if expected_val.lower() not in mock_values:
                    issues.append(
                        f"Object '{obj.object_id}' behavior expects {field}='{expected_val}' "
                        f"but mock tool returns {field} in {mock_values}"
                    )
    return issues


# ── Validator: read/write misclassification ───────────────────────────────────

def find_read_write_misclassifications(tc: "Sample") -> list[str]:
    """
    Return warnings where an object queries a peer that cannot respond.

    If a peer relationship uses lookup/query semantics (relationship mentions
    "query", "lookup", "retrieve", etc.) but the target object has no
    event_sources and no _data skill, the querying object will set PENDING and
    wait forever — the chain stalls permanently.
    """
    WRITE_ONLY_BEHAVIOR = re.compile(
        r"\bdo not reply\b|\bnot? reply\b|\bwrite.only\b|\bnever respond\b",
        re.IGNORECASE,
    )
    object_map = {o.object_id: o for o in tc.objects}
    issues = []

    for obj in tc.objects:
        for neighbor_id in obj.neighbors:
            target = object_map.get(neighbor_id)
            if target is None:
                continue
            # Flag read_service targets that have write-only behavior — they cannot
            # respond to queries, so any caller that awaits a reply will stall.
            is_write_only = bool(WRITE_ONLY_BEHAVIOR.search(target.behavior))
            has_event_sources = bool(target.event_sources)
            has_any_skill = bool(target.skills)
            if is_write_only or (not has_event_sources and not has_any_skill):
                pass  # benign write services are common neighbors; only flag read services
            if is_write_only and target.node_type and "read" in str(target.node_type):
                issues.append(
                    f"Object '{obj.object_id}' routes to read_service '{target.object_id}' "
                    f"but it has write-only behavior and cannot respond"
                )
    return issues


# ── Validator: sequential confirmation chains ─────────────────────────────────

def find_sequential_confirmation_chains(tc: "Sample") -> list[str]:
    """
    Return warnings for behavior descriptions that wait for confirmation from
    fire-and-forget write services.

    Detects: "after X confirms", "when X responds", "once X has confirmed",
    "following X's reply", "upon X completing", etc.

    Limitation: heuristic regex, catches common surface forms.  Highly
    paraphrased variants ("pending X's acknowledgement") and legitimate
    conditional logic ("when the API responds with an error") may escape.
    """
    # Matches "after/when/once X confirms/responds/replies/acknowledges" patterns.
    # Exclusions to avoid false positives:
    #   (?!\s+(?:a|an|the)\b) — skips "when a confirmation email...", "when the response..."
    #   (?!\s+\w+ing\b)        — skips "after formatting is complete", "when answering..."
    #   confirm(?!ation)       — skips "confirmation" (noun), keeps "confirms/confirmed"
    #   repl(?:ies|ied|y\b)    — skips "reply email" (noun), keeps "replies/replied/reply (verb)"
    CONFIRMATION_PATTERN = re.compile(
        r"\b(after|when|once|following|upon)\b"
        r"(?!\s+(?:a|an|the)\b)"
        r"(?!\s+\w+ing\b)"
        r".{0,40}"
        r"\b(confirm(?!ation)\w*|responds?\b|acknowledg\w*|repl(?:ies|ied|y\b)|complet(?:es?|ed)\b|send.?back\b|'s\s+(?:reply|response))",
        re.IGNORECASE,
    )
    # Objects that already have an inbound mock tool trigger are patched —
    # the confirmation callback will fire, so the stall is resolved.
    patched_objects = {
        trigger.target_object_id
        for tool in tc.tools
        for trigger in tool.triggers
    }
    issues = []
    for obj in tc.objects:
        if CONFIRMATION_PATTERN.search(obj.behavior) and obj.object_id not in patched_objects:
            issues.append(
                f"Object '{obj.object_id}' behavior contains sequential confirmation language "
                f"('after X confirms', 'when X responds', 'once X has confirmed', etc.) — "
                f"fire-and-forget peers never confirm, causing the chain to stall permanently"
            )
    return issues


# ── Validator: missing data in step text ──────────────────────────────────────

def find_missing_step_data(tc: "Sample") -> list[str]:
    """
    Return warnings where event expectations reference identifiers not traceable
    to any step text or mock tool data.

    Checks:
    - Slack channel names (#channel) in expected actions
    - Quoted ticket/record IDs (e.g., "PROJ-1042") in expected actions
    """
    available_text = " ".join(step.input for step in tc.events if step.role == "base")
    available_text += " " + " ".join(e.input for e in tc.events)
    available_text += " " + " ".join(o.behavior for o in tc.objects)
    available_text += " " + " ".join(m.intent for m in tc.modifications)
    # Derive implicit channel names from object IDs (e.g., slack-general → #general)
    for o in tc.objects:
        if o.object_id.startswith("slack-"):
            available_text += f" #{o.object_id[len('slack-'):]}"
    for tool in tc.tools:
        available_text += " " + tool.response_template
        for resp in tool.scripted_responses:
            available_text += " " + resp
        # Extract channel_name values from JSON mock data and add with # prefix
        # so validators match "#technical-support" even when stored as "technical-support"
        try:
            import json as _json
            mock_json = _json.loads(tool.response_template)
            for match in re.findall(r'"channel_name"\s*:\s*"([\w-]+)"', tool.response_template):
                available_text += f" #{match}"
        except Exception:
            pass
    available_text_lower = available_text.lower()

    issues = []
    for event in tc.events:
        if event.expect is None:
            continue

        for channel in re.findall(r"#([\w-]+)", event.expect.action):
            if f"#{channel}".lower() not in available_text_lower:
                issues.append(
                    f"Event '{event.id}' expects Slack channel '#{channel}' in action "
                    f"but '#{channel}' does not appear in any step text or mock data"
                )

        for qid in re.findall(r'"([A-Z]{2,}-\d+)"', event.expect.action):
            if qid.lower() not in available_text_lower:
                issues.append(
                    f"Event '{event.id}' expects identifier '{qid}' in action "
                    f"but '{qid}' does not appear in any step text or mock data"
                )
    return issues


# ── Validator: threshold evaluation in expectations ───────────────────────────

def find_threshold_evaluation_errors(tc: "Sample") -> list[str]:
    """
    Return warnings where an event expectation includes a conditional output
    even though the threshold condition is not met by the event's input.

    Only checks cases where the metric keyword appears explicitly in the event
    input alongside a numeric value (avoids false positives on unrelated numbers).

    Limitation: detects simple single-condition thresholds of the form
    'metric OP N → action'. Compound conditions, ranges, and negations are
    not covered.
    """
    THRESHOLD_PATTERN = re.compile(
        r"\b(\w+)\s+([<>]=?)\s+(\d+(?:\.\d+)?)\s*(?:→|->|:)\s*(\w[^\n.;,]+)",
        re.IGNORECASE,
    )
    object_map = {o.object_id: o for o in tc.objects}
    issues = []

    for event in tc.events:
        if event.expect is None:
            continue
        target = object_map.get(event.recipient)
        if target is None:
            continue

        for metric, op, threshold_str, conditional_action in THRESHOLD_PATTERN.findall(target.behavior):
            threshold_val = float(threshold_str)
            conditional_action = conditional_action.strip().lower()
            metric_lower = metric.strip().lower()

            metric_num_match = re.search(
                rf"\b{re.escape(metric_lower)}\b[^\d]*?(\d+(?:\.\d+)?)",
                event.input,
                re.IGNORECASE,
            )
            if metric_num_match is None:
                continue

            num_val = float(metric_num_match.group(1))
            threshold_met = (
                (op == "<"  and num_val <  threshold_val) or
                (op == "<=" and num_val <= threshold_val) or
                (op == ">"  and num_val >  threshold_val) or
                (op == ">=" and num_val >= threshold_val)
            )
            if threshold_met:
                continue

            action_keywords = conditional_action.split()[:2]
            if all(kw in event.expect.action.lower() for kw in action_keywords):
                issues.append(
                    f"Event '{event.id}': expectation includes '{conditional_action[:60]}' "
                    f"but threshold '{metric} {op} {threshold_val}' is NOT met "
                    f"(input value: {num_val})"
                )
    return issues


# ── Validator: trigger reference integrity ────────────────────────────────────

def find_trigger_reference_errors(tc: "Sample") -> list[str]:
    """
    Return reference errors in MockToolDef triggers and Event.triggered_by chains.

    Checks:
    1. trigger.target_object_id exists in tc.objects — a missing object means
       inject_event silently delivers to a non-existent recipient.
    2. trigger.message_template only uses placeholders defined in
       arguments_schema.properties (plus {call_index}) — undefined placeholders
       cause a KeyError at runtime and leave the message unformatted.
       Example: template uses {ticket_id} but schema only declares {id}.
    3. event.triggered_by references a valid sibling event ID — a dangling
       reference means the child event is never dispatched by the evaluator.
    """
    object_ids = {o.object_id for o in tc.objects}
    event_ids  = {e.id for e in tc.events}
    issues: list[str] = []

    for tool in tc.tools:
        valid_placeholders = (
            set(tool.arguments_schema.get("properties", {}).keys()) | {"call_index"}
        )
        for trigger in tool.triggers:
            if trigger.target_object_id not in object_ids:
                issues.append(
                    f"MockTool '{tool.tool_name}' trigger targets '{trigger.target_object_id}' "
                    f"but no object with that ID exists — injected events will be silently lost"
                )

            try:
                template_keys = {
                    fname
                    for _, fname, _, _ in _string.Formatter().parse(trigger.message_template)
                    if fname is not None
                }
            except (ValueError, KeyError):
                template_keys = set()

            undefined = template_keys - valid_placeholders
            if undefined:
                issues.append(
                    f"MockTool '{tool.tool_name}' trigger message_template references "
                    f"undefined placeholders {sorted(undefined)} — not in arguments_schema; "
                    f"will cause a KeyError and leave the injected message unformatted"
                )

    for event in tc.events:
        if event.triggered_by and event.triggered_by not in event_ids:
            issues.append(
                f"Event '{event.id}' has triggered_by='{event.triggered_by}' "
                f"but no event with that ID exists — the child event will never be dispatched"
            )
        elif event.triggered_by and event.triggered_by == event.id:
            issues.append(
                f"Event '{event.id}' has triggered_by='{event.triggered_by}' "
                f"which references itself — self-referential trigger creates an infinite loop"
            )

    return issues


# ── Validator: invalid peer declarations ──────────────────────────────────────

def find_invalid_peer_declarations(tc: "Sample") -> list[str]:
    """
    Return warnings for PeerDecl entries that reference non-existent objects.

    Every PeerDecl.object_id must exist in tc.objects.  A dangling peer
    reference means the object will attempt to send messages to a non-existent
    recipient — the send is silently dropped, breaking the chain.

    Note: this validates that DECLARED peers exist.  Fan-out completeness
    (behavior implying more targets than declared peers) is checked separately
    by find_undeclared_peer_references.
    """
    object_ids = {o.object_id for o in tc.objects}
    issues = []
    for obj in tc.objects:
        for neighbor_id in obj.neighbors:
            if neighbor_id not in object_ids:
                issues.append(
                    f"Object '{obj.object_id}' declares neighbor '{neighbor_id}' "
                    f"but no object with that ID exists — sends to this neighbor "
                    f"will be silently dropped at runtime"
                )
    return issues


# ── Validator: undeclared peer references (fan-out completeness) ───────────────

def find_undeclared_peer_references(tc: "Sample") -> list[str]:
    """
    Return warnings where an object's behavior describes sending a message to
    another object but that object is not declared as a peer.

    An object can only send messages to declared peers — the runtime silently
    drops sends to undeclared recipients.  This catches fan-out omissions like
    "send to gmail-drafts AND hubspot-tasks" when only gmail-drafts is a peer.

    Strategy: look for outgoing-action verbs ("send to", "notify", "forward to",
    etc.) immediately followed by a known object_id slug that is not a declared
    peer.  Only outgoing-action contexts are checked to avoid false positives
    from event-source references ("arrives from X", "sent by X").

    Limitation: only catches exact object_id slugs in outgoing-action phrasing.
    Paraphrased references ("the HubSpot task creator") escape detection.
    """
    OUTGOING_VERBS = re.compile(
        r"\b(send(?:ing)?\s+to|notif(?:y|ies|ying)|forward(?:ing)?\s+to|"
        r"route(?:ing)?\s+to|post(?:ing)?\s+to|deliver(?:ing)?\s+to|"
        r"write(?:ing)?\s+to|push(?:ing)?\s+to|relay(?:ing)?\s+to|"
        r"escalate(?:ing)?\s+to|pass(?:ing)?\s+to)\s+",
        re.IGNORECASE,
    )

    object_ids = {o.object_id for o in tc.objects}
    issues = []

    for obj in tc.objects:
        declared_peer_ids = set(obj.neighbors)
        # Find all positions where an outgoing-action verb appears in the behavior
        for verb_match in OUTGOING_VERBS.finditer(obj.behavior):
            # Look at the text immediately after the verb for an object_id
            after_verb = obj.behavior[verb_match.end():]
            for other_id in object_ids:
                if other_id == obj.object_id or other_id in declared_peer_ids:
                    continue
                # Match at the start of after_verb; require the slug is not part
                # of a longer hyphenated identifier (e.g., "foo-bar" in "foo-bar-baz")
                if re.match(rf"{re.escape(other_id)}(?![-\w])", after_verb, re.IGNORECASE):
                    issues.append(
                        f"Object '{obj.object_id}' behavior says '{verb_match.group().strip()} "
                        f"{other_id}' but '{other_id}' is not declared as a peer — "
                        f"this send will be silently dropped"
                    )
    return issues


# ── Validator: peer graph dead-ends ───────────────────────────────────────────

def find_peer_graph_dead_ends(tc: "Sample") -> list[str]:
    """
    Return warnings for entry-point objects (with event_sources) that have no peers.

    An isolated entry-point absorbs incoming events but notifies nothing else —
    the automation chain terminates after the first object.

    Single-object test cases are exempt: a solo entry-point is valid when it
    IS the terminal action (e.g., a direct write service).
    """
    if len(tc.objects) <= 1:
        return []

    issues = []
    for obj in tc.objects:
        if obj.event_sources and not obj.neighbors:
            issues.append(
                f"Object '{obj.object_id}' has event_sources {obj.event_sources} "
                f"but no peers — incoming events terminate here and cannot propagate"
            )
    return issues


# ── Validator: unreachable objects ────────────────────────────────────────────

def find_unreachable_objects(tc: "Sample") -> list[str]:
    """
    Return warnings for objects not reachable from any entry point via the peer graph.

    Uses BFS from all objects with event_sources.  Any object not reached is an
    orphan — it will never receive a message during evaluation, making it dead
    data in the test case.

    Single-object test cases are exempt.
    """
    if len(tc.objects) <= 1:
        return []

    entry_points = {o.object_id for o in tc.objects if o.event_sources}
    if not entry_points:
        return []

    peer_map: dict[str, set[str]] = {
        o.object_id: set(o.neighbors)
        for o in tc.objects
    }

    visited: set[str] = set(entry_points)
    queue: deque[str] = deque(entry_points)
    while queue:
        node = queue.popleft()
        for neighbor in peer_map.get(node, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    unreachable = {o.object_id for o in tc.objects} - visited
    return [
        f"Object '{oid}' is not reachable from any entry point via the peer graph — "
        f"it will never receive a message during evaluation"
        for oid in sorted(unreachable)
    ]


# ── Validator: missing mock tools for _data skills ────────────────────────────

def find_missing_mock_tools(tc: "Sample") -> list[str]:
    """
    Return warnings for _data skills that have no corresponding MockToolDef.

    If an object declares a skill like 'faq_knowledge_base_data' but no mock
    tool with that name exists, the _data call falls through to
    PassthroughExecutor which returns '{}' — causing field-access failures and
    stalling request-reply chains.
    """
    mocked = {t.tool_name for t in tc.tools}
    issues = []
    for obj in tc.objects:
        for skill in obj.skills:
            if "_data" in skill.lower() and skill not in mocked:
                issues.append(
                    f"Object '{obj.object_id}' has _data skill '{skill}' "
                    f"but no mock tool with that name — _data calls will fall through "
                    f"to PassthroughExecutor and return '{{}}', stalling the chain"
                )
    return issues


# ── Validator: _data tool references in behavior text ────────────────────────

_DATA_TOOL_RE = re.compile(r"\b([\w][\w-]*_data)\b")


def find_behavior_data_tool_references(tc: "Sample") -> list[str]:
    """
    Return errors where an object's behavior text references a *_data tool
    that has no corresponding MockToolDef.

    Unlike find_missing_mock_tools (which checks declared skills), this
    validator parses free-text behavior descriptions for tool name patterns
    ending in '_data'.  Objects often call read-service tools inline
    (e.g. 'call the gmail_drafts_data tool') without listing them as skills.
    Without a mock, the PassthroughExecutor returns '{}', causing the object
    to stall or proceed with empty data — silently breaking the chain.

    Limitation: regex-based, so it may catch tool-like substrings that are
    not actual tool calls.  False positives are rare because '_data' suffixes
    are a strong signal in this codebase.
    """
    mocked = {t.tool_name for t in tc.tools}
    issues = []
    for obj in tc.objects:
        for match in _DATA_TOOL_RE.finditer(obj.behavior):
            tool_name = match.group(1)
            if tool_name not in mocked:
                issues.append(
                    f"Object '{obj.object_id}' behavior references tool '{tool_name}' "
                    f"but no MockToolDef with that name exists — the call will return '{{}}' "
                    f"and the object will stall or proceed with empty data"
                )
    return issues


# ── Validator: write-service objects with no tool skills ─────────────────────

_WRITE_SERVICE_RE = re.compile(
    r"\b(send|post|write|update|create|publish|notify|deliver|dispatch|log|record|"
    r"insert|upsert|push|upload|submit|emit)\b",
    re.IGNORECASE,
)


def find_write_service_without_tools(tc: "Sample") -> list[str]:
    """
    Return errors for objects whose behavior describes write-side API calls
    but that have no corresponding MockToolDef registered for the test case.

    Write-service objects (email sender, Slack poster, HubSpot writer, etc.)
    must invoke external APIs to produce their observable side-effects.
    Without a MockToolDef, the evaluation framework has nothing to assert
    tool-invocation against — the object can claim success without ever
    calling the API, and evaluations pass incorrectly.

    Coverage is satisfied if:
      - the object declares skills that map to mock tools, OR
      - tc.tools contains at least one write-side tool (any tool whose
        name does not end in '_data') — indicating write-side tools were
        registered for this test case.
    """
    write_tools_registered = any(
        "_data" not in t.tool_name.lower() for t in tc.tools
    )
    if write_tools_registered:
        return []

    issues = []
    for obj in tc.objects:
        if obj.skills:
            continue
        if obj.event_sources or obj.neighbors:
            continue
        if _WRITE_SERVICE_RE.search(obj.behavior):
            issues.append(
                f"Object '{obj.object_id}' appears to be a write-service "
                f"(behavior contains write verbs) but no write-side MockToolDef "
                f"is registered for this test case — no tool invocation can be "
                f"asserted during evaluation; run retrofit_mock_tools to add them"
            )
    return issues


# ── Validator: peer graph cycles ──────────────────────────────────────────────

def find_peer_graph_cycles(tc: "Sample") -> list[str]:
    """
    Return warnings for cycles in the peer graph that could cause infinite
    message loops at runtime.

    Uses DFS with a recursion stack to detect back edges.  A cycle does not
    guarantee an infinite loop (the LLM may choose not to send back to the
    caller), but it is a strong signal that the behavior description needs
    to specify a termination condition explicitly.

    Single-object test cases are exempt.
    """
    if len(tc.objects) <= 1:
        return []

    peer_map: dict[str, list[str]] = {
        o.object_id: list(o.neighbors)
        for o in tc.objects
    }
    object_ids = set(peer_map)

    visited: set[str] = set()
    rec_stack: set[str] = set()
    cycles: list[list[str]] = []

    def _dfs(node: str, path: list[str]) -> None:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        for neighbor in peer_map.get(node, []):
            if neighbor not in object_ids:
                continue
            if neighbor not in visited:
                _dfs(neighbor, path)
            elif neighbor in rec_stack:
                # Found cycle — extract the loop portion
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:] + [neighbor])
        path.pop()
        rec_stack.discard(node)

    for oid in object_ids:
        if oid not in visited:
            _dfs(oid, [])

    return [
        f"Peer graph cycle detected: {' → '.join(cycle)} — "
        f"without an explicit termination condition this can loop indefinitely"
        for cycle in cycles
    ]


# ── Validator: modification integrity ────────────────────────────────────────

def _parse_timestamp(when: str) -> "tuple[int, int, int, int] | None":
    """Parse 'W02-1T09:00' → (2, 1, 9, 0) for comparison. Returns None on failure."""
    m = re.match(r"W(\d+)-(\d+)T(\d+):(\d+)", when or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def find_modification_target_errors(tc: "Sample") -> list[str]:
    """
    Return errors where a modification targets a non-existent object_id.

    If `target` doesn't match any object in tc.objects, the runtime delivers
    the modification to a ghost — the behavior change never applies and all
    events that were supposed to run under the modified behavior instead test
    the default behavior, producing wrong pass/fail signals.
    """
    object_ids = {o.object_id for o in tc.objects}
    issues = []
    for mod in tc.modifications:
        if mod.target not in object_ids:
            issues.append(
                f"Modification '{mod.id}' targets '{mod.target}' "
                f"but no object with that ID exists — modification is never applied"
            )
    return issues


def find_modification_timeline_errors(tc: "Sample") -> list[str]:
    """
    Return warnings where a modification's `when` falls after every event, or
    where the `when` timestamp is malformed.

    A modification that occurs after all events is never observable — no event
    runs under the modified behavior. The TC tests nothing about the modification.

    Also catches modifications with `when` timestamps that cannot be parsed
    (wrong format, missing field).
    """
    if not tc.modifications or not tc.events:
        return []

    event_times = [_parse_timestamp(e.when) for e in tc.events]
    valid_event_times = [t for t in event_times if t is not None]
    if not valid_event_times:
        return []

    last_event_time = max(valid_event_times)
    issues = []
    for mod in tc.modifications:
        mod_time = _parse_timestamp(mod.when)
        if mod_time is None:
            issues.append(
                f"Modification '{mod.id}' has unparseable `when` timestamp '{mod.when}' "
                f"— expected format W{{week}}-{{day}}T{{hh}}:{{mm}}"
            )
            continue
        if mod_time > last_event_time:
            issues.append(
                f"Modification '{mod.id}' when='{mod.when}' occurs after the last event "
                f"— no event observes this modification; the TC tests nothing about it"
            )
    return issues


def find_event_timeline_errors(tc: "Sample") -> list[str]:
    """
    Return warnings where a child event's `when` timestamp is not strictly after
    its parent's `when` (as declared via triggered_by).

    A child event must occur at or after its parent; if it precedes the parent
    it can never be causally triggered by it, and the evaluator may dispatch
    events out of causal order.

    Only checks pairs where both timestamps are parseable.
    """
    if not tc.events:
        return []

    event_time_map = {}
    for e in tc.events:
        t = _parse_timestamp(e.when)
        if t is not None:
            event_time_map[e.id] = t

    issues = []
    for event in tc.events:
        if not event.triggered_by or event.triggered_by not in event_time_map:
            continue
        parent_time = event_time_map[event.triggered_by]
        child_time = event_time_map.get(event.id)
        if child_time is not None and child_time < parent_time:
            issues.append(
                f"Event '{event.id}' (when='{event.when}') is triggered_by '{event.triggered_by}' "
                f"but occurs BEFORE its parent — causal order is violated"
            )
    return issues


# ── Validator: unnatural identifiers in mock data ─────────────────────────────

def find_unnatural_identifiers(tc: "Sample") -> list[str]:
    """
    Return warnings for short identifiers that appear in BOTH mock data AND
    event expectations — where judge false-failures are most likely.

    Short Slack IDs (U4821) or ticket IDs (PROJ-42) in mock data are only
    a real problem when they also appear in expectations: the system may output
    the person's name instead of the ID, and a strict judge treats them as
    different entities.  IDs that appear only in mock data but not in any
    expectation are lower risk and are not flagged.

    Checks:
    - Slack user IDs: 9+ alphanumeric chars (e.g. U01ABCDEF); flags U4821-style.
    - Ticket/record IDs: 3+ digit numbers (e.g. PROJ-1042); flags PROJ-42-style.
    """
    SHORT_SLACK_ID = re.compile(r'\bU[A-Z0-9]{2,6}\b')
    VALID_SLACK_ID = re.compile(r'\bU[A-Z0-9]{8,}\b')
    SHORT_TICKET   = re.compile(r'\b[A-Z]{2,10}-\d{1,2}\b')

    mock_text = " ".join(
        tool.response_template + " " + " ".join(tool.scripted_responses)
        for tool in tc.tools
    )
    expectation_text = (
        " ".join(
            (e.expect.action + " " + (e.expect.reason or ""))
            for e in tc.events if e.expect
        )
        + " "
        + " ".join(
            (s.expect.action + " " + (s.expect.reason or ""))
            for s in tc.events if s.role == "base" and s.expect
        )
    )

    mock_short_slack  = {
        m.group() for m in SHORT_SLACK_ID.finditer(mock_text)
        if not VALID_SLACK_ID.match(m.group())
    }
    mock_short_ticket = {m.group() for m in SHORT_TICKET.finditer(mock_text)}

    issues: list[str] = []
    for uid in sorted(mock_short_slack):
        if uid in expectation_text:
            issues.append(
                f"Short Slack user ID '{uid}' appears in both mock data and expectations — "
                f"real Slack IDs are 9+ chars (e.g. U01ABCDEF); if the system outputs a "
                f"name instead, a strict judge may fail a correct result"
            )

    for tid in sorted(mock_short_ticket):
        if tid in expectation_text:
            issues.append(
                f"Short ticket ID '{tid}' appears in both mock data and expectations — "
                f"prefer 3+ digit numbers (e.g. PROJ-1042) to reduce judge false-failures"
            )

    return issues


# ── Validator: event entity IDs missing from mock tool coverage ───────────────

_ENTITY_ID_RE = re.compile(r'\b[A-Z]{2,10}-\d{3,}\b')
_EMAIL_RE = re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b')
# Company names: one or more title-case words followed by a business-entity suffix.
# The suffix anchor prevents matching generic phrases like "Status Change".
_COMPANY_NAME_RE = re.compile(
    r'\b(?:[A-Z][a-z]+ ){1,4}(?:Inc|LLC|Ltd|Corp|Group|Systems|Services|Solutions|Technologies|Logistics|Financial|Publishing|BioTech|Analytics|Consulting|Holdings|Ventures|Partners)\b'
)


def find_event_entity_mock_gaps(tc: "Sample") -> list[str]:
    """
    Return warnings where an event input or expectation references a structured
    entity ID (e.g. EMP-3317, ACC-10284) or email address that does not appear
    anywhere in mock tool response data.

    Root cause: mock tools are stateless — response_template is hardcoded for
    the S001 entity.  When a later event introduces a different entity, the
    mock tool returns stale data for the wrong entity, causing the LLM to spin
    until timeout.

    Coverage sources checked (all must be absent to flag):
      - response_template of every mock tool
      - scripted_responses entries of every mock tool
      - scripted_match_responses match values AND response strings

    Design: deliberately errs toward flagging (no false negatives).  It cannot
    distinguish read vs. write operations, so some flagged entities may be new
    records being created rather than looked up.  Those false positives are
    acceptable; missing a real gap is not.  Suppress by adding the entity to
    the mock tool's scripted_match_responses, not by weakening this check.

    Skipped if the test case has no mock tools (nothing to be inconsistent with).
    """
    if not tc.tools:
        return []

    # Build full mock tool text corpus — every string the mock could return
    mock_parts: list[str] = []
    for tool in tc.tools:
        mock_parts.append(tool.response_template)
        mock_parts.extend(tool.scripted_responses)
        for smr in tool.scripted_match_responses:
            mock_parts.extend(smr.match.values())
            mock_parts.append(smr.response)
    mock_text = " ".join(mock_parts)

    tool_names_str = ", ".join(f"'{t.tool_name}'" for t in tc.tools)

    # Collect entity IDs and emails from ALL event inputs + expectations.
    # Deduplicate by entity so each gap is reported once regardless of how
    # many events reference it.
    reported: set[str] = set()
    issues: list[str] = []

    for evt in tc.events:
        search_text = (evt.input or "")
        if evt.expect:
            search_text += " " + evt.expect.action

        for m in _ENTITY_ID_RE.finditer(search_text):
            entity = m.group()
            if entity not in reported and entity not in mock_text:
                reported.add(entity)
                issues.append(
                    f"Event '{evt.id}' references entity '{entity}' which does not "
                    f"appear in any mock tool response data — if any object queries a "
                    f"mock tool for this entity it will receive mismatched data and may "
                    f"timeout (mock tools: {tool_names_str})"
                )

        for m in _EMAIL_RE.finditer(search_text):
            email = m.group()
            if email not in reported and email not in mock_text:
                reported.add(email)
                issues.append(
                    f"Event '{evt.id}' references email '{email}' which does not "
                    f"appear in any mock tool response data — if any object queries a "
                    f"mock tool for this recipient it will receive no matching data "
                    f"(mock tools: {tool_names_str})"
                )

        for m in _COMPANY_NAME_RE.finditer(search_text):
            name = m.group()
            if name not in reported and name not in mock_text:
                reported.add(name)
                issues.append(
                    f"Event '{evt.id}' references company/account '{name}' which does not "
                    f"appear in any mock tool response data — if any object queries a mock "
                    f"tool for this account it will receive data for the wrong entity and "
                    f"may timeout (mock tools: {tool_names_str})"
                )

    return issues


# ── Severity classification ───────────────────────────────────────────────────
#
# BLOCKING: chain is guaranteed broken — regenerate the sample.
#   Used by the pipeline's auto-repair loop; triggers human review if still
#   failing after max_fix_attempts.
#
# WARNING: chain may degrade but might still partially pass — print and continue.

BLOCKING_VALIDATORS = frozenset({
    "find_peer_graph_dead_ends",             # entry-point with no peers → chain dead immediately
    "find_unreachable_objects",              # orphan objects → never receive a message
    "find_invalid_peer_declarations",        # dangling peer → sends silently dropped
    "find_missing_mock_tools",               # _data skill with no mock → stalls silently
    "find_behavior_data_tool_references",    # behavior references _data tool with no mock → stalls silently
    "find_write_service_without_tools",      # write-service with no skills → no tool invocation to assert
    "find_read_write_misclassifications",    # PENDING forever stall
    "find_modification_target_errors",       # modification targets ghost → never applied
})

WARNING_VALIDATORS = frozenset({
    "find_mock_field_mismatches",            # wrong field values → downstream mismatch
    "find_missing_step_data",                # objects hallucinate missing data
    "find_threshold_evaluation_errors",      # false expectation failure
    "find_unnatural_identifiers",            # judge false-failure risk
    "find_trigger_reference_errors",         # bad trigger config → runtime KeyError
    "find_sequential_confirmation_chains",   # fixable via mock tool triggers
    "find_modification_timeline_errors",     # modification after all events → unobservable
    "find_event_timeline_errors",            # child event before parent → causal violation
    "find_undeclared_peer_references",       # fan-out omission → sends silently dropped
    "find_peer_graph_cycles",               # potential infinite loop
    "find_event_entity_mock_gaps",           # entity in event not in mock data → timeout risk
})

# ── Composite runners ─────────────────────────────────────────────────────────

_SAMPLE_VALIDATORS = [
    # Structural checks that apply to Stage-1 samples (no events/modifications yet)
    find_read_write_misclassifications,
    find_sequential_confirmation_chains,
    find_invalid_peer_declarations,
    find_undeclared_peer_references,
    find_peer_graph_dead_ends,
    find_peer_graph_cycles,
    find_unreachable_objects,
    find_missing_mock_tools,
    find_behavior_data_tool_references,
    find_write_service_without_tools,
]

_TEST_CASE_VALIDATORS = [
    # All validators — run on fully-formed test cases after Stage 3
    find_mock_field_mismatches,
    find_read_write_misclassifications,
    find_sequential_confirmation_chains,
    find_missing_step_data,
    find_threshold_evaluation_errors,
    find_trigger_reference_errors,
    find_invalid_peer_declarations,
    find_peer_graph_dead_ends,
    find_peer_graph_cycles,
    find_unreachable_objects,
    find_missing_mock_tools,
    find_behavior_data_tool_references,
    find_write_service_without_tools,
    find_unnatural_identifiers,
    find_modification_target_errors,
    find_modification_timeline_errors,
    find_event_timeline_errors,
    find_undeclared_peer_references,
    find_event_entity_mock_gaps,
]


def validate_sample(sample: "Workflow") -> dict[str, list[str]]:
    """
    Run structural validators on a Stage-1 sample.

    Returns {validator_name: [issue_str, ...]} for any validator that found issues.
    An empty dict means no problems detected.
    """
    from src.data.schema import Sample
    # Construct a minimal Sample so all validators can operate on a uniform type
    tc = Sample(
        id=sample.id,
        name=sample.name,
        domain=sample.domain,
        source_type=sample.source_type,
        objects=sample.objects,
        steps=sample.steps,
        tools=sample.tools,
        modifications=[],
        events=[],
    )
    return {
        fn.__name__: issues
        for fn in _SAMPLE_VALIDATORS
        if (issues := fn(tc))
    }


def validate_test_case(tc: "Sample") -> dict[str, list[str]]:
    """
    Run all validators on a fully-formed Sample.

    Returns {validator_name: [issue_str, ...]} for any validator that found issues.
    An empty dict means no problems detected.
    """
    return {
        fn.__name__: issues
        for fn in _TEST_CASE_VALIDATORS
        if (issues := fn(tc))
    }


def print_validation_report(
    results: "dict[str, dict[str, list[str]]]",
    label: str = "Validation",
) -> int:
    """
    Print a compact validation report.

    Args:
        results: {item_id: {validator_name: [issue_str, ...]}}
        label:   prefix printed before the summary line

    Returns:
        Total number of issues found (0 = clean).
    """
    total = sum(
        len(issues)
        for per_validator in results.values()
        for issues in per_validator.values()
    )
    if total == 0:
        print(f"  {label}: all checks passed ({len(results)} item(s) validated).")
        return 0

    affected = len(results)
    print(f"  {label}: {total} issue(s) across {affected} item(s):")
    for item_id, per_validator in results.items():
        for validator_name, issues in per_validator.items():
            short = validator_name.replace("find_", "")
            for issue in issues:
                print(f"    [{item_id}] {short}: {issue}")
    return total
