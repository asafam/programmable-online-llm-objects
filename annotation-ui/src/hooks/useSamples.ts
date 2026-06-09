import { useEffect, useState } from 'react';
import { collection, getDocs, onSnapshot } from 'firebase/firestore';
import { db } from '../firebase';
import type { Annotation, SampleListEntry, SampleSummary, Verdict } from '../types';

export function useSamples(runId: string) {
  const [summaries, setSummaries] = useState<{ summary: SampleSummary; id: string }[]>([]);
  const [annotationsMap, setAnnotationsMap] = useState<Map<string, Annotation>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Load summaries once when runId changes
  useEffect(() => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    getDocs(collection(db, 'runs', runId, 'summaries'))
      .then(snap => {
        const list: { summary: SampleSummary; id: string }[] = [];
        snap.forEach(d => list.push({ id: d.id, summary: d.data() as SampleSummary }));
        setSummaries(list);
      })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false));
  }, [runId]);

  // Live-subscribe to annotations so verdicts update without reload
  useEffect(() => {
    const unsub = onSnapshot(
      collection(db, 'annotations'),
      snap => {
        const map = new Map<string, Annotation>();
        snap.forEach(d => map.set(d.id, d.data() as Annotation));
        setAnnotationsMap(map);
      },
      e => setError(String(e)),
    );
    return unsub;
  }, []);

  const entries: SampleListEntry[] = summaries
    .map(({ id, summary }) => {
      const annKey = runId ? `${runId}__${id}` : id;
      const annotation = annotationsMap.get(annKey);
      return {
        ...summary,
        verdict: ((annotation?.sample_verdict) ?? 'pending') as Verdict,
        updated_at: annotation?.updated_at,
      };
    })
    .sort((a, b) => a.order - b.order);

  return { entries, loading, error };
}
