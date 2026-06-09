import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { doc, getDoc } from 'firebase/firestore';
import { db } from '../firebase';
import { useAnnotation } from '../hooks/useAnnotation';
import { useSampleContext } from '../SampleContext';
import { useAuth } from '../hooks/useAuth';
import ObjectCard from '../components/ObjectCard';
import ModificationCard from '../components/ModificationCard';
import AnnotationPanel from '../components/AnnotationPanel';
import EventCard from '../components/EventCard';
import type { Sample, Verdict, StateConstraint, SampleEvent } from '../types';

const verdictBadge: Record<Verdict, string> = {
  pending: 'bg-gray-100 text-gray-500',
  accepted: 'bg-green-100 text-green-700',
  rejected: 'bg-red-100 text-red-700',
};

export default function SampleDetailPage() {
  const { sampleId } = useParams<{ sampleId: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { getAdjacentIds, entries, runId } = useSampleContext();

  const [sample, setSample] = useState<Sample | null>(null);
  const [sampleLoading, setSampleLoading] = useState(true);
  const [sampleError, setSampleError] = useState<string | null>(null);
  const [isAtBottom, setIsAtBottom] = useState(false);

  const sentinelRef = useRef<HTMLDivElement>(null);

  const id = decodeURIComponent(sampleId ?? '');
  const { prev, next, index, total } = getAdjacentIds(id);
  const versions = entries.find(e => e.id === id)?.versions ?? [id];
  const listHref = runId ? `/runs/${runId}` : '/';
  const sampleHref = (sid: string) => runId ? `/runs/${runId}/sample/${encodeURIComponent(sid)}` : `/sample/${encodeURIComponent(sid)}`;

  const {
    annotation,
    setSampleVerdict,
    setSampleComment,
    setModVerdict,
    setModComment,
    getModAnnotation,
  } = useAnnotation(id, user?.email ?? '', runId ?? undefined);

  useEffect(() => {
    if (!id) return;
    setSampleLoading(true);
    setSampleError(null);
    setIsAtBottom(false);
    getDoc(doc(db, 'samples', id)).then((snap) => {
      if (snap.exists()) setSample(snap.data() as Sample);
      else setSampleError('Sample not found.');
      setSampleLoading(false);
    }).catch((e) => { setSampleError(String(e)); setSampleLoading(false); });
  }, [id]);

  // Observe sentinel to know when annotation panel is naturally visible
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => setIsAtBottom(entry.isIntersecting),
      { threshold: 0.1 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [sample]); // re-observe after sample loads

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (document.activeElement as HTMLElement)?.tagName;
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
      if (e.key === 'j' || e.key === 'ArrowRight') {
        if (next) navigate(sampleHref(next));
      } else if (e.key === 'k' || e.key === 'ArrowLeft') {
        if (prev) navigate(sampleHref(prev));
      } else if (e.key === 'a') {
        setSampleVerdict(annotation.sample_verdict === 'accepted' ? 'pending' : 'accepted');
      } else if (e.key === 'r') {
        setSampleVerdict(annotation.sample_verdict === 'rejected' ? 'pending' : 'rejected');
      } else if (e.key === 'Escape') {
        (document.activeElement as HTMLElement)?.blur();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [next, prev, navigate, setSampleVerdict, annotation.sample_verdict]);

  if (sampleLoading) {
    return <div className="min-h-screen bg-gray-50 flex items-center justify-center text-gray-400">Loading sample...</div>;
  }
  if (sampleError || !sample) {
    return <div className="min-h-screen bg-gray-50 flex items-center justify-center text-red-500">{sampleError ?? 'Unknown error'}</div>;
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Nav bar */}
      <div className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-4 py-3 flex items-center gap-3">
          <Link to={listHref} className="text-sm text-gray-500 hover:text-gray-700 transition-colors">
            ← Back to list
          </Link>
          {index >= 0 && <span className="text-sm text-gray-400">Sample {index + 1} / {total}</span>}
          <div className="ml-auto flex items-center gap-2">
            <button disabled={!prev} onClick={() => prev && navigate(sampleHref(prev))}
              className="px-3 py-1.5 text-sm rounded border border-gray-300 disabled:opacity-40 hover:bg-gray-50 transition-colors" title="Previous (k / ←)">
              ← Prev
            </button>
            <button disabled={!next} onClick={() => next && navigate(sampleHref(next))}
              className="px-3 py-1.5 text-sm rounded border border-gray-300 disabled:opacity-40 hover:bg-gray-50 transition-colors" title="Next (j / →)">
              Next →
            </button>
          </div>
        </div>
      </div>

      {/* Floating annotation bar — visible while scrolling, hides when sentinel is reached */}
      <div className={`fixed bottom-0 left-0 right-0 z-20 transition-transform duration-200 ${isAtBottom ? 'translate-y-full' : 'translate-y-0'}`}>
        <div className="max-w-4xl mx-auto px-4 pb-0">
          <div className="bg-white border border-gray-200 border-b-0 rounded-t-xl shadow-lg px-5 pt-3 pb-4 space-y-2">
            {/* Top row: name + verdict + buttons */}
            <div className="flex items-center gap-3 flex-wrap">
              <span className="text-sm font-medium text-gray-700 truncate max-w-xs">{sample.name}</span>
              <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${verdictBadge[annotation.sample_verdict]}`}>
                {annotation.sample_verdict}
              </span>
              <div className="flex items-center gap-2 ml-auto">
                <button
                  onClick={() => setSampleVerdict(annotation.sample_verdict === 'accepted' ? 'pending' : 'accepted')}
                  className={`px-3 py-1.5 text-sm font-medium rounded border transition-colors ${
                    annotation.sample_verdict === 'accepted'
                      ? 'bg-green-600 text-white border-green-600'
                      : 'bg-white text-green-700 border-green-300 hover:bg-green-50'
                  }`}
                >
                  ✓ Accept (a)
                </button>
                <button
                  onClick={() => setSampleVerdict(annotation.sample_verdict === 'rejected' ? 'pending' : 'rejected')}
                  className={`px-3 py-1.5 text-sm font-medium rounded border transition-colors ${
                    annotation.sample_verdict === 'rejected'
                      ? 'bg-red-600 text-white border-red-600'
                      : 'bg-white text-red-700 border-red-300 hover:bg-red-50'
                  }`}
                >
                  ✗ Reject (r)
                </button>
                {annotation.sample_verdict !== 'pending' && (
                  <button onClick={() => setSampleVerdict('pending')}
                    className="px-3 py-1.5 text-sm font-medium rounded border bg-white text-gray-600 border-gray-300 hover:bg-gray-50 transition-colors">
                    ↩ Reset
                  </button>
                )}
                <button disabled={!next} onClick={() => next && navigate(sampleHref(next))}
                  className="px-3 py-1.5 text-sm font-medium rounded border bg-gray-800 text-white border-gray-800 disabled:opacity-40 hover:bg-gray-700 transition-colors">
                  Next →
                </button>
              </div>
            </div>
            {/* Comment field */}
            <textarea
              value={annotation.sample_comment}
              onChange={(e) => setSampleComment(e.target.value)}
              placeholder="Add a comment..."
              rows={2}
              className="w-full text-sm border border-gray-300 rounded px-3 py-2 resize-none focus:outline-none focus:ring-2 focus:ring-blue-300 placeholder-gray-400"
            />
          </div>
        </div>
      </div>

      <div className="max-w-4xl mx-auto px-4 py-6 space-y-6" style={{ paddingBottom: isAtBottom ? '1.5rem' : '5rem' }}>
        {/* Header card — name + metadata only */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <div className="flex items-start justify-between gap-4 mb-2">
            <div>
              <h1 className="text-xl font-bold text-gray-900">{sample.name}</h1>
              <div className="font-mono text-xs text-gray-400 mt-0.5">{sample.id}</div>
            </div>
          </div>
          <div className="flex flex-wrap gap-3 text-sm text-gray-600">
            <span><span className="font-medium">Domain:</span> {sample.domain}</span>
            <span><span className="font-medium">Base:</span> <span className="font-mono text-xs">{sample.sample_id}</span></span>
          </div>
          {versions.length > 1 && (
            <div className="mt-3 flex items-center gap-2 flex-wrap">
              <span className="text-xs text-gray-500 font-medium">Versions:</span>
              {versions.map((vid) => (
                <button
                  key={vid}
                  onClick={() => navigate(sampleHref(vid))}
                  className={`text-xs px-2 py-0.5 rounded font-mono border transition-colors ${
                    vid === id
                      ? 'bg-gray-800 text-white border-gray-800'
                      : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'
                  }`}
                >
                  {vid}
                </button>
              ))}
            </div>
          )}
          <p className="text-xs text-gray-400 mt-3">Shortcuts: a = accept, r = reject, j/→ = next, k/← = prev</p>
        </div>

        {/* Workflow section */}
        <WorkflowSection
          steps={sample.steps}
          link={sample.link}
          sourceType={sample.source_type}
        />

        {/* Seed — initial reference state */}
        {sample.seed && <SeedSection seed={sample.seed} />}

        {/* State constraint (separate card) */}
        {sample.state_constraint && (
          <StateConstraintSection
            stateConstraint={sample.state_constraint}
            scenarioEvents={sample.events.filter((e) => /^SC\d+/.test(e.id))}
          />
        )}


        {/* Modifications */}
        <Section title={`Modifications (${sample.modifications.length})`}>
          <div className="space-y-4">
            {sample.modifications.map((mod) => {
              const modAnn = getModAnnotation(mod.id);
              const targetObj = sample.objects.find((o) => o.object_id === mod.target) ?? null;
              return (
                <ModificationCard key={mod.id} mod={mod} targetObj={targetObj} sample={sample}
                  annotation={modAnn}
                  onVerdictChange={(v) => setModVerdict(mod.id, v)}
                  onCommentChange={(c) => setModComment(mod.id, c)} />
              );
            })}
          </div>
        </Section>

        {/* Objects */}
        <Section title={`Objects (${sample.objects.length})`}>
          <div className="space-y-2">
            {sample.objects.map((obj) => <ObjectCard key={obj.object_id} obj={obj} />)}
          </div>
        </Section>

        {/* Sentinel + inline annotation panel — appears when user scrolls to bottom */}
        <div ref={sentinelRef}>
          <div className="bg-white rounded-xl border border-gray-200 p-5">
            <h2 className="text-base font-semibold text-gray-800 mb-1">Sample verdict</h2>
            <p className="text-xs text-gray-400 mb-3">{sample.name}</p>
            <AnnotationPanel
              verdict={annotation.sample_verdict}
              comment={annotation.sample_comment}
              onVerdictChange={setSampleVerdict}
              onCommentChange={setSampleComment}
              label="Sample"
              showShortcuts
            />
          </div>
        </div>

        {/* Bottom navigation */}
        <div className="flex justify-between pb-8">
          <button disabled={!prev} onClick={() => prev && navigate(sampleHref(prev))}
            className="px-4 py-2 text-sm rounded-lg border border-gray-300 disabled:opacity-40 hover:bg-white transition-colors">
            ← Previous sample
          </button>
          <button disabled={!next} onClick={() => next && navigate(sampleHref(next))}
            className="px-4 py-2 text-sm rounded-lg border border-gray-300 disabled:opacity-40 hover:bg-white transition-colors">
            Next sample →
          </button>
        </div>
      </div>
    </div>
  );
}

function WorkflowSection({
  steps, link, sourceType,
}: {
  steps: string[];
  link?: string;
  sourceType?: string;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
        <h2 className="text-base font-semibold text-gray-800">Workflow Steps</h2>
        {link && (
          <a href={link} target="_blank" rel="noopener noreferrer"
            className="text-xs font-medium text-blue-600 hover:underline">
            View on {sourceType} ↗
          </a>
        )}
      </div>
      <div className="px-5 py-4">
        <ol className="space-y-2">
          {steps.map((step, i) => (
            <li key={i} className="flex gap-3 text-sm text-gray-700">
              <span className="flex-shrink-0 w-6 h-6 bg-gray-100 text-gray-600 rounded-full flex items-center justify-center text-xs font-bold">{i + 1}</span>
              <span className="leading-relaxed pt-0.5">{step}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function SeedSection({ seed }: { seed: string }) {
  let pretty: string;
  try {
    pretty = JSON.stringify(JSON.parse(seed), null, 2);
  } catch {
    pretty = seed;
  }
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-100">
        <h2 className="text-base font-semibold text-gray-800">Seed — initial reference state</h2>
        <p className="text-xs text-gray-400 mt-0.5">Read-service mock data served verbatim by the mock APIs</p>
      </div>
      <pre className="px-5 py-4 text-xs text-gray-700 font-mono leading-relaxed overflow-x-auto whitespace-pre-wrap break-words bg-gray-50">
        {pretty}
      </pre>
    </div>
  );
}

function StateConstraintSection({
  stateConstraint, scenarioEvents,
}: {
  stateConstraint: StateConstraint;
  scenarioEvents: SampleEvent[];
}) {
  return (
    <div className="bg-white rounded-xl border border-amber-200 overflow-hidden">
      <div className="px-5 py-4 border-b border-amber-100 bg-amber-50">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold text-amber-900">State Constraint</h2>
          <span className="text-xs font-semibold bg-amber-200 text-amber-800 px-2 py-0.5 rounded-full">{stateConstraint.type}</span>
          <span className="text-sm text-amber-700">Threshold: {stateConstraint.threshold}</span>
        </div>
        {stateConstraint.description && <p className="text-sm text-amber-800 mt-1">{stateConstraint.description}</p>}
      </div>
      {scenarioEvents.length > 0 && (
        <div className="px-5 py-4">
          <div className="text-xs font-semibold text-amber-700 uppercase tracking-wide mb-2">Scenario ({scenarioEvents.length} events)</div>
          <div className="space-y-2">{scenarioEvents.map((e) => <EventCard key={e.id} event={e} />)}</div>
        </div>
      )}
    </div>
  );
}

function Section({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-base font-semibold text-gray-800">{title}</h2>
        {action}
      </div>
      {children}
    </div>
  );
}
