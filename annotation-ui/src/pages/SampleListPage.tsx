import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { signOut } from 'firebase/auth';
import { collection, getDocs, query, where } from 'firebase/firestore';
import { auth, db } from '../firebase';
import { useSampleContext } from '../SampleContext';
import type { Annotation, ModType, SampleSummary, StateConstraintType, Verdict } from '../types';

interface RunVersion { run_id: string; dataset_version: number; }

const modTypeColors: Record<ModType, string> = {
  temporal: 'bg-blue-100 text-blue-700',
  contextual: 'bg-teal-100 text-teal-700',
  exception: 'bg-orange-100 text-orange-700',
  correction: 'bg-yellow-100 text-yellow-700',
  expansion: 'bg-green-100 text-green-700',
  removal: 'bg-red-100 text-red-700',
};

const stateConstraintColors: Record<StateConstraintType, string> = {
  cap: 'bg-amber-100 text-amber-700',
  counter: 'bg-purple-100 text-purple-700',
  rate_limit: 'bg-orange-100 text-orange-700',
  trigger: 'bg-pink-100 text-pink-700',
};

const verdictColors: Record<Verdict, string> = {
  pending: 'bg-gray-100 text-gray-600',
  accepted: 'bg-green-100 text-green-700',
  rejected: 'bg-red-100 text-red-700',
};

const PAGE_SIZE = 50;

