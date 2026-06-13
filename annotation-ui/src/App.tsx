import { BrowserRouter, Routes, Route, Navigate, useParams } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { doc, getDoc } from 'firebase/firestore';
import { db } from './firebase';
import { useAuth } from './hooks/useAuth';
import { useSamples } from './hooks/useSamples';
import { SampleContext } from './SampleContext';
import LoginPage from './pages/LoginPage';
import RunSelectPage from './pages/RunSelectPage';
import SampleListPage from './pages/SampleListPage';
import SampleDetailPage from './pages/SampleDetailPage';

const ALLOWED_EMAIL = 'asaf.ach@gmail.com';

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-gray-400 bg-gray-50">
        Loading...
      </div>
    );
  }
  if (!user || user.email !== ALLOWED_EMAIL) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

function RunRoutes() {
  const { runId } = useParams<{ runId: string }>();
  const decodedRunId = decodeURIComponent(runId ?? '');
  const { entries, loading, error } = useSamples(decodedRunId);

  const [datasetName, setDatasetName] = useState<string | null>(null);
  const [datasetVersion, setDatasetVersion] = useState<number | null>(null);

  useEffect(() => {
    if (!decodedRunId) return;
    getDoc(doc(db, 'runs', decodedRunId)).then((snap) => {
      if (snap.exists()) {
        const data = snap.data();
        setDatasetName(data.dataset_name ?? null);
        setDatasetVersion(data.dataset_version ?? null);
      }
    });
  }, [decodedRunId]);

  const getAdjacentIds = (id: string) => {
    const idx = entries.findIndex((e) => e.id === id);
    return {
      prev: idx > 0 ? entries[idx - 1].id : null,
      next: idx >= 0 && idx < entries.length - 1 ? entries[idx + 1].id : null,
      index: idx,
      total: entries.length,
    };
  };

  if (error) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center text-red-500">
        {error}
      </div>
    );
  }

  return (
    <SampleContext.Provider value={{ entries, runId: decodedRunId, datasetName, datasetVersion, getAdjacentIds }}>
      <Routes>
        <Route index element={<SampleListPage loading={loading} />} />
        <Route path="sample/:sampleId" element={<SampleDetailPage />} />
      </Routes>
    </SampleContext.Provider>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/runs"
          element={<RequireAuth><RunSelectPage /></RequireAuth>}
        />
        <Route
          path="/runs/:runId/*"
          element={<RequireAuth><RunRoutes /></RequireAuth>}
        />
        <Route path="*" element={<Navigate to="/runs" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
