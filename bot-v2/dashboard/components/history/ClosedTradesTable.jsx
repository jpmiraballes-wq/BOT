import React, { useState, useMemo } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { C, fmtMoney, fmtMoneySigned, fmtDate, pnlColor } from '@/components/dashboard/theme';

const FilterBtn = ({ active, onClick, label, count, color }) => (
  <button
    onClick={onClick}
    className="px-4 py-2 rounded-lg text-sm font-bold transition"
    style={{
      background: active ? `${color}22` : 'transparent',
      color: active ? color : C.muted,
      border: `1px solid ${active ? color + '66' : C.border}`,
    }}
  >
    {label} <span className="opacity-60 text-xs ml-1">({count})</span>
  </button>
);

const Badge = ({ won }) => (
  <span
    className="px-2.5 py-1 rounded-md text-[10px] font-black tracking-wider"
    style={{
      background: won ? `${C.green}22` : `${C.red}22`,
      color: won ? C.green : C.red,
      border: `1px solid ${won ? C.green : C.red}44`,
    }}
  >
    {won ? 'WON' : 'LOST'}
  </span>
);

const DetailRow = ({ label, value }) => (
  <div>
    <div className="text-[10px] uppercase tracking-wider mb-0.5" style={{ color: C.dim }}>
      {label}
    </div>
    <div className="text-sm" style={{ color: C.text }}>{value || '—'}</div>
  </div>
);

export default function ClosedTradesTable({ closedTrades }) {
  const [filter, setFilter] = useState('all');
  const [expanded, setExpanded] = useState(null);

  const counts = useMemo(() => {
    const won = closedTrades.filter((t) => Number(t.pnl) > 0).length;
    const lost = closedTrades.filter((t) => Number(t.pnl) < 0).length;
    return { all: closedTrades.length, won, lost };
  }, [closedTrades]);

  const visible = useMemo(() => {
    const sorted = [...closedTrades].sort(
      (a, b) => new Date(b.exit_time || b.entry_time || 0) - new Date(a.exit_time || a.entry_time || 0)
    );
    if (filter === 'won') return sorted.filter((t) => Number(t.pnl) > 0);
    if (filter === 'lost') return sorted.filter((t) => Number(t.pnl) < 0);
    return sorted;
  }, [closedTrades, filter]);

  const duration = (t) => {
    if (!t.entry_time || !t.exit_time) return '—';
    const ms = new Date(t.exit_time) - new Date(t.entry_time);
    if (ms < 0) return '—';
    const min = Math.round(ms / 60_000);
    if (min < 60) return `${min}min`;
    const h = Math.floor(min / 60);
    return `${h}h ${min % 60}min`;
  };

  return (
    <div
      className="rounded-2xl p-5 backdrop-blur-sm"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <div className="text-lg font-bold" style={{ color: C.text }}>
          Trades Cerrados
        </div>
        <div className="flex gap-2">
          <FilterBtn active={filter === 'all'} onClick={() => setFilter('all')}
            label="All" count={counts.all} color={C.blue} />
          <FilterBtn active={filter === 'won'} onClick={() => setFilter('won')}
            label="Won" count={counts.won} color={C.green} />
          <FilterBtn active={filter === 'lost'} onClick={() => setFilter('lost')}
            label="Lost" count={counts.lost} color={C.red} />
        </div>
      </div>

      {visible.length === 0 ? (
        <div className="py-10 text-center text-sm" style={{ color: C.muted }}>
          No hay trades cerrados en esta vista.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr style={{ color: C.muted }} className="text-[10px] uppercase tracking-wider">
                <th className="text-left py-2 px-2 w-6"></th>
                <th className="text-left py-2 px-2">Mercado</th>
                <th className="text-left py-2 px-2">Side</th>
                <th className="text-right py-2 px-2">Entrada</th>
                <th className="text-right py-2 px-2">Salida</th>
                <th className="text-right py-2 px-2">Size</th>
                <th className="text-right py-2 px-2">PnL ($)</th>
                <th className="text-right py-2 px-2">PnL (%)</th>
                <th className="text-center py-2 px-2">Estado</th>
                <th className="text-right py-2 px-2">Fecha</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((t, i) => {
                const won = Number(t.pnl) > 0;
                const isOpen = expanded === (t.id || i);
                const key = t.id || i;
                return (
                  <React.Fragment key={key}>
                    <tr
                      className="cursor-pointer transition hover:bg-white/5"
                      style={{ borderTop: `1px solid ${C.border}` }}
                      onClick={() => setExpanded(isOpen ? null : key)}
                    >
                      <td className="py-3 px-2">
                        {isOpen
                          ? <ChevronDown className="w-4 h-4" style={{ color: C.muted }} />
                          : <ChevronRight className="w-4 h-4" style={{ color: C.muted }} />}
                      </td>
                      <td className="py-3 px-2 max-w-[280px] truncate" style={{ color: C.text }}>
                        {t.market || '—'}
                      </td>
                      <td className="py-3 px-2" style={{ color: t.side === 'BUY' ? C.green : C.red }}>
                        {t.side || '—'}
                      </td>
                      <td className="py-3 px-2 text-right tabular-nums" style={{ color: C.text }}>
                        {t.entry_price != null ? Number(t.entry_price).toFixed(3) : '—'}
                      </td>
                      <td className="py-3 px-2 text-right tabular-nums" style={{ color: C.text }}>
                        {t.exit_price != null ? Number(t.exit_price).toFixed(3) : '—'}
                      </td>
                      <td className="py-3 px-2 text-right tabular-nums" style={{ color: C.muted }}>
                        {fmtMoney(t.size_usdc)}
                      </td>
                      <td className="py-3 px-2 text-right tabular-nums font-bold"
                        style={{ color: pnlColor(t.pnl) }}>
                        {fmtMoneySigned(t.pnl)}
                      </td>
                      <td className="py-3 px-2 text-right tabular-nums font-bold"
                        style={{ color: pnlColor(t.pnl_pct) }}>
                        {t.pnl_pct != null
                          ? `${Number(t.pnl_pct) >= 0 ? '+' : ''}${Number(t.pnl_pct).toFixed(2)}%`
                          : '—'}
                      </td>
                      <td className="py-3 px-2 text-center">
                        <Badge won={won} />
                      </td>
                      <td className="py-3 px-2 text-right text-xs" style={{ color: C.muted }}>
                        {fmtDate(t.exit_time || t.entry_time)}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr style={{ background: C.panel2 }}>
                        <td colSpan={10} className="px-6 py-4">
                          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                            <DetailRow label="Strategy" value={t.strategy} />
                            <DetailRow label="Duracion" value={duration(t)} />
                            <DetailRow label="Order ID" value={t.order_id || t.id} />
                            <DetailRow label="Status" value={t.status} />
                            <div className="md:col-span-4">
                              <DetailRow label="Notas" value={t.notes} />
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
