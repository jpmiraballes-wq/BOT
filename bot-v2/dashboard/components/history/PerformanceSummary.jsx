import React, { useMemo } from 'react';
import { TrendingUp, TrendingDown, Target, BarChart3 } from 'lucide-react';
import { C, fmtMoney } from '@/components/dashboard/theme';

const Card = ({ icon: Icon, label, value, color, glow }) => (
  <div
    className="rounded-2xl p-6 backdrop-blur-sm transition-transform hover:scale-[1.02]"
    style={{
      background: `linear-gradient(135deg, ${color}11 0%, ${C.panel2} 100%)`,
      border: `1px solid ${color}33`,
      boxShadow: glow ? `0 0 40px ${color}22, inset 0 1px 0 rgba(255,255,255,0.05)` : 'none',
    }}
  >
    <div className="flex items-center justify-between mb-3">
      <div
        className="w-11 h-11 rounded-xl flex items-center justify-center"
        style={{ background: `${color}22`, border: `1px solid ${color}44` }}
      >
        <Icon className="w-5 h-5" style={{ color }} />
      </div>
      <div className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: C.muted }}>
        {label}
      </div>
    </div>
    <div
      className="text-4xl font-black tabular-nums tracking-tight"
      style={{ color, textShadow: glow ? `0 0 20px ${color}66` : 'none' }}
    >
      {value}
    </div>
  </div>
);

export default function PerformanceSummary({ closedTrades }) {
  const stats = useMemo(() => {
    const withPnl = closedTrades.filter((t) => t.pnl != null);
    const wins = withPnl.filter((t) => Number(t.pnl) > 0);
    const losses = withPnl.filter((t) => Number(t.pnl) < 0);
    const totalWon = wins.reduce((s, t) => s + Number(t.pnl || 0), 0);
    const totalLost = Math.abs(losses.reduce((s, t) => s + Number(t.pnl || 0), 0));
    const winRate = withPnl.length > 0 ? (wins.length / withPnl.length) * 100 : 0;
    const avgPnlPct = withPnl.length > 0
      ? withPnl.reduce((s, t) => s + Number(t.pnl_pct || 0), 0) / withPnl.length
      : 0;
    return { totalWon, totalLost, winRate, avgPnlPct, count: withPnl.length };
  }, [closedTrades]);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      <Card icon={TrendingUp} label="Total Ganado" value={fmtMoney(stats.totalWon)} color={C.green} glow />
      <Card icon={TrendingDown} label="Total Perdido" value={fmtMoney(stats.totalLost)} color={C.red} glow />
      <Card icon={Target} label="Win Rate"
        value={`${stats.winRate.toFixed(1)}%`}
        color={stats.winRate >= 50 ? C.green : C.yellow} />
      <Card icon={BarChart3} label="Avg PnL"
        value={`${stats.avgPnlPct >= 0 ? '+' : ''}${stats.avgPnlPct.toFixed(2)}%`}
        color={stats.avgPnlPct >= 0 ? C.green : C.red} />
    </div>
  );
}
