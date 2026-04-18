import React, { useMemo } from 'react';
import { History } from 'lucide-react';
import { C } from '@/components/dashboard/theme';
import PerformanceSummary from './PerformanceSummary';
import ScalingSimulator from './ScalingSimulator';
import ClosedTradesTable from './ClosedTradesTable';

export default function PerformanceHistory({ trades }) {
  const closedTrades = useMemo(
    () => trades.filter((t) => t.status === 'closed'),
    [trades]
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 pt-2">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: `${C.cyan}22`, border: `1px solid ${C.cyan}44` }}
        >
          <History className="w-5 h-5" style={{ color: C.cyan }} />
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.2em] font-bold" style={{ color: C.cyan }}>
            Historial de Rendimiento
          </div>
          <div className="text-xs" style={{ color: C.muted }}>
            {closedTrades.length} trades cerrados
          </div>
        </div>
      </div>

      <PerformanceSummary closedTrades={closedTrades} />
      <ScalingSimulator closedTrades={closedTrades} />
      <ClosedTradesTable closedTrades={closedTrades} />
    </div>
  );
}
