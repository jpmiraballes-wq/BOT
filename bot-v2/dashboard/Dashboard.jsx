import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { externalBase44Read } from '@/functions/externalBase44Read';
import { InvokeLLM } from '@/api/integrations';

import { C, isToday } from '@/components/dashboard/theme';
import Panel from '@/components/dashboard/Panel';
import HealthHeader from '@/components/dashboard/HealthHeader';
import PnLHero from '@/components/dashboard/PnLHero';
import CapitalBar from '@/components/dashboard/CapitalBar';
import EquityCurve from '@/components/dashboard/EquityCurve';
import DaySummary from '@/components/dashboard/DaySummary';
import OpenOrders from '@/components/dashboard/OpenOrders';
import ActivityTicker from '@/components/dashboard/ActivityTicker';
import TradesTable from '@/components/dashboard/TradesTable';
import CollapsibleSection from '@/components/dashboard/CollapsibleSection';
import PerformanceHistory from '@/components/history/PerformanceHistory';

// Lee una entidad desde la app externa via el proxy backend.
const readExternal = async (entity, sort = '-created_date', limit = 100) => {
  const { data } = await externalBase44Read({ entity, sort, limit });
  return data?.records || [];
};

export default function Dashboard() {
  const [systemState, setSystemState] = useState(null);
  const [trades, setTrades] = useState([]);
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [lastUpdate, setLastUpdate] = useState(null);
  const [now, setNow] = useState(Date.now());

  const [aiLoading, setAiLoading] = useState(false);
  const [aiResult, setAiResult] = useState(null);
  const [aiError, setAiError] = useState('');

  const fetchAll = useCallback(async () => {
    try {
      const [stateData, tradesData, logsData] = await Promise.all([
        readExternal('SystemState', '-created_date', 1),
        readExternal('Trade', '-created_date', 500),
        readExternal('LogEvent', '-created_date', 30),
      ]);
      if (stateData?.length) setSystemState(stateData[0]);
      setTrades(tradesData || []);
      setLogs(logsData || []);
      setLastUpdate(new Date());
      setLoading(false);
    } catch (e) {
      console.error('fetchAll error', e);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const t = setInterval(fetchAll, 15000);
    return () => clearInterval(t);
  }, [fetchAll]);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10000);
    return () => clearInterval(t);
  }, []);

  const heartbeatIso =
    systemState?.last_heartbeat ||
    systemState?.heartbeat_at ||
    systemState?.updated_date ||
    null;
  const heartbeatAgeMs = heartbeatIso ? now - new Date(heartbeatIso).getTime() : null;
  const isOnline =
    heartbeatAgeMs !== null &&
    !Number.isNaN(heartbeatAgeMs) &&
    heartbeatAgeMs >= 0 &&
    heartbeatAgeMs < 120 * 1000;

  const equityData = useMemo(() => {
    const sorted = [...trades]
      .filter((t) => t.entry_time && t.pnl != null)
      .sort((a, b) => new Date(a.entry_time) - new Date(b.entry_time));
    let cum = 0;
    return sorted.map((t) => {
      cum += Number(t.pnl || 0);
      return {
        time: new Date(t.entry_time).toLocaleDateString('es-ES', {
          month: 'short', day: '2-digit',
        }),
        equity: Number(cum.toFixed(2)),
      };
    });
  }, [trades]);

  const { tradesToday, bestToday, worstToday } = useMemo(() => {
    const today = trades.filter((t) => isToday(t.entry_time));
    const withPnl = today.filter((t) => t.pnl != null);
    const best = withPnl.reduce(
      (acc, t) => (acc == null || t.pnl > acc.pnl ? t : acc), null
    );
    const worst = withPnl.reduce(
      (acc, t) => (acc == null || t.pnl < acc.pnl ? t : acc), null
    );
    return { tradesToday: today.length, bestToday: best, worstToday: worst };
  }, [trades]);

  const runAI = async () => {
    setAiLoading(true);
    setAiError('');
    setAiResult(null);
    try {
      const sample = trades.slice(0, 50).map((t) => ({
        market: t.market, side: t.side,
        entry_price: t.entry_price, exit_price: t.exit_price,
        size_usdc: t.size_usdc, pnl: t.pnl,
        strategy: t.strategy, status: t.status,
      }));
      const res = await InvokeLLM({
        prompt: `Eres un analista cuantitativo. Analiza estos ${sample.length} trades de un bot de market making en Polymarket y devuelve un JSON con:
- summary: resumen de rendimiento en 2-3 frases
- best_markets: top 3 mercados mas rentables
- worst_markets: top 3 mercados con perdidas
- best_strategy: estrategia con mejor performance
- recommendations: array de 3-5 recomendaciones concretas

Datos:
${JSON.stringify(sample, null, 2)}`,
        response_json_schema: {
          type: 'object',
          properties: {
            summary: { type: 'string' },
            best_markets: { type: 'array', items: { type: 'string' } },
            worst_markets: { type: 'array', items: { type: 'string' } },
            best_strategy: { type: 'string' },
            recommendations: { type: 'array', items: { type: 'string' } },
          },
          required: ['summary', 'recommendations'],
        },
      });
      setAiResult(res);
    } catch (e) {
      console.error(e);
      setAiError(e?.message || 'Error al invocar IA');
    } finally {
      setAiLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center"
        style={{ background: C.bg, color: C.text }}>
        <div className="text-center">
          <div className="text-4xl mb-3 animate-pulse">⚡</div>
          <div className="text-lg">Cargando Trading Hub Pro…</div>
        </div>
      </div>
    );
  }

  const ss = systemState || {};

  return (
    <div className="min-h-screen"
      style={{ background: C.bgGrad, color: C.text, backgroundAttachment: 'fixed' }}>
      <div className="max-w-[1600px] mx-auto p-5 space-y-4">
        <HealthHeader
          isOnline={isOnline}
          heartbeatIso={heartbeatIso}
          heartbeatAgeMs={heartbeatAgeMs}
          mode={ss.mode}
          botVersion={ss.bot_version}
          lastUpdate={lastUpdate}
          onRefresh={fetchAll}
        />
        <PnLHero
          dailyPnl={ss.daily_pnl}
          totalPnl={ss.total_pnl}
          winRate={ss.win_rate}
          tradesToday={tradesToday}
        />
        <CapitalBar
          capitalTotal={ss.capital_total}
          capitalDeployed={ss.capital_deployed}
          drawdownPct={ss.drawdown_pct}
          openPositions={ss.open_positions}
          uptimeHours={ss.uptime_hours}
        />
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <EquityCurve data={equityData} />
          <DaySummary tradesToday={tradesToday} best={bestToday} worst={worstToday} />
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <OpenOrders trades={trades} />
          <ActivityTicker logs={logs} />
        </div>

        {/* Historial de Rendimiento (seccion premium) */}
        <PerformanceHistory trades={trades} />

        <CollapsibleSection
          title="Historial de Trades"
          badge={String(trades.length)}
          defaultOpen={false}
        >
          <div className="pt-4">
            <TradesTable trades={trades} />
          </div>
        </CollapsibleSection>

        <CollapsibleSection title="Analisis con IA" defaultOpen={false}>
          <div className="pt-4">
            <Panel
              title=""
              right={
                <button
                  onClick={runAI}
                  disabled={aiLoading || trades.length === 0}
                  className="px-4 py-2 rounded-lg text-sm font-bold transition hover:opacity-80"
                  style={{
                    background: `${C.purple}22`,
                    color: C.purple,
                    border: `1px solid ${C.purple}40`,
                    opacity: aiLoading || trades.length === 0 ? 0.4 : 1,
                  }}
                >
                  {aiLoading ? 'Analizando…' : '🧠 Analizar con IA'}
                </button>
              }
            >
              {aiError && <div style={{ color: C.red }} className="text-sm mb-3">{aiError}</div>}
              {!aiResult && !aiLoading && (
                <div style={{ color: C.muted }} className="text-sm">
                  Genera un resumen basado en los ultimos 50 trades.
                </div>
              )}
              {aiResult && (
                <div className="space-y-4 text-sm">
                  <div>
                    <div className="text-xs uppercase mb-1" style={{ color: C.muted }}>Resumen</div>
                    <div>{aiResult.summary}</div>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {aiResult.best_markets?.length > 0 && (
                      <div>
                        <div className="text-xs uppercase mb-1" style={{ color: C.green }}>
                          Mejores mercados
                        </div>
                        <ul className="list-disc list-inside space-y-0.5">
                          {aiResult.best_markets.map((m, i) => <li key={i}>{m}</li>)}
                        </ul>
                      </div>
                    )}
                    {aiResult.worst_markets?.length > 0 && (
                      <div>
                        <div className="text-xs uppercase mb-1" style={{ color: C.red }}>
                          Peores mercados
                        </div>
                        <ul className="list-disc list-inside space-y-0.5">
                          {aiResult.worst_markets.map((m, i) => <li key={i}>{m}</li>)}
                        </ul>
                      </div>
                    )}
                  </div>
                  {aiResult.recommendations?.length > 0 && (
                    <div>
                      <div className="text-xs uppercase mb-1" style={{ color: C.yellow }}>
                        Recomendaciones
                      </div>
                      <ul className="list-disc list-inside space-y-1">
                        {aiResult.recommendations.map((r, i) => <li key={i}>{r}</li>)}
                      </ul>
                    </div>
                  )}
                </div>
              )}
            </Panel>
          </div>
        </CollapsibleSection>

        <div className="text-center text-[11px] pt-2" style={{ color: C.dim }}>
          Trading Hub Pro · Auto-refresh 15s · {trades.length} trades cargados
        </div>
      </div>
    </div>
  );
}
