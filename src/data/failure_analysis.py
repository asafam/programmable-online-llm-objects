"""Retroactive failure analysis for eval results JSONL.

Parses an evaluation results file (default eval or baseline), extracts structured
interaction metrics from the ``EventResult.evidence`` text, classifies failed
events into a fixed taxonomy, and emits:
  - ``<input>.analysis.jsonl`` — per-record augmented with new fields
  - ``<input>.analysis.report.json`` — aggregate report

Does not mutate the original results file.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from src.data.failure_classifier import (
    CATEGORIES,
    ClassifierResult,
    FailureClassifier,
)


# ── Evidence parsing ──────────────────────────────────────────────────────────

_SECTION_RE = re.compile(r"^=== (.+?) ===\s*$", re.MULTILINE)
_BUS_LINE_RE = re.compile(
    r"^\s+(?P<sender_raw>\[[^\]]+\]|[A-Za-z0-9_\-]+)\s+(?P<arrow>→|↩)\s+(?P<recipient>[A-Za-z0-9_\-]+)\s*\((?P<kind>[^)]+)\):"
)
_TOOL_LINE_RE = re.compile(r"^\s+\[(?P<tool>[^\]]+)\]\s+call#")


@dataclass
class InteractionMetrics:
    peer_messages_count: int = 0
    tool_calls_count: int = 0
    unique_peers_touched: int = 0
    chain_depth: int = 0


def _extract_sections(evidence: str) -> dict[str, str]:
    """Split evidence into sections by '=== HEADER ===' markers."""
    if not evidence:
        return {}
    pieces = _SECTION_RE.split(evidence)
    # pieces alternates: [preamble, header1, body1, header2, body2, ...]
    sections: dict[str, str] = {}
    for i in range(1, len(pieces), 2):
        header = pieces[i].strip()
        body = pieces[i + 1] if i + 1 < len(pieces) else ""
        sections[header] = body
    return sections


def _extract_this_event_subsections(body: str) -> dict[str, str]:
    """Split the THIS EVENT body into its labelled subsections."""
    result: dict[str, str] = {}
    current_label: Optional[str] = None
    current_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not line.startswith(" ") and stripped in {
            "Tool calls:",
            "Message bus activity:",
            "Replies:",
        }:
            if current_label is not None:
                result[current_label] = "\n".join(current_lines).strip()
            current_label = stripped.rstrip(":")
            current_lines = []
        else:
            if current_label is not None:
                current_lines.append(line)
    if current_label is not None:
        result[current_label] = "\n".join(current_lines).strip()
    return result


def _extract_metrics(evidence: str) -> InteractionMetrics:
    sections = _extract_sections(evidence)
    this_event = sections.get("THIS EVENT", "")
    if not this_event:
        return InteractionMetrics()

    subs = _extract_this_event_subsections(this_event)

    tool_calls_count = 0
    for line in subs.get("Tool calls", "").splitlines():
        if _TOOL_LINE_RE.match(line):
            tool_calls_count += 1

    # Parse bus activity into (sender, recipient, kind) triples.
    bus_body = subs.get("Message bus activity", "")
    edges: list[tuple[str, str, str]] = []
    for line in bus_body.splitlines():
        m = _BUS_LINE_RE.match(line)
        if not m:
            continue
        sender_raw = m.group("sender_raw")
        is_external = sender_raw.startswith("[") and sender_raw.endswith("]")
        sender = sender_raw[1:-1] if is_external else sender_raw
        recipient = m.group("recipient")
        kind = m.group("kind").strip()
        edges.append((sender, recipient, kind))

    # peer_messages_count = non-reply, non-external-event edges between internal objects
    peer_edges: list[tuple[str, str]] = []
    all_nodes: set[str] = set()
    for sender, recipient, kind in edges:
        all_nodes.add(sender)
        all_nodes.add(recipient)
        if kind == "reply":
            continue
        if kind == "event" and sender.startswith("__") or sender in {"__external__", "__user__"}:
            continue
        # Filter out external event sources (in brackets in raw form, already stripped)
        # A bit heuristic — internal objects use kebab-case IDs, external sources tend to be single tokens
        peer_edges.append((sender, recipient))

    peer_messages_count = len(peer_edges)

    # unique_peers_touched = distinct internal objects participating in peer messages
    peer_nodes: set[str] = set()
    for sender, recipient in peer_edges:
        peer_nodes.add(sender)
        peer_nodes.add(recipient)

    # chain_depth = longest directed path in peer-edge DAG (approx via BFS over edges)
    chain_depth = _longest_path_len(peer_edges)

    return InteractionMetrics(
        peer_messages_count=peer_messages_count,
        tool_calls_count=tool_calls_count,
        unique_peers_touched=len(peer_nodes),
        chain_depth=chain_depth,
    )


def _longest_path_len(edges: list[tuple[str, str]]) -> int:
    """Longest path (#edges) in a directed graph, tolerating cycles by capping depth."""
    if not edges:
        return 0
    adj: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for a, b in edges:
        adj[a].append(b)
        nodes.add(a)
        nodes.add(b)

    best = 0
    for start in nodes:
        stack: list[tuple[str, int, set[str]]] = [(start, 0, {start})]
        while stack:
            node, depth, visited = stack.pop()
            if depth > best:
                best = depth
            for nxt in adj.get(node, []):
                if nxt in visited or depth >= 10:
                    continue
                stack.append((nxt, depth + 1, visited | {nxt}))
    return best


# ── Expected complexity from Sample graph ───────────────────────────────────

@dataclass
class ExpectedComplexity:
    expected_peer_hops: int
    expected_unique_peers: int


def _compute_expected_complexity_index(test_cases_path: Optional[Path]) -> dict[tuple[str, str], ExpectedComplexity]:
    """Build {(tc_id, event_id) → ExpectedComplexity} from a test_cases.jsonl file.

    expected_peer_hops: longest peer chain reachable from the event recipient.
    expected_unique_peers: total unique peers reachable transitively.
    """
    index: dict[tuple[str, str], ExpectedComplexity] = {}
    if not test_cases_path or not test_cases_path.exists():
        return index
    try:
        with open(test_cases_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                tc = json.loads(line)
                if "id" not in tc or "events" not in tc or "objects" not in tc:
                    continue
                # Build peer adjacency for this TC
                adj: dict[str, list[str]] = {}
                for obj in tc.get("objects", []):
                    obj_id = obj.get("object_id", "")
                    adj[obj_id] = [p.get("object_id", "") for p in obj.get("peers", [])]
                # Also process steps (which have S-prefixed event IDs S001, S002, ...) — map by target
                for idx, step in enumerate(tc.get("steps", []), start=1):
                    hops, peers = _reachable_stats(step.get("target", ""), adj)
                    index[(tc["id"], f"S{idx:03d}")] = ExpectedComplexity(hops, peers)
                for ev in tc.get("events", []):
                    hops, peers = _reachable_stats(ev.get("recipient", ""), adj)
                    index[(tc["id"], ev.get("id", ""))] = ExpectedComplexity(hops, peers)
    except Exception:
        # Best-effort — expected complexity is optional enrichment
        return index
    return index


def _reachable_stats(start: str, adj: dict[str, list[str]]) -> tuple[int, int]:
    if not start or start not in adj:
        return (0, 0)
    # BFS for unique peers + longest path depth (simple DFS with cycle guard)
    seen: set[str] = {start}
    queue = [start]
    while queue:
        node = queue.pop(0)
        for nxt in adj.get(node, []):
            if nxt and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    reachable = seen - {start}

    # Longest path from start
    best = [0]

    def dfs(node: str, depth: int, visited: set[str]) -> None:
        if depth > best[0]:
            best[0] = depth
        for nxt in adj.get(node, []):
            if nxt and nxt not in visited and depth < 10:
                dfs(nxt, depth + 1, visited | {nxt})

    dfs(start, 0, {start})
    return (best[0], len(reachable))


# ── Analysis driver ───────────────────────────────────────────────────────────

@dataclass
class AnalysisAggregate:
    total_events: int = 0
    passed_events: int = 0
    failed_events: int = 0
    category_counts: Counter = field(default_factory=Counter)
    category_counts_by_role: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    category_counts_by_domain: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    # Interaction distributions
    peer_messages_passed: list[int] = field(default_factory=list)
    peer_messages_failed: list[int] = field(default_factory=list)
    tool_calls_passed: list[int] = field(default_factory=list)
    tool_calls_failed: list[int] = field(default_factory=list)
    # Pass rate by expected peer hops
    pass_by_hops: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))
    pass_by_observed_peer_bucket: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))
    # Cache stats
    classifier_calls: int = 0
    classifier_cache_hits: int = 0
    classifier_input_tokens: int = 0
    classifier_output_tokens: int = 0

    def bucket_hops(self, n: int) -> str:
        if n <= 0:
            return "0"
        if n >= 4:
            return "4+"
        return str(n)

    def bucket_msgs(self, n: int) -> str:
        if n == 0:
            return "0"
        if n <= 2:
            return "1-2"
        if n <= 5:
            return "3-5"
        if n <= 10:
            return "6-10"
        return "11+"

    def record(
        self,
        passed: bool,
        role: Optional[str],
        domain: Optional[str],
        metrics: InteractionMetrics,
        expected: ExpectedComplexity,
        classifier: Optional[ClassifierResult],
    ) -> None:
        self.total_events += 1
        if passed:
            self.passed_events += 1
            self.peer_messages_passed.append(metrics.peer_messages_count)
            self.tool_calls_passed.append(metrics.tool_calls_count)
        else:
            self.failed_events += 1
            self.peer_messages_failed.append(metrics.peer_messages_count)
            self.tool_calls_failed.append(metrics.tool_calls_count)
            if classifier:
                cat = classifier.category
                self.category_counts[cat] += 1
                if role:
                    self.category_counts_by_role[role][cat] += 1
                if domain:
                    self.category_counts_by_domain[domain][cat] += 1

        hops_bucket = self.bucket_hops(expected.expected_peer_hops)
        self.pass_by_hops[hops_bucket].append(passed)
        msg_bucket = self.bucket_msgs(metrics.peer_messages_count)
        self.pass_by_observed_peer_bucket[msg_bucket].append(passed)

    def to_report(self) -> dict:
        def mean(xs: list[float]) -> Optional[float]:
            return round(statistics.mean(xs), 3) if xs else None

        def rate(votes: list[bool]) -> Optional[float]:
            return round(sum(votes) / len(votes), 3) if votes else None

        pass_by_hops_out = {
            k: {"pass_rate": rate(v), "n": len(v)} for k, v in sorted(self.pass_by_hops.items())
        }
        pass_by_msgs_out = {
            k: {"pass_rate": rate(v), "n": len(v)}
            for k, v in sorted(self.pass_by_observed_peer_bucket.items())
        }

        return {
            "total_events": self.total_events,
            "passed_events": self.passed_events,
            "failed_events": self.failed_events,
            "pass_rate": rate([True] * self.passed_events + [False] * self.failed_events),
            "failure_category_counts": dict(self.category_counts),
            "failure_category_counts_by_role": {
                role: dict(cnt) for role, cnt in self.category_counts_by_role.items()
            },
            "failure_category_counts_by_domain": {
                domain: dict(cnt) for domain, cnt in self.category_counts_by_domain.items()
            },
            "mean_peer_messages_passed": mean(self.peer_messages_passed),
            "mean_peer_messages_failed": mean(self.peer_messages_failed),
            "mean_tool_calls_passed": mean(self.tool_calls_passed),
            "mean_tool_calls_failed": mean(self.tool_calls_failed),
            "pass_rate_by_expected_hops": pass_by_hops_out,
            "pass_rate_by_observed_peer_msgs": pass_by_msgs_out,
            "classifier_calls": self.classifier_calls,
            "classifier_cache_hits": self.classifier_cache_hits,
            "classifier_input_tokens": self.classifier_input_tokens,
            "classifier_output_tokens": self.classifier_output_tokens,
        }


