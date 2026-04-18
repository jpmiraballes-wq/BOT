import React, { useState } from 'react';
import { Pause, Play } from 'lucide-react';
import { C } from '@/components/dashboard/theme';
import { externalBase44Write } from '@/functions/externalBase44Write';

export default function PauseResumeToggle({ botConfig, onToggled }) {
  const paused = !!botConfig?.paused;
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState(null);

  const toggle = async () => {
    setRunning(true);
    setMsg(null);
    try {
      const data = { paused: !paused };
      if (paused) data.emergency_stop = false;
      if (botConfig?.id) {
        await externalBase44Write({ entity: 'BotConfig', action: 'update', id: botConfig.id, data });
      } else {
        await externalBase44Write({ entity: 'BotConfig', action: 'create', data });
      }
      setMsg({ type: 'ok', text: paused ? 'Bot reanudado' : 'Bot pausado' });
      onToggled?.();
    } catch (e) {
      setMsg({ type: 'err', text: e?.message || 'Error al cambiar estado' });
    } finally {
      setRunning(false);
    }
  };

  const color = paused ? C.green : C.amber;
  const Icon = paused ? Play : Pause;
  const label = paused ? 'REANUDAR' : 'PAUSAR';
  const desc = paused
    ? 'El bot esta detenido. Reanuda para volver a operar.'
    : 'Detiene nuevas entradas. Mantiene posiciones abiertas.';

  return (
    <div className="rounded-2xl p-6 flex flex-col items-center justify-center text-center"
      style={{
        background: `linear-gradient(135deg, ${color}15 0%, ${C.panel2} 100%)`,
        border: `1px solid ${color}44`,
        boxShadow: `0 0 30px ${color}22`,
      }}>
      <Icon className="w-10 h-10 mb-3" style={{ color }} />
      <div className="text-[11px] uppercase tracking-[0.2em] font-bold mb-1" style={{ color }}>
        {paused ? 'Bot pausado' : 'Bot activo'}
      </div>
      <div className="text-xs mb-5" style={{ color: C.muted }}>{desc}</div>
      <button
        onClick={toggle}
        disabled={running}
        className="w-full px-6 py-4 rounded-xl font-black text-lg tracking-wider transition hover:scale-[1.02]"
        style={{
          background: `linear-gradient(135deg, ${color} 0%, ${color}cc 100%)`,
          color: '#05070d',
          boxShadow: `0 4px 20px ${color}66`,
          opacity: running ? 0.6 : 1,
        }}>
        {running ? '…' : label}
      </button>
      {msg && (
        <div className="text-xs mt-3 font-semibold"
          style={{ color: msg.type === 'ok' ? C.green : C.red }}>
          {msg.text}
        </div>
      )}
    </div>
  );
}
