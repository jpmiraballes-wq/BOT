import React, { useMemo } from 'react';
import { Rocket, Calendar, CalendarDays, CalendarRange } from 'lucide-react';
import { C, fmtMoney } from '@/components/dashboard/theme';

const CAPITAL = 2000;
const BASELINE_DAILY = 0.003;
const MIN_TRADES_FOR_REAL = 10;

const Projection = ({ icon: Icon, label, amount, pct }) => (
  <div
    className="rounded-xl p-5 flex-1"
    style={{ background: C.panel2, border: `1px solid ${C.border}` }}
  >
    <div className="flex items-center gap-2 mb-2">
      <Icon className="w-4 h-4" style={{ color: C.cyan }} />
      <div className="text-xs uppercase tracking-wider font-semibold" style={{ color: C.muted }}>
        {label}
      </div>
    </div>
    <div className="text-3xl font-black tabular-nums" style={{ color: C.green }}>
      {fmtMoney(amount)}
    </div>
    <div className="text-xs mt-1" style={{ color: C.dim }}>
      {pct >= 0 ? '+' : ''}{(pct * 100).toFixed(2)}% sobre ${CAPITAL}
    </div>
  </div>
);

export default function ScalingSimulator({ closedTrades }) {
  const { dailyReturn, source, tradesPerDay } = useMemo(() => {
    const withPnl = closedTrades.filter((t) => t.pnl_pct != null && t.pnl != null);
    if (withPnl.length < MIN_TRADES_FOR_REAL) {
      return { dailyReturn: BASELINE_DAILY, source: 'baseline', tradesPerDay: 1 };
    }
    const avgPnlPct = withPnl.reduce((s, t) => s + Number(t.pnl_pct || 0), 0) / withPnl.length;
    const times = withPnl
      .map((t) => new Date(t.exit_time || t.entry_time).getTime())
      .filter((t) => !Number.isNaN(t));
    let perDay = 1;
    if (times.length >= 2) {
      const days = Math.max(1, (Math.max(...times) - Math.min(...times)) / 86_400_000);
      perDay = Math.max(0.5, withPnl.length / days);
    }
    const dailyReturnCalc = (avgPnlPct / 100) * perDay;
    return { dailyReturn: dailyReturnCalc, source: 'real', tradesPerDay: perDay };
  }, [closedTrades]);

  const daily = CAPITAL * dailyReturn;
  const weekly = CAPITAL * dailyReturn * 7;
  const monthly = CAPITAL * dailyReturn * 30;

  return (
    <div
      className="rounded-2xl p-6 backdrop-blur-sm"
      style={{
        background: `linear-gradient(135deg, ${C.purple}15 0%, ${C.cyan}10 50%, ${C.panel2} 100%)`,
        border: `1px solid ${C.purple}44`,
        boxShadow: `0 0 50px ${C.purple}22, inset 0 1px 0 rgba(255,255,255,0.06)`,
      }}
    >
      <div className="flex items-start justify-between mb-5">
        <div>
          <div className="flex items-center gap-2 mb-2">
            <div
              className="w-10 h-10 rounded-xl flex items-center justify-center"
              style={{ background: `${C.purple}22`, border: `1px solid ${C.purple}44` }}
            >
              <Rocket className="w-5 h-5" style={{ color: C.purple }} />
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider font-semibold" style={{ color: C.purple }}>
                Simulador de Escalado
              </div>
              <div className="text-2xl font-black" style={{ color: C.text }}>
                Con ${CAPITAL.toLocaleString()} de capital estimado:
              </div>
            </div>
          </div>
        </div>
        <div
          className="text-[10px] uppercase px-3 py-1 rounded-full font-bold tracking-wider"
          style={{
            background: source === 'real' ? `${C.green}22` : `${C.amber}22`,
            color: source === 'real' ? C.green : C.amber,
            border: `1px solid ${source === 'real' ? C.green : C.amber}44`,
          }}
        >
          {source === 'real' ? `Real · ${tradesPerDay.toFixed(1)} trades/dia` : 'Baseline 0.3%'}
        </div>
      </div>

      <div className="flex flex-col md:flex-row gap-3">
        <Projection icon={Calendar} label="Diario" amount={daily} pct={dailyReturn} />
        <Projection icon={CalendarDays} label="Semanal" amount={weekly} pct={dailyReturn * 7} />
        <Projection icon={CalendarRange} label="Mensual" amount={monthly} pct={dailyReturn * 30} />
      </div>

      <div className="text-[11px] mt-4" style={{ color: C.dim }}>
        Proyeccion lineal sin reinversion. Los resultados reales pueden variar significativamente.
      </div>
    </div>
  );
}