def _detect_format(first_record: dict) -> str:
    if first_record.get("record_type") == "run_config":
        return "default"
    if first_record.get("type") == "meta":
        return "baseline"
    return "unknown"


def _resolve_test_cases_path(first_record: dict, eval_path: Path) -> Optional[Path]:
    """Try to find the source test_cases.jsonl that produced this results file."""
    fmt = _detect_format(first_record)
    candidate: Optional[str] = None
    if fmt == "default":
        candidate = first_record.get("input_path")
    elif fmt == "baseline":
        params = first_record.get("params", {}) or {}
        candidate = params.get("input")
    if not candidate:
        return None
    p = Path(candidate)
    if p.exists():
        return p
    # Try relative to repo root
    repo_root = Path(__file__).parent.parent.parent
    p2 = repo_root / candidate
    if p2.exists():
        return p2
    return None


def analyze_file(
    input_path: Path,
    classifier: Optional[FailureClassifier] = None,
    *,
    output_jsonl: Optional[Path] = None,
    output_report: Optional[Path] = None,
    progress: bool = False,
    max_classify: Optional[int] = None,
) -> dict:
    """Analyze one eval-results JSONL file. Returns the report dict."""
    input_path = Path(input_path)
    if output_jsonl is None:
        output_jsonl = input_path.with_suffix(".analysis.jsonl")
    if output_report is None:
        output_report = input_path.with_suffix(".analysis.report.json")

    with open(input_path) as f:
        records = [json.loads(line) for line in f if line.strip()]
    if not records:
        report = {"total_events": 0, "note": "empty input"}
        output_report.write_text(json.dumps(report, indent=2))
        return report

    fmt = _detect_format(records[0])
    tc_path = _resolve_test_cases_path(records[0], input_path)
    expected_idx = _compute_expected_complexity_index(tc_path)

    agg = AnalysisAggregate()
    out_records: list[dict] = []

    iterator: Iterable[dict] = records
    if progress:
        try:
            from tqdm import tqdm
            iterator = tqdm(records, desc=input_path.name)
        except ImportError:
            pass

    for rec in iterator:
        if "events" not in rec or not isinstance(rec.get("events"), list):
            # Config / summary records — pass through
            out_records.append(rec)
            continue

        tc_id = rec.get("tc_id", "")
        domain = rec.get("domain")
        new_events = []
        for ev in rec.get("events", []):
            evidence = ev.get("evidence", "") or ""
            metrics = _extract_metrics(evidence)
            event_id = ev.get("event_id", "")
            expected = expected_idx.get((tc_id, event_id), ExpectedComplexity(0, 0))

            # Overlay metrics onto the event copy
            ev_copy = dict(ev)
            ev_copy["peer_messages_count"] = metrics.peer_messages_count
            ev_copy["tool_calls_count"] = metrics.tool_calls_count
            ev_copy["unique_peers_touched"] = metrics.unique_peers_touched
            ev_copy["chain_depth"] = metrics.chain_depth
            ev_copy["expected_peer_hops"] = expected.expected_peer_hops
            ev_copy["expected_unique_peers"] = expected.expected_unique_peers

            passed = bool(ev.get("passed", False))
            classifier_res: Optional[ClassifierResult] = None
            budget_ok = max_classify is None or agg.classifier_calls < max_classify
            if not passed and classifier is not None and budget_ok:
                classifier_res = classifier.classify(
                    condition=ev.get("expected", "") or "",
                    reasoning=ev.get("reasoning", "") or "",
                    evidence=evidence,
                    prior_context=ev.get("prior_context", "") or "",
                    event_id=event_id,
                )
                ev_copy["failure_category"] = classifier_res.category
                ev_copy["failure_rationale"] = classifier_res.short_rationale
                ev_copy["failure_confidence"] = classifier_res.confidence
                agg.classifier_calls += 1
                if classifier_res.cached:
                    agg.classifier_cache_hits += 1
                agg.classifier_input_tokens += classifier_res.input_tokens
                agg.classifier_output_tokens += classifier_res.output_tokens

            agg.record(
                passed=passed,
                role=ev.get("role"),
                domain=domain,
                metrics=metrics,
                expected=expected,
                classifier=classifier_res,
            )
            new_events.append(ev_copy)

        rec_copy = dict(rec)
        rec_copy["events"] = new_events
        out_records.append(rec_copy)

    report = agg.to_report()
    report["source"] = str(input_path)
    report["format"] = fmt
    report["test_cases_path"] = str(tc_path) if tc_path else None

    with open(output_jsonl, "w") as f:
        for r in out_records:
            f.write(json.dumps(r) + "\n")
    with open(output_report, "w") as f:
        json.dump(report, f, indent=2)

    return report