export default function SampleListPage({ loading }: { loading: boolean }) {
  const { entries, runId, datasetName, datasetVersion } = useSampleContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const [exporting, setExporting] = useState(false);
  const [versions, setVersions] = useState<RunVersion[]>([]);
  const navigate = useNavigate();

  useEffect(() => {
    if (!datasetName) return;
    getDocs(query(collection(db, 'runs'), where('dataset_name', '==', datasetName)))
      .then(snap => {
        const list: RunVersion[] = [];
        snap.forEach(d => {
          const data = d.data();
          list.push({ run_id: data.run_id, dataset_version: data.dataset_version ?? 0 });
        });
        list.sort((a, b) => a.dataset_version - b.dataset_version);
        setVersions(list);
      });
  }, [datasetName]);

  const filterVerdict = (searchParams.get('verdict') ?? 'all') as Verdict | 'all';
  const filterModType = (searchParams.get('mod_type') ?? 'all') as ModType | 'all';
  const filterSC = (searchParams.get('sc_type') ?? 'all') as StateConstraintType | 'all';
  const page = parseInt(searchParams.get('page') ?? '1', 10);

  const setFilter = (key: string, value: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set(key, value);
      next.set('page', '1');
      return next;
    });
  };

  const filtered = useMemo(() => {
    return entries.filter((e) => {
      if (filterVerdict !== 'all' && e.verdict !== filterVerdict) return false;
      if (filterModType !== 'all' && e.mod_type !== filterModType) return false;
      if (filterSC !== 'all' && e.state_constraint_type !== filterSC) return false;
      return true;
    });
  }, [entries, filterVerdict, filterModType, filterSC]);

  const totalItems = filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const paginated = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  const counts = useMemo(() => ({
    total: entries.length,
    accepted: entries.filter((e) => e.verdict === 'accepted').length,
    rejected: entries.filter((e) => e.verdict === 'rejected').length,
    pending: entries.filter((e) => e.verdict === 'pending').length,
  }), [entries]);

  const sampleHref = (id: string) => `/runs/${encodeURIComponent(runId ?? '')}/sample/${encodeURIComponent(id)}`;

  const exportAnnotations = async () => {
    setExporting(true);
    try {
      const [summariesSnap, annotationsSnap] = await Promise.all([
        getDocs(collection(db, 'runs', runId ?? '', 'summaries')),
        getDocs(collection(db, 'annotations')),
      ]);
      const annotationsMap = new Map<string, Annotation>();
      annotationsSnap.forEach((doc) => annotationsMap.set(doc.id, doc.data() as Annotation));

      const lines: string[] = [];
      summariesSnap.forEach((doc) => {
        const summary = doc.data() as SampleSummary;
        const annKey = runId ? `${runId}__${doc.id}` : doc.id;
        const ann = annotationsMap.get(annKey);
        if (!ann || ann.sample_verdict === 'pending') return;
        lines.push(JSON.stringify({
          id: summary.id,
          sample_id: summary.sample_id,
          name: summary.name,
          domain: summary.domain,
          mod_type: summary.mod_type,
          state_constraint_type: summary.state_constraint_type,
          sample_verdict: ann.sample_verdict,
          sample_comment: ann.sample_comment,
          modifications: ann.modifications,
          annotator: ann.annotator,
        }));
      });

      const blob = new Blob([lines.join('\n')], { type: 'application/x-ndjson' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `annotations_${runId}_${new Date().toISOString().slice(0, 10)}.jsonl`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Link to="/runs" className="text-sm text-gray-400 hover:text-gray-600 transition-colors">← Datasets</Link>
            <h1 className="text-base font-bold text-gray-900">{datasetName ?? runId}</h1>
            {versions.length > 1 ? (
              <select
                value={runId ?? ''}
                onChange={e => navigate(`/runs/${encodeURIComponent(e.target.value)}`)}
                className="text-xs border border-gray-300 rounded px-2 py-1 bg-white text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-300"
              >
                {[...versions].reverse().map(v => (
                  <option key={v.run_id} value={v.run_id}>v{v.dataset_version}</option>
                ))}
              </select>
            ) : datasetVersion != null && (
              <span className="text-xs font-semibold bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">
                v{datasetVersion}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={exportAnnotations}
              disabled={exporting}
              className="text-sm px-3 py-1.5 bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50 transition-colors"
            >
              {exporting ? 'Exporting...' : '↓ Export Annotations'}
            </button>
            <button
              onClick={() => signOut(auth)}
              className="text-sm text-gray-500 hover:text-gray-700 transition-colors"
            >
              Sign out
            </button>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 py-6 space-y-5">
        {/* Progress */}
        {!loading && counts.total > 0 && (
          <div className="bg-white rounded-xl border border-gray-200 p-4">
            <div className="flex items-center gap-4 text-sm mb-3">
              <span className="font-semibold text-gray-900">{counts.total} samples</span>
              <span className="text-green-600 font-medium">{counts.accepted} accepted</span>
              <span className="text-red-600 font-medium">{counts.rejected} rejected</span>
              <span className="text-gray-500">{counts.pending} pending</span>
              <span className="ml-auto text-gray-400">
                {Math.round(((counts.accepted + counts.rejected) / counts.total) * 100)}% reviewed
              </span>
            </div>
            <div className="h-2 bg-gray-100 rounded-full overflow-hidden flex">
              <div className="bg-green-500 transition-all" style={{ width: `${(counts.accepted / counts.total) * 100}%` }} />
              <div className="bg-red-400 transition-all" style={{ width: `${(counts.rejected / counts.total) * 100}%` }} />
            </div>
          </div>
        )}

        {/* Filters */}
        <div className="flex flex-wrap gap-3 items-center">
          <FilterGroup label="Verdict" value={filterVerdict}
            options={[{value:'all',label:'All'},{value:'pending',label:'Pending'},{value:'accepted',label:'Accepted'},{value:'rejected',label:'Rejected'}]}
            onChange={(v) => setFilter('verdict', v)} />
          <FilterGroup label="Mod type" value={filterModType}
            options={[{value:'all',label:'All'},{value:'temporal',label:'Temporal'},{value:'contextual',label:'Contextual'},{value:'exception',label:'Exception'},{value:'correction',label:'Correction'},{value:'expansion',label:'Expansion'},{value:'removal',label:'Removal'}]}
            onChange={(v) => setFilter('mod_type', v)} />
          <FilterGroup label="State constraint" value={filterSC}
            options={[{value:'all',label:'All'},{value:'cap',label:'Cap'},{value:'counter',label:'Counter'},{value:'rate_limit',label:'Rate limit'},{value:'trigger',label:'Trigger'}]}
            onChange={(v) => setFilter('sc_type', v)} />
          {filtered.length !== entries.length && (
            <span className="text-sm text-gray-500 ml-2">{filtered.length} shown</span>
          )}
        </div>

        {/* Table */}
        {loading ? (
          <div className="text-center py-12 text-gray-400">Loading samples...</div>
        ) : (
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">#</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Name</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Mod type</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">State constraint</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Domain</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Verdict</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {paginated.map((entry, i) => (
                  <tr key={entry.id} className="hover:bg-blue-50 transition-colors">
                    <td className="px-4 py-3 text-gray-400 text-xs">{(currentPage - 1) * PAGE_SIZE + i + 1}</td>
                    <td className="px-4 py-3">
                      <Link to={sampleHref(entry.id)} className="font-medium text-blue-700 hover:underline">
                        {entry.name}
                      </Link>
                      <div className="text-xs text-gray-400 font-mono truncate max-w-xs">{entry.id}</div>
                    </td>
                    <td className="px-4 py-3">
                      {entry.mod_type && (
                        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${modTypeColors[entry.mod_type]}`}>{entry.mod_type}</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {entry.state_constraint_type && (
                        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${stateConstraintColors[entry.state_constraint_type] ?? 'bg-gray-100 text-gray-600'}`}>{entry.state_constraint_type}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-gray-600">{entry.domain}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${verdictColors[entry.verdict]}`}>{entry.verdict}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {totalPages > 1 && (
              <div className="px-4 py-3 border-t border-gray-100 flex items-center justify-between text-sm text-gray-600">
                <span>{(currentPage - 1) * PAGE_SIZE + 1}–{Math.min(currentPage * PAGE_SIZE, totalItems)} of {totalItems}</span>
                <div className="flex gap-2">
                  <button disabled={currentPage <= 1}
                    onClick={() => setSearchParams((p) => { const n = new URLSearchParams(p); n.set('page', String(currentPage - 1)); return n; })}
                    className="px-3 py-1 rounded border border-gray-300 disabled:opacity-40 hover:bg-gray-50 transition-colors">← Prev</button>
                  <span className="px-3 py-1">Page {currentPage} / {totalPages}</span>
                  <button disabled={currentPage >= totalPages}
                    onClick={() => setSearchParams((p) => { const n = new URLSearchParams(p); n.set('page', String(currentPage + 1)); return n; })}
                    className="px-3 py-1 rounded border border-gray-300 disabled:opacity-40 hover:bg-gray-50 transition-colors">Next →</button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function FilterGroup({ label, value, options, onChange }: {
  label: string; value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs font-semibold text-gray-500 mr-1">{label}:</span>
      {options.map((opt) => (
        <button key={opt.value} onClick={() => onChange(opt.value)}
          className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
            value === opt.value ? 'bg-gray-800 text-white border-gray-800' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'
          }`}>
          {opt.label}
        </button>
      ))}
    </div>
  );
}
