import { useEffect, useState } from 'react'
import { AlertTriangle, Play, Square } from 'lucide-react'

type FieldMap = Record<string, unknown>
type Stock = { symbol: string; company: string }
type Config = { strategy_id: string; common: FieldMap; strategy: FieldMap; costs: FieldMap }
type Bar = { time: string; open: number; high: number; low: number; close: number }
type Zone = {
  id: string
  side: string
  lower: number
  upper: number
  status: string
  reason: string
  confirmed_time: string | null
  invalidated_time: string | null
}
type ChartTrade = { entry_time: string; exit_time: string; entry_price: number; exit_price: number; initial_stop: number; target: number; direction: string }
type Chart = { bars: Bar[]; zones: Zone[]; trades: ChartTrade[] }
type Paper = {
  id?: string
  status: string
  trades?: Array<FieldMap>
  open_positions?: Array<FieldMap>
  last_error?: string | null
}

const API = '/api'
const REASONS = [
  'NO_VALID_ZONE', 'ZONE_INVALIDATED', 'EMA_FILTER_FAILED', 'REJECTION_CONFIRMATION_FAILED',
  'LOW_RELATIVE_VOLUME', 'INSUFFICIENT_TARGET_SPACE', 'ENTRY_CUTOFF', 'ENTRY_TOO_EARLY',
  'POSITION_RISK_LIMIT', 'INSUFFICIENT_LIQUIDITY', 'COOLDOWN',
]

async function requestJson(url: string, init?: RequestInit) {
  const response = await fetch(url, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : response.statusText)
  return body
}

function numberField(label: string, key: string, value: unknown, onChange: (key: string, value: number) => void, step = 'any') {
  return (
    <label className="lab-field" key={key}>
      <span>{label}</span>
      <input type="number" step={step} value={value === undefined || value === null ? '' : Number(value)} onChange={(event) => onChange(key, Number(event.target.value))} />
    </label>
  )
}

function metricText(value: unknown) {
  if (!value || typeof value !== 'object') return '—'
  const row = value as FieldMap
  if ('count' in row) return `${row.count} · ${row.percent}%`
  return `${row.trade_count ?? 0} trades · win ${row.win_rate ?? 0}% · gross ${row.gross_pnl ?? 0} · avg R ${row.average_r ?? '—'}`
}

