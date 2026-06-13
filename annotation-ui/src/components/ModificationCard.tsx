import type { Modification, Sample, SampleEvent, ObjectDef } from '../types';
import EventCard from './EventCard';

const modTypeColors: Record<string, string> = {
  temporal: 'bg-blue-100 text-blue-700',
  contextual: 'bg-teal-100 text-teal-700',
  exception: 'bg-orange-100 text-orange-700',
  correction: 'bg-yellow-100 text-yellow-700',
  expansion: 'bg-green-100 text-green-700',
  removal: 'bg-red-100 text-red-700',
};

const stateConstraintColors: Record<string, string> = {
  cap: 'bg-amber-100 text-amber-700',
  counter: 'bg-purple-100 text-purple-700',
  rate_limit: 'bg-orange-100 text-orange-700',
  trigger: 'bg-pink-100 text-pink-700',
};

interface Props {
  mod: Modification;
  sample: Sample;
  targetObj: ObjectDef | null;
}

export default function ModificationCard({ mod, sample, targetObj }: Props) {
  const isSC = (e: { id: string }) => /^SC\d+/.test(e.id);
  const baselineEvents = sample.events.filter(
    (e) => !isSC(e) && (e.role === 'base' || e.role === 'pre_mod') && e.after_mod_ids.length === 0
  );
  const postModEvents = sample.events.filter(
    (e) => !isSC(e) && e.role === 'post_mod' && e.after_mod_ids.includes(mod.id)
  );
  const irrelevantEvents = sample.events.filter((e) => !isSC(e) && e.role === 'irrelevant');

  return (
    <div className="border border-gray-300 rounded-xl bg-gray-50 overflow-hidden">
      <div className="px-5 py-4 space-y-5">
        {/* 1. Baseline — what happens before the modification */}
        {baselineEvents.length > 0 && (
          <EventSection title="Baseline / Pre-modification" events={baselineEvents} />
        )}

        {/* 2. The modification itself — labels + intent + target, right before post-mod */}
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-blue-600 mb-2">
            Modification
          </div>
          <div className="bg-white border border-blue-200 rounded-lg px-4 py-3 space-y-3">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-mono text-sm font-bold text-gray-800">{mod.id}</span>
              {sample.state_constraint?.type && (
                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${stateConstraintColors[sample.state_constraint.type] ?? 'bg-gray-100 text-gray-600'}`}>
                  {sample.state_constraint.type}
                </span>
              )}
              {mod.mod_type && (
                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${modTypeColors[mod.mod_type] ?? 'bg-gray-100 text-gray-600'}`}>
                  {mod.mod_type}
                </span>
              )}
              <span className="text-xs text-gray-400 ml-auto">{mod.when}</span>
            </div>
            <p className="text-sm text-gray-800 leading-relaxed font-medium">"{mod.intent}"</p>
            <div className="bg-blue-50 border border-blue-100 rounded px-3 py-2">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-semibold text-blue-500 uppercase tracking-wide">Target object</span>
                <span className="font-mono text-xs font-bold text-blue-800 bg-blue-100 px-2 py-0.5 rounded">
                  {mod.target}
                </span>
              </div>
              {targetObj ? (
                <>
                  <p className="text-sm text-blue-900 font-medium leading-snug">{targetObj.role}</p>
                  <p className="text-xs text-blue-700 mt-1 leading-relaxed line-clamp-3">{targetObj.behavior}</p>
                </>
              ) : (
                <p className="text-xs text-blue-400 italic">Object not found in graph</p>
              )}
            </div>
          </div>
        </div>

        {/* 3. Post-modification — what should happen after */}
        {postModEvents.length > 0 && (
          <EventSection title="Post-modification events" events={postModEvents} accent="green" />
        )}

        {/* 4. Non-interference */}
        {irrelevantEvents.length > 0 && (
          <EventSection title="Non-interference events" events={irrelevantEvents} accent="orange" />
        )}
      </div>
    </div>
  );
}

function EventSection({
  title,
  events,
  accent,
}: {
  title: string;
  events: SampleEvent[];
  accent?: 'green' | 'orange';
}) {
  const titleClass = accent === 'green'
    ? 'text-green-700'
    : accent === 'orange'
    ? 'text-orange-700'
    : 'text-gray-600';

  return (
    <div>
      <div className={`text-xs font-semibold uppercase tracking-wide mb-2 ${titleClass}`}>
        {title}
      </div>
      <div className="space-y-2">
        {events.map((e) => (
          <EventCard key={e.id} event={e} />
        ))}
      </div>
    </div>
  );
}
