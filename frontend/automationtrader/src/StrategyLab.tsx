import { useEffect, useState } from 'react'
import { AlertTriangle, Download, Play, Save, Square, Trash2 } from 'lucide-react'
import { EmaResultDetails, EmaSupplyForm } from './EmaSupplyPanel'

type Strategy = { key: string; name: string; description: string; defaults: Record<string, number | string | boolean> }
type Trade = {
  symbol: string
  strategy: string
  direction: string
  entry_time: string
  exit_time: string
  entry_signal: string
  entry_price: number
  exit_price: number
  quantity: number
  initial_stop: number
  target: number
  planned_risk: number
  exit_reason: string
  gross_pnl: number
  net_pnl: number | null
  costs: Record<string, number> | null
  r_multiple: number | null
  holding_bars: number
  simulation_status: string
  relative_volume?: number | null
  zone_id?: string
  prior_touches?: number | null
}

type Result = {
  run_id: string
  strategy_id: string
  starting_capital: number
  ending_equity: number | null
  ending_equity_gross: number
  gross_pnl: number
  gross_profit: number
  gross_loss: number
  net_pnl: number | null
  total_costs: number | null
  costs_configured: boolean
  cost_warning: string | null
  return_percent: number
  trade_count: number
  winning_trades: number
  losing_trades: number
  break_even_trades: number
  win_rate: number
  average_win: number
  average_loss: number
  expectancy_net: number | null
  expectancy_gross: number
  profit_factor: number | null
  max_drawdown_percent: number
  average_holding_bars: number
  longest_winning_streak: number
  longest_losing_streak: number
  signals_rejected: number
  universe_size: number
  stocks_with_signals: number
  stocks_with_trades: number
  survivorship_warning: string
  trades: Trade[]
  stock_performance: Array<Record<string, string | number | null>>
  equity_curve: Array<{ time: string; equity: number }>
  timeframe: string
  start_date: string
  end_date: string
  symbols_checked: number
  symbols_failed: string[]
  data_source: string
  rejection_breakdown?: Record<string, number>
  average_realized_r?: number | null
  short_sale_warning?: string
}

type Run = { id: string; strategy_id: string; status: string; created_at: string; completed_at: string | null; error_text: string | null }
type Preset = { id: number; name: string; strategy_id: string; config: WorkbenchConfig }
type WorkbenchConfig = { version: number; strategy_id: string; common: Record<string, unknown>; strategy: Record<string, unknown>; costs: Record<string, unknown> }

const API = '/api'
const dateValue = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
const calendarDays = (start: string, end: string) => {
  const startMs = Date.parse(`${start}T00:00:00Z`)
  const endMs = Date.parse(`${end}T00:00:00Z`)
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return NaN
  return Math.round((endMs - startMs) / 86_400_000) + 1
}
const dayOffset = (days: number) => { const date = new Date(); date.setDate(date.getDate() - days); return dateValue(date) }
const money = (value: number | null) => value === null ? 'Not available' : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
const timeLabel = (value: string) => new Intl.DateTimeFormat('en-IN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value))

async function requestJson(url: string, init?: RequestInit) {
  const response = await fetch(url, init)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail ?? `Request failed (${response.status})`)
  return data
}

const initialCommon = () => ({
  starting_capital: 100000,
  universe: 'nifty100_current',
  symbols: [],
  timeframe: '4h',
  start_date: dayOffset(364),
  end_date: dateValue(new Date()),
  risk_percent: 0.25,
  aggregate_open_risk_percent: 0.5,
  daily_loss_limit_percent: 1,
  losing_day_streak_limit: 3,
  cooldown_days: 3,
  volume_filter_enabled: true,
  min_relative_volume: 1,
  volume_lookback: 20,
  max_positions: 3,
  max_allocation_percent: 50,
  max_holding_bars: 12,
  max_entries_per_symbol_per_day: 1,
  intraday: true,
  entry_start_time: '09:20',
  entry_cutoff: '14:30',
  square_off_time: '15:25',
})

const initialCosts = () => ({
  brokerage_per_order: '', stt_sell_percent: '', exchange_turnover_percent: '',
  gst_percent: '18', stamp_buy_percent: '', sebi_per_crore: '',
  slippage_bps: '0', spread_bps: '0', source: '', effective_date: '',
})

