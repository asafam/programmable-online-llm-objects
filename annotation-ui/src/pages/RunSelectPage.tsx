import { useEffect, useState } from 'react';
import { collection, getDocs, orderBy, query, doc, deleteDoc, writeBatch } from 'firebase/firestore';
import { useNavigate } from 'react-router-dom';
import { db } from '../firebase';
import { signOut } from 'firebase/auth';
import { auth } from '../firebase';

interface RunMeta {
  run_id: string;
  dataset_name: string;
  dataset_version: number;
  created_at: string;
  input_file: string;
  total_samples: number;
  new_samples: number;
}

interface DatasetGroup {
  name: string;
  latest: RunMeta;
  older: RunMeta[];
}

export default function RunSelectPage() {
  const [groups, setGroups] = useState<DatasetGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    getDocs(query(collection(db, 'runs'), orderBy('created_at', 'desc')))
      .then(snap => {
        const all: RunMeta[] = [];
        snap.forEach(d => all.push(d.data() as RunMeta));

        // Group by dataset_name, sort each group by version desc
        const byName = new Map<string, RunMeta[]>();
        all.forEach(run => {
          const name = run.dataset_name ?? run.run_id;
          if (!byName.has(name)) byName.set(name, []);
          byName.get(name)!.push(run);
        });

        const grouped: DatasetGroup[] = [];
        byName.forEach((runs, name) => {
          const sorted = [...runs].sort((a, b) => (b.dataset_version ?? 0) - (a.dataset_version ?? 0));
          grouped.push({ name, latest: sorted[0], older: sorted.slice(1) });
        });
        // Sort groups by latest run's created_at desc
        grouped.sort((a, b) => b.latest.created_at.localeCompare(a.latest.created_at));
        setGroups(grouped);
      })
      .finally(() => setLoading(false));
  }, []);

  const handleDelete = async (runId: string) => {
    setDeleting(runId);
    try {
      const summariesSnap = await getDocs(collection(db, 'runs', runId, 'summaries'));
      const BATCH_SIZE = 500;
      for (let i = 0; i < summariesSnap.docs.length; i += BATCH_SIZE) {
        const batch = writeBatch(db);
        summariesSnap.docs.slice(i, i + BATCH_SIZE).forEach(d => batch.delete(d.ref));
        await batch.commit();
      }
      await deleteDoc(doc(db, 'runs', runId));
      setGroups(prev => prev
        .map(g => ({
          ...g,
          latest: g.latest.run_id === runId ? g.older[0] : g.latest,
          older: g.latest.run_id === runId ? g.older.slice(1) : g.older.filter(r => r.run_id !== runId),
        }))
        .filter(g => g.latest != null)
      );
    } finally {
      setDeleting(null);
      setConfirmingDelete(null);
    }
  };

  const formatDate = (iso: string) => {
    try {
      return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch { return iso; }
  };

  const toggleExpand = (name: string) => {
    setExpandedGroups(prev => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  };

  if (loading) {
    return <div className="min-h-screen bg-gray-50 flex items-center justify-center text-gray-400">Loading datasets...</div>;
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-2xl mx-auto px-4 py-12">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Annotation Backoffice</h1>
            <p className="text-sm text-gray-500 mt-1">Select a dataset to annotate</p>
          </div>
          <button onClick={() => signOut(auth)} className="text-sm text-gray-400 hover:text-gray-600 transition-colors">
            Sign out
          </button>
        </div>

        {groups.length === 0 ? (
          <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-400">
            No datasets uploaded yet. Run the import script to add one.
          </div>
        ) : (
          <div className="space-y-3">
            {groups.map(group => {
              const isExpanded = expandedGroups.has(group.name);
              const runsToShow = isExpanded ? [group.latest, ...group.older] : [group.latest];

              return (
                <div key={group.name} className="bg-white rounded-xl border border-gray-200 overflow-hidden">
                  {runsToShow.map((run, i) => {
                    const isConfirming = confirmingDelete === run.run_id;
                    const isDeleting = deleting === run.run_id;
                    const isOld = i > 0;

                    return (
                      <div
                        key={run.run_id}
                        className={`group flex items-start gap-4 px-5 py-4 transition-colors ${isOld ? 'bg-gray-50 border-t border-gray-100' : 'hover:bg-blue-50'}`}
                      >
                        {/* Main clickable area */}
                        <button
                          className="flex-1 min-w-0 text-left"
                          onClick={() => !isConfirming && navigate(`/runs/${encodeURIComponent(run.run_id)}`)}
                        >
                          <div className="flex items-center gap-2">
                            <span className={`text-sm font-semibold ${isOld ? 'text-gray-500' : 'text-gray-900'}`}>
                              {run.dataset_name ?? run.run_id}
                            </span>
                            {run.dataset_version != null && (
                              <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${isOld ? 'bg-gray-200 text-gray-500' : 'bg-blue-100 text-blue-700'}`}>
                                v{run.dataset_version}
                              </span>
                            )}
                          </div>
                          <div className="text-xs text-gray-400 mt-0.5">{formatDate(run.created_at)}</div>
                          {isOld && <div className="text-xs text-gray-400 font-mono truncate mt-0.5">{run.run_id}</div>}
                        </button>

                        {/* Sample counts */}
                        <div className="text-right text-xs text-gray-400 flex-shrink-0">
                          <div>{run.total_samples} samples</div>
                          <div>{run.new_samples} new</div>
                        </div>

                        {/* Delete */}
                        <div className="flex-shrink-0 flex items-center">
                          {isConfirming ? (
                            <div className="flex items-center gap-2">
                              <span className="text-xs text-gray-500">Delete?</span>
                              <button onClick={() => handleDelete(run.run_id)} disabled={isDeleting}
                                className="text-xs px-2.5 py-1 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 transition-colors">
                                {isDeleting ? '...' : 'Yes'}
                              </button>
                              <button onClick={() => setConfirmingDelete(null)} disabled={isDeleting}
                                className="text-xs px-2.5 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50 transition-colors">
                                No
                              </button>
                            </div>
                          ) : (
                            <button
                              onClick={e => { e.stopPropagation(); setConfirmingDelete(run.run_id); }}
                              className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-600 transition-all p-1.5 rounded hover:bg-red-50"
                              title="Delete dataset"
                            >
                              <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <polyline points="3 6 5 6 21 6" />
                                <path d="M19 6l-1 14H6L5 6" />
                                <path d="M10 11v6M14 11v6" />
                                <path d="M9 6V4h6v2" />
                              </svg>
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}

                  {/* Older versions toggle */}
                  {group.older.length > 0 && (
                    <button
                      onClick={() => toggleExpand(group.name)}
                      className="w-full px-5 py-2 border-t border-gray-100 text-xs text-gray-400 hover:text-gray-600 hover:bg-gray-50 transition-colors text-left"
                    >
                      {isExpanded ? '▲ Hide older versions' : `▼ ${group.older.length} older version${group.older.length > 1 ? 's' : ''}`}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
