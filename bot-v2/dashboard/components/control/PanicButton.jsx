import React, { useState } from 'react';
import { AlertOctagon, X } from 'lucide-react';
import { C } from '@/components/dashboard/theme';
import { externalBase44Write } from '@/functions/externalBase44Write';

export default function PanicButton({ botConfig, onTriggered }) {
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState(null);

  const trigger = async () => {
    setRunning(true);
    setMsg(null);
    try {
      const data = { paused: true, emergency_stop: true, emergency_stop_at: new Date().toISOString() };
      if (botConfig?.id) {
        await externalBase44Write({ entity: 'BotConfig', action: 'update', id: botConfig.id, data });
      } else {
        await externalBase44Write({ entity: 'BotConfig', action: 'create', data });
      }
      setMsg({ type: 'ok', text: 'Emergency stop activado. Bot cerrando posiciones…' });
      setConfirming(false);
      onTriggered?.();
    } catch (e) {
      setMsg({ type: 'err', text: e?.message || 'Error al activar emergencia' });
    } finally {
      setRunning(false);
    }
  };

  if (!confirming) {
    return (
      <div className="rounded-2xl p-6 flex flex-col items-center justify-center text-center"
        style={{
          background: `linear-gradient(135deg, ${C.red}15 0%, ${C.panel2} 100%)`,
          border: `1px solid ${C.red}44`,
          boxShadow: `0 0 40px ${C.red}22`,
        }}>
        <AlertOctagon className="w-10 h-10 mb-3" style={{ color: C.red }} />
        <div className="text-[11px] uppercase tracking-[0.2em] font-bold mb-1"
          style={{ color: C.red }}>Parada de emergencia</div>
        <div className="text-xs mb-5" style={{ color: C.muted }}>
          Cancela TODAS las ordenes y cierra posiciones
        </div>
        <button
          onClick={() => setConfirming(true)}
          className="w-full px-6 py-4 rounded-xl font-black text-lg tracking-wider transition hover:scale-[1.02]"
          style={{
            background: `linear-gradient(135deg, ${C.red} 0%, #c62638 100%)`,
            color: '#fff',
            boxShadow: `0 4px 20px ${C.red}66`,
          }}>
          🛑 EMERGENCY STOP
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

  return (
    <div className="rounded-2xl p-6 text-center"
      style={{
        background: `linear-gradient(135deg, ${C.red}22 0%, ${C.panel2} 100%)`,
        border: `2px solid ${C.red}`,
        boxShadow: `0 0 50px ${C.red}44`,
      }}>
      <AlertOctagon className="w-12 h-12 mx-auto mb-3 animate-pulse" style={{ color: C.red }} />
      <div className="text-lg font-black mb-2" style={{ color: C.text }}>
        ¿Cerrar TODAS las posiciones?
      </div>
      <div className="text-xs mb-5" style={{ color: C.muted }}>
        Esta accion no se puede deshacer
      </div>
      <div className="flex gap-2">
        <button
          onClick={() => setConfirming(false)}
          disabled={running}
          className="flex-1 px-4 py-3 rounded-xl font-bold transition flex items-center justify-center gap-2"
          style={{
            background: C.panel2,
            border: `1px solid ${C.border}`,
            color: C.muted,
          }}>
          <X className="w-4 h-4" /> Cancelar
        </button>
        <button
          onClick={trigger}
          disabled={running}
          className="flex-1 px-4 py-3 rounded-xl font-black transition"
          style={{
            background: `linear-gradient(135deg, ${C.red} 0%, #c62638 100%)`,
            color: '#fff',
            boxShadow: `0 4px 20px ${C.red}66`,
            opacity: running ? 0.6 : 1,
          }}>
          {running ? 'Ejecutando…' : 'CONFIRMAR STOP'}
        </button>
      </div>
      {msg && (
        <div className="text-xs mt-3 font-semibold"
          style={{ color: msg.type === 'ok' ? C.green : C.red }}>
          {msg.text}
        </div>
      )}
    </div>
  );
}
