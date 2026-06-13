import type { Verdict } from '../types';

interface Props {
  verdict: Verdict;
  comment: string;
  onVerdictChange: (v: Verdict) => void;
  onCommentChange: (c: string) => void;
  label?: string;
  showShortcuts?: boolean;
}

const verdictStyles: Record<Verdict, string> = {
  pending: 'bg-gray-100 text-gray-600 border-gray-300',
  accepted: 'bg-green-600 text-white border-green-600',
  rejected: 'bg-red-600 text-white border-red-600',
};

export default function AnnotationPanel({
  verdict,
  comment,
  onVerdictChange,
  onCommentChange,
  label = '',
  showShortcuts = false,
}: Props) {
  return (
    <div className="mt-4 border-t border-gray-200 pt-4 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-medium text-gray-600">
          {label ? `${label}:` : 'Verdict:'}
        </span>
        <button
          onClick={() => onVerdictChange(verdict === 'accepted' ? 'pending' : 'accepted')}
          className={`px-3 py-1 text-sm font-medium rounded border transition-colors ${
            verdict === 'accepted'
              ? verdictStyles.accepted
              : 'bg-white text-green-700 border-green-300 hover:bg-green-50'
          }`}
        >
          ✓ Accept{showShortcuts && verdict !== 'accepted' ? ' (a)' : ''}
        </button>
        <button
          onClick={() => onVerdictChange(verdict === 'rejected' ? 'pending' : 'rejected')}
          className={`px-3 py-1 text-sm font-medium rounded border transition-colors ${
            verdict === 'rejected'
              ? verdictStyles.rejected
              : 'bg-white text-red-700 border-red-300 hover:bg-red-50'
          }`}
        >
          ✗ Reject{showShortcuts && verdict !== 'rejected' ? ' (r)' : ''}
        </button>
        {verdict !== 'pending' && (
          <button
            onClick={() => onVerdictChange('pending')}
            className="px-3 py-1 text-sm font-medium rounded border bg-white text-gray-600 border-gray-300 hover:bg-gray-50 transition-colors"
          >
            ↩ Reset
          </button>
        )}
        {verdict && verdict !== 'pending' && (
          <span
            className={`px-2 py-0.5 text-xs font-semibold rounded-full ${
              verdict === 'accepted' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
            }`}
          >
            {verdict.toUpperCase()}
          </span>
        )}
      </div>
      <textarea
        value={comment}
        onChange={(e) => onCommentChange(e.target.value)}
        placeholder="Add a comment..."
        rows={2}
        className="w-full text-sm border border-gray-300 rounded px-3 py-2 resize-none focus:outline-none focus:ring-2 focus:ring-blue-300 placeholder-gray-400"
      />
    </div>
  );
}