export function EmaSupplyForm({
  strategy, common, costs, universeOptions, startDate, endDate, updateStrategy, updateCommon, updateCost, buildConfig,
}: {
  strategy: FieldMap
  common: FieldMap
  costs: FieldMap
  universeOptions: Stock[]
  startDate: string
  endDate: string
  updateStrategy: (key: string, value: unknown) => void
  updateCommon: (key: string, value: unknown) => void
  updateCost: (key: string, value: unknown) => void
  buildConfig: () => Config
}) {
  const [paper, setPaper] = useState<Paper>({ status: 'NONE', trades: [], open_positions: [] })
  const [paperError, setPaperError] = useState('')
  const symbols = Array.isArray(common.symbols) ? common.symbols.map(String) : []
  const paperSymbols = (Array.isArray(strategy.paper_symbols) ? strategy.paper_symbols.map(String) : symbols).slice(0, 10)
  const today = new Date()
  const todayValue = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`

  function shiftDate(value: string, amount: number) {
    return new Date(Date.parse(`${value}T00:00:00Z`) + amount * 86_400_000).toISOString().slice(0, 10)
  }

  function commitDates(which: 'start' | 'end', value: string, timeframe = String(strategy.timeframe ?? '5m')) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return
    const limit = ['3m', '5m', '15m'].includes(timeframe) ? 28 : 365
    let start = which === 'start' ? value : startDate
    let end = which === 'end' ? value : endDate
    if (end > todayValue) end = todayValue
    if (start > todayValue) start = todayValue
    if (start > end) {
      if (which === 'start') end = start
      else start = end
    }
    const span = Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000) + 1
    if (span > limit) {
      if (which === 'start') {
        end = shiftDate(start, limit - 1)
        if (end > todayValue) {
          end = todayValue
          start = shiftDate(end, 1 - limit)
        }
      } else {
        start = shiftDate(end, 1 - limit)
      }
    }
    updateStrategy('start_date', start)
    updateStrategy('end_date', end)
  }

  useEffect(() => {
    const limit = ['3m', '5m', '15m'].includes(String(strategy.timeframe ?? '5m')) ? 28 : 365
    const span = Math.round((Date.parse(`${endDate}T00:00:00Z`) - Date.parse(`${startDate}T00:00:00Z`)) / 86_400_000) + 1
    if (endDate > todayValue || startDate > endDate || span > limit) commitDates('end', endDate > todayValue ? todayValue : endDate)
    // The dates are corrected once. commitDates is recreated each render and must not retrigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startDate, endDate, strategy.timeframe, todayValue])

  useEffect(() => {
    let stop = false
    async function load() {
      try {
        const data = await requestJson(`${API}/paper/sessions/current`)
        if (!stop) setPaper(data)
      } catch {
        /* The panel stays usable when the API is offline. */
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => { stop = true; window.clearInterval(timer) }
  }, [])

  async function startPaper() {
    setPaperError('')
    if (paperSymbols.length === 0) {
      setPaperError('Select at least one stock. Paper watches those names only, up to ten, and does not poll the full list.')
      return
    }
    try {
      const config = buildConfig()
      config.common = { ...config.common, universe: 'selected_symbols', symbols: paperSymbols }
      await requestJson(`${API}/paper/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_id: 'ema_supply_demand', config }),
      })
      setPaper(await requestJson(`${API}/paper/sessions/current`))
    } catch (error) {
      setPaperError(error instanceof Error ? error.message : 'Paper session could not start.')
    }
  }

  async function stopPaper() {
    if (!paper.id) return
    setPaperError('')
    try {
      await requestJson(`${API}/paper/sessions/${paper.id}/stop`, { method: 'POST' })
      setPaper(await requestJson(`${API}/paper/sessions/current`))
    } catch (error) {
      setPaperError(error instanceof Error ? error.message : 'Paper session could not stop.')
    }
  }

  return (
    <div className="ema-panel">
      <section className="lab-config-group">
        <h3>EMA &amp; Trend</h3>
        <div className="ema-fields">
          {numberField('Fast EMA', 'fast_period', strategy.fast_period, updateStrategy, '1')}
          {numberField('Slow EMA', 'slow_period', strategy.slow_period, updateStrategy, '1')}
          <label className="lab-field"><span>Trend mode</span><select value={String(strategy.trend_mode ?? 'trend')} onChange={(event) => updateStrategy('trend_mode', event.target.value)}><option value="trend">Current alignment</option><option value="fresh_crossover">Fresh crossover</option></select></label>
          {numberField('Crossover lookback', 'crossover_lookback', strategy.crossover_lookback, updateStrategy, '1')}
          <label className="check-field"><input type="checkbox" checked={Boolean(strategy.require_slow_ema_slope)} onChange={(event) => updateStrategy('require_slow_ema_slope', event.target.checked)} /> EMA 21 slope over three candles</label>
          {numberField('Slope lookback', 'slope_lookback', strategy.slope_lookback, updateStrategy, '1')}
          <label className="lab-field"><span>Direction</span><select value={String(strategy.direction ?? 'long_short')} onChange={(event) => updateStrategy('direction', event.target.value)}><option value="long_only">Long only</option><option value="long_short">Long and short</option></select></label>
        </div>
      </section>
      <section className="lab-config-group">
        <h3>Supply &amp; Demand</h3>
        <div className="ema-fields">
          {numberField('Swing strength', 'pivot_strength', strategy.pivot_strength, updateStrategy, '1')}
          {numberField('Min zone width (ATR)', 'zone_width_min_atr', strategy.zone_width_min_atr, updateStrategy)}
          {numberField('Max zone width (ATR)', 'zone_width_max_atr', strategy.zone_width_max_atr, updateStrategy)}
          {numberField('Departure (ATR)', 'departure_atr', strategy.departure_atr, updateStrategy)}
          {numberField('Departure relative volume', 'min_departure_relative_volume', strategy.min_departure_relative_volume, updateStrategy)}
          {numberField('Max zone age (bars)', 'max_zone_age_bars', strategy.max_zone_age_bars, updateStrategy, '1')}
          {numberField('Max prior touches', 'max_prior_touches', strategy.max_prior_touches, updateStrategy, '1')}
          {numberField('Min opposing distance (ATR)', 'min_opposing_distance_atr', strategy.min_opposing_distance_atr, updateStrategy)}
          {numberField('ATR period', 'atr_period', strategy.atr_period, updateStrategy, '1')}
          {numberField('Invalidation buffer (ATR)', 'invalidation_atr_buffer', strategy.invalidation_atr_buffer, updateStrategy)}
          <label className="lab-field"><span>Entry mode</span><select value={String(strategy.entry_mode ?? 'touch_trend_rejection')} onChange={(event) => updateStrategy('entry_mode', event.target.value)}><option value="touch_trend_rejection">Touch, trend, and rejection</option><option value="touch_rejection">Touch and rejection</option></select></label>
        </div>
      </section>
      <section className="lab-config-group">
        <h3>Volume</h3>
        <div className="ema-fields">
          <label className="check-field"><input type="checkbox" checked={Boolean(strategy.volume_filter_enabled)} onChange={(event) => updateStrategy('volume_filter_enabled', event.target.checked)} /> Same-slot relative volume</label>
          {numberField('Minimum relative volume', 'min_relative_volume', strategy.min_relative_volume, updateStrategy)}
          {numberField('Lookback sessions', 'volume_lookback_sessions', strategy.volume_lookback_sessions, updateStrategy, '1')}
        </div>
        <p className="ema-note">Volume is this candle divided by the median of the same IST clock slot over prior sessions. The current session is excluded.</p>
      </section>
      <section className="lab-config-group">
        <h3>Entry Timing</h3>
        <div className="ema-fields">
          <label className="lab-field"><span>Entry start</span><input type="time" value={String(strategy.entry_start_time ?? '09:30')} onChange={(event) => updateStrategy('entry_start_time', event.target.value)} /></label>
          <label className="lab-field"><span>Last execution</span><input type="time" value={String(strategy.entry_cutoff ?? '13:00')} onChange={(event) => updateStrategy('entry_cutoff', event.target.value)} /></label>
          <label className="lab-field"><span>Square-off</span><input type="time" value={String(strategy.square_off_time ?? '15:15')} onChange={(event) => updateStrategy('square_off_time', event.target.value)} /></label>
          {numberField('Max holding (bars)', 'max_holding_bars', strategy.max_holding_bars, updateStrategy, '1')}
        </div>
        <p className="ema-note">An execution bar at 13:00 IST or later is rejected. Stops, targets, and the other exits still run after that time.</p>
      </section>
      <section className="lab-config-group">
        <h3>Stops, targets &amp; risk / reward</h3>
        <div className="ema-fields">
          <label className="lab-field"><span>Target</span><select value={String(strategy.target_mode ?? 'opposing_zone')} onChange={(event) => updateStrategy('target_mode', event.target.value)}><option value="opposing_zone">Nearest opposing zone</option><option value="fixed_r">Fixed risk / reward</option><option value="first_of_both">First of zone or fixed R</option></select></label>
          {numberField('Risk / reward (R)', 'reward_to_risk', strategy.reward_to_risk, updateStrategy)}
          {numberField('Maximum reward (R)', 'max_target_r', strategy.max_target_r, updateStrategy)}
          {numberField('Minimum reward (R)', 'min_reward_to_risk', strategy.min_reward_to_risk, updateStrategy)}
          <p className="ema-note">A value of 2 is 1:2. The profit target is twice the distance from entry to stop. Fixed risk / reward uses that number. Nearest opposing zone uses the zone, and never aims farther than the maximum reward. A setup is skipped when the room is below the minimum reward.</p>
          <label className="lab-field"><span>Stop</span><select value={String(strategy.stop_method ?? 'zone_atr')} onChange={(event) => updateStrategy('stop_method', event.target.value)}><option value="zone_atr">Zone boundary plus ATR</option><option value="swing">Confirmed swing</option><option value="atr">ATR stop</option></select></label>
          {numberField('Zone ATR buffer', 'atr_buffer', strategy.atr_buffer, updateStrategy)}
          {numberField('ATR stop multiple', 'atr_stop_multiple', strategy.atr_stop_multiple, updateStrategy)}
          {numberField('Swing lookback', 'swing_lookback', strategy.swing_lookback, updateStrategy, '1')}
          <label className="check-field"><input type="checkbox" checked={Boolean(strategy.momentum_failure_enabled)} onChange={(event) => updateStrategy('momentum_failure_enabled', event.target.checked)} /> Momentum-failure exit</label>
          {numberField('Momentum bars', 'momentum_failure_bars', strategy.momentum_failure_bars, updateStrategy, '1')}
          {numberField('Progress threshold', 'momentum_progress_threshold', strategy.momentum_progress_threshold, updateStrategy)}
          {numberField('Max participation of signal volume', 'max_participation', strategy.max_participation, updateStrategy)}
        </div>
      </section>
      <section className="lab-config-group">
        <h3>Risk</h3>
        <div className="ema-fields">
          {numberField('Starting capital', 'starting_capital', common.starting_capital, updateCommon, '1')}
          {numberField('Risk per trade (%)', 'risk_percent', common.risk_percent, updateCommon)}
          {numberField('Aggregate open risk (%)', 'aggregate_open_risk_percent', common.aggregate_open_risk_percent, updateCommon)}
          {numberField('Daily loss limit (%)', 'daily_loss_limit_percent', common.daily_loss_limit_percent, updateCommon)}
          {numberField('Max open positions', 'max_positions', strategy.max_positions ?? common.max_positions, (key, value) => { updateStrategy(key, value); updateCommon(key, value) }, '1')}
          {numberField('Max allocation (%)', 'max_allocation_percent', common.max_allocation_percent, updateCommon, '1')}
          {numberField('Loss days before pause', 'losing_day_streak_limit', common.losing_day_streak_limit, updateCommon, '1')}
          {numberField('Cooldown days', 'cooldown_days', common.cooldown_days, updateCommon, '1')}
        </div>
      </section>
      <section className="lab-config-group">
        <h3>Costs</h3>
        <p className="ema-note">Leave these blank to withhold net P&amp;L. Missing rates are not filled in.</p>
        <div className="ema-fields">
          {[
            ['brokerage_per_order', 'Brokerage / order (₹)'], ['stt_sell_percent', 'STT sell (%)'],
            ['exchange_turnover_percent', 'Exchange (%)'], ['gst_percent', 'GST (%)'],
            ['stamp_buy_percent', 'Stamp duty buy (%)'], ['sebi_per_crore', 'SEBI / crore (₹)'],
            ['slippage_bps', 'Slippage (bps)'], ['spread_bps', 'Spread (bps)'],
          ].map(([key, label]) => <label className="lab-field" key={key}><span>{label}</span><input type="number" min={0} step="any" value={String(costs[key] ?? '')} onChange={(event) => updateCost(key, event.target.value)} /></label>)}
          <label className="lab-field"><span>Rate source</span><input value={String(costs.source ?? '')} onChange={(event) => updateCost('source', event.target.value)} /></label>
          <label className="lab-field"><span>Effective date</span><input type="date" value={String(costs.effective_date ?? '')} onChange={(event) => updateCost('effective_date', event.target.value)} /></label>
        </div>
      </section>
      <section className="lab-config-group">
        <h3>Data &amp; Execution</h3>
        <div className="ema-fields">
          <label className="lab-field"><span>Universe</span><select value={String(common.universe)} onChange={(event) => updateCommon('universe', event.target.value)}><option value="nifty100_current">Nifty 100 · current members</option><option value="selected_symbols">Selected stocks</option><option value="all_nse_equities">All NSE cash equities</option></select></label>
          <label className="lab-field"><span>Timeframe</span><select value={String(strategy.timeframe ?? '5m')} onChange={(event) => { updateStrategy('timeframe', event.target.value); commitDates('end', endDate > todayValue ? todayValue : endDate, event.target.value) }}>{['3m', '5m', '15m', '30m', '1h', '4h'].map((value) => <option key={value}>{value}</option>)}</select></label>
          <label className="lab-field"><span>Start date</span><input type="date" max={todayValue} value={startDate} onChange={(event) => commitDates('start', event.target.value)} /></label>
          <label className="lab-field"><span>End date</span><input type="date" max={todayValue} value={endDate} onChange={(event) => commitDates('end', event.target.value)} /></label>
          <p className="ema-note">{Math.round((Date.parse(`${endDate}T00:00:00Z`) - Date.parse(`${startDate}T00:00:00Z`)) / 86_400_000) + 1} days selected. On 3-, 5-, and 15-minute charts the window stays at 28 days or less, and the other date moves with your change so Run backtest stays available.</p>
          {common.universe === 'selected_symbols' && <label className="lab-field ema-symbols"><span>Stocks</span><select multiple size={6} value={symbols} onChange={(event) => updateCommon('symbols', Array.from(event.target.selectedOptions, (option) => option.value))}>{universeOptions.map((stock) => <option key={stock.symbol} value={stock.symbol}>{stock.symbol} · {stock.company}</option>)}</select></label>}
        </div>
        <p className="ema-note">Runs use the current constituent list. Three-, five-, and fifteen-minute history is capped at 28 days. A three-minute versus five-minute comparison is a second run. This screen does not place orders.</p>
      </section>
      <section className="lab-config-group paper-panel">
        <h3>Paper session</h3>
        <p className="ema-note">Completed candles are polled for up to ten selected stocks. Fills stay in the paper tables and no order is sent.</p>
        <label className="lab-field ema-symbols"><span>Paper stocks</span><select multiple size={6} value={paperSymbols} onChange={(event) => updateStrategy('paper_symbols', Array.from(event.target.selectedOptions, (option) => option.value).slice(0, 10))}>{universeOptions.map((stock) => <option key={stock.symbol} value={stock.symbol}>{stock.symbol} · {stock.company}</option>)}</select></label>
        <div className="paper-actions">
          <button type="button" className="scan-button" disabled={paper.status === 'RUNNING'} onClick={() => void startPaper()}><Play size={14} /> Start paper</button>
          <button type="button" className="quiet-button danger-button" disabled={paper.status !== 'RUNNING'} onClick={() => void stopPaper()}><Square size={13} /> Stop</button>
          <span className={`run-state ${String(paper.status).toLowerCase()}`}>{paper.status}</span>
        </div>
        {paperSymbols.length === 0 && <p className="ema-note">Select at least one stock above. Paper watches those names only, up to ten, and does not poll the full Nifty 100 list.</p>}
        {paperError && <div className="lab-error" role="alert">{paperError}</div>}
        {paper.last_error && <div className="warning-strip"><AlertTriangle size={15} />{paper.last_error}</div>}
        <div className="table-scroll"><table className="lab-table"><thead><tr><th>Open</th><th>Side</th><th>Entry</th><th>Qty</th></tr></thead><tbody>
          {(paper.open_positions ?? []).map((position, index) => <tr key={`${position.symbol}-${index}`}><td>{String(position.symbol)}</td><td>{String(position.direction)}</td><td>{String(position.entry_time ?? '')}</td><td>{String(position.quantity ?? '')}</td></tr>)}
          {!(paper.open_positions ?? []).length && <tr><td colSpan={4} className="no-trades">No open simulated position.</td></tr>}
        </tbody></table></div>
        <div className="table-scroll"><table className="lab-table"><thead><tr><th>Fill</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th></tr></thead><tbody>
          {(paper.trades ?? []).slice(-12).map((trade, index) => <tr key={`${trade.symbol}-${index}`}><td>{String(trade.symbol)}</td><td>{String(trade.direction)}</td><td>{String(trade.entry_time ?? '')}</td><td>{String(trade.exit_time ?? '')}</td><td>{String(trade.exit_reason ?? '')}</td></tr>)}
          {!(paper.trades ?? []).length && <tr><td colSpan={5} className="no-trades">No paper fills yet.</td></tr>}
        </tbody></table></div>
      </section>
    </div>
  )
}

