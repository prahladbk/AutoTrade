import { useEffect, useState } from 'react'
import {
  Activity, ArrowDownRight, ArrowUpRight, BarChart3, Check, Clock3,
  CircleAlert, CircleHelp, Database, ScanLine, ShieldCheck, TrendingUp,
} from 'lucide-react'
import './App.css'
import StrategyLab from './StrategyLab'

type Signal = {
  symbol: string
  company: string
  instrument_key: string
  close: number
  sma6: number
  sma30: number
  candle_time: string
  change_percent: number
}

type Scan = {
  id: number
  started_at: string
  scanned_count: number
  signal_count: number
  error_count: number
}

type Trade = {
  id: number
  timestamp: string
  symbol: string
  side: string
  quantity: number
  price: number
  status: string
  order_id: string | null
}

const API = '/api'
const formatTime = (value?: string) => value
  ? new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
  : 'Not scanned yet'
const money = (value: number) => `₹${value.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

async function readJson(response: Response) {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail ?? `API request failed (${response.status}).`)
  return payload
}

function App() {
  const [signals, setSignals] = useState<Signal[]>([])
  const [scans, setScans] = useState<Scan[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [latest, setLatest] = useState<Scan | null>(null)
  const [apiOnline, setApiOnline] = useState(false)
  const [tokenReady, setTokenReady] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [message, setMessage] = useState('')
  async function loadDashboard() {
    try {
      const [statusResponse, scanResponse, tradeResponse] = await Promise.all([
        fetch(`${API}/status`), fetch(`${API}/scans`), fetch(`${API}/trades`),
      ])
      const [status, scanData, tradeData] = await Promise.all([
        readJson(statusResponse), readJson(scanResponse), readJson(tradeResponse),
      ])
      setApiOnline(true)
      setTokenReady(status.token_configured)
      setScans(scanData.scans)
      setLatest(scanData.latest)
      setSignals(scanData.signals)
      setTrades(tradeData)
    } catch (error) {
      setApiOnline(false)
      setMessage(error instanceof Error ? error.message : 'Start the FastAPI service to connect the dashboard.')
    }
  }

  useEffect(() => { void loadDashboard() }, [])

  async function runScan() {
    setScanning(true)
    setMessage('')
    try {
      const response = await fetch(`${API}/scan`, { method: 'POST' })
      const result = await readJson(response)
      setSignals(result.signals)
      setLatest(result.scan)
      await loadDashboard()
      setMessage(`Scan complete. ${result.scan.signal_count} crossover signals found.`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Scan failed.')
    } finally {
      setScanning(false)
    }
  }

  return (
    <div className="app-shell">
      <aside className="rail">
        <a className="brand" href="#overview" aria-label="Crossline dashboard">
          <span className="brand-mark"><Activity size={19} strokeWidth={2.2} /></span>
          <span>crossline<span className="brand-period">.</span></span>
        </a>
        <div className="rail-label">WORKSPACE</div>
        <a className="rail-link active" href="#overview"><BarChart3 size={17} /> Overview</a>
        <a className="rail-link" href="#signals"><ScanLine size={17} /> Scan results</a>
        <a className="rail-link" href="#strategy-lab"><TrendingUp size={17} /> Strategy Lab</a>
        <a className="rail-link" href="#trades"><TrendingUp size={17} /> Trade log</a>
        <div className="rail-bottom">
          <div className="market-chip"><span className="market-dot" /> NSE MARKET<br /><b>4H · CLOSED CANDLES</b></div>
          <div className="rail-foot">STRATEGY DESK <span>01 / 01</span></div>
        </div>
      </aside>

      <main className="main-content" id="overview">
        <header className="topbar">
          <div className="breadcrumb">MARKET MONITOR <span>/</span> NIFTY 100</div>
          <div className="topbar-right">
            <div className={`connection ${apiOnline ? 'is-online' : ''}`}><span />{apiOnline ? 'API CONNECTED' : 'API OFFLINE'}</div>
            <div className="avatar">P</div>
          </div>
        </header>

        <section className="page-intro">
          <div>
            <div className="eyebrow"><span className="eyebrow-line" /> SYSTEMATIC SCAN <span className="eyebrow-code">CL-04</span></div>
            <h1>Market overview<span>.</span></h1>
            <p className="intro-copy">A focused read on Nifty 100 price action, updated when you run a scan.</p>
          </div>
          <button className="scan-button" type="button" onClick={runScan} disabled={scanning || !apiOnline || !tokenReady}>
            <ScanLine size={16} /> {scanning ? 'Scanning universe…' : 'Run market scan'}
          </button>
        </section>

        {message && <div className={`notice ${message.startsWith('Scan complete') ? 'notice-success' : ''}`} role="status">
          {message.startsWith('Scan complete') ? <Check size={16} /> : <CircleAlert size={16} />}{message}
        </div>}
        {!tokenReady && apiOnline && <div className="notice notice-warn"><CircleHelp size={16} />Configure <code>UPSTOX_ACCESS_TOKEN</code> in the backend environment to scan market data.</div>}

        <section className="metrics" aria-label="Market scan summary">
          <article className="metric metric-universe">
            <div className="metric-label">UNIVERSE <Database size={15} /></div>
            <div className="metric-value">100<span> stocks</span></div>
            <div className="metric-foot">NIFTY 100 CONSTITUENTS</div>
          </article>
          <article className="metric metric-signals">
            <div className="metric-label">ACTIVE SIGNALS <TrendingUp size={15} /></div>
            <div className="metric-value">{latest?.signal_count ?? '—'}<span> matches</span></div>
            <div className="metric-foot">6 SMA CROSSED ABOVE 30 SMA</div>
          </article>
          <article className="metric metric-scanned">
            <div className="metric-label">LAST SCAN <Clock3 size={15} /></div>
            <div className="metric-value metric-date">{latest ? formatTime(latest.started_at) : 'Awaiting scan'}</div>
            <div className="metric-foot">{latest ? `${latest.scanned_count} SYMBOLS CHECKED` : 'NO SCAN RUN IN THIS SESSION'}</div>
          </article>
          <article className="metric metric-trades">
            <div className="metric-label">TRADE LOG <ShieldCheck size={15} /></div>
            <div className="metric-value">{trades.length}<span> logged</span></div>
            <div className="metric-foot">LOCAL ACTIVITY RECORD</div>
          </article>
        </section>

        <section className="content-grid">
          <article className="panel signals-panel" id="signals">
            <div className="panel-heading">
              <div><div className="section-index">01 / SCAN OUTPUT</div><h2>Crossover signals</h2></div>
              <div className="panel-meta"><span className="live-indicator" /> {signals.length} RESULTS</div>
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>INSTRUMENT</th><th>LAST PRICE</th><th>SMA 6</th><th>SMA 30</th><th>4H CHANGE</th><th>SETUP</th></tr></thead>
                <tbody>
                  {signals.map((signal) => <tr key={signal.symbol}>
                    <td><div className="instrument-name">{signal.symbol}</div><div className="instrument-company">{signal.company}</div></td>
                    <td className="number-cell">{money(signal.close)}</td><td className="number-cell">{money(signal.sma6)}</td><td className="number-cell">{money(signal.sma30)}</td>
                    <td className={signal.change_percent >= 0 ? 'positive number-cell' : 'negative number-cell'}>{signal.change_percent >= 0 ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}{Math.abs(signal.change_percent).toFixed(2)}%</td>
                    <td><span className="setup-tag"><span /> BULLISH CROSS</span></td>
                  </tr>)}
                  {signals.length === 0 && <tr><td className="empty-cell" colSpan={6}>
                    <div className="empty-mark"><ScanLine size={21} /></div>
                    <strong>{latest ? 'No crossover signals found' : 'Waiting for your first scan'}</strong>
                    <span>{latest ? 'No Nifty 100 stock met the 4H crossover rule on the latest completed candle.' : 'Run a scan to check the Nifty 100 against the 6 / 30 SMA rule.'}</span>
                  </td></tr>}
                </tbody>
              </table>
            </div>
            <div className="panel-foot"><span>RULE: SMA 6 CROSSES ABOVE SMA 30 AND CLOSE &gt; SMA 30</span><span>4 HOUR · COMPLETED BAR</span></div>
          </article>

          <aside className="side-stack">
            <article className="panel rule-panel">
              <div className="section-index">02 / STRATEGY</div><h2>Signal conditions</h2>
              <div className="rule-step"><span className="rule-number">01</span><span>Instrument belongs to the Nifty 100 universe</span><Check size={15} /></div>
              <div className="rule-step"><span className="rule-number">02</span><span>6-period SMA crosses above 30-period SMA</span><Check size={15} /></div>
              <div className="rule-step"><span className="rule-number">03</span><span>Latest completed 4-hour close is above SMA 30</span><Check size={15} /></div>
              <div className="rule-note"><CircleHelp size={15} /><span>Signals are informational; this dashboard does not place orders.</span></div>
            </article>

            <article className="panel history-panel">
              <div className="history-heading"><div><div className="section-index">03 / RECENT RUNS</div><h2>Scan history</h2></div><span className="history-count">{scans.length.toString().padStart(2, '0')}</span></div>
              {scans.length === 0 ? <div className="history-empty">Your completed scans will appear here.</div> : scans.slice(0, 5).map((scan, index) => (
                <div className="history-row" key={scan.id}><span className={`history-symbol ${index === 0 ? 'history-symbol-current' : ''}`}>{String(scan.signal_count).padStart(2, '0')}</span><span className="history-label"><b>{scan.signal_count} signals</b><small>{formatTime(scan.started_at)}</small></span><span className="history-universe">{scan.scanned_count}<small>CHECKED</small></span></div>
              ))}
            </article>
          </aside>
        </section>

        <section className="panel trades-panel" id="trades">
          <div className="panel-heading"><div><div className="section-index">04 / EXECUTION RECORD</div><h2>Trade log</h2></div><span className="log-label"><ShieldCheck size={14} /> MANUAL / SCRIPT LOG</span></div>
          <div className="trade-grid trade-grid-head"><span>TIME</span><span>INSTRUMENT</span><span>SIDE</span><span>QUANTITY</span><span>PRICE</span><span>STATUS</span></div>
          {trades.length === 0 ? <div className="trade-empty">No trades recorded. The current sandbox smoke test places and cancels its test order; it does not create an execution history.</div> : trades.map((trade) => (
            <div className="trade-grid trade-row" key={trade.id}><span>{formatTime(trade.timestamp)}</span><b>{trade.symbol}</b><span className={trade.side === 'BUY' ? 'positive' : 'negative'}>{trade.side}</span><span>{trade.quantity}</span><span>{money(trade.price)}</span><span className="trade-status">{trade.status}</span></div>
          ))}
        </section>

        <StrategyLab tokenReady={apiOnline && tokenReady} />

        <footer className="page-footer"><span>CROSSLINE <i>·</i> NIFTY 100 MARKET MONITOR</span><span><span className={apiOnline ? 'footer-dot connected' : 'footer-dot'} />{apiOnline ? 'FASTAPI CONNECTION ACTIVE' : 'WAITING FOR API'}</span></footer>
      </main>
    </div>
  )
}

export default App
