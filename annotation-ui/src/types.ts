export interface PeerDecl {
  object_id: string;
  relationship: string;
}

export interface ObjectDef {
  object_id: string;
  role: string;
  state_description: string;
  behavior: string;
  peers: PeerDecl[];
  skills: string[];
  subscriptions: string[];
  event_sources: string[];
}

export interface EventExpect {
  action: string;
  reason: string;
}

export type EventRole = 'base' | 'pre_mod' | 'post_mod' | 'irrelevant';

export interface SampleEvent {
  id: string;
  call_type: string;
  source: string;
  recipient: string;
  input: string;
  when: string;
  expect: EventExpect | null;
  triggered_by: string | null;
  trigger_delay_minutes: number;
  trigger_delay_seconds: number;
  role: EventRole | null;
  after_mod_ids: string[];
  depends_on: string[];
  concurrent_group: string | null;
}

export type ModType = 'temporal' | 'contextual' | 'exception' | 'correction' | 'expansion' | 'removal';
export type Ambiguity = 'precise' | 'semantic' | 'vague' | 'implicit';

export interface Modification {
  id: string;
  call_type: string;
  source: string;
  target: string;
  when: string;
  mod_type: ModType;
  intent: string;
  ambiguity: Ambiguity;
}

export type StateConstraintType = 'cap' | 'counter' | 'rate_limit' | 'trigger';

export interface StateConstraint {
  type: StateConstraintType;
  threshold: string;
  description: string;
}

export interface Sample {
  id: string;
  sample_id: string;
  name: string;
  domain: string;
  source_type: string;
  link: string;
  seed?: string;
  objects: ObjectDef[];
  llm_classes: unknown[];
  steps: string[];
  modifications: Modification[];
  events: SampleEvent[];
  tools: unknown[];
  state_constraint?: StateConstraint | null;
}

export interface SampleSummary {
  id: string;
  sample_id: string;
  name: string;
  domain: string;
  source_type: string;
  link: string;
  mod_type: ModType | null;
  state_constraint_type: StateConstraintType | null;
  order: number;
  versions?: string[];  // all ids sharing this sample_id, ordered as in JSONL
}

export type Verdict = 'pending' | 'accepted' | 'rejected';

export interface ModAnnotation {
  verdict: Verdict;
  comment: string;
}

export interface Annotation {
  sample_verdict: Verdict;
  sample_comment: string;
  modifications: Record<string, ModAnnotation>;
  annotator: string;
  created_at: unknown;
  updated_at: unknown;
}

export interface SampleListEntry extends SampleSummary {
  verdict: Verdict;
  updated_at?: unknown;
}
