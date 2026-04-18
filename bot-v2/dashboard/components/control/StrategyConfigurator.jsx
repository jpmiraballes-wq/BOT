import React, { useState, useMemo } from 'react';
import { Sliders, Target, Zap, Save } from 'lucide-react';
import { C, fmtMoney } from '@/components/dashboard/theme';
import { externalBase44Write } from '@/functions/externalBase44Write';

function deriveParams(level) {
  const t = (level - 1) / 9;
  const max_position_pct = Number((0.02 + t * 0.13).toFixed(4));
  const min_spread_pct = Number((0.04 - t * 0.025).toFixed(4));
  const stop_loss = Number((-0.10 - t * 0.25).toFixed(3));
  const take_profit = Number((0.05 + t * 0.20).toFixed(3));
  let risk_label = 'Conservador';
  let risk_color = C.green;
  if (level >= 4 && level <= 7) { risk_label = 'Moderado'; risk_color = C.yellow; }
  else if (level > 7) { risk_label = 'Agresivo'; risk_color = C.red; }
  return { max_position_pct, min_spread_pct, stop_loss, take_profit, risk_label, risk_color };
}

function estimate({ capital, level, params }) {
  const daily_return = 0.002 + (level / 10) * 0.008;
  const daily_pnl = capital * daily_return;
  const monthly_pnl = daily_pnl * 22;
  const bad_day = capital * params.max_position_pct * Math.abs(params.stop_loss) * 3;
  return { daily_pnl, monthly_pnl, bad_day };
}

const Row = ({ label, value, color }) => (
  <div className="flex items-center justify-between py-2"
    style={{ borderBottom: `1px solid ${C.border}` }}>
    <div className="text-xs uppercase tracking-wider" style={{ color: C.muted }}>{label}</div>
    <div className="text-sm font-bold tabular-nums" style={{ color: color || C.text }}>{value}</div>
  </div>
);

const EstimateCard = ({ label, value, color }) => (
  <div className="rounded-xl p-4"
    style={{ background: C.panel2, border: `1px solid ${color}33` }}>
    <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: C.muted }}>{label}</div>
    <div className="text-2xl font-black tabular-nums" style={{ color }}>{value}</div>
  </div>
);

export default function StrategyConfigurator({ botConfig, onApplied }) {
  const [level, setLevel] = useState(5);
  const [capital, setCapital] = useState(2000);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);

  const params = useMemo(() => deriveParams(level), [level]);
  const est = useMemo(() => estimate({ capital, level, params }), [capital, level, params]);

  const apply = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const data = {
        max_position_pct: params.max_position_pct,
        min_spread_pct: params.min_spread_pct,
        stop_loss: params.stop_loss,
        take_profit: params.take_profit,
        capital_usdc: Number(capital),
      };
      if (botConfig?.id) {
        await externalBase44Write({ entity: 'BotConfig', action: 'update', id: botConfig.id, data });
      } else {
        await externalBase44Write({ entity: 'BotConfig', action: 'create', data });
      }
      setMsg({ type: 'ok', text: 'Configuracion aplicada al bot' });
      onApplied?.();
    } catch (e) {
      setMsg({ type: 'err', text: e?.message || 'Error al aplicar' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-2xl p-6 backdrop-blur-sm"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}>
      <div className="flex items-center gap-3 mb-5">
        <div className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: `${C.cyan}22`, border: `1px solid ${C.cyan}44` }}>
          <Sliders className="w-5 h-5" style={{ color: C.cyan }} />
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.2em] font-bold" style={{ color: C.cyan }}>
            Strategy Configurator
          </div>
          <div className="text-xs" style={{ color: C.muted }}>
            Ajusta el perfil de riesgo del bot
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="space-y-5">
          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <Zap className="w-4 h-4" style={{ color: params.risk_color }} />
                <span className="text-xs uppercase tracking-wider font-semibold"
                  style={{ color: C.muted }}>Agresividad</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-3xl font-black tabular-nums" style={{ color: params.risk_color }}>
                  {level}
                </span>
                <span className="text-xs font-bold px-2 py-0.5 rounded-full"
                  style={{
                    background: `${params.risk_color}22`,
                    color: params.risk_color,
                    border: `1px solid ${params.risk_color}44`,
                  }}>
                  {params.risk_label}
                </span>
              </div>
            </div>
            <input
              type="range" min={1} max={10} step={1}
              value={level}
              onChange={(e) => setLevel(Number(e.target.value))}
              className="w-full"
              style={{ accentColor: params.risk_color }}
            />
            <div className="flex justify-between text-[10px] mt-1" style={{ color: C.dim }}>
              <span>1 · Conservador</span><span>5 · Moderado</span><span>10 · Agresivo</span>
            </div>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-2">
              <Target className="w-4 h-4" style={{ color: C.cyan }} />
              <span className="text-xs uppercase tracking-wider font-semibold"
                style={{ color: C.muted }}>Capital objetivo (USDC)</span>
            </div>
            <input
              type="number" min={10} step={50}
              value={capital}
              onChange={(e) => setCapital(Math.max(10, Number(e.target.value) || 0))}
              className="w-full px-4 py-3 rounded-lg text-xl font-bold tabular-nums"
              style={{
                background: C.panel2,
                border: `1px solid ${C.border}`,
                color: C.text,
              }}
            />
          </div>

          <div>
            <div className="text-xs uppercase tracking-wider font-semibold mb-2"
              style={{ color: C.muted }}>Parametros generados</div>
            <Row label="Max position" value={`${(params.max_position_pct * 100).toFixed(2)}%`} />
            <Row label="Min spread" value={`${(params.min_spread_pct * 100).toFixed(2)}%`} />
            <Row label="Take profit" value={`+${(params.take_profit * 100).toFixed(1)}%`} color={C.green} />
            <Row label="Stop loss" value={`${(params.stop_loss * 100).toFixed(1)}%`} color={C.red} />
          </div>
        </div>

        <div className="space-y-3">
          <div className="text-xs uppercase tracking-wider font-semibold mb-1"
            style={{ color: C.muted }}>Proyecciones estimadas</div>
          <EstimateCard label="Ganancia estimada / dia" value={fmtMoney(est.daily_pnl)} color={C.green} />
          <EstimateCard label="Ganancia estimada / mes" value={fmtMoney(est.monthly_pnl)} color={C.green} />
          <EstimateCard label="Perdida maxima en dia malo" value={`-${fmtMoney(est.bad_day).replace('$', '$')}`} color={C.red} />

          <button
            onClick={apply}
            disabled={saving}
            className="w-full mt-3 px-5 py-3 rounded-xl font-bold transition flex items-center justify-center gap-2"
            style={{
              background: saving ? C.panel2 : `linear-gradient(135deg, ${C.cyan}44 0%, ${C.purple}44 100%)`,
              border: `1px solid ${C.cyan}66`,
              color: C.text,
              opacity: saving ? 0.5 : 1,
            }}
          >
            <Save className="w-4 h-4" />
            {saving ? 'Aplicando…' : 'Aplicar al Bot'}
          </button>

          {msg && (
            <div className="text-xs text-center font-semibold"
              style={{ color: msg.type === 'ok' ? C.green : C.red }}>
              {msg.text}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
