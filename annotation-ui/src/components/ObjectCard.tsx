import { useState } from 'react';
import type { ObjectDef } from '../types';

interface Props {
  obj: ObjectDef;
}

export default function ObjectCard({ obj }: Props) {
  const [expanded, setExpanded] = useState(false);
  const isEntry = obj.event_sources.length > 0;

  return (
    <div className="border border-gray-200 rounded-lg bg-white overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-gray-50 transition-colors"
      >
        <span className="font-mono text-xs font-bold bg-blue-100 text-blue-800 px-2 py-0.5 rounded">
          {obj.object_id}
        </span>
        {isEntry && (
          <span className="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full font-medium">
            entry
          </span>
        )}
        <span className="text-sm text-gray-600 truncate flex-1 ml-1">
          {obj.role.split('.')[0]}
        </span>
        <span className="text-gray-400 text-sm flex-shrink-0">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-gray-100 pt-3">
          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Role</div>
            <p className="text-sm text-gray-800">{obj.role}</p>
          </div>

          {obj.state_description && (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">State</div>
              <p className="text-sm text-gray-700">{obj.state_description}</p>
            </div>
          )}

          <div>
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Behavior</div>
            <p className="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap">{obj.behavior}</p>
          </div>

          {obj.peers.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Peers</div>
              <div className="space-y-1">
                {obj.peers.map((p) => (
                  <div key={p.object_id} className="flex items-center gap-2 text-sm">
                    <span className="font-mono text-xs bg-gray-100 px-1.5 py-0.5 rounded text-gray-700">
                      {p.object_id}
                    </span>
                    <span className="text-gray-500">— {p.relationship}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {obj.event_sources.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Event sources</div>
              <div className="flex flex-wrap gap-1">
                {obj.event_sources.map((s) => (
                  <span key={s} className="text-xs bg-purple-50 text-purple-700 px-2 py-0.5 rounded border border-purple-200">
                    {s}
                  </span>
                ))}
              </div>
            </div>
          )}

          {obj.skills.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Skills</div>
              <div className="flex flex-wrap gap-1">
                {obj.skills.map((s) => (
                  <span key={s} className="text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded">
                    {s}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