function StrategyLab({ tokenReady }: { tokenReady: boolean }) {
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [universeOptions, setUniverseOptions] = useState<Array<{ symbol: string; company: string }>>([])
  const [activeStrategy, setActiveStrategy] = useState('sma_crossover')
  const [strategyConfigs, setStrategyConfigs] = useState<Record<string, Record<string, unknown>>>({})
  const [common, setCommon] = useState<Record<string, unknown>>(initialCommon())
  const [costs, setCosts] = useState<Record<string, unknown>>(initialCosts())
  const [presets, setPresets] = useState<Preset[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [results, setResults] = useState<Record<string, Result>>({})
  const [runStatus, setRunStatus] = useState('')
  const [activeRunId, setActiveRunId] = useState('')
  const [error, setError] = useState('')
  const [presetName, setPresetName] = useState('')
  const [selectedPreset, setSelectedPreset] = useState('')
  const [compareIds, setCompareIds] = useState<string[]>([])
  const [comparison, setComparison] = useState<Record<string, unknown> | null>(null)
  const [tradeFilter, setTradeFilter] = useState('all')
  const [tradeSearch, setTradeSearch] = useState('')
  const [selectedTrade, setSelectedTrade] = useState<Trade | null>(null)

  async function loadRuns() {
    const data = await requestJson(`${API}/backtests`)
    setRuns(data)
  }

  async function loadPresets() {
    const data = await requestJson(`${API}/strategy-presets`)
    setPresets(data)
  }

  useEffect(() => {
    void Promise.all([requestJson(`${API}/strategies`), requestJson(`${API}/universe?scope=all`), loadRuns(), loadPresets()])
      .then(([catalog, universe]) => {
        setStrategies(catalog)
        setUniverseOptions(universe)
        setStrategyConfigs(Object.fromEntries(catalog.map((item: Strategy) => [item.key, item.defaults])))
      })
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load Strategy Lab.'))
  }, [])

  const active = strategies.find((strategy) => strategy.key === activeStrategy)
  const strategyConfig = strategyConfigs[activeStrategy] ?? active?.defaults ?? {}
  const activeResult = results[activeStrategy]
  const emaTimeframe = String(strategyConfig.timeframe || '5m')
  const emaIntraday = ['3m', '5m', '15m'].includes(emaTimeframe)
  const emaStart = String(strategyConfig.start_date || (emaIntraday ? dayOffset(27) : common.start_date))
  const emaEnd = String(strategyConfig.end_date || (emaIntraday ? dateValue(new Date()) : common.end_date))
  const rangeStart = activeStrategy === 'ema_supply_demand' ? emaStart : String(common.start_date)
  const rangeEnd = activeStrategy === 'ema_supply_demand' ? emaEnd : String(common.end_date)
  const days = calendarDays(rangeStart, rangeEnd)
  const timeframe = activeStrategy === 'ema_supply_demand' ? emaTimeframe : String(common.timeframe)
  const valid = Number(common.starting_capital) > 0 && days > 0 && days <= 365
    && Number(common.risk_percent) > 0 && Number(common.risk_percent) <= 2
    && Number(common.aggregate_open_risk_percent) > 0 && Number(common.aggregate_open_risk_percent) <= 5
    && (!['3m', '5m', '15m'].includes(timeframe) || days <= 28)
    && Number(strategyConfig.fast_period ?? 1) < Number(strategyConfig.slow_period ?? 2)

  function updateStrategy(key: string, value: unknown) {
    setStrategyConfigs((current) => ({ ...current, [activeStrategy]: { ...current[activeStrategy], [key]: value } }))
    setResults((current) => { const next = { ...current }; delete next[activeStrategy]; return next })
  }

  function updateCommon(key: string, value: unknown) {
    setCommon((current) => ({ ...current, [key]: value }))
    setResults((current) => { const next = { ...current }; delete next[activeStrategy]; return next })
  }

  function updateCost(key: string, value: unknown) {
    setCosts((current) => ({ ...current, [key]: value }))
    setResults((current) => { const next = { ...current }; delete next[activeStrategy]; return next })
  }

  function currentConfig(): WorkbenchConfig {
    const normalizeCosts = Object.fromEntries(Object.entries(costs).map(([key, value]) => {
      if (key === 'source' || key === 'effective_date') return [key, value || null]
      return [key, value === '' ? null : Number(value)]
    }))
    const payloadCommon = { ...common }
    if (activeStrategy === 'ema_supply_demand') {
      payloadCommon.timeframe = emaTimeframe
      payloadCommon.start_date = emaStart
      payloadCommon.end_date = emaEnd
      payloadCommon.entry_start_time = String(strategyConfig.entry_start_time ?? '09:30')
      payloadCommon.entry_cutoff = String(strategyConfig.entry_cutoff ?? '13:00')
      payloadCommon.square_off_time = String(strategyConfig.square_off_time ?? '15:15')
      payloadCommon.max_holding_bars = Number(strategyConfig.max_holding_bars ?? 8)
      payloadCommon.volume_filter_enabled = Boolean(strategyConfig.volume_filter_enabled)
      payloadCommon.min_relative_volume = Number(strategyConfig.min_relative_volume ?? 1.2)
      payloadCommon.volume_lookback = Number(strategyConfig.volume_lookback_sessions ?? 20)
    }
    return { version: 1, strategy_id: activeStrategy, common: payloadCommon, strategy: { ...strategyConfig }, costs: normalizeCosts }
  }

  async function runBacktest() {
    setError('')
    setComparison(null)
    setRunStatus('Preparing run…')
    try {
      const queued = await requestJson(`${API}/backtests`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_id: activeStrategy, config: currentConfig() }),
      })
      setActiveRunId(queued.id)
      setRunStatus('Running historical simulation…')
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 1200))
        const status = await requestJson(`${API}/backtests/${queued.id}`)
        if (status.status === 'COMPLETED') {
          setResults((current) => ({ ...current, [status.strategy_id]: status.result }))
          setRunStatus('Completed')
          await loadRuns()
          return
        }
        if (status.status === 'FAILED') throw new Error(status.error ?? 'Backtest failed.')
        if (status.status === 'CANCELLED') { setRunStatus('Cancelled'); await loadRuns(); return }
        setRunStatus('Running historical simulation…')
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Backtest failed.')
      setRunStatus('Failed')
      await loadRuns()
    }
  }

  async function cancelRun() {
    if (!activeRunId) return
    try {
      await requestJson(`${API}/backtests/${activeRunId}/cancel`, { method: 'POST' })
      setRunStatus('Cancelling…')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not cancel this run.')
    }
  }

  async function savePreset(duplicate = false) {
    if (!presetName.trim()) { setError('Enter a name for this configuration first.'); return }
    try {
      const saved = await requestJson(`${API}/strategy-presets`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: duplicate ? `${presetName} copy` : presetName, strategy_id: activeStrategy, config: currentConfig() }),
      })
      await loadPresets()
      setSelectedPreset(String(saved.id))
      setError('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not save preset.') }
  }

  async function updatePreset() {
    if (!selectedPreset || !presetName.trim()) { setError('Select a saved configuration and enter its name.'); return }
    try {
      await requestJson(`${API}/strategy-presets/${selectedPreset}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: presetName, strategy_id: activeStrategy, config: currentConfig() }),
      })
      await loadPresets()
      setError('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not update preset.') }
  }

  function resetStrategy() {
    if (!active) return
    setStrategyConfigs((current) => ({ ...current, [activeStrategy]: { ...active.defaults } }))
    setCommon(initialCommon())
    setCosts(initialCosts())
    setResults((current) => { const next = { ...current }; delete next[activeStrategy]; return next })
    setError('')
  }

  async function applyPreset() {
    const preset = presets.find((item) => String(item.id) === selectedPreset)
    if (!preset) return
    setActiveStrategy(preset.strategy_id)
    setPresetName(preset.name)
    setCommon(preset.config.common)
    setCosts({ ...initialCosts(), ...preset.config.costs })
    setStrategyConfigs((current) => ({ ...current, [preset.strategy_id]: preset.config.strategy }))
    setResults((current) => { const next = { ...current }; delete next[preset.strategy_id]; return next })
  }

  async function deletePreset() {
    if (!selectedPreset) return
    try {
      await requestJson(`${API}/strategy-presets/${selectedPreset}`, { method: 'DELETE' })
      setSelectedPreset('')
      await loadPresets()
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not delete preset.') }
  }

  async function loadRun(runId: string) {
    try {
      const data = await requestJson(`${API}/backtests/${runId}`)
      if (!data.result) { setError(data.error ?? 'This run has no completed result.'); return }
      setActiveStrategy(data.strategy_id)
      setCommon(data.configuration.common)
      setCosts({ ...initialCosts(), ...data.configuration.costs })
      setStrategyConfigs((current) => ({ ...current, [data.strategy_id]: data.configuration.strategy }))
      setResults((current) => ({ ...current, [data.strategy_id]: data.result }))
      setActiveRunId(runId)
      setError('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not open this run.') }
  }

  async function compareRuns() {
    try {
      setComparison(await requestJson(`${API}/backtests/compare`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(compareIds),
      }))
      setError('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not compare runs.') }
  }

  function forgetRun(runId: string) {
    setCompareIds((current) => current.filter((id) => id !== runId))
    setComparison((current) => {
      const listed = (current?.runs as Array<Result & { id?: string }> | undefined) ?? []
      return listed.some((run) => run.run_id === runId || run.id === runId) ? null : current
    })
    setResults((current) => {
      const next = { ...current }
      for (const [key, result] of Object.entries(next)) {
        if (result.run_id === runId) delete next[key]
      }
      return next
    })
    setActiveRunId((current) => current === runId ? '' : current)
    setSelectedTrade(null)
  }

  async function deleteRun(run: Run) {
    const name = strategies.find((strategy) => strategy.key === run.strategy_id)?.name ?? run.strategy_id
    if (!window.confirm(`Delete the ${name} run from ${timeLabel(run.created_at)}? Its trades and results will be removed.`)) return
    try {
      await requestJson(`${API}/backtests/${run.id}`, { method: 'DELETE' })
      forgetRun(run.id)
      await loadRuns()
      setError('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not delete this run.') }
  }

  async function clearHistory() {
    const finished = runs.filter((run) => run.status !== 'RUNNING')
    if (!finished.length) return
    if (!window.confirm(`Delete ${finished.length} finished run${finished.length === 1 ? '' : 's'}? A run that is still going stays on the list.`)) return
    try {
      await requestJson(`${API}/backtests`, { method: 'DELETE' })
      for (const run of finished) forgetRun(run.id)
      await loadRuns()
      setError('')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not clear run history.') }
  }

  const visibleTrades = (activeResult?.trades ?? []).filter((trade) => {
    const filterMatch = tradeFilter === 'all' || (tradeFilter === 'winners' && trade.gross_pnl > 0) || (tradeFilter === 'losers' && trade.gross_pnl < 0)
    return filterMatch && trade.symbol.toLowerCase().includes(tradeSearch.toLowerCase())
  })
  const chart = activeResult?.equity_curve ?? []
  const linePoints = chart.length > 1 ? chart.map((point, index) => `${(index / (chart.length - 1)) * 400},${95 - (point.equity / Math.max(activeResult?.ending_equity_gross ?? 1, 1)) * 80}`).join(' ') : ''

  function parameterInput(name: string, value: unknown) {
    if (name === 'direction') return <select value={String(value)} onChange={(event) => updateStrategy(name, event.target.value)}><option value="long_only">Long only</option><option value="long_short">Long / short</option></select>
    if (name === 'reference') return <select value={String(value)} onChange={(event) => updateStrategy(name, event.target.value)}><option value="vwap">Session VWAP</option><option value="sma">Moving average</option></select>
    if (name === 'entry_cutoff') return <input type="time" value={String(value)} onChange={(event) => updateStrategy(name, event.target.value)} />
    if (typeof value === 'boolean') return <input type="checkbox" checked={value} onChange={(event) => updateStrategy(name, event.target.checked)} />
    if (typeof value === 'number') return <input type="number" value={value} step={Number.isInteger(value) ? 1 : 0.1} onChange={(event) => updateStrategy(name, Number(event.target.value))} />
    return <input value={String(value)} onChange={(event) => updateStrategy(name, event.target.value)} />
  }

  return (
    <section className="strategy-lab panel" id="strategy-lab">
      <div className="panel-heading strategy-lab-heading">
        <div><div className="section-index">05 / RESEARCH WORKBENCH</div><h2>Strategy Lab</h2></div>
        <div className="simulation-mode">HISTORICAL SIMULATION · NO LIVE ORDERS</div>
      </div>
      <div className="strategy-tabs" role="tablist" aria-label="Strategy selector">
        {strategies.map((strategy) => <button key={strategy.key} type="button" role="tab" aria-selected={activeStrategy === strategy.key} className={activeStrategy === strategy.key ? 'strategy-tab selected' : 'strategy-tab'} onClick={() => setActiveStrategy(strategy.key)}>{strategy.name}</button>)}
      </div>
      <div className="lab-toolbar">
        <div className="lab-preset-actions">
          <select value={selectedPreset} onChange={(event) => setSelectedPreset(event.target.value)} aria-label="Saved configurations">
            <option value="">Saved configurations</option>{presets.map((preset) => <option key={preset.id} value={preset.id}>{preset.name} · {preset.strategy_id}</option>)}
          </select>
          <button type="button" className="quiet-button" onClick={applyPreset} disabled={!selectedPreset}>Load</button>
          <button type="button" className="quiet-button" onClick={() => void updatePreset()} disabled={!selectedPreset}>Update</button>
          <button type="button" className="quiet-button danger-button" onClick={deletePreset} disabled={!selectedPreset} title="Delete selected preset"><Trash2 size={14} /></button>
        </div>
        <div className="lab-save-actions">
          <input aria-label="Configuration name" placeholder="Configuration name" value={presetName} onChange={(event) => setPresetName(event.target.value)} />
          <button type="button" className="quiet-button" onClick={() => void savePreset()}><Save size={14} /> Save</button>
          <button type="button" className="quiet-button" onClick={() => void savePreset(true)}>Duplicate</button>
          <button type="button" className="quiet-button" onClick={resetStrategy}>Reset</button>
        </div>
      </div>
      {active && <div className="active-strategy-description"><b>{active.name}</b><span>{active.description}</span><span className="config-version">CONFIG V1</span></div>}

      {activeStrategy === 'ema_supply_demand' ? (
        <EmaSupplyForm strategy={strategyConfig} common={common} costs={costs} universeOptions={universeOptions} startDate={emaStart} endDate={emaEnd} updateStrategy={updateStrategy} updateCommon={updateCommon} updateCost={updateCost} buildConfig={currentConfig} />
      ) : (
      <div className="lab-config-grid">
        <section className="lab-config-group strategy-rules">
          <h3>Strategy Rules</h3>
          {Object.entries(strategyConfig).map(([name, value]) => <label className="lab-field" key={name}><span>{name.replaceAll('_', ' ')}</span>{parameterInput(name, value)}</label>)}
        </section>
        <section className="lab-config-group risk-settings">
          <h3>Risk Management</h3>
          <label className="lab-field"><span>Starting capital (₹)</span><input type="number" min={1} step={10000} value={Number(common.starting_capital)} onChange={(event) => updateCommon('starting_capital', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Risk per trade (%)</span><input type="number" min={0.01} max={2} step={0.05} value={Number(common.risk_percent)} onChange={(event) => updateCommon('risk_percent', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Aggregate open risk (%)</span><input type="number" min={0.01} max={5} step={0.05} value={Number(common.aggregate_open_risk_percent)} onChange={(event) => updateCommon('aggregate_open_risk_percent', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Daily loss limit (%)</span><input type="number" min={0.01} max={10} step={0.1} value={Number(common.daily_loss_limit_percent)} onChange={(event) => updateCommon('daily_loss_limit_percent', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Loss days before pause</span><input type="number" min={1} max={10} step={1} value={Number(common.losing_day_streak_limit)} onChange={(event) => updateCommon('losing_day_streak_limit', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Pause after loss streak (days)</span><input type="number" min={1} max={20} step={1} value={Number(common.cooldown_days)} onChange={(event) => updateCommon('cooldown_days', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Volume filter</span><input type="checkbox" checked={Boolean(common.volume_filter_enabled)} onChange={(event) => updateCommon('volume_filter_enabled', event.target.checked)} /></label>
          <label className="lab-field"><span>Minimum relative volume</span><input type="number" min={0.1} step={0.1} value={Number(common.min_relative_volume)} onChange={(event) => updateCommon('min_relative_volume', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Volume lookback</span><input type="number" min={2} step={1} value={Number(common.volume_lookback)} onChange={(event) => updateCommon('volume_lookback', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Max open positions</span><input type="number" min={1} max={10} step={1} value={Number(common.max_positions)} onChange={(event) => updateCommon('max_positions', Number(event.target.value))} /></label>
          <label className="lab-field"><span>Max allocation per position (%)</span><input type="number" min={1} max={100} step={1} value={Number(common.max_allocation_percent)} onChange={(event) => updateCommon('max_allocation_percent', Number(event.target.value))} /></label>
        </section>
        <section className="lab-config-group universe-settings">
          <h3>Universe & Session</h3>
          <label className="lab-field"><span>Universe</span><select value={String(common.universe)} onChange={(event) => updateCommon('universe', event.target.value)}><option value="nifty100_current">Nifty 100 · current members</option><option value="selected_symbols">Selected stocks</option><option value="all_nse_equities">All NSE cash equities</option></select></label>
          {common.universe === 'selected_symbols' && <label className="lab-field"><span>Stocks</span><select multiple size={5} value={Array.isArray(common.symbols) ? common.symbols.map(String) : []} onChange={(event) => updateCommon('symbols', Array.from(event.target.selectedOptions, (option) => option.value))}>{universeOptions.map((stock) => <option key={stock.symbol} value={stock.symbol}>{stock.symbol} · {stock.company}</option>)}</select></label>}
          <label className="lab-field"><span>Timeframe</span><select value={String(common.timeframe)} onChange={(event) => updateCommon('timeframe', event.target.value)}>{['3m', '5m', '15m', '30m', '1h', '4h'].map((value) => <option key={value}>{value}</option>)}</select></label>
          <div className="lab-date-fields"><label className="lab-field"><span>Start date</span><input type="date" value={String(common.start_date)} onChange={(event) => updateCommon('start_date', event.target.value)} /></label><label className="lab-field"><span>End date</span><input type="date" value={String(common.end_date)} onChange={(event) => updateCommon('end_date', event.target.value)} /></label></div>
          <label className="lab-field"><span>Entry start</span><input type="time" value={String(common.entry_start_time)} onChange={(event) => updateCommon('entry_start_time', event.target.value)} /></label>
          <label className="lab-field"><span>Last entry</span><input type="time" value={String(common.entry_cutoff)} onChange={(event) => updateCommon('entry_cutoff', event.target.value)} /></label>
          <label className="lab-field"><span>Square-off</span><input type="time" value={String(common.square_off_time)} onChange={(event) => updateCommon('square_off_time', event.target.value)} /></label>
          <label className="lab-field"><span>Max holding (bars)</span><input type="number" min={1} max={1000} value={Number(common.max_holding_bars)} onChange={(event) => updateCommon('max_holding_bars', Number(event.target.value))} /></label>
        </section>
        <section className="lab-config-group cost-settings">
          <h3>Costs & Execution</h3>
          <div className="cost-warning"><AlertTriangle size={15} />Cost rates are unconfigured by default; net performance is withheld until all required rates are entered.</div>
          <div className="cost-grid">
            {[
              ['brokerage_per_order', 'Brokerage / order (₹)'], ['stt_sell_percent', 'STT sell (%)'],
              ['exchange_turnover_percent', 'Exchange (%)'], ['gst_percent', 'GST (%)'],
              ['stamp_buy_percent', 'Stamp duty buy (%)'], ['sebi_per_crore', 'SEBI / crore (₹)'],
              ['slippage_bps', 'Slippage (bps)'], ['spread_bps', 'Spread (bps)'],
            ].map(([key, label]) => <label className="lab-field" key={key}><span>{label}</span><input type="number" min={0} step="any" value={String(costs[key] ?? '')} onChange={(event) => updateCost(key, event.target.value)} /></label>)}
          </div>
          <div className="cost-grid cost-source-grid">
            <label className="lab-field"><span>Rate source</span><input value={String(costs.source ?? '')} onChange={(event) => updateCost('source', event.target.value)} placeholder="Source URL / plan" /></label>
            <label className="lab-field"><span>Effective date</span><input type="date" value={String(costs.effective_date ?? '')} onChange={(event) => updateCost('effective_date', event.target.value)} /></label>
          </div>
        </section>
      </div>
      )}

      <div className="lab-run-bar">
        <div className="run-status"><span className={runStatus.includes('Running') ? 'run-dot running' : 'run-dot'} />{runStatus || `${days} days · ${timeframe} candles · Nifty 100`}</div>
        {activeRunId && runStatus.includes('Running') && <button type="button" className="quiet-button danger-button" onClick={() => void cancelRun()}><Square size={13} /> Cancel run</button>}
        <button type="button" className="scan-button" disabled={!tokenReady || !valid || Boolean(activeRunId && runStatus.includes('Running'))} onClick={() => void runBacktest()}><Play size={15} /> Run backtest</button>
      </div>
      {(!valid || !tokenReady) && <div className="lab-notice"><AlertTriangle size={15} />{!tokenReady ? 'Upstox access token is not configured.' : !Number.isFinite(days) || days <= 0 ? 'The start date has to be on or before the end date.' : ['3m', '5m', '15m'].includes(timeframe) && days > 28 ? `${timeframe} candles cover 28 days. This range is ${days} days, so move the start date later or choose 30m, 1h, or 4h.` : 'Check the risk and position limits.'}</div>}
      {error && <div className="lab-error" role="alert">{error}</div>}

      <div className="lab-history">
        <div className="lab-subheading"><h3>Completed runs</h3><div className="comparison-controls"><span>{runs.length} runs</span><button className="quiet-button" disabled={compareIds.length < 2} onClick={() => void compareRuns()}>Compare selected</button><button type="button" className="quiet-button danger-button" disabled={!runs.some((run) => run.status !== 'RUNNING')} onClick={() => void clearHistory()}><Trash2 size={13} /> Clear history</button></div></div>
        <div className="run-list">{runs.length === 0 && <div className="history-empty">Finished runs will appear here. Delete one, or clear the list, when you no longer need it.</div>}{runs.map((run) => <div className="run-history-row" key={run.id}>
          <input type="checkbox" aria-label={`Select run ${run.id} for comparison`} checked={compareIds.includes(run.id)} onChange={(event) => setCompareIds((current) => event.target.checked ? [...current, run.id].slice(-6) : current.filter((id) => id !== run.id))} />
          <button type="button" className="run-open" onClick={() => void loadRun(run.id)}><b>{strategies.find((strategy) => strategy.key === run.strategy_id)?.name ?? run.strategy_id}</b><small>{timeLabel(run.created_at)} · {run.status}</small></button>
          <span className={`run-state ${run.status.toLowerCase()}`}>{run.status}</span>
          <button type="button" className="quiet-button danger-button run-delete" aria-label={`Delete ${strategies.find((strategy) => strategy.key === run.strategy_id)?.name ?? run.strategy_id} run from ${timeLabel(run.created_at)}`} title={run.status === 'RUNNING' ? 'Cancel this run before deleting it' : 'Delete this run'} disabled={run.status === 'RUNNING'} onClick={() => void deleteRun(run)}><Trash2 size={13} /></button>
        </div>)}</div>
      </div>
      {comparison && <div className="comparison-result"><b>Comparison</b><span>{String(comparison.warning ?? 'Runs use matching date, timeframe, and capital assumptions.')}</span><div>{(comparison.runs as Result[]).map((run) => <span key={run.run_id}>{run.strategy_id}: {run.net_pnl === null ? `gross ${money(run.gross_pnl)}` : money(run.net_pnl)} · DD {run.max_drawdown_percent}% · {run.trade_count} trades</span>)}</div></div>}

      {activeResult && <div className="lab-results">
        <div className="results-heading"><div><div className="section-index">COMPLETED SIMULATION</div><h3>{strategies.find((strategy) => strategy.key === activeResult.strategy_id)?.name} · {activeResult.timeframe} · {activeResult.start_date} to {activeResult.end_date}</h3></div><a className="quiet-button export-button" href={`${API}/backtests/${activeResult.run_id}/trades.csv`}><Download size={14} /> Export CSV</a></div>
        <div className="results-notices"><div className="warning-strip"><AlertTriangle size={15} />{activeResult.survivorship_warning}</div>{activeResult.cost_warning && <div className="warning-strip"><AlertTriangle size={15} />{activeResult.cost_warning}</div>}</div>
        <div className="result-kpis">
          {[
            ['Starting capital', money(activeResult.starting_capital)],
            [activeResult.costs_configured ? 'Ending equity' : 'Ending equity (gross)', money(activeResult.costs_configured ? activeResult.ending_equity : activeResult.ending_equity_gross)],
            ['Gross P&L', money(activeResult.gross_pnl)],
            ['Net P&L', activeResult.net_pnl === null ? 'Enter cost rates' : money(activeResult.net_pnl)],
            ['Trades · win rate', `${activeResult.trade_count} · ${activeResult.win_rate.toFixed(1)}%`],
            ['Gross profit / loss', `${money(activeResult.gross_profit)} / ${money(-activeResult.gross_loss)}`],
            ['Expectancy', money(activeResult.expectancy_net ?? activeResult.expectancy_gross)],
            ['Profit factor', activeResult.profit_factor?.toFixed(2) ?? '—'],
            ['Max drawdown', `${activeResult.max_drawdown_percent.toFixed(2)}%`],
            ['Total costs', activeResult.total_costs === null ? 'Enter cost rates' : money(activeResult.total_costs)],
            ['Risk / reward', `1 : ${strategyConfig.reward_to_risk ?? '—'}`],
            ...(activeResult.strategy_id === 'ema_supply_demand' ? [['Average realized R', activeResult.average_realized_r == null ? '—' : String(activeResult.average_realized_r)] as [string, string]] : []),
            ['Rejected signals', String(activeResult.signals_rejected)],
            ['Stocks evaluated', `${activeResult.symbols_checked} · ${activeResult.symbols_failed.length} failed`],
          ].map(([label, value]) => <div className="result-kpi" key={label}><small>{label}</small><b>{value}</b></div>)}
        </div>
        <div className="equity-chart">
          <div className="chart-title">Equity curve · {activeResult.costs_configured ? 'net of configured charges' : 'gross, before costs'}</div>
          {linePoints ? <svg viewBox="0 0 400 100" preserveAspectRatio="none" role="img" aria-label="Backtest equity curve"><polyline points={linePoints} fill="none" stroke="#25765d" strokeWidth="2" vectorEffect="non-scaling-stroke" /></svg> : <div className="chart-empty">No completed trades to plot.</div>}
        </div>
        {activeResult.rejection_breakdown && <EmaResultDetails result={activeResult} />}
        <div className="lab-subheading"><h3>Trade ledger <span>{visibleTrades.length} shown</span></h3><div className="trade-filters"><input value={tradeSearch} placeholder="Filter symbol" onChange={(event) => setTradeSearch(event.target.value)} /><select value={tradeFilter} onChange={(event) => setTradeFilter(event.target.value)}><option value="all">All trades</option><option value="winners">Winners</option><option value="losers">Losers</option></select></div></div>
        <div className="table-scroll"><table className="lab-table"><thead><tr><th>Stock</th><th>Side</th><th>Entry / exit</th><th>Entry signal</th><th>Entry</th><th>Stop</th><th>Target</th><th>Qty</th><th>Gross P&amp;L</th><th>Net P&amp;L</th><th>Exit reason</th><th>R</th></tr></thead><tbody>
          {visibleTrades.map((trade, index) => <tr key={`${trade.symbol}-${trade.entry_time}-${index}`} onClick={() => setSelectedTrade(trade)}><td>{trade.symbol}</td><td>{trade.direction}</td><td>{timeLabel(trade.entry_time)}<small>{timeLabel(trade.exit_time)}</small></td><td>{trade.entry_signal}</td><td>{money(trade.entry_price)}</td><td>{money(trade.initial_stop)}</td><td>{money(trade.target)}</td><td>{trade.quantity}</td><td className={trade.gross_pnl >= 0 ? 'positive' : 'negative'}>{money(trade.gross_pnl)}</td><td>{money(trade.net_pnl)}</td><td>{trade.exit_reason}</td><td>{trade.r_multiple?.toFixed(2) ?? '—'}</td></tr>)}
          {!visibleTrades.length && <tr><td colSpan={12} className="no-trades">No trades match the current filter.</td></tr>}
        </tbody></table></div>
        {selectedTrade && <div className="trade-detail"><div><b>{selectedTrade.symbol} · {selectedTrade.direction} · {selectedTrade.simulation_status}</b><button type="button" className="quiet-button" onClick={() => setSelectedTrade(null)}>Close</button></div><p>{selectedTrade.entry_signal}; {selectedTrade.holding_bars} bars held; planned risk {money(selectedTrade.planned_risk)}; exit: {selectedTrade.exit_reason}.{selectedTrade.relative_volume != null ? ` Relative volume ${selectedTrade.relative_volume}.` : ''}{selectedTrade.zone_id ? ` Zone ${selectedTrade.zone_id}, ${selectedTrade.prior_touches ?? 0} prior touches.` : ''}</p><p>Fees: {selectedTrade.costs ? JSON.stringify(selectedTrade.costs) : 'Not configured; net P&L withheld.'}</p></div>}
        <div className="lab-subheading"><h3>Stock performance <span>{activeResult.stock_performance.length} constituents</span></h3></div>
        <div className="table-scroll"><table className="lab-table"><thead><tr><th>Stock</th><th>Signals</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win rate</th><th>Gross P&amp;L</th><th>Net P&amp;L</th><th>Costs</th></tr></thead><tbody>
          {activeResult.stock_performance.map((stock) => <tr key={String(stock.symbol)}><td>{String(stock.symbol)}</td><td>{String(stock.signals)}</td><td>{String(stock.trades)}</td><td>{String(stock.wins)}</td><td>{String(stock.losses)}</td><td>{String(stock.win_rate)}%</td><td>{money(Number(stock.gross_pnl))}</td><td>{money(stock.net_pnl === null ? null : Number(stock.net_pnl))}</td><td>{money(stock.costs === null ? null : Number(stock.costs))}</td></tr>)}
        </tbody></table></div>
      </div>}
    </section>
  )
}

export default StrategyLab
