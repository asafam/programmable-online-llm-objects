import { createContext, useContext } from 'react';
import type { SampleListEntry } from './types';

interface SampleContextValue {
  entries: SampleListEntry[];
  runId: string | null;
  datasetName: string | null;
  datasetVersion: number | null;
  getAdjacentIds: (id: string) => { prev: string | null; next: string | null; index: number; total: number };
}

export const SampleContext = createContext<SampleContextValue>({
  entries: [],
  runId: null,
  datasetName: null,
  datasetVersion: null,
  getAdjacentIds: () => ({ prev: null, next: null, index: -1, total: 0 }),
});

export function useSampleContext() {
  return useContext(SampleContext);
}
