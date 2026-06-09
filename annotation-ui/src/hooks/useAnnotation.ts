import { useEffect, useRef, useState } from 'react';
import {
  doc, onSnapshot, setDoc, serverTimestamp, getDoc,
} from 'firebase/firestore';
import { db } from '../firebase';
import type { Annotation, ModAnnotation, Verdict } from '../types';

const EMPTY_ANNOTATION: Annotation = {
  sample_verdict: 'pending',
  sample_comment: '',
  modifications: {},
  annotator: '',
  created_at: null,
  updated_at: null,
};

function mergeModComments(
  incoming: Record<string, ModAnnotation>,
  local: Record<string, ModAnnotation>,
): Record<string, ModAnnotation> {
  const result = { ...incoming };
  for (const [id, modAnn] of Object.entries(local)) {
    result[id] = { ...(result[id] ?? modAnn), comment: modAnn.comment };
  }
  return result;
}

export function useAnnotation(sampleId: string, userEmail: string, runId?: string) {
  const [annotation, setAnnotation] = useState<Annotation>(EMPTY_ANNOTATION);
  const [loading, setLoading] = useState(true);
  const commentTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Scope the annotation doc to the run so different dataset versions don't share feedback.
  const annKey = runId ? `${runId}__${sampleId}` : sampleId;

  useEffect(() => {
    setLoading(true);
    const ref = doc(db, 'annotations', annKey);
    const unsub = onSnapshot(ref, (snap) => {
      if (snap.exists()) {
        const incoming = { ...EMPTY_ANNOTATION, ...snap.data() } as Annotation;
        setAnnotation((prev) => {
          // If a debounce timer is pending the user is mid-type — keep their
          // locally-edited comment values rather than overwriting with Firestore.
          if (commentTimerRef.current === null) return incoming;
          return {
            ...incoming,
            sample_comment: prev.sample_comment,
            modifications: mergeModComments(incoming.modifications, prev.modifications),
          };
        });
      } else {
        setAnnotation({ ...EMPTY_ANNOTATION, annotator: userEmail });
      }
      setLoading(false);
    });
    return unsub;
  }, [sampleId, userEmail]);

  const ensureCreatedAt = async () => {
    const ref = doc(db, 'annotations', annKey);
    const snap = await getDoc(ref);
    return snap.exists() ? {} : { created_at: serverTimestamp() };
  };

  const setSampleVerdict = async (verdict: Verdict) => {
    const ref = doc(db, 'annotations', annKey);
    const base = await ensureCreatedAt();
    await setDoc(ref, {
      ...base,
      sample_verdict: verdict,
      annotator: userEmail,
      updated_at: serverTimestamp(),
    }, { merge: true });
  };

  const setSampleComment = (comment: string) => {
    setAnnotation((prev) => ({ ...prev, sample_comment: comment }));
    if (commentTimerRef.current) clearTimeout(commentTimerRef.current);
    commentTimerRef.current = setTimeout(async () => {
      commentTimerRef.current = null;
      const ref = doc(db, 'annotations', annKey);
      const base = await ensureCreatedAt();
      await setDoc(ref, {
        ...base,
        sample_comment: comment,
        annotator: userEmail,
        updated_at: serverTimestamp(),
      }, { merge: true });
    }, 800);
  };

  const setModVerdict = async (modId: string, verdict: Verdict) => {
    const ref = doc(db, 'annotations', annKey);
    const base = await ensureCreatedAt();
    await setDoc(ref, {
      ...base,
      [`modifications.${modId}.verdict`]: verdict,
      annotator: userEmail,
      updated_at: serverTimestamp(),
    }, { merge: true });
  };

  const setModComment = (modId: string, comment: string) => {
    setAnnotation((prev) => ({
      ...prev,
      modifications: {
        ...prev.modifications,
        [modId]: { ...prev.modifications[modId], comment },
      },
    }));
    if (commentTimerRef.current) clearTimeout(commentTimerRef.current);
    commentTimerRef.current = setTimeout(async () => {
      commentTimerRef.current = null;
      const ref = doc(db, 'annotations', annKey);
      const base = await ensureCreatedAt();
      await setDoc(ref, {
        ...base,
        [`modifications.${modId}.comment`]: comment,
        annotator: userEmail,
        updated_at: serverTimestamp(),
      }, { merge: true });
    }, 800);
  };

  const getModAnnotation = (modId: string): ModAnnotation => {
    return (annotation.modifications ?? {})[modId] ?? { verdict: 'pending', comment: '' };
  };

  return {
    annotation,
    loading,
    setSampleVerdict,
    setSampleComment,
    setModVerdict,
    setModComment,
    getModAnnotation,
  };
}