def format_report_stdout(report: dict) -> str:
    """Render the report as a short, human-readable table."""
    lines: list[str] = []
    lines.append(f"Source: {report.get('source', '?')}  ({report.get('format', '?')})")
    tcs = report.get("test_cases_path")
    if tcs:
        lines.append(f"Test cases: {tcs}")
    lines.append(
        f"Events: {report['total_events']}  "
        f"pass={report['passed_events']} fail={report['failed_events']} "
        f"rate={report.get('pass_rate')}"
    )

    cat = report.get("failure_category_counts") or {}
    if cat:
        lines.append("\nFailure categories:")
        total = sum(cat.values()) or 1
        for k in sorted(cat, key=cat.get, reverse=True):
            v = cat[k]
            lines.append(f"  {k:<30s} {v:>5d}  ({v/total:.1%})")

    hops = report.get("pass_rate_by_expected_hops") or {}
    if hops:
        lines.append("\nPass rate by expected peer hops (hypothesis):")
        for k in sorted(hops):
            rec = hops[k]
            lines.append(f"  hops={k:<3s}  pass_rate={rec['pass_rate']}  n={rec['n']}")

    msgs = report.get("pass_rate_by_observed_peer_msgs") or {}
    if msgs:
        lines.append("\nPass rate by observed peer messages:")
        order = ["0", "1-2", "3-5", "6-10", "11+"]
        for k in [x for x in order if x in msgs]:
            rec = msgs[k]
            lines.append(f"  msgs={k:<5s} pass_rate={rec['pass_rate']}  n={rec['n']}")

    lines.append(
        f"\nMean peer msgs: passed={report.get('mean_peer_messages_passed')} "
        f"failed={report.get('mean_peer_messages_failed')}"
    )
    lines.append(
        f"Mean tool calls: passed={report.get('mean_tool_calls_passed')} "
        f"failed={report.get('mean_tool_calls_failed')}"
    )
    lines.append(
        f"\nClassifier: calls={report.get('classifier_calls')} "
        f"cache_hits={report.get('classifier_cache_hits')} "
        f"tokens_in={report.get('classifier_input_tokens')} "
        f"tokens_out={report.get('classifier_output_tokens')}"
    )
    return "\n".join(lines)