function ZoneChart({ chart }: { chart: Chart }) {
  const [zoneId, setZoneId] = useState<string | null>(null)
  const bars = chart.bars
  if (!bars.length) return <div className="chart-empty">No cached bars for this symbol.</div>
  const prices = bars.flatMap((bar) => [bar.high, bar.low])
  const low = Math.min(...prices)
  const high = Math.max(...prices)
  const span = high - low || 1
  const width = 760
  const height = 280
  const xAt = (index: number) => (bars.length === 1 ? width / 2 : (index / (bars.length - 1)) * width)
  const yAt = (price: number) => ((high - price) / span) * (height - 20) + 10
  const slot = Math.max(3, width / bars.length)
  const timeIndex = (time: string | null | undefined) => {
    if (!time) return -1
    return bars.findIndex((bar) => bar.time === time || bar.time.slice(0, 16) === time.slice(0, 16))
  }
  const selected = chart.zones.find((zone) => zone.id === zoneId)
  return (
    <div className="zone-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Candles and supply demand zones">
        {chart.zones.map((zone) => {
          const start = Math.max(0, timeIndex(zone.confirmed_time))
          const endIndex = timeIndex(zone.invalidated_time)
          const end = endIndex >= 0 ? endIndex : bars.length - 1
          return (
            <rect
              key={zone.id}
              x={xAt(start)}
              y={yAt(zone.upper)}
              width={Math.max(8, xAt(end) - xAt(start))}
              height={Math.max(2, yAt(zone.lower) - yAt(zone.upper))}
              className={`zone-band ${zone.side} ${zone.status} ${zone.id === zoneId ? 'selected' : ''}`}
              onClick={() => setZoneId(zone.id)}
            />
          )
        })}
        {bars.map((bar, index) => {
          const up = bar.close >= bar.open
          const bodyTop = yAt(Math.max(bar.open, bar.close))
          const bodyHeight = Math.max(1, Math.abs(yAt(bar.open) - yAt(bar.close)))
          return (
            <g key={bar.time} className={up ? 'candle up' : 'candle down'}>
              <line x1={xAt(index)} x2={xAt(index)} y1={yAt(bar.high)} y2={yAt(bar.low)} />
              <rect x={xAt(index) - slot * 0.28} y={bodyTop} width={slot * 0.56} height={bodyHeight} />
            </g>
          )
        })}
        {chart.trades.map((trade) => {
          const entry = timeIndex(trade.entry_time)
          const exit = timeIndex(trade.exit_time)
          if (entry < 0) return null
          return (
            <g key={`${trade.entry_time}-${trade.direction}`}>
              <circle cx={xAt(entry)} cy={yAt(trade.entry_price)} r="3.5" className="trade-entry" />
              {exit >= 0 && <circle cx={xAt(exit)} cy={yAt(trade.exit_price)} r="3.5" className="trade-exit" />}
              <line x1={xAt(entry) - 6} x2={xAt(entry) + 6} y1={yAt(trade.initial_stop)} y2={yAt(trade.initial_stop)} className="trade-stop" />
              <line x1={xAt(entry) - 6} x2={xAt(entry) + 6} y1={yAt(trade.target)} y2={yAt(trade.target)} className="trade-target" />
            </g>
          )
        })}
      </svg>
      {selected && <p className="zone-note"><b>{selected.side} · {selected.status}</b> {selected.reason}</p>}
    </div>
  )
}

