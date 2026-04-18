import React from 'react';
import { Command } from 'lucide-react';
import { C } from '@/components/dashboard/theme';
import StrategyConfigurator from './StrategyConfigurator';
import PanicButton from './PanicButton';
import PauseResumeToggle from './PauseResumeToggle';

export default function ControlPanel({ botConfig, onRefresh }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 pt-2">
        <div className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: `${C.purple}22`, border: `1px solid ${C.purple}44` }}>
          <Command className="w-5 h-5" style={{ color: C.purple }} />
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.2em] font-bold" style={{ color: C.purple }}>
            Control Panel
          </div>
          <div className="text-xs" style={{ color: C.muted }}>
            Estrategia, pausa y parada de emergencia
          </div>
        </div>
      </div>

      <StrategyConfigurator botConfig={botConfig} onApplied={onRefresh} />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <PauseResumeToggle botConfig={botConfig} onToggled={onRefresh} />
        <PanicButton botConfig={botConfig} onTriggered={onRefresh} />
      </div>
    </div>
  );
}
