import type { SampleEvent, EventRole } from '../types';

const roleColors: Record<EventRole, string> = {
  base: 'bg-gray-100 text-gray-600',
  pre_mod: 'bg-blue-100 text-blue-700',
  post_mod: 'bg-green-100 text-green-700',
  irrelevant: 'bg-orange-100 text-orange-700',
};

interface Props {
  event: SampleEvent;
  showExpected?: boolean;
}

export default function EventCard({ event, showExpected = true }: Props) {
  const roleStyle = event.role ? roleColors[event.role] : 'bg-gray-100 text-gray-600';

  return (
    <div className="border border-gray-200 rounded-lg p-3 bg-white space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-mono text-xs font-semibold bg-gray-800 text-white px-2 py-0.5 rounded">
          {event.id}
        </span>
        {event.role && (
          <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${roleStyle}`}>
            {event.role.replace('_', '-')}
          </span>
        )}
        {event.recipient && (
          <span className="text-xs text-gray-500">
            → <span className="font-mono text-gray-700">{event.recipient}</span>
          </span>
        )}
        <span className="text-xs text-gray-400 ml-auto">{event.when}</span>
      </div>
      <p className="text-sm text-gray-800 leading-relaxed">{event.input}</p>
      {showExpected && (
        event.expect ? (
          <div className="bg-gray-50 rounded p-2 space-y-1">
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Expected outcome</div>
            <p className="text-sm text-gray-700 leading-relaxed">{event.expect.action}</p>
            {event.expect.reason && (
              <p className="text-xs text-gray-500 italic">{event.expect.reason}</p>
            )}
          </div>
        ) : (
          <p className="text-xs text-gray-400 italic">No expected outcome specified</p>
        )
      )}
    </div>
  );
}