export function EmaResultDetails({ result: source }: { result: object }) {
  const result = source as FieldMap
  const breakdown = (result.rejection_breakdown ?? {}) as Record<string, number>
  const charts = (result.charts ?? {}) as Record<string, Chart>
  const symbols = Object.keys(charts)
  const [symbol, setSymbol] = useState(symbols[0] ?? '')
  const activeSymbol = charts[symbol] ? symbol : symbols[0]
  const samples = Array.isArray(result.rejection_samples) ? result.rejection_samples as Array<FieldMap> : []
  const sensitivity = (result.sensitivity ?? {}) as FieldMap
  const periods = (result.period_splits ?? {}) as FieldMap
  const slices = (periods.slices ?? {}) as FieldMap
  const groups: Array<[string, FieldMap | undefined]> = [
    ['Long / short', result.direction_breakdown as FieldMap],
    ['Entry time', result.entry_time_breakdown as FieldMap],
    ['Zone touches', result.zone_touch_breakdown as FieldMap],
    ['Exit mix', result.exit_mix as FieldMap],
  ]
  return (
    <div className="ema-results">
      {result.short_sale_warning ? <div className="warning-strip"><AlertTriangle size={15} />{String(result.short_sale_warning)}</div> : null}
      <div className="lab-subheading"><h3>Rejection reasons <span>{String(result.signals_rejected ?? 0)} counted · {String(result.zone_count ?? 0)} zones</span></h3></div>
      <div className="result-kpis">
        {REASONS.map((reason) => <div className="result-kpi" key={reason}><small>{reason}</small><b>{breakdown[reason] ?? 0}</b></div>)}
        <div className="result-kpi"><small>Average realized R</small><b>{result.average_realized_r == null ? '—' : String(result.average_realized_r)}</b></div>
        <div className="result-kpi"><small>Average holding bars</small><b>{String(result.average_holding_bars ?? '—')}</b></div>
      </div>
      {groups.map(([title, rows]) => rows ? <div key={title} className="ema-breakdown"><h4>{title}</h4><div className="result-kpis">{Object.entries(rows).map(([key, value]) => <div className="result-kpi" key={key}><small>{key}</small><b>{metricText(value)}</b></div>)}</div></div> : null)}
      {Object.keys(slices).length > 0 && <div className="ema-breakdown"><h4>Date thirds</h4><p className="ema-note">{String(periods.note ?? '')}</p><div className="result-kpis">{Object.entries(slices).map(([key, value]) => <div className="result-kpi" key={key}><small>{key}</small><b>{metricText(value)}</b></div>)}</div></div>}
      {sensitivity.volume_filter_off ? <div className="ema-breakdown"><h4>Same-candle replays</h4><p className="ema-note">{String(sensitivity.note ?? '')}</p><div className="result-kpis"><div className="result-kpi"><small>Volume filter off</small><b>{metricText(sensitivity.volume_filter_off)}</b></div><div className="result-kpi"><small>EMA slope off</small><b>{metricText(sensitivity.ema_slope_filter_off)}</b></div></div></div> : null}
      <div className="lab-subheading"><h3>Zone chart</h3>{symbols.length > 0 && <select value={activeSymbol} onChange={(event) => setSymbol(event.target.value)}>{symbols.map((item) => <option key={item}>{item}</option>)}</select>}</div>
      {activeSymbol && charts[activeSymbol] ? <ZoneChart chart={charts[activeSymbol]} /> : <div className="chart-empty">A chart is cached only for symbols that traded, up to eight names.</div>}
      {samples.length > 0 && <details className="rejection-sample"><summary>Sample rejections ({samples.length})</summary><ul>{samples.slice(0, 40).map((item, index) => <li key={`${item.symbol}-${index}`}><b>{String(item.symbol)}</b> {String(item.reason)} <small>{String(item.detail ?? '')}</small></li>)}</ul></details>}
    </div>
  )
}
