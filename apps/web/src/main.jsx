import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import packageMetadata from '../package.json'
import { createRoot } from 'react-dom/client'
import {
  Activity, AlertTriangle, ArrowDownLeft, ArrowLeftRight, ArrowRight, ArrowUpRight, BadgeCheck, BookOpen, GraduationCap, Landmark, Gauge, PanelsTopLeft,
  Banknote, Bell, CalendarClock, Check, CheckCircle2, ChevronDown, CircleHelp,
  Clock3, CreditCard, FileClock, Fingerprint, Home, LockKeyhole, Menu,
  MessageCircle, MoreHorizontal, Plus, RefreshCw, Search, Send, Shield,
  ShieldCheck, Sparkles, TrendingUp, Wallet, X, UserRound, SlidersHorizontal,
  Palette, Type, LayoutGrid, Rows3, Monitor, Globe2, Save, RotateCcw, Users,
} from 'lucide-react'
import { CopilotKitProvider, CopilotChatToolCallsView, useComponent, useRenderTool } from '@copilotkit/react-core/v2'
import { useAgent, useCopilotKit, useAgentContext, useFrontendTool } from '@copilotkit/react-core/v2/headless'
import { capabilitySchema, presentationSchema, presentationContract, executeFrontendCapability, validateApplicationContext, resolveWorkspace, ACADEMY_TOPICS, ASSISTANT_CAPABILITIES, ASSISTANT_CAPABILITY_CATALOGUE_VERSION, AGENT_TURN_CONTRACT_VERSION, WORKSPACE_CATALOGUE, WORKSPACE_CATALOGUE_VERSION } from './contracts.js'
import { advanceManualNavigationEpoch, executeNavigationOnce, isNavigationEpochCurrent } from './navigation-actions.js'
import { transferDraftReplayDecision } from './transfer-action-guard.js'
import { auditDetailEntries } from './audit-details.js'
import { getMessageRevealState, saveMessageRevealState } from './message-stream-state.js'
import './styles.css'
import { HERO_ARTWORK_OPTIONS, DEMO_DISCLOSURE_TITLE, DEMO_DISCLOSURE_TEXT } from './demo-presentation.js'

const fmtMoney = (minor, currency = 'EUR') => new Intl.NumberFormat('en-IE', {
  style: 'currency', currency, maximumFractionDigits: 2,
}).format((Number(minor) || 0) / 100)
const fmtDate = (value) => value ? new Intl.DateTimeFormat('en-GB', {
  day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
}).format(new Date(value)) : '—'
const fmtDateOnly = (value) => value ? new Intl.DateTimeFormat('en-GB', {
  day: '2-digit', month: 'short', year: 'numeric',
}).format(new Date(`${String(value).slice(0, 10)}T12:00:00`)) : '—'
const TERMINAL_TRANSFER_STATES = ['SETTLED', 'FAILED', 'REQUIRES_RECONCILIATION']
const CHAT_SCROLL_STATE = new Map()
const CHAT_SCROLL_STATE_KEY = 'oraiflow_agent'

const DEFAULT_UI_SETTINGS = Object.freeze({
  fontSize: 'standard',
  density: 'comfortable',
  theme: 'navy',
  defaultWorkspace: 'Overview',
  reducedMotion: false,
})
function readUiSettings() {
  try {
    const saved = JSON.parse(window.localStorage.getItem('oraiflow.uiSettings') || '{}')
    return {
      fontSize: ['small', 'standard', 'large'].includes(saved.fontSize) ? saved.fontSize : DEFAULT_UI_SETTINGS.fontSize,
      density: ['comfortable', 'compact'].includes(saved.density) ? saved.density : DEFAULT_UI_SETTINGS.density,
      theme: ['black', 'navy', 'modern-white', 'silver'].includes(saved.theme) ? saved.theme : DEFAULT_UI_SETTINGS.theme,
      defaultWorkspace: ['Overview', 'Accounts', 'Payments', 'Loans', 'Cards', 'Contacts', 'Activity', 'Academy', 'AI assistant', 'Controls & audit'].includes(saved.defaultWorkspace) ? saved.defaultWorkspace : DEFAULT_UI_SETTINGS.defaultWorkspace,
      reducedMotion: saved.reducedMotion === true,
    }
  } catch { return { ...DEFAULT_UI_SETTINGS } }
}

// Workspace names and aliases are supplied by the canonical OCCF registry.
const makeIdempotencyKey = () => `ui-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) },
  })
  const raw = await response.text()
  let data = {}
  try { data = raw ? JSON.parse(raw) : {} } catch { data = { detail: raw || 'The server returned an unreadable response.' } }
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`)
  return data
}

function Mark({ compact = false }) {
  return <div className={`brand ${compact ? 'brand-compact' : ''}`}>
    <div className="brand-mark"><span /><span /><span /></div>
    {!compact && <div className="brand-copy"><strong>ORAI<span>flow</span></strong><small>INTELLIGENT FINANCE</small></div>}
  </div>
}

function WorkspaceFooter({ providerLabel }) {
  return <footer className="page-footer">
    <div><Mark compact /><span>ORAIflow <b>·</b> Synthetic demo framework</span></div>
    <div className="footer-right">
      <span className="footer-provider"><span className="online-dot" /> {providerLabel}</span>
      <span>Build {packageMetadata.version}</span>
      <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API docs <ArrowUpRight size={12} /></a>
      <a href="http://localhost:8233" target="_blank" rel="noreferrer">Temporal <ArrowUpRight size={12} /></a>
      <a href="http://localhost:8101/docs" target="_blank" rel="noreferrer">Agent API <ArrowUpRight size={12} /></a>
      <a href="https://docs.copilotkit.ai/inspector" target="_blank" rel="noreferrer">CopilotKit Inspector guide <ArrowUpRight size={12} /></a>
    </div>
  </footer>
}

function NavItem({ icon: Icon, label, active, onClick, badge }) {
  return <button className={`nav-item ${active ? 'active' : ''}`} onClick={onClick}>
    <Icon size={18} strokeWidth={1.8} /><span>{label}</span>{badge && <i>{badge}</i>}
  </button>
}

function StatCard({ label, value, caption, icon: Icon, tone = 'cyan', onClick, actionLabel = 'Open account activity' }) {
  const Tag = onClick ? 'button' : 'div'
  return <Tag className={`stat-card glass-card${onClick ? ' is-clickable' : ''}`} onClick={onClick} type={onClick ? 'button' : undefined} aria-label={onClick ? `${label}: ${value}. ${actionLabel}` : undefined}>
    <div className="stat-top"><span>{label}</span><div className={`stat-icon ${tone}`}><Icon size={18} /></div></div>
    <div className="stat-value">{value}</div>
    <div className="stat-caption"><span className="caption-dot" />{caption}</div>
  </Tag>
}

function Amount({ amount, currency, signed = false }) {
  const value = Number(amount) || 0
  return <span className={`amount ${value < 0 ? 'negative' : 'positive'}`}>{signed && (value < 0 ? '−' : '+')}{fmtMoney(Math.abs(value), currency)}</span>
}

function MessageBody({ content, messageId, hideTables = false, progressive = false, onProgress }) {
  const sourceText = String(content || '')
  const cachedState = progressive ? getMessageRevealState(messageId, sourceText) : null
  const initialText = progressive && sourceText.length > 64
    ? (cachedState?.visibleText ?? sourceText.slice(0, 24))
    : sourceText
  const [visibleText, setVisibleText] = useState(initialText)
  const visibleTextRef = useRef(initialText)
  const latestTextRef = useRef(sourceText)
  const previousSourceRef = useRef(sourceText)
  const timerRef = useRef(null)
  const animatingRef = useRef(Boolean(progressive && initialText.length < sourceText.length && !cachedState?.completed))

  useEffect(() => {
    latestTextRef.current = sourceText
    const previousSource = previousSourceRef.current
    previousSourceRef.current = sourceText

    const stopTimer = () => {
      if (timerRef.current) window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    const setVisible = (value, completed = value.length >= latestTextRef.current.length) => {
      visibleTextRef.current = value
      setVisibleText(value)
      if (progressive) {
        saveMessageRevealState(messageId, {
          sourceText: latestTextRef.current,
          visibleText: value,
          completed,
        })
      }
    }
    const tick = () => {
      const target = latestTextRef.current
      const current = visibleTextRef.current
      if (!target.startsWith(current)) {
        animatingRef.current = false
        timerRef.current = null
        setVisible(target, true)
        return
      }
      const next = target.slice(0, Math.min(target.length, current.length + 14))
      const completed = next.length >= latestTextRef.current.length
      setVisible(next, completed)
      if (!completed) {
        timerRef.current = window.setTimeout(tick, 35)
      } else {
        timerRef.current = null
        animatingRef.current = false
      }
    }
    const startAnimation = () => {
      animatingRef.current = true
      saveMessageRevealState(messageId, {
        sourceText: latestTextRef.current,
        visibleText: visibleTextRef.current,
        completed: false,
      })
      if (!timerRef.current) timerRef.current = window.setTimeout(tick, 35)
    }

    if (!progressive) {
      stopTimer()
      animatingRef.current = false
      setVisible(sourceText, true)
      return
    }
    if (animatingRef.current) {
      startAnimation()
      return
    }
    if (sourceText.startsWith(previousSource)) {
      const appendedLength = sourceText.length - previousSource.length
      // Native AG-UI deltas remain immediate. Only a completed response that
      // arrives as a large batch uses the presentation pacing fallback.
      if (appendedLength > 56 && sourceText.length > 64 && visibleTextRef.current.length < sourceText.length) {
        startAnimation()
      } else {
        setVisible(sourceText, true)
      }
    } else if (sourceText.length > 64) {
      setVisible(sourceText.slice(0, 24), false)
      startAnimation()
    } else {
      setVisible(sourceText, true)
    }
  }, [sourceText, messageId, progressive])

  useEffect(() => () => {
    if (timerRef.current) window.clearTimeout(timerRef.current)
  }, [])

  const previousVisibleTextRef = useRef(visibleText)
  useEffect(() => {
    const changed = previousVisibleTextRef.current !== visibleText
    previousVisibleTextRef.current = visibleText
    if (changed && progressive && typeof onProgress === 'function') onProgress()
  }, [visibleText, progressive, onProgress])

  const lines = visibleText.split('\n')
  return <div className="message-body">{lines.map((line, index) => {
    const trimmed = line.trim()
    if (!trimmed) return <div className="message-spacer" key={index} />
    if (/^\|?\s*:?-{2,}/.test(trimmed)) return null
    if (trimmed.includes('|')) {
      if (hideTables) return null
      const cells = trimmed.replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim())
      return <div className="message-table-row" key={index}>{cells.map((cell, cellIndex) => <span key={cellIndex}>{cell.replace(/\*\*/g, '')}</span>)}</div>
    }
    const bullet = trimmed.startsWith('•') || trimmed.startsWith('- ')
    const parts = trimmed.replace(/^(?:•|- )\s*/, '').split(/(\*\*.*?\*\*)/g)
    return <div className={bullet ? 'message-bullet' : ''} key={index}>
      {parts.map((part, i) => part.startsWith('**') && part.endsWith('**')
        ? <strong key={i}>{part.slice(2, -2)}</strong> : <React.Fragment key={i}>{part}</React.Fragment>)}
    </div>
  })}</div>
}

function TransferModal({ accounts, initialDraft, onClose, onAccepted }) {
  const compatibleInitialAccounts = (accounts || []).filter((account) => account.currency === (initialDraft?.currency || 'EUR'))
  const suppliedInitialSource = compatibleInitialAccounts.find((account) => account.account_id === initialDraft?.from_account_id)
  const initialSourceAccountId = suppliedInitialSource?.account_id || (compatibleInitialAccounts.length === 1 ? compatibleInitialAccounts[0].account_id : '')
  const [from, setFrom] = useState(initialSourceAccountId)
  const [beneficiary, setBeneficiary] = useState(initialDraft?.beneficiary || '')
  const [beneficiaryContactId, setBeneficiaryContactId] = useState(initialDraft?.beneficiary_contact_id || '')
  const [beneficiaryContact, setBeneficiaryContact] = useState(initialDraft?.beneficiary_contact || null)
  const [amount, setAmount] = useState(initialDraft?.amount || '')
  const [currency, setCurrency] = useState(initialDraft?.currency || 'EUR')
  const [preview, setPreview] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [scaCode, setScaCode] = useState('123456')
  const [idempotencyKey, setIdempotencyKey] = useState(makeIdempotencyKey)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)

  function changeDraft(callback) {
    callback()
    setPreview(null)
    setConfirmed(false)
    setIdempotencyKey(makeIdempotencyKey())
  }

  async function getPreview(event) {
    event?.preventDefault()
    setError(''); setBusy(true)
    try {
      const data = await api('/api/transfers/preview', {
        method: 'POST', body: JSON.stringify({ from_account_id: from, beneficiary, amount, currency, ...(beneficiaryContactId ? { beneficiary_contact_id: beneficiaryContactId } : {}) }),
      })
      setPreview(data)
      setIdempotencyKey(makeIdempotencyKey())
      setConfirmed(false)
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  async function confirmTransfer() {
    if (!preview || !confirmed || busy) return
    setError(''); setBusy(true)
    try {
      const data = await api('/api/transfers', {
        method: 'POST',
        body: JSON.stringify({
          preview_id: preview.preview_id,
          payload_digest: preview.payload_digest,
          idempotency_key: idempotencyKey,
          customer_confirmed: true,
          sca_code: scaCode,
        }),
      })
      let latest = data
      // The command is durably accepted before Temporal starts. Follow the
      // workflow briefly so the local demo can show the settled ledger result.
      for (let attempt = 0; attempt < 25 && !TERMINAL_TRANSFER_STATES.includes(latest.status); attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 800))
        try { latest = await api(`/api/transfers/${data.command_id}`) } catch { /* keep the last confirmed state */ }
      }
      setResult(latest)
      onAccepted(data.command_id)
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <div className="transfer-modal">
      <div className="modal-head"><div><div className="eyebrow">PAYMENTS · LOCAL DEMO</div><h2>New transfer</h2><p>Prepare a payment, inspect the preview, then confirm it.</p></div><button className="icon-button" onClick={onClose} aria-label="Close"><X size={19} /></button></div>
      {!result ? <>
        <form onSubmit={getPreview} className="transfer-form">
          <label>From account<select required value={from} onChange={(e) => changeDraft(() => setFrom(e.target.value))}><option value="" disabled>Select a source account</option>{accounts.filter((account) => account.currency === currency).map((account) => <option key={account.account_id} value={account.account_id}>{account.account_name} · {account.masked_number}</option>)}</select></label>
          <label>Beneficiary<input value={beneficiary} onChange={(e) => changeDraft(() => {
            setBeneficiary(e.target.value)
            if (beneficiaryContact && e.target.value.trim().toLocaleLowerCase() !== beneficiaryContact.full_name.toLocaleLowerCase()) {
              setBeneficiaryContactId('')
              setBeneficiaryContact(null)
            }
          })} required minLength="2" /></label>
          {beneficiaryContact && beneficiaryContactId && <div className="contact-verification-note"><BadgeCheck size={15}/><span><strong>Saved contact matched</strong><small>{beneficiaryContact.bank_name} · {beneficiaryContact.account_reference_masked}</small></span></div>}
          <label>Amount<input value={amount} onChange={(e) => changeDraft(() => setAmount(e.target.value))} inputMode="decimal" required /></label>
          <label>Currency<select value={currency} onChange={(e) => changeDraft(() => { const nextCurrency = e.target.value; const compatible = accounts.filter((account) => account.currency === nextCurrency); setCurrency(nextCurrency); setFrom(compatible.length === 1 ? compatible[0].account_id : '') })}><option value="EUR">EUR</option><option value="USD">USD</option><option value="GBP">GBP</option></select></label>
          <div className="field-note"><Shield size={14} /> Local demo policy limit: €1,000 per transfer · No external payment rail.</div>
          <button className="primary-button full" type="submit" disabled={busy}><ArrowRight size={17} /> {busy ? 'Checking policy…' : 'Review transfer'}</button>
        </form>
        {preview && <div className="preview-card">
          <div className="preview-title"><CheckCircle2 size={17} /> Transfer preview <span>POLICY CHECK PASSED</span></div>
          <div className="preview-amount">{preview.amount} {preview.currency}</div>
          <div className="preview-line"><span>To</span><strong>{preview.beneficiary}</strong></div>
          {preview.beneficiary_contact_id && <div className="preview-line"><span>Saved contact</span><strong>{preview.beneficiary_bank_name || 'Verified contact'} · {preview.beneficiary_account_reference_masked || 'Masked account'}</strong></div>}
          <div className="preview-line"><span>From</span><strong>{preview.from_account_name} · {preview.from_masked_number}</strong></div>
          <div className="preview-line"><span>Fee</span><strong>€0.00</strong></div>
          <div className="preview-line total"><span>Total</span><strong>{preview.amount} {preview.currency}</strong></div>
          <div className="preview-integrity-note"><ShieldCheck size={13} /> Preview locked · expires {new Date(preview.expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · approval bound to this exact payment</div>
          <label className="confirm-check"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /> I confirm this exact beneficiary and amount.</label>
          <label className="sca-field">Demo SCA code<input value={scaCode} onChange={(e) => setScaCode(e.target.value)} /></label>
          <div className="demo-code"><Fingerprint size={15} /> Demo challenge code: <strong>123456</strong> <em>Not real SCA</em></div>
          <button className="primary-button full" onClick={confirmTransfer} disabled={busy || !confirmed}><LockKeyhole size={16} /> {busy ? 'Submitting…' : 'Confirm & submit to Temporal'}</button>
        </div>}
      </> : <div className="result-state">
        <div className="result-icon">{result.status === 'SETTLED' ? <CheckCircle2 size={27} /> : result.status === 'FAILED' ? <CircleHelp size={27} /> : result.status === 'REQUIRES_RECONCILIATION' ? <AlertTriangle size={27} /> : <RefreshCw size={27} />}</div>
        <div className="eyebrow">DURABLE EXECUTION · {result.status || 'PENDING'}</div>
        <h3>{result.status === 'SETTLED' ? 'Transfer settled' : result.status === 'FAILED' ? 'Transfer failed' : result.status === 'REQUIRES_RECONCILIATION' ? 'Outcome needs reconciliation' : 'Transfer accepted'}</h3>
        <p>Command <code>{result.command_id.slice(0, 12)}…</code> is recorded in PostgreSQL. {result.status === 'SETTLED' ? `The synthetic ledger booked the debit${result.ledger_transaction_id ? ` as ${result.ledger_transaction_id}` : ''}; reconciliation: ${result.reconciliation_status || 'pending'}. ORAI can now retrieve it through MCP.` : result.status === 'FAILED' ? (result.error || 'The local policy or ledger rejected this transfer.') : result.status === 'REQUIRES_RECONCILIATION' ? `The execution outcome could not be confirmed automatically. Do not submit another payment. Check the command and ledger evidence before retrying. ${result.error || ''}` : 'It is durably queued for the Temporal worker. Refresh Payments to see its latest state.'}</p>
        <div className="workflow-track"><span className="done"><Check size={13} /></span><i /><span className={result.status === 'FAILED' ? 'failed' : result.status === 'REQUIRES_RECONCILIATION' ? 'needs-review' : ['EXECUTING', 'SETTLED'].includes(result.status) ? 'done' : 'running'}>{result.status === 'FAILED' ? <CircleHelp size={13} /> : result.status === 'REQUIRES_RECONCILIATION' ? <AlertTriangle size={13} /> : result.status === 'SETTLED' ? <Check size={13} /> : <RefreshCw size={13} />}</span><i /><span className={result.status === 'SETTLED' ? 'done' : ''}><Check size={13} /></span></div>
        <div className="workflow-labels"><span>Recorded</span><span>Executing</span><span>Settled</span></div>
        <button className="secondary-button full" onClick={onClose}>Close</button>
      </div>}
      {error && <div className="form-error"><CircleHelp size={15} /> {error}</div>}
      <div className="modal-disclaimer"><LockKeyhole size={13} /> Synthetic local environment. Demo SCA is not a regulated authentication mechanism.</div>
    </div>
  </div>
}

function PageIntro({ eyebrow, title, description, action, workspaceId }) {
  const workspace = workspaceId ? WORKSPACE_CATALOGUE?.[workspaceId] : null
  const pageDescription = workspace
    ? `${workspace.purpose} ${workspace.value}`
    : description
  return <section className="workspace-intro" data-workspace-catalogue-version={WORKSPACE_CATALOGUE_VERSION}>
    <div><div className="eyebrow"><span className="eyebrow-line" /> {eyebrow}</div><h1>{title}</h1><p>{pageDescription}</p></div>
    {action}
  </section>
}

function AccountSummaryCard({ title, accounts = [] }) {
  const rows = Array.isArray(accounts) ? accounts : []
  const totals = rows.reduce((result, account) => {
    const currency = account.currency || 'EUR'
    result[currency] = (result[currency] || 0) + (Number(account.balance_minor) || 0)
    return result
  }, {})
  const totalEntries = Object.entries(totals)
  return <div className="agent-genui-card">
    <div className="agent-genui-heading"><Wallet size={16}/><strong>{title || 'Account summary'}</strong><span>{rows.length} account{rows.length === 1 ? '' : 's'}</span></div>
    {rows.map((account) => <div className="agent-account-row" key={account.account_id || account.account_name}>
      <div><strong>{account.account_name || 'Account'}</strong><small>{account.account_type || 'Bank account'} · {account.status || '—'}</small></div>
      <div className="agent-account-balance">{fmtMoney(account.balance_minor, account.currency || 'EUR')}</div>
    </div>)}
    {!rows.length && <div className="agent-genui-empty">No account data was returned.</div>}
    {rows.length > 0 && <div className="agent-account-total"><div><strong>Total available balance</strong><small>Combined only within the same currency</small></div><div>{totalEntries.map(([currency, amount]) => <strong key={currency}>{fmtMoney(amount, currency)}</strong>)}</div></div>}
    <div className="agent-genui-foot"><LockKeyhole size={12}/> Retrieved through ORAIflow's read-only banking capability</div>
  </div>
}

function TransactionListCard({ title, transactions = [] }) {
  const rows = Array.isArray(transactions) ? transactions : []
  return <div className="agent-genui-card">
    <div className="agent-genui-heading"><Activity size={16}/><strong>{title || 'Recent activity'}</strong><span>{rows.length} records</span></div>
    {rows.map((transaction) => <div className="agent-transaction-row" key={transaction.transaction_id}>
      <div><strong>{transaction.description || 'Transaction'}</strong><small>{transaction.account_name || 'Account'} · {transaction.status || '—'}</small></div>
      <div className={`agent-transaction-amount ${Number(transaction.amount_minor) >= 0 ? 'positive' : 'negative'}`}>{Number(transaction.amount_minor) < 0 ? '−' : '+'}{fmtMoney(Math.abs(Number(transaction.amount_minor) || 0), transaction.currency || 'EUR')}</div>
    </div>)}
    {!rows.length && <div className="agent-genui-empty">No transactions found.</div>}
    <div className="agent-genui-foot"><LockKeyhole size={12}/> Retrieved through ORAIflow's read-only banking capability</div>
  </div>
}

function TransactionDetailCard({ title, transactions = [] }) {
  const transaction = Array.isArray(transactions) ? transactions[0] : null
  if (!transaction) return <div className="agent-genui-card"><div className="agent-genui-heading"><Activity size={16}/><strong>{title || 'Latest transaction'}</strong></div><div className="agent-genui-empty">No transaction was found.</div></div>
  return <div className="agent-genui-card transaction-detail-card">
    <div className="agent-genui-heading"><Activity size={16}/><strong>{title || 'Latest transaction'}</strong><span>{transaction.status || '—'}</span></div>
    <div className="transaction-detail-main"><div><strong>{transaction.description || 'Transaction'}</strong><small>{transaction.category || 'Transaction'} · {transaction.account_name || 'Account'}</small></div><div className={`agent-transaction-amount ${Number(transaction.amount_minor) >= 0 ? 'positive' : 'negative'}`}>{Number(transaction.amount_minor) < 0 ? '−' : '+'}{fmtMoney(Math.abs(Number(transaction.amount_minor) || 0), transaction.currency || 'EUR')}</div></div>
    <div className="transaction-detail-meta"><span><small>Date</small><strong>{fmtDate(transaction.occurred_at)}</strong></span><span><small>Transaction reference</small><strong>{transaction.transaction_id}</strong></span><span><small>Status</small><strong>{transaction.status || '—'}</strong></span></div>
    <div className="agent-genui-foot"><LockKeyhole size={12}/> Exactly one latest transaction · verified through read-only MCP</div>
  </div>
}

function AgentToolActivity({ title, detail, status, result }) {
  // Completed tool calls are already reflected in the assistant's final answer
  // or in a Generative UI component. Keep only live execution feedback visible.
  if (status === 'complete') return null
  const label = status === 'executing' ? 'Running' : 'Preparing'
  return <div className="agent-tool-activity live" role="status" aria-live="polite">
    <span className="agent-tool-indicator"/><div><strong>{title}</strong><small>{detail || result || label}</small></div><em>{label}</em>
  </div>
}

function hasGenerativeAnswer(message) {
  const calls = message?.toolCalls || message?.tool_calls || []
  return calls.some((call) => {
    const name = call?.name || call?.function?.name || call?.toolName || call?.tool_name || ''
    return ['render_account_summary', 'render_transaction_list', 'render_transaction_detail'].includes(name)
  })
}

function CopilotAssistantPanel({ tall = false, agent, isReady, copilotkit, suggestionRequest = null }) {
  const [draft, setDraft] = useState('')
  const [sendError, setSendError] = useState('')
  const [queuedMessages, setQueuedMessages] = useState([])
  const messagesContainerRef = useRef(null)
  const savedChatView = CHAT_SCROLL_STATE.get(CHAT_SCROLL_STATE_KEY)
  const shouldAutoFollowRef = useRef(savedChatView?.shouldAutoFollow ?? true)
  const handleChatScroll = useCallback(() => {
    const container = messagesContainerRef.current
    if (!container) return
    const distanceFromBottom = container.scrollHeight - container.clientHeight - container.scrollTop
    shouldAutoFollowRef.current = distanceFromBottom < 96
    CHAT_SCROLL_STATE.set(CHAT_SCROLL_STATE_KEY, {
      scrollTop: container.scrollTop,
      shouldAutoFollow: shouldAutoFollowRef.current,
    })
  }, [])
  const scrollChatToLatest = useCallback(() => {
    const container = messagesContainerRef.current
    if (container && shouldAutoFollowRef.current) {
      container.scrollTo({ top: container.scrollHeight, behavior: 'auto' })
      CHAT_SCROLL_STATE.set(CHAT_SCROLL_STATE_KEY, {
        scrollTop: container.scrollTop,
        shouldAutoFollow: true,
      })
    }
  }, [])
  useLayoutEffect(() => {
    const container = messagesContainerRef.current
    if (!container) return
    const saved = CHAT_SCROLL_STATE.get(CHAT_SCROLL_STATE_KEY)
    if (saved) {
      shouldAutoFollowRef.current = saved.shouldAutoFollow
      container.scrollTop = saved.scrollTop
    } else {
      shouldAutoFollowRef.current = true
      container.scrollTop = container.scrollHeight
    }
  }, [])
  const runTimingRef = useRef({ startedAt: 0, active: false, firstAssistantAt: 0 })
  const messageQueueRef = useRef([])
  const queuePumpRef = useRef(false)
  const maxQueuedMessages = 5
  const sentSuggestionRef = useRef(null)
  const allAgentMessages = agent?.messages || []
  const toolViewMessages = allAgentMessages.filter((message) => {
    if (message.role !== 'tool') return true
    const resultText = typeof message.content === 'string'
      ? message.content.trim()
      : Array.isArray(message.content)
        ? message.content.map((part) => typeof part === 'string' ? part : part?.text || '').join('').trim()
        : ''
    return !/^(?:none|null|undefined)$/i.test(resultText)
  })
  const messages = allAgentMessages.filter((message, index) => {
    if (!['user', 'assistant'].includes(message.role)) return false

    // Tool-call-only assistant messages are protocol events, not chat bubbles.
    // CopilotKit can emit several of these during a single frontend-tool cycle;
    // rendering them as ordinary messages leaves empty avatar/bubble artifacts.
    const rawContent = typeof message.content === 'string'
      ? message.content.trim()
      : Array.isArray(message.content)
        ? message.content.map((part) => typeof part === 'string' ? part : part?.text || '').join('').trim()
        : ''
    if (message.role === 'assistant' && !rawContent && !hasGenerativeAnswer(message)) return false

    // A Generative UI component is the answer for this turn. Suppress the
    // model's follow-up prose for that same turn so balances/transactions
    // are not presented a second time as bullets or Markdown.
    if (message.role === 'assistant') {
      let turnStart = index - 1
      while (turnStart >= 0 && allAgentMessages[turnStart].role !== 'user') turnStart -= 1
      let turnEnd = index + 1
      while (turnEnd < allAgentMessages.length && allAgentMessages[turnEnd].role !== 'user') turnEnd += 1
      const turnHasGenerativeAnswer = allAgentMessages.slice(turnStart + 1, turnEnd).some(hasGenerativeAnswer)
      if (turnHasGenerativeAnswer && !hasGenerativeAnswer(message)) return false
    }

    const contentText = typeof message.content === 'string'
      ? message.content.trim()
      : Array.isArray(message.content)
        ? message.content.map((part) => typeof part === 'string' ? part : part?.text || '').join('').trim()
        : ''
    // A browser/tool adapter can occasionally serialize an absent Python
    // return value as the literal string "None". It is protocol noise, not a
    // customer answer, and must not appear as a chat bubble.
    if (message.role === 'assistant' && /^(?:none|null|undefined)$/i.test(contentText)) return false
    const toolCalls = message.toolCalls || message.tool_calls || []
    const hasRenderableCalls = Array.isArray(toolCalls) && toolCalls.some((call) =>
      Boolean(call?.name || call?.function?.name || call?.toolName || call?.tool_name)
    )
    return contentText.length > 0 || hasRenderableCalls
  })

  useEffect(() => {
    // Follow newly arriving text only while the reader is already near the
    // bottom. Scrolling up to inspect history must never be overridden by the
    // stream or by tool/status updates.
    if (shouldAutoFollowRef.current) scrollChatToLatest()
  }, [messages.length, queuedMessages.length, agent?.messages?.[agent.messages.length - 1]?.content, agent?.isRunning, scrollChatToLatest])

  // Measure the time between assistant output and the actual AG-UI run finish.
  // This logs timings only; no prompt, account data, or message content is recorded.
  useEffect(() => {
    const metrics = runTimingRef.current
    const now = performance.now()
    if (agent?.isRunning && !metrics.active) {
      metrics.active = true
      if (!metrics.startedAt) metrics.startedAt = now
      console.info('[ORAI chat timing] AG-UI run active', {
        submit_to_active_ms: Math.round(now - metrics.startedAt),
      })
    } else if (!agent?.isRunning && metrics.active) {
      console.info('[ORAI chat timing] composer ready', {
        total_run_ms: Math.round(now - metrics.startedAt),
        assistant_output_to_ready_ms: metrics.firstAssistantAt
          ? Math.round(now - metrics.firstAssistantAt)
          : null,
      })
      metrics.active = false
      metrics.startedAt = 0
      metrics.firstAssistantAt = 0
    }
  }, [agent?.isRunning])

  const latestUserIndex = allAgentMessages.map((message) => message.role).lastIndexOf('user')
  const latestTurnMessages = latestUserIndex >= 0 ? allAgentMessages.slice(latestUserIndex + 1) : []
  const latestAssistantMessage = [...latestTurnMessages].reverse().find((message) => {
    if (message.role !== 'assistant') return false
    const content = typeof message.content === 'string' ? message.content.trim() : ''
    const calls = message.toolCalls || message.tool_calls || []
    return Boolean(content || (Array.isArray(calls) && calls.length))
  })
  useEffect(() => {
    const metrics = runTimingRef.current
    if (!agent?.isRunning || !metrics.startedAt || metrics.firstAssistantAt || !latestAssistantMessage) return
    metrics.firstAssistantAt = performance.now()
    console.info('[ORAI chat timing] first assistant output', {
      submit_to_output_ms: Math.round(metrics.firstAssistantAt - metrics.startedAt),
    })
  }, [latestAssistantMessage?.id, latestAssistantMessage?.content, agent?.isRunning])

  // A streamed assistant response already tells the user that ORAI is working.
  // Do not show a second three-dot bubble underneath it. Show the indicator only
  // while a run is active and no assistant text has arrived for the latest turn.
  const latestTurnHasAssistantText = latestTurnMessages.some((message) => {
    if (message.role !== 'assistant') return false
    if (typeof message.content === 'string') return Boolean(message.content.trim())
    if (Array.isArray(message.content)) return message.content.some((part) => typeof part === 'string' ? Boolean(part.trim()) : Boolean(part?.text?.trim()))
    return false
  })
  const showThinkingIndicator = Boolean(agent?.isRunning && !latestTurnHasAssistantText)

  async function runQueuedMessage(item) {
    runTimingRef.current = { startedAt: performance.now(), active: false, firstAssistantAt: 0 }
    console.info('[ORAI chat timing] message submitted', { queue_id: item.id })
    try {
      agent.addMessage({ id: item.id, role: 'user', content: item.text })
      const runStartedAt = runTimingRef.current.startedAt
      await copilotkit.runAgent({ agent })
      console.info('[ORAI chat timing] runAgent promise resolved', {
        queue_id: item.id,
        elapsed_ms: Math.round(performance.now() - runStartedAt),
        agent_still_running: Boolean(agent.isRunning),
      })
    } catch (error) {
      console.error('[ORAI assistant] CopilotKit run failed:', error)
      setSendError(error?.message || 'ORAI could not complete this request. Check the Copilot Runtime and Agent Runtime logs.')
    }
  }

  async function pumpMessageQueue() {
    if (queuePumpRef.current || !isReady || !agent || !copilotkit || agent.isRunning) return
    queuePumpRef.current = true
    try {
      while (messageQueueRef.current.length && !agent.isRunning) {
        const next = messageQueueRef.current.shift()
        setQueuedMessages([...messageQueueRef.current])
        await runQueuedMessage(next)
        // Some CopilotKit versions settle the returned promise just before the
        // reactive isRunning state flips. Wait for that state transition before
        // starting the next queued message to guarantee sequential execution.
        if (agent.isRunning) break
      }
    } finally {
      queuePumpRef.current = false
      if (messageQueueRef.current.length && !agent.isRunning) {
        void Promise.resolve().then(() => pumpMessageQueue())
      }
    }
  }

  function sendMessage(value) {
    const text = String(value || '').trim()
    if (!text || !agent || !isReady) return
    if (messageQueueRef.current.length >= maxQueuedMessages) {
      setSendError(`ORAI's queue is full. Please wait until one of the ${maxQueuedMessages} queued messages has started.`)
      return
    }
    shouldAutoFollowRef.current = true
    const currentContainer = messagesContainerRef.current
    if (currentContainer) {
      CHAT_SCROLL_STATE.set(CHAT_SCROLL_STATE_KEY, {
        scrollTop: currentContainer.scrollHeight,
        shouldAutoFollow: true,
      })
    }
    const item = {
      id: `oraiflow-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      text,
    }
    messageQueueRef.current.push(item)
    setQueuedMessages([...messageQueueRef.current])
    setDraft('')
    setSendError('')
    if (agent.isRunning || queuePumpRef.current) {
      console.info('[ORAI chat timing] message queued', { queue_id: item.id, queued_count: messageQueueRef.current.length })
    }
    void pumpMessageQueue()
  }

  useEffect(() => {
    const requestId = suggestionRequest?.id
    const requestText = suggestionRequest?.text
    if (!requestId || !requestText || !isReady || sentSuggestionRef.current === requestId) return
    sentSuggestionRef.current = requestId
    sendMessage(requestText)
  }, [suggestionRequest?.id, suggestionRequest?.text, isReady])

  useEffect(() => {
    if (!agent?.isRunning) void pumpMessageQueue()
  }, [agent?.isRunning, isReady, copilotkit])

  const messageText = (content) => {
    if (typeof content === 'string') return content
    if (Array.isArray(content)) return content.map((part) => typeof part === 'string' ? part : part?.text || '').join('')
    return ''
  }

  return <section className={`chat-card panel-card copilot-assistant-card ${tall ? 'chat-card-tall copilot-page-card' : ''}`}>
    <div className="panel-head chat-head">
      <div className="agent-avatar"><Sparkles size={18}/></div>
      <div className="panel-title"><strong>ORAI assistant</strong><small><span className={`online-dot ${agent?.isRunning ? 'working' : ''}`}/> {agent?.isRunning ? 'Responding · CopilotKit + AG-UI' : isReady ? 'Connected · CopilotKit + AG-UI' : 'Connecting to ORAI…'}</small></div>
      <div className="head-pill"><span className="mini-lock"><LockKeyhole size={12}/></span> Controlled AI</div>
    </div>
    <div ref={messagesContainerRef} onScroll={handleChatScroll} className="chat-messages" role="log" aria-live="polite" aria-label="ORAI conversation">
      {messages.length === 0 && <div className="chat-row assistant">
        <div className="chat-avatar"><Sparkles size={14}/></div>
        <div className="bubble-wrap">
          <div className="bubble"><MessageBody messageId="oraiflow-welcome" content="Hello. I’m your ORAIflow financial co-pilot. I can securely look up your accounts and recent activity, navigate your workspace, and help prepare an editable transfer draft. What would you like to explore?" progressive onProgress={scrollChatToLatest}/></div>
        </div>
      </div>}
      {messages.map((message, index) => <div className={`chat-row ${message.role}`} key={message.id || `${message.role}-${index}`}>
        {message.role === 'assistant' && <div className="chat-avatar"><Sparkles size={14}/></div>}
        <div className="bubble-wrap"><div className="bubble">
          {!hasGenerativeAnswer(message) && <MessageBody messageId={message.id || `${message.role}-${index}`} content={messageText(message.content)} progressive={message.role === 'assistant'} onProgress={message.role === 'assistant' ? scrollChatToLatest : undefined} />}
          {message.role === 'assistant' && <CopilotChatToolCallsView message={message} messages={toolViewMessages}/>}
        </div></div>
      </div>)}
      {queuedMessages.map((item) => <div className="chat-row user queued" key={item.id}>
        <div className="bubble-wrap"><div className="bubble"><MessageBody content={item.text}/><small className="chat-queued-label">Queued · sent in order</small></div></div>
      </div>)}
      {showThinkingIndicator && <div className="chat-row assistant chat-thinking-indicator"><div className="chat-avatar"><Sparkles size={14}/></div><div className="bubble typing"><i/><i/><i/></div></div>}
    </div>
    {messages.length === 0 && <div className="suggestions">
      <button type="button" onClick={() => sendMessage('Show my account balances')}>My balances</button>
      <button type="button" onClick={() => sendMessage('What are my recent transactions?')}>Recent activity</button>
      <button type="button" onClick={() => sendMessage('What can you help me with?')}>What can you do?</button>
    </div>}
    {sendError && <div className="form-error">{sendError}</div>}
    <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); sendMessage(draft) }}>
      <input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={!isReady ? 'Connecting to ORAI…' : agent?.isRunning ? 'Draft your next message while ORAI finishes…' : 'Ask anything about your finances…'} disabled={!isReady} aria-busy={Boolean(agent?.isRunning)}/>
      <button className="send-button" type="submit" disabled={!isReady || !draft.trim() || queuedMessages.length >= maxQueuedMessages} aria-label={agent?.isRunning ? 'Queue message' : 'Send message'} title={agent?.isRunning ? 'Queue this message for the next turn' : 'Send message'}><Send size={17}/></button>
    </form>
    <div className={`chat-foot ${agent?.isRunning ? 'chat-foot-busy' : ''}`} aria-live="polite">
      {agent?.isRunning
        ? <><RefreshCw size={12}/> ORAI is responding. New messages are queued and sent in order{queuedMessages.length ? ` · ${queuedMessages.length} queued` : ''}.</>
        : queuedMessages.length
          ? <><RefreshCw size={12}/> Starting the next queued message · {queuedMessages.length} waiting.</>
          : <><LockKeyhole size={12}/> AI can read permitted data. Payments require separate review and approval.</>}
    </div>
  </section>
}

function BalanceTrend({ account, transactions, allAccounts }) {
  const [range, setRange] = useState(7)
  const selected = account ? [account] : allAccounts
  const values = useMemo(() => {
    const now = new Date()
    const perAccount = selected.map((acc) => {
      const rows = transactions.filter((t) => t.account_id === acc.account_id)
      const current = Number(acc.balance_minor) || 0
      return Array.from({ length: range }, (_, index) => {
        const offset = range - 1 - index
        const day = new Date(now); day.setHours(0, 0, 0, 0); day.setDate(day.getDate() - offset)
        const next = new Date(day); next.setDate(next.getDate() + 1)
        const later = rows.filter((t) => new Date(t.occurred_at) >= next).reduce((sum, t) => sum + Number(t.amount_minor || 0), 0)
        return current - later
      })
    })
    return Array.from({ length: range }, (_, i) => perAccount.reduce((sum, series) => sum + (series[i] || 0), 0))
  }, [selected, transactions, range])
  const currentBalance = account ? Number(account.balance_minor) : allAccounts.reduce((sum, a) => sum + Number(a.balance_minor || 0), 0)
  const width = 640, height = 170, pad = 14
  const min = Math.min(...values, 0), max = Math.max(...values, 1), span = Math.max(1, max - min)
  const coords = values.map((value, i) => [pad + i * (width - 2 * pad) / Math.max(1, values.length - 1), height - pad - ((value - min) / span) * (height - 2 * pad)])
  const line = coords.map(([x, y], i) => `${i ? 'L' : 'M'} ${x} ${y}`).join(' ')
  const area = `${line} L ${width-pad} ${height-pad} L ${pad} ${height-pad} Z`
  return <section className="panel-card balance-card">
    <div className="panel-head balance-head"><div><div className="eyebrow">BALANCE HISTORY</div><h3>{account?.account_name || 'All accounts'}</h3><small>Transaction-derived estimate · current ledger balance anchor</small></div><div className="range-switch">{[7, 14, 30].map((n) => <button key={n} className={range === n ? 'active' : ''} onClick={() => setRange(n)}>{n}D</button>)}</div></div>
    <div className="balance-total">{fmtMoney(currentBalance, account?.currency || 'EUR')}<span>Current balance</span></div>
    <div className={`balance-chart ${values.some((value) => value !== values[0]) ? '' : 'balance-chart-empty'}`}>
      {values.some((value) => value !== values[0]) ? <>
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Estimated balance trend"><defs><linearGradient id="balanceFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="#58d5df" stopOpacity=".32"/><stop offset="100%" stopColor="#58d5df" stopOpacity="0"/></linearGradient></defs>{[.25,.5,.75].map((v) => <line key={v} x1={pad} x2={width-pad} y1={height*v} y2={height*v} stroke="rgba(150,190,220,.12)" strokeDasharray="4 6"/>)}<path d={area} fill="url(#balanceFill)"/><path d={line} fill="none" stroke="#67dce5" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"/>{coords.length > 0 && <circle cx={coords.at(-1)[0]} cy={coords.at(-1)[1]} r="5" fill="#8bf0ee" stroke="#0c2035" strokeWidth="3"/>}</svg>
        <div className="chart-axis"><span>{range} days ago</span><span>Today</span></div>
      </> : <div className="balance-chart-empty-state"><TrendingUp size={18}/><strong>No balance movement in this period</strong><span>There are no booked transactions to plot for the selected range.</span></div>}
    </div>
    <div className="chart-note"><span className="online-dot"/> Based on booked transactions; unavailable history is not fabricated.</div>
  </section>
}

function financialHealthLabel(status) {
  return ({
    LOW_LEVERAGE: 'Excellent',
    MODERATE_LEVERAGE: 'Good',
    ELEVATED_LEVERAGE: 'Needs attention',
    HIGH_LEVERAGE: 'Warning',
    LIABILITIES_MATCH_ASSETS: 'Warning',
    LIABILITIES_EXCEED_ASSETS: 'Warning',
    INSUFFICIENT_DATA: 'Not enough recorded data',
  })[status] || 'Financial health unavailable'
}

function OverviewPage({ overview, loans = [], financialHealth, transactions, config, dateLabel, onNewTransfer, onNavigate, onRefresh, selectedAccountId, onSelectAccount, agent, isReady, copilotkit, heroBackground, heroBannerVisible }) {
  const accounts = overview?.accounts || []
  const total = overview?.total_balance?.amount_minor ?? 0
  const everydayAccount = accounts.find((account) => account.account_id === 'acc-001')
  const savingsAccount = accounts.find((account) => account.account_id === 'acc-002')
  const primaryLoan = (loans || []).find((loan) => loan && loan.demo_only !== false) || (loans || [])[0] || null
  const nextLoanInstallment = primaryLoan?.installments?.[0] || null
  const preferredCurrency = overview?.customer?.preferred_currency || 'EUR'
  const healthPosition = (financialHealth?.positions || []).find((position) => position.currency === preferredCurrency)
    || (financialHealth?.positions || []).find((position) => position.currency === 'EUR')
    || (financialHealth?.positions || [])[0]
  const loanToEquity = Number.isFinite(healthPosition?.loan_to_equity_bps) ? `${(healthPosition.loan_to_equity_bps / 100).toFixed(1)}%` : 'Not available'
  const debtToAssets = Number.isFinite(healthPosition?.debt_to_assets_bps) ? `${(healthPosition.debt_to_assets_bps / 100).toFixed(1)}%` : 'Not available'
  const providerLabel = config?.llm_provider === 'demo' ? 'DEMO MODE' : `${config?.llm_provider?.toUpperCase() || 'LLM'} · ${config?.llm_model || ''}`
  const healthTone = healthPosition?.status === 'LOW_LEVERAGE' ? 'low'
    : ['MODERATE_LEVERAGE'].includes(healthPosition?.status) ? 'moderate'
      : ['ELEVATED_LEVERAGE', 'HIGH_LEVERAGE', 'LIABILITIES_MATCH_ASSETS', 'LIABILITIES_EXCEED_ASSETS'].includes(healthPosition?.status) ? 'high' : 'unknown'
  return <>
    <section className="welcome-row"><div><div className="eyebrow"><span className="eyebrow-line" /> {dateLabel}</div><h1>Good morning, {overview?.customer?.display_name?.split(' ')[0] || 'there'} <span>✦</span></h1><p>Your financial picture, connected and in control.</p></div><button className="primary-button" onClick={onNewTransfer}><Plus size={17} /> New transfer</button></section>
    {heroBannerVisible && (
      <section className="hero-banner" aria-label="ORAIflow intelligence and recorded financial health">
        <img className="hero-background-image" src={(HERO_ARTWORK_OPTIONS.find(option => option.id === heroBackground) || HERO_ARTWORK_OPTIONS[0]).asset} alt="" aria-hidden="true" />
        <div className="hero-copy"><div className="hero-kicker"><Sparkles size={14} /> INTELLIGENCE, WITH CONTROL</div><h2>Money moves at the<br /><em>speed of thought.</em></h2><p>Ask naturally. See clearly. Stay in control of every decision.</p><button className="hero-link" onClick={() => onNavigate('AI assistant')}>Meet your AI assistant <ArrowRight size={16} /></button></div>
        <aside className={`hero-health-panel tone-${healthTone}`} aria-label="Recorded Financial Health indicator">
          <div className="hero-health-top"><span className="hero-health-icon"><ShieldCheck size={17}/></span><span>FINANCIAL HEALTH</span></div>
          {healthPosition ? <>
            <strong className="hero-health-status">{financialHealthLabel(healthPosition.status)}</strong>
            <div className="hero-health-primary"><span>Loans / recorded equity</span><b>{loanToEquity}</b></div>
            <div className="hero-health-secondary"><span>All liabilities / recorded deposits</span><b>{debtToAssets}</b></div>
            <small>Recorded ORAIflow data only · Single snapshot, no trend inferred · Not a credit score</small>
          </> : <>
            <strong className="hero-health-status">Not enough recorded data</strong>
            <p className="hero-health-empty">Connect or record the assets and liabilities needed for this snapshot.</p>
          </>}
          <button type="button" onClick={() => onNavigate('Academy')}>Understand your badge <ArrowRight size={14}/></button>
        </aside>
      </section>
    )}
    <section className="stats-grid">
      <StatCard label="Total balance" value={fmtMoney(total)} caption="Across your EUR accounts · View accounts" icon={Wallet} tone="cyan" onClick={() => onNavigate('Accounts')} />
      <StatCard label="Everyday account" value={fmtMoney(everydayAccount?.balance_minor || 0)} caption={`${everydayAccount?.masked_number || '•••• 4821'} · Current · View activity`} icon={Banknote} tone="violet" onClick={everydayAccount ? () => onSelectAccount(everydayAccount.account_id) : undefined} />
      <StatCard label="Savings" value={fmtMoney(savingsAccount?.balance_minor || 0)} caption={`${savingsAccount?.masked_number || '•••• 9034'} · Savings · View activity`} icon={TrendingUp} tone="amber" onClick={savingsAccount ? () => onSelectAccount(savingsAccount.account_id) : undefined} />
      <StatCard
        label="Loans"
        value={primaryLoan ? fmtMoney(primaryLoan.outstanding_principal_minor ?? primaryLoan.principal_minor ?? 0, primaryLoan.currency || 'EUR') : 'None recorded'}
        caption={primaryLoan?.demo_only
          ? `Illustrative demo liability · not a real credit agreement${nextLoanInstallment ? ` · sample instalment ${fmtMoney(nextLoanInstallment.payment_minor, primaryLoan.currency || 'EUR')} · illustrative date ${fmtDateOnly(nextLoanInstallment.due_date)}` : ''}`
          : primaryLoan && nextLoanInstallment
            ? `Outstanding · next instalment ${fmtMoney(nextLoanInstallment.payment_minor, primaryLoan.currency || 'EUR')} · due ${fmtDateOnly(nextLoanInstallment.due_date)}`
            : 'No loan is recorded in your connected ORAIflow data'}
        icon={CreditCard} tone="green" actionLabel="Open loan details"
        onClick={primaryLoan ? () => onNavigate('Loans') : undefined}
      />
    </section>
    <div className="dashboard-grid">
      <div className="dashboard-assistant dashboard-tile"><CopilotAssistantPanel agent={agent} isReady={isReady} copilotkit={copilotkit} /></div>
      <section className="panel-card accounts-card dashboard-tile">
        <div className="panel-head"><div><div className="eyebrow">YOUR MONEY</div><h3>Accounts</h3></div><button className="subtle-link" onClick={() => onNavigate('Accounts')}>View all <ArrowRight size={14} /></button></div>
        <div className="account-list">{accounts.slice(0, 3).map((account, index) => <button type="button" className={`account-row account-select ${selectedAccountId===account.account_id?"selected":""}`} key={account.account_id} onClick={() => onSelectAccount(account.account_id)} aria-label={`Open activity for ${account.account_name}`} aria-pressed={selectedAccountId===account.account_id}>
          <div className={`account-icon account-${index}`}><Wallet size={18} /></div><div className="account-copy"><strong>{account.account_name}</strong><small>{account.masked_number} · {account.account_type.toLowerCase()}</small></div><div className="account-balance"><strong>{fmtMoney(account.balance_minor, account.currency)}</strong><small>{account.status.toLowerCase()}</small></div>
        </button>)}</div>
        <div className="account-foot"><span><LockKeyhole size={13} /> Read through MCP capabilities</span><span className="tiny-pulse" /></div>
      </section>
      <div className="dashboard-tile dashboard-everyday"><BalanceTrend account={accounts.find(a => a.account_id === 'acc-001') || null} transactions={transactions} allAccounts={accounts}/></div>
      <section className="panel-card activity-card dashboard-tile">
        <div className="panel-head"><div><div className="eyebrow">THE LATEST</div><h3>Recent activity</h3></div><button className="icon-button small" onClick={onRefresh} title="Refresh activity"><RefreshCw size={15} /></button></div>
        <div className="activity-list">{transactions.slice(0, 4).map((txn) => <ActivityRow txn={txn} key={txn.transaction_id} />)}</div>
        <div className="activity-footer"><button onClick={() => onNavigate('Activity')}>All transactions <ArrowRight size={14} /></button></div>
      </section>
      <div className="dashboard-tile dashboard-savings"><BalanceTrend account={accounts.find(a => a.account_id === 'acc-002') || null} transactions={transactions} allAccounts={accounts}/></div>
    </div>
    <WorkspaceFooter providerLabel={providerLabel} />
  </>
}

function LoanDetailsPage({ loans = [] }) {
  return <>
    <PageIntro workspaceId="Loans" eyebrow="LOANS & CREDIT · LIABILITIES" title="Loans" description="Understand recorded loan liabilities, repayment structure and the cost of borrowing. Demo schedule dates and payment status are illustrative only." />
    <section className="workspace-card">
      <div className="workspace-card-head"><div><h2>Loans &amp; credit</h2><p>Loan liabilities are separate from deposit accounts and never count as available money or transfer sources.</p></div><span className="status-chip">{loans.length} recorded loan{loans.length === 1 ? '' : 's'}</span></div>
      {loans.length ? loans.map((loan) => <div className="loan-overview-card" key={loan.loan_id}>
        <div className="loan-overview-head"><div><strong>{loan.product_name || 'Personal loan'}</strong><small>{loan.demo_only ? 'Illustrative demo liability · not a real credit agreement' : 'Recorded loan liability'}</small></div><span className={`state-chip ${loan.demo_only ? '' : 'settled'}`}>{loan.demo_only ? 'ILLUSTRATIVE' : 'RECORDED'}</span></div>
        <div className="loan-metrics">
          <div><small>Original principal</small><strong>{fmtMoney(loan.principal_minor || 0, loan.currency)}</strong></div>
          <div><small>Outstanding principal</small><strong>{fmtMoney(loan.outstanding_principal_minor ?? loan.principal_minor ?? 0, loan.currency)}</strong></div>
          <div><small>APR assumption</small><strong>{(Number(loan.annual_interest_rate_bps || 0) / 100).toFixed(2)}%</strong></div>
          <div><small>Illustrative total interest</small><strong>{fmtMoney(loan.total_interest_minor || 0, loan.currency)}</strong></div>
        </div>
        <div className="loan-schedule-head"><strong>Repayment schedule</strong><span>{loan.term_months} months · illustrative</span></div>
        <div className="loan-schedule-list">{(loan.installments || []).slice(0, 6).map((row) => <div className="loan-schedule-row" key={`${loan.loan_id}-${row.installment_number}`}><span>Instalment {row.installment_number} · {fmtDateOnly(row.due_date)}</span><strong>{fmtMoney(row.payment_minor, loan.currency)}</strong><small>Principal {fmtMoney(row.principal_minor, loan.currency)} · interest {fmtMoney(row.interest_minor, loan.currency)}</small></div>)}</div>
        <div className="info-strip loan-demo-notice"><CalendarClock size={17}/><div><strong>Demo dates and payment status are illustrative</strong><p>Actual payment completion is not tracked. ORAIflow does not originate credit, mark repayments overdue, send payment reminders or submit loan repayments in this workspace.</p></div></div>
      </div>) : <div className="empty-state"><div><CreditCard size={24}/></div><h3>No loan information available</h3><p>Loan terms appear here only when an approved loan data source is available.</p></div>}
    </section>
  </>
}

function AcademyPage({ agent, isReady, copilotkit }) {
  const [suggestionRequest, setSuggestionRequest] = useState(null)
  const startTopic = (topic) => setSuggestionRequest({ id: `academy-${topic.topic_id}-${Date.now()}`, text: topic.starter_prompt })
  return <>
    <PageIntro workspaceId="Academy" eyebrow="ORAI LEARNING" title="Academy" description="Friendly, practical financial education—grounded in verified ORAIflow data when you ask about your own accounts or recorded leverage." />
    <div className="academy-layout">
      <section className="academy-chat-section">
        <div className="academy-section-heading"><div><span className="academy-heading-icon"><GraduationCap size={20}/></span><div><h2>Learn with ORAI</h2><p>Ask a question, explore a topic or bring your own financial situation.</p></div></div><span className="academy-personal-note">No judgement · clear explanations</span></div>
        <CopilotAssistantPanel tall agent={agent} isReady={isReady} copilotkit={copilotkit} suggestionRequest={suggestionRequest}/>
      </section>
      <section className="academy-topics-section">
        <div className="academy-section-heading"><div><span className="academy-heading-icon"><BookOpen size={20}/></span><div><h2>Explore learning paths</h2><p>Short topics with practical next steps.</p></div></div></div>
        <div className="academy-topic-list">{ACADEMY_TOPICS.map((topic) => <article className="academy-topic-card" key={topic.topic_id}>
          <div className="academy-topic-top"><span>{topic.level || 'Foundations'}</span><span className="academy-topic-marker"><BookOpen size={15}/></span></div>
          <h3>{topic.title}</h3><p>{topic.description}</p>
          <ul>{(topic.learning_points || []).slice(0, 3).map((point) => <li key={point}>{point}</li>)}</ul>
          <button type="button" className="academy-topic-start" onClick={() => startTopic(topic)}>Explore topic <ArrowRight size={14}/></button>
        </article>)}</div>
      </section>
    </div>
    <div className="info-strip workspace-disclosure"><ShieldCheck size={18}/><div><strong>Educational, not a credit decision</strong><p>Academy explains general financial concepts. Personalised ratios and account-specific facts come from read-only, authenticated capabilities. ORAIflow does not calculate a bureau credit score or guarantee any score outcome.</p></div></div>
  </>
}


const ASSISTANT_CAPABILITY_ICONS = Object.freeze({
  wallet: Wallet,
  activity: Activity,
  landmark: Landmark,
  gauge: Gauge,
  'arrow-left-right': ArrowLeftRight,
  'graduation-cap': GraduationCap,
  'panels-top-left': PanelsTopLeft,
})

function AssistantPage({ agent, isReady, copilotkit }) {
  const [suggestionRequest, setSuggestionRequest] = useState(null)
  const startCapability = (capability, example = null) => {
    const text = String(example || capability.starter_prompt || '').trim()
    if (!text) return
    setSuggestionRequest({
      id: `assistant-${capability.capability_id}-${Date.now()}`,
      text,
    })
  }
  return <>
    <PageIntro workspaceId="AI assistant" eyebrow="ORAI INTELLIGENCE" title="AI assistant" description="Your guide to ORAIflow: understand your finances, explore every workspace, learn financial concepts and prepare controlled banking actions." />
    <div className="assistant-layout">
      <section className="assistant-chat-section">
        <div className="academy-section-heading"><div><span className="academy-heading-icon"><Sparkles size={20}/></span><div><h2>Talk with ORAI</h2><p>Ask naturally; ORAI checks registered capabilities and verified data before answering.</p></div></div><span className="academy-personal-note">Evidence-led · controlled actions</span></div>
        <CopilotAssistantPanel tall agent={agent} isReady={isReady} copilotkit={copilotkit} suggestionRequest={suggestionRequest}/>
      </section>
      <section className="assistant-capabilities-section">
        <div className="academy-section-heading"><div><span className="academy-heading-icon"><PanelsTopLeft size={20}/></span><div><h2>Explore assistant capabilities</h2><p>Browse what ORAI can help with, or start from a ready-made example.</p></div></div></div>
        <div className="assistant-capability-list" data-catalogue-version={ASSISTANT_CAPABILITY_CATALOGUE_VERSION}>
          {ASSISTANT_CAPABILITIES.map((capability) => {
            const Icon = ASSISTANT_CAPABILITY_ICONS[capability.icon] || CircleHelp
            return <article className="assistant-capability-card" key={capability.capability_id}>
              <div className="assistant-capability-top"><span>{capability.category || 'ORAI assistance'}</span><span className="academy-topic-marker"><Icon size={15}/></span></div>
              <h3>{capability.title}</h3>
              <p>{capability.description}</p>
              {Array.isArray(capability.examples) && capability.examples.length > 0 && <div className="assistant-capability-examples">
                {capability.examples.slice(0, 2).map((example) => <button type="button" key={example} onClick={() => startCapability(capability, example)}>{example}<ArrowRight size={12}/></button>)}
              </div>}
              <button type="button" className="academy-topic-start" onClick={() => startCapability(capability)}>Try example <ArrowRight size={14}/></button>
            </article>
          })}
        </div>
      </section>
    </div>
    <div className="info-strip workspace-disclosure"><ShieldCheck size={18}/><div><strong>Helpful guidance, controlled execution</strong><p>ORAI explains registered ORAIflow features and uses authenticated, read-only capabilities for customer-specific financial facts. It can prepare editable transfer drafts; approval, strong customer authentication and payment execution remain separate Banking Kernel controls.</p></div></div>
  </>
}

function ActivityRow({ txn }) {
  const credit = Number(txn.amount_minor) >= 0
  return <div className="activity-row">
    <div className={`merchant-icon ${credit ? 'credit' : ''}`}>{credit ? <ArrowDownLeft size={17} /> : <ArrowUpRight size={17} />}</div>
    <div className="activity-copy"><strong>{txn.description}</strong><small>{String(txn.category || 'OTHER').toLowerCase()} · {fmtDate(txn.occurred_at)}</small></div>
    <div className={`activity-amount ${credit ? 'credit-text' : ''}`}>{credit ? '+' : '−'}{fmtMoney(Math.abs(Number(txn.amount_minor)), txn.currency)}</div>
  </div>
}

function AccountsPage({ accounts, onNewTransfer, selectedAccountId, onSelectAccount }) {
  return <>
    <PageIntro workspaceId="Accounts" eyebrow="YOUR MONEY" title="Accounts" description="Your demo accounts and live balances from the local synthetic ledger." action={<button className="primary-button" onClick={onNewTransfer}><Plus size={17} /> New transfer</button>} />
    <section className="workspace-card">
      <div className="workspace-card-head"><div><h2>All accounts</h2><p>Balances are read from PostgreSQL through the Banking Kernel read capabilities.</p></div><span className="status-chip"><span className="online-dot" /> 3 active</span></div>
      <div className="data-table-wrap"><table className="data-table"><thead><tr><th>Account</th><th>Type</th><th>Account number</th><th>Status</th><th className="align-right">Available balance</th></tr></thead><tbody>
        {accounts.map((account) => <tr key={account.account_id} className="account-table-row" role="button" tabIndex={0} aria-label={`Open activity for ${account.account_name}`} onClick={() => onSelectAccount(account.account_id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelectAccount(account.account_id) } }}><td><strong>{account.account_name}</strong><small>{account.account_id}</small></td><td>{account.account_type}</td><td>{account.masked_number}</td><td><span className="status-chip"><span className="online-dot" /> {account.status}</span></td><td className="align-right"><strong>{fmtMoney(account.balance_minor, account.currency)}</strong></td></tr>)}
      </tbody></table></div>
    </section>
  </>
}

function ContactsPage({ contacts = [], onPrepareTransfer }) {
  const [query, setQuery] = useState('')
  const filtered = contacts.filter((contact) => [contact.full_name, contact.email, contact.phone, contact.bank_name, contact.account_reference_masked]
    .some((value) => String(value || '').toLocaleLowerCase().includes(query.toLocaleLowerCase().trim())))
  return <>
    <PageIntro workspaceId="Contacts" eyebrow="SAVED BENEFICIARIES" title="Contacts" description="Customer-owned saved contacts used to resolve transfer recipients. All records in this workspace are synthetic demo data." />
    <section className="workspace-card">
      <div className="workspace-card-head"><div><h2>Saved contacts</h2><p>{contacts.length} contacts · duplicate names are intentionally included to test clarification.</p></div><span className="status-chip"><Users size={13}/> {contacts.length} contacts</span></div>
      <div className="contacts-toolbar"><label className="contacts-search"><Search size={16}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search by name, bank or account reference…" aria-label="Search contacts"/></label><span className="contacts-count">{filtered.length} shown</span></div>
      {filtered.length ? <div className="data-table-wrap"><table className="data-table contacts-table"><thead><tr><th>Contact</th><th>Bank / account</th><th>Contact details</th><th>Currency</th><th>Status</th><th></th></tr></thead><tbody>
        {filtered.map((contact) => <tr key={contact.contact_id}><td><strong>{contact.full_name}</strong><small>{contact.contact_id}</small></td><td><strong>{contact.bank_name}</strong><small>{contact.account_reference_masked}</small></td><td><strong>{contact.email || '—'}</strong><small>{contact.phone || '—'}</small></td><td>{contact.currency}</td><td><span className={`state-chip ${String(contact.status).toLowerCase()}`}>{contact.status}</span></td><td className="contacts-action-cell"><button type="button" className="secondary-button" onClick={() => onPrepareTransfer(contact)} disabled={contact.status !== 'ACTIVE'}><ArrowRight size={14}/> Prepare transfer</button></td></tr>)}
      </tbody></table></div> : <div className="empty-state"><div><Users size={24}/></div><h3>No matching contacts</h3><p>Try a different name, institution or account reference.</p></div>}
    </section>
    <div className="info-strip"><ShieldCheck size={18}/><div><strong>Recipient resolution is not payment authorization</strong><p>ORAI can resolve a saved contact and prepare an editable draft. The Banking Kernel independently rechecks contact ownership and status at preview and confirmation. No contact record represents a verified live bank account.</p></div></div>
  </>
}

function TablePagination({ page, pageSize, total, onPageChange, label }) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  if (total <= pageSize) return null
  const first = page * pageSize + 1
  const last = Math.min((page + 1) * pageSize, total)
  return <div className="table-pagination" aria-label={`${label} pagination`}>
    <span>Showing {first}–{last} of {total}</span>
    <div><button type="button" className="secondary-button" onClick={() => onPageChange(Math.max(0, page - 1))} disabled={page === 0}>Previous</button>
      <strong>Page {page + 1} of {pageCount}</strong>
      <button type="button" className="secondary-button" onClick={() => onPageChange(Math.min(pageCount - 1, page + 1))} disabled={page >= pageCount - 1}>Next</button></div>
  </div>
}

function PaymentsPage({ transfers, accounts, onNewTransfer, onRefresh, page, onPageChange }) {
  const pageSize = 10
  const pageTransfers = transfers.slice(page * pageSize, (page + 1) * pageSize)
  return <>
    <PageIntro workspaceId="Payments" eyebrow="PAYMENTS · LOCAL DEMO" title="Payments" description="Review submitted demo transfers and create a new payment. No external payment rail is connected." action={<button className="primary-button" onClick={onNewTransfer}><Plus size={17} /> New transfer</button>} />
    <section className="workspace-card">
      <div className="workspace-card-head"><div><h2>Transfer history</h2><p>Commands are recorded in PostgreSQL, dispatched by the worker, and settled by Temporal.</p></div><button className="secondary-button" onClick={onRefresh}><RefreshCw size={15} /> Refresh</button></div>
      {transfers.length ? <><div className="data-table-wrap"><table className="data-table"><thead><tr><th>Beneficiary</th><th>From account</th><th>Created</th><th>Status</th><th className="align-right">Amount</th></tr></thead><tbody>
        {pageTransfers.map((transfer) => <tr key={transfer.command_id}><td><strong>{transfer.beneficiary}</strong><small>{transfer.command_id.slice(0, 13)}…</small></td><td>{accounts.find((a) => a.account_id === transfer.from_account_id)?.account_name || transfer.from_account_id}</td><td>{fmtDate(transfer.created_at)}</td><td><span className={`state-chip ${String(transfer.status).toLowerCase()}`}>{transfer.status}</span>{transfer.error_message && <small className="table-error">{transfer.error_message}</small>}</td><td className="align-right"><strong>{fmtMoney(transfer.amount_minor, transfer.currency)}</strong></td></tr>)}
      </tbody></table></div><TablePagination page={page} pageSize={pageSize} total={transfers.length} onPageChange={onPageChange} label="Payments" /></> : <div className="empty-state"><div><ArrowLeftRight size={24} /></div><h3>No transfers yet</h3><p>Your confirmed local-demo transfers will appear here after they are accepted.</p><button className="primary-button" onClick={onNewTransfer}><Plus size={17} /> Create your first transfer</button></div>}
    </section>
    <div className="info-strip"><ShieldCheck size={18} /><div><strong>Controlled execution</strong><p>Preview and policy check → customer confirmation and demo SCA → PostgreSQL command + outbox → Temporal workflow → synthetic ledger settlement.</p></div></div>
  </>
}

function ActivityPage({ transactions, accounts, selectedAccountId, onSelectAccount, onRefresh, page, onPageChange }) {
  const pageSize = 10
  const visibleTransactions = transactions.filter(txn => !selectedAccountId || txn.account_id === selectedAccountId)
  const pageTransactions = visibleTransactions.slice(page * pageSize, (page + 1) * pageSize)
  const selectedName = accounts.find(a => a.account_id === selectedAccountId)?.account_name
  return <>
    <PageIntro workspaceId="Activity" eyebrow="ACCOUNT ACTIVITY" title="Activity" description="A searchable view of booked transactions across your demo accounts." action={<button className="secondary-button" onClick={onRefresh}><RefreshCw size={15} /> Refresh</button>} />
    <div className="activity-filter-bar"><label htmlFor="activity-account-filter">Account view</label><select id="activity-account-filter" value={selectedAccountId||"all"} onChange={e=>{onSelectAccount(e.target.value==="all"?null:e.target.value);onPageChange(0)}}><option value="all">All accounts</option>{accounts.map(a=><option key={a.account_id} value={a.account_id}>{a.account_name}</option>)}</select><span className="status-chip">{visibleTransactions.length} records</span></div>
    <BalanceTrend account={accounts.find(a => a.account_id === selectedAccountId) || null} transactions={transactions} allAccounts={accounts}/>
    <section className="workspace-card">
      <div className="workspace-card-head"><div><h2>{selectedName || "All account transactions"}</h2><p>Newly settled transfers are added to this history.</p></div></div>
      {visibleTransactions.length ? <><div className="data-table-wrap"><table className="data-table"><thead><tr><th>Description</th><th>Account</th><th>Category</th><th>Date</th><th>Status</th><th className="align-right">Amount</th></tr></thead><tbody>
        {pageTransactions.map((txn) => <tr key={txn.transaction_id}><td><strong>{txn.description}</strong><small>{txn.transaction_id}</small></td><td>{txn.account_name}</td><td>{txn.category}</td><td>{fmtDate(txn.occurred_at)}</td><td><span className="state-chip settled">{txn.status}</span></td><td className="align-right"><Amount amount={txn.amount_minor} currency={txn.currency} signed /></td></tr>)}
      </tbody></table></div><TablePagination page={page} pageSize={pageSize} total={visibleTransactions.length} onPageChange={onPageChange} label="Activity" /></> : <div className="empty-state"><p>No transactions found.</p></div>}
    </section>
  </>
}

function CardsPage({ displayName = 'Alex Morgan' }) {
  return <>
    <PageIntro workspaceId="Cards" eyebrow="CARDS" title="Cards" description="A visual preview of the demo debit card. Card servicing and card transactions are not connected in this release." />
    <section className="card-workspace">
      <div className="demo-bank-card"><div className="demo-card-top"><Mark compact /><span>DEBIT · DEMO</span></div><div className="demo-chip" /><div className="demo-card-number">•••• &nbsp; •••• &nbsp; •••• &nbsp; 4821</div><div className="demo-card-bottom"><div><small>CARDHOLDER</small><strong>{displayName.toUpperCase()}</strong></div><div><small>VALID THRU</small><strong>12/29</strong></div><div className="demo-card-brand">ORAI</div></div></div>
      <div className="workspace-card card-placeholder"><div className="placeholder-icon"><CreditCard size={22} /></div><h2>Card controls are a next milestone</h2><p>This card is illustrative only. Freeze/unfreeze, limits, replacement, and card transaction controls are intentionally not simulated in this release.</p><span className="status-chip">UI preview only</span></div>
    </section>
    <div className="info-strip workspace-disclosure"><LockKeyhole size={18} /><div><strong>No real card credentials</strong><p>The display uses synthetic masked data and is not linked to a payment network or physical card.</p></div></div>
  </>
}

function AuditDetails({ detail }) {
  const entries = auditDetailEntries(detail)
  if (!entries.length) return <span className="audit-detail-empty">—</span>
  return <dl className="audit-detail-list">
    {entries.map((entry) => <div className="audit-detail-pair" key={entry.key}>
      <dt title={entry.key}>{entry.label}</dt>
      <dd className={`audit-detail-value${entry.wrap ? ' is-wrappable' : ' is-compact'}`} title={entry.value}>{entry.value}</dd>
    </div>)}
  </dl>
}

function ControlsPage({ events, verification, total, page, onPageChange, onRefresh }) {
  const pageSize = 10
  const pageCount = Math.max(1, Math.ceil((total || 0) / pageSize))
  return <>
    <PageIntro workspaceId="Controls & audit" eyebrow="GOVERNANCE" title="Controls & audit" description="Inspect the local demo policy and the audit trail for accepted and settled commands." action={<button className="secondary-button" onClick={onRefresh}><RefreshCw size={15} /> Refresh</button>} />
    <div className="control-grid">
      <section className="workspace-card policy-card"><div className="policy-icon"><ShieldCheck size={20} /></div><div className="eyebrow">DEMO POLICY</div><h2>Transfer guardrails</h2><div className="policy-row"><span>Per-transfer limit</span><strong>€1,000.00</strong></div><div className="policy-row"><span>Approval</span><strong>Customer confirmation</strong></div><div className="policy-row"><span>Demo SCA code</span><strong>123456</strong></div><div className="policy-row"><span>Payment rail</span><strong>Local synthetic ledger</strong></div><p className="policy-disclaimer">This is a development policy and demo challenge, not regulated SCA or a production banking control.</p></section>
      <section className="workspace-card audit-summary"><div className="policy-icon"><FileClock size={20} /></div><div className="eyebrow">AUDIT TRAIL</div><h2>Recorded evidence</h2><div className="audit-metric">{total ?? events.length}</div><p>Recorded events for the demo customer.</p><span className="status-chip">PostgreSQL · audit_events</span><span className={`status-chip ${verification?.valid ? 'chain-valid' : 'chain-warning'}`}>{verification ? (verification.valid ? `Hash chain verified · ${verification.checked_events} events` : `Hash chain mismatch · event ${verification.failed_event_id}`) : 'Verifying hash chain…'}</span></section>
    </div>
    <section className="workspace-card audit-table-card"><div className="workspace-card-head"><div><h2>Recent audit events</h2><p>Command acceptance and ledger settlement are recorded separately.</p></div></div>
      {events.length ? <div className="data-table-wrap"><table className="data-table"><thead><tr><th>Event</th><th>Subject</th><th>Details</th><th>Hash evidence</th><th>Date</th></tr></thead><tbody>
        {events.map((event) => <tr key={event.event_id}><td><strong>{event.event_type}</strong><small>Actor: {event.actor_id}</small></td><td>{event.subject_id || '—'}</td><td className="audit-detail-cell"><AuditDetails detail={event.detail || {}} /></td><td className="audit-hash-cell"><small title={event.previous_hash || ''}>PREV · {(event.previous_hash || '—').slice(0, 16)}…</small><small title={event.event_hash || ''}>HASH · {(event.event_hash || '—').slice(0, 16)}…</small></td><td>{fmtDate(event.occurred_at)}</td></tr>)}
      </tbody></table></div> : <div className="empty-state"><p>No audit events recorded yet. Confirm a demo transfer to create the first entries.</p></div>}
      {total > pageSize && <div className="audit-pagination"><span>Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, total)} of {total} events</span><div><button type="button" className="secondary-button" onClick={() => onPageChange(Math.max(0, page - 1))} disabled={page === 0}>Previous</button><strong>Page {page + 1} of {pageCount}</strong><button type="button" className="secondary-button" onClick={() => onPageChange(Math.min(pageCount - 1, page + 1))} disabled={page >= pageCount - 1}>Next</button></div></div>}
    </section>
  </>
}

function ProfilePage({ profile, heroBackground, onHeroBackgroundChange, heroBannerVisible, onHeroBannerVisibilityChange, uiSettings, onUiSettingsChange, onProfileSaved, onNotice }) {
  const [activeTab, setActiveTab] = useState('personal')
  const [form, setForm] = useState({
    display_name: profile?.display_name || 'Alex Morgan',
    preferred_name: profile?.preferred_name || '',
    email: profile?.email || '',
    phone: profile?.phone || '',
    preferred_currency: (profile?.preferred_currency || 'EUR').trim(),
    language: profile?.language || 'en',
  })
  const [savedForm, setSavedForm] = useState(form)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const next = {
      display_name: profile?.display_name || 'Alex Morgan',
      preferred_name: profile?.preferred_name || '',
      email: profile?.email || '',
      phone: profile?.phone || '',
      preferred_currency: (profile?.preferred_currency || 'EUR').trim(),
      language: profile?.language || 'en',
    }
    setForm(next)
    setSavedForm(next)
  }, [profile])

  const update = (key, value) => setForm(current => ({ ...current, [key]: value }))
  const updateUi = (key, value) => onUiSettingsChange({ ...uiSettings, [key]: value })

  async function save(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const result = await api('/api/profile', { method: 'PUT', body: JSON.stringify(form) })
      const next = result.profile
      const profileValues = {
        display_name: next.display_name || '',
        preferred_name: next.preferred_name || '',
        email: next.email || '',
        phone: next.phone || '',
        preferred_currency: (next.preferred_currency || 'EUR').trim(),
        language: next.language || 'en',
      }
      setForm(profileValues)
      setSavedForm(profileValues)
      onProfileSaved(next)
      onNotice('Profile changes saved successfully.')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function cancelProfileEdits() {
    setForm({ ...savedForm })
    setError('')
  }

  const tabs = [
    { id: 'personal', label: 'Personal Information', icon: UserRound },
    { id: 'preferences', label: 'Preferences', icon: SlidersHorizontal },
    { id: 'customization', label: 'Customization', icon: Palette },
  ]

  const bannerOptions = HERO_ARTWORK_OPTIONS
  const themeOptions = [
    { id: 'black', title: 'Black', detail: 'Deep black · graphite', preview: 'theme-preview-black' },
    { id: 'navy', title: 'Navy', detail: 'ORAIflow signature', preview: 'theme-preview-navy' },
    { id: 'modern-white', title: 'Modern White', detail: 'Bright · crisp · minimal', preview: 'theme-preview-white' },
    { id: 'silver', title: 'Silver', detail: 'Soft grey · refined', preview: 'theme-preview-silver' },
  ]

  return <section className="workspace-card profile-settings-card">
    <div className="profile-settings-title">
      <div className="profile-title-icon"><UserRound size={34} strokeWidth={1.8} /></div>
      <div><h1>Profile</h1><p>Manage your personal details and make ORAIflow feel like your own.</p></div>
    </div>

    <div className="profile-tabs" role="tablist" aria-label="Profile settings sections">
      {tabs.map(({ id, label, icon: Icon }) => <button key={id} type="button" role="tab" aria-selected={activeTab === id} className={`profile-tab ${activeTab === id ? 'active' : ''}`} onClick={() => { setActiveTab(id); setError('') }}>
        <Icon size={19} strokeWidth={1.8} /><span>{label}</span>
      </button>)}
    </div>

    <form onSubmit={save}>
      <div className="profile-settings-content">
        {activeTab === 'personal' && <section className="settings-section" aria-labelledby="personal-section-title">
          <div className="settings-section-heading"><div><span className="settings-section-icon"><UserRound size={23} /></span><div><h2 id="personal-section-title">Personal Information</h2><p>Keep your customer contact details up to date.</p></div></div><span className="settings-section-kicker">CUSTOMER PROFILE</span></div>
          <div className="profile-form-grid">
            <label>Display name<input value={form.display_name} onChange={e => update('display_name', e.target.value)} required minLength="2" maxLength="80" autoComplete="name" /></label>
            <label><span className="profile-field-label">Preferred name <span className="optional-label">OPTIONAL</span></span><input value={form.preferred_name} onChange={e => update('preferred_name', e.target.value)} maxLength="80" placeholder="How should ORAI address you?" /></label>
            <label>Email address<input type="email" value={form.email} onChange={e => update('email', e.target.value)} maxLength="254" autoComplete="email" placeholder="you@example.com" /></label>
            <label>Phone number<input type="tel" value={form.phone} onChange={e => update('phone', e.target.value)} maxLength="40" autoComplete="tel" placeholder="+420 …" /></label>
          </div>
          <div className="profile-security-note"><span><LockKeyhole size={18} /></span><div><strong>Your banking identity is protected</strong><p>These are editable customer profile details only. Your authenticated subject, customer ID, and tenant remain controlled by the Banking Kernel.</p></div><BadgeCheck size={20} /></div>
        </section>}

        {activeTab === 'preferences' && <section className="settings-section" aria-labelledby="preferences-section-title">
          <div className="settings-section-heading"><div><span className="settings-section-icon"><SlidersHorizontal size={23} /></span><div><h2 id="preferences-section-title">Preferences</h2><p>Set the regional and workspace defaults you use every day.</p></div></div><span className="settings-section-kicker">YOUR WORKSPACE</span></div>
          <div className="profile-form-grid preferences-grid">
            <label>Preferred currency<select value={form.preferred_currency} onChange={e => update('preferred_currency', e.target.value)}><option value="EUR">EUR · Euro</option><option value="USD">USD · US Dollar</option><option value="GBP">GBP · Pound Sterling</option></select><small>Saved to your customer profile.</small></label>
            <label>Language<select value={form.language} onChange={e => update('language', e.target.value)}><option value="en">English</option><option value="cs">Čeština</option></select><small>Stored as your preferred language. Interface translation can be expanded independently.</small></label>
            <label>Start page<select value={uiSettings.defaultWorkspace} onChange={e => updateUi('defaultWorkspace', e.target.value)}><option>Overview</option><option>Accounts</option><option>Payments</option><option>Loans</option><option>Cards</option><option>Contacts</option><option>Activity</option><option>Academy</option><option>AI assistant</option><option>Controls &amp; audit</option></select><small>Opens this workspace the next time the app is loaded.</small></label>
          </div>
          <div className="preference-toggle-row"><div className="preference-toggle-copy"><strong>Reduce interface motion</strong><p>Use shorter transitions and disable decorative movement across the workspace.</p></div><label className="switch-control"><input type="checkbox" checked={uiSettings.reducedMotion} onChange={e => updateUi('reducedMotion', e.target.checked)} /><span aria-hidden="true" /></label></div>
          <div className="profile-security-note preference-note"><span><Globe2 size={18} /></span><div><strong>Regional preferences are separate from account balances</strong><p>Changing your preferred currency does not convert funds or alter any ledger balance. Balances continue to use their account currency.</p></div></div>
        </section>}

        {activeTab === 'customization' && <section className="settings-section" aria-labelledby="customization-section-title">
          <div className="settings-section-heading"><div><span className="settings-section-icon"><Palette size={24} /></span><div><h2 id="customization-section-title">Workspace Theme</h2><p>Choose a complete visual theme for the entire ORAIflow workspace.</p></div></div><span className="settings-section-kicker">GLOBAL APPEARANCE</span></div>
          <div className="theme-choice-grid" role="group" aria-label="Workspace theme">
            {themeOptions.map(option => <button type="button" key={option.id} className={`theme-choice ${uiSettings.theme === option.id ? 'active' : ''}`} aria-pressed={uiSettings.theme === option.id} onClick={() => updateUi('theme', option.id)}>
              <span className={`theme-preview ${option.preview}`} aria-hidden="true"><i /><i /><i /><i /></span>
              <span className="theme-choice-copy"><strong>{option.title}</strong><small>{option.detail}</small></span>
              {uiSettings.theme === option.id && <CheckCircle2 size={17} />}
            </button>)}
          </div>
          <p className="customization-note settings-saved-note"><CheckCircle2 size={14} /> Theme selection is saved in this browser and applied immediately across the whole workspace.</p>

          <div className="settings-section-heading banner-section-heading"><div><span className="settings-section-icon"><Palette size={22} /></span><div><h2>Overview Banner Background</h2><p>Choose the visual style for the top banner on your Overview page.</p></div></div><span className="settings-section-kicker">OVERVIEW</span></div>
          <div className="preference-toggle-row banner-visibility-row">
            <div className="preference-toggle-copy"><strong>Show banner on Overview</strong><p>Shows ORAIflow's brand message and your recorded Financial Health indicators. You can hide it at any time.</p></div>
            <label className="switch-control" aria-label="Show Overview banner"><input type="checkbox" checked={heroBannerVisible} onChange={event => onHeroBannerVisibilityChange(event.target.checked)} /><span aria-hidden="true" /></label>
          </div>
          <div className="hero-background-options profile-hero-options" role="group" aria-label="Overview banner background">
            {bannerOptions.map(option => <button key={option.id} type="button" className={`hero-background-option ${heroBackground === option.id ? 'active' : ''}`} aria-pressed={heroBackground === option.id} onClick={() => onHeroBackgroundChange(option.id)}>
              <span className="option-preview"><img src={option.asset} alt="" aria-hidden="true" /></span>
              <span className="hero-option-footer"><span className={`option-radio ${heroBackground === option.id ? 'checked' : ''}`}><span /></span><span className="hero-option-copy"><strong>{option.title}</strong><small>{option.detail}</small></span>{option.recommended && <span className="option-recommended">Recommended</span>}</span>
            </button>)}
          </div>
          <p className="customization-note">Your artwork choice is saved even when the banner is hidden. When enabled, it appears below the Overview greeting and New transfer action, before the financial summary. This setting changes presentation only—not customer profile or banking data.</p>

          <div className="appearance-controls">
            <div className="appearance-control-block">
              <div className="appearance-control-heading"><span className="settings-section-icon"><Type size={21} /></span><div><h3>Interface text size</h3><p>Adjust readability across the entire workspace.</p></div></div>
              <div className="setting-choice-row" role="group" aria-label="Interface text size">
                {[{ id: 'small', title: 'Small', sample: 'Aa' }, { id: 'standard', title: 'Standard', sample: 'Aa' }, { id: 'large', title: 'Large', sample: 'Aa' }].map(option => <button type="button" key={option.id} className={`setting-choice ${uiSettings.fontSize === option.id ? 'active' : ''}`} aria-pressed={uiSettings.fontSize === option.id} onClick={() => updateUi('fontSize', option.id)}><strong className={`font-choice-sample ${option.id}`}>{option.sample}</strong><span>{option.title}</span>{uiSettings.fontSize === option.id && <CheckCircle2 size={16} />}</button>)}
              </div>
            </div>
            <div className="appearance-control-block">
              <div className="appearance-control-heading"><span className="settings-section-icon"><LayoutGrid size={21} /></span><div><h3>Layout density</h3><p>Choose how much space appears between content.</p></div></div>
              <div className="setting-choice-row density-choice-row" role="group" aria-label="Layout density">
                <button type="button" className={`setting-choice ${uiSettings.density === 'comfortable' ? 'active' : ''}`} aria-pressed={uiSettings.density === 'comfortable'} onClick={() => updateUi('density', 'comfortable')}><Monitor size={20} /><span><strong>Comfortable</strong><small>More breathing room</small></span>{uiSettings.density === 'comfortable' && <CheckCircle2 size={16} />}</button>
                <button type="button" className={`setting-choice ${uiSettings.density === 'compact' ? 'active' : ''}`} aria-pressed={uiSettings.density === 'compact'} onClick={() => updateUi('density', 'compact')}><Rows3 size={20} /><span><strong>Compact</strong><small>More content on screen</small></span>{uiSettings.density === 'compact' && <CheckCircle2 size={16} />}</button>
              </div>
            </div>
          </div>
          <p className="customization-note settings-saved-note"><CheckCircle2 size={14} /> Text size and layout density are saved in this browser and take effect immediately.</p>
        </section>}
      </div>

      <div className="profile-settings-footer">
        <button type="button" className="secondary-button profile-cancel-button" onClick={cancelProfileEdits}><RotateCcw size={15} /> Cancel</button>
        <button type="submit" className="primary-button profile-save-button" disabled={busy}><Save size={16} /> {busy ? 'Saving changes…' : 'Save Changes'}</button>
      </div>
      {error && <div className="form-error"><AlertTriangle size={15} />{error}</div>}
    </form>
  </section>
}
function App() {
  const [overview, setOverview] = useState(null)
  const [profile, setProfile] = useState(null)
  const [uiSettings, setUiSettings] = useState(readUiSettings)
  const [financialHealth, setFinancialHealth] = useState(null)
  const [heroBackground, setHeroBackground] = useState(() => {
    try {
      const saved = window.localStorage.getItem('oraiflow.overviewHeroBackground')
      return ['svg', 'image', 'city', 'mountain'].includes(saved) ? saved : 'svg'
    } catch { return 'svg' }
  })
  const [heroBannerVisible, setHeroBannerVisible] = useState(() => {
    // Hidden is the product default. An explicit saved customer preference
    // always wins, so existing enabled/disabled choices remain intact.
    try { return window.localStorage.getItem('oraiflow.overviewHeroVisible') === 'true' }
    catch { return false }
  })
  function changeHeroBannerVisibility(visible) {
    const safeVisible = visible === true
    setHeroBannerVisible(safeVisible)
    try { window.localStorage.setItem('oraiflow.overviewHeroVisible', safeVisible ? 'true' : 'false') }
    catch { /* Browser storage may be disabled; current-session state still works. */ }
  }
  function changeHeroBackground(mode) {
    const safeMode = ['svg', 'image', 'city', 'mountain'].includes(mode) ? mode : 'svg'
    setHeroBackground(safeMode)
    try { window.localStorage.setItem('oraiflow.overviewHeroBackground', safeMode) } catch { /* Browser storage may be disabled; current-session state still works. */ }
  }
  function changeUiSettings(next) {
    const safe = {
      fontSize: ['small', 'standard', 'large'].includes(next.fontSize) ? next.fontSize : 'standard',
      density: ['comfortable', 'compact'].includes(next.density) ? next.density : 'comfortable',
      theme: ['black', 'navy', 'modern-white', 'silver'].includes(next.theme) ? next.theme : 'navy',
      defaultWorkspace: ['Overview', 'Accounts', 'Payments', 'Loans', 'Cards', 'Contacts', 'Activity', 'Academy', 'AI assistant', 'Controls & audit'].includes(next.defaultWorkspace) ? next.defaultWorkspace : 'Overview',
      reducedMotion: next.reducedMotion === true,
    }
    setUiSettings(safe)
    try { window.localStorage.setItem('oraiflow.uiSettings', JSON.stringify(safe)) } catch { /* Browser storage may be disabled; current-session state still works. */ }
  }
  const [transferDraft, setTransferDraft] = useState(null)
  const [transferDraftLifecycle, setTransferDraftLifecycle] = useState(null)
  const transferActionReceipts = useRef(new Map())
  const transferActionRuntimeRef = useRef(null)
  const [config, setConfig] = useState(null)
  const [transactions, setTransactions] = useState([])
  const [contacts, setContacts] = useState([])
  const [loans, setLoans] = useState([])
  const [transfers, setTransfers] = useState([])
  const [auditEvents, setAuditEvents] = useState([])
  const [auditTotal, setAuditTotal] = useState(0)
  const [auditPage, setAuditPage] = useState(0)
  const [paymentsPage, setPaymentsPage] = useState(0)
  const [activityPage, setActivityPage] = useState(0)
  const [auditVerification, setAuditVerification] = useState(null)
  const [activeNav, setActiveNav] = useState(() => readUiSettings().defaultWorkspace)
  const navigationActionReceiptsRef = useRef(new Map())
  const navigationActionRuntimeRef = useRef(null)
  const manualNavigationEpochRef = useRef(0)
  const [selectedAccountId, setSelectedAccountId] = useState(null)
  const [showTransfer, setShowTransfer] = useState(false)
  const [notice, setNotice] = useState('')
  const [commandId, setCommandId] = useState(null)
  const [apiError, setApiError] = useState('')
  const [mobileNav, setMobileNav] = useState(false)

  async function refresh() {
    try {
      const [data, cfg, txnData, transferData, auditData, auditVerify, profileData, contactsData, loanData, financialHealthData] = await Promise.all([
        api('/api/overview'), api('/api/config'), api('/api/transactions?limit=250'),
        api('/api/transfers?limit=100'), api(`/api/audit?limit=10&offset=${auditPage * 10}`), api('/api/audit/verify'), api('/api/profile'), api('/api/contacts'), api('/api/loans').catch(() => ({ loans: [] })), api('/api/financial-health').catch(() => null),
      ])
      setOverview(data); setConfig(cfg); setPaymentsPage(0); setActivityPage(0)
      setTransactions(txnData.transactions || data.transactions || [])
      setTransfers(transferData.transfers || [])
      setAuditEvents(auditData.events || [])
      setAuditTotal(auditData.total ?? (auditData.events || []).length)
      setAuditVerification(auditVerify || null)
      setProfile(profileData?.profile || null)
      setContacts(contactsData?.contacts || [])
      setLoans(loanData?.loans || [])
      setFinancialHealth(financialHealthData)
      setApiError('')
    } catch (error) { setApiError(error.message) }
  }

  async function changeAuditPage(nextPage) {
    const safePage = Math.max(0, nextPage)
    setAuditPage(safePage)
    try {
      const data = await api(`/api/audit?limit=10&offset=${safePage * 10}`)
      setAuditEvents(data.events || [])
      setAuditTotal(data.total ?? 0)
    } catch (error) { setApiError(error.message) }
  }

  useEffect(() => { refresh() }, [])
  useEffect(() => {
    if (!notice) return undefined
    const timeout = window.setTimeout(() => setNotice(''), 5000)
    return () => window.clearTimeout(timeout)
  }, [notice])
  useEffect(() => {
    if (!commandId) return undefined
    let cancelled = false
    let checking = false
    const timer = setInterval(async () => {
      if (checking) return
      checking = true
      try {
        const status = await api(`/api/transfers/${commandId}`)
        if (TERMINAL_TRANSFER_STATES.includes(status.status)) {
          clearInterval(timer)
          if (!cancelled) {
            setNotice(status.status === 'SETTLED'
              ? 'Transfer settled successfully in the synthetic ledger. Balances and activity refreshed.'
              : status.status === 'REQUIRES_RECONCILIATION'
                ? `Transfer outcome requires reconciliation. Do not resubmit: ${status.error || 'Check command and ledger evidence.'}`
                : `Transfer failed: ${status.error || 'See API logs.'}`)
            setCommandId(null)
            await refresh()
          }
        }
      } catch { /* The durable command and worker continue independently of the UI poll. */ }
      finally { checking = false }
    }, 1000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [commandId])

  const accounts = overview?.accounts || []
  const selectedAccount = accounts.find((account) => account.account_id === selectedAccountId) || null
  const navigate = (requestedPage, options = {}) => {
    const page = resolveWorkspace(requestedPage)
    if (!page) {
      console.warn('[ORAI navigation] rejected unsupported workspace:', requestedPage)
      return false
    }
    const source = options?.source === 'agent' ? 'agent' : 'manual'
    if (source === 'agent') {
      // Agent actions carry the route revision captured when the customer turn
      // began. If the customer has since clicked another tab, this stale action
      // is rejected and cannot pull the route back to its old destination.
      if (!isNavigationEpochCurrent(options?.expectedEpoch, manualNavigationEpochRef.current)) {
        console.info('[ORAI navigation] stale agent navigation ignored', {
          workspace: page, expected_epoch: options?.expectedEpoch,
          current_epoch: manualNavigationEpochRef.current,
        })
        return false
      }
    } else {
      // Any customer-initiated route change supersedes all previously issued
      // agent navigation proposals, including ones still in flight.
      manualNavigationEpochRef.current = advanceManualNavigationEpoch(manualNavigationEpochRef.current)
    }
    if (page === 'Overview') setSelectedAccountId(null)
    if (page !== activeNav) { setPaymentsPage(0); setActivityPage(0) }
    setActiveNav(page)
    setMobileNav(false)
    return true
  }
  const navClick = (page) => navigate(page)

  // Keep the CopilotKit agent and all render registrations mounted at the
  // application level. Workspace navigation must not tear down an in-flight
  // AG-UI run or unregister its Generative UI/tool renderers.
  const { agent, isReady } = useAgent({ agentId: 'oraiflow_agent' })
  const { copilotkit } = useCopilotKit()
  useComponent({
    name: 'render_account_summary',
    description: presentationContract('render_account_summary').description,
    parameters: presentationSchema('render_account_summary'),
    render: AccountSummaryCard,
    followUp: presentationContract('render_account_summary').follow_up,
  }, [])
  useComponent({
    name: 'render_transaction_list',
    description: presentationContract('render_transaction_list').description,
    parameters: presentationSchema('render_transaction_list'),
    render: TransactionListCard,
    followUp: presentationContract('render_transaction_list').follow_up,
  }, [])
  useComponent({
    name: 'render_transaction_detail',
    description: presentationContract('render_transaction_detail').description,
    parameters: presentationSchema('render_transaction_detail'),
    render: TransactionDetailCard,
    followUp: presentationContract('render_transaction_detail').follow_up,
  }, [])

  useRenderTool({ name: 'list_customer_accounts', parameters: capabilitySchema('banking.accounts.list'), render: ({ status }) => <AgentToolActivity title="Read account balances" detail="Querying read-only MCP capability" status={status} result={status === 'complete' ? 'Account data retrieved through MCP' : undefined}/> }, [])
  useRenderTool({ name: 'get_account_balance', parameters: capabilitySchema('banking.accounts.balance'), render: ({ status }) => <AgentToolActivity title="Read account balance" detail="Querying read-only MCP capability" status={status} result={status === 'complete' ? 'Balance retrieved through MCP' : undefined}/> }, [])
  useRenderTool({ name: 'get_recent_transactions', parameters: capabilitySchema('banking.transactions.recent'), render: ({ status }) => <AgentToolActivity title="Read recent activity" detail="Querying read-only MCP capability" status={status} result={status === 'complete' ? 'Transactions retrieved through MCP' : undefined}/> }, [])
  useRenderTool({ name: 'analyze_customer_transactions', parameters: capabilitySchema('banking.transactions.analyze'), render: ({ parameters, status }) => <AgentToolActivity title="Analyze transaction activity" detail={`Applying registered filters and aggregations${parameters.period ? ` for ${parameters.period.replaceAll('_', ' ')}` : ''}`} status={status} result={status === 'complete' ? 'Authenticated transaction analysis retrieved' : undefined}/> }, [])
  useRenderTool({ name: 'get_customer_loan_overview', parameters: capabilitySchema('banking.loans.overview'), render: ({ status }) => <AgentToolActivity title="Read illustrative loan terms" detail="Querying the authenticated read-only loan overview" status={status} result={status === 'complete' ? 'Illustrative loan terms retrieved; payment tracking is not available' : undefined}/> }, [])
  useRenderTool({ name: 'get_customer_financial_health_overview', parameters: capabilitySchema('banking.financial_health.overview'), render: ({ status }) => <AgentToolActivity title="Calculate recorded Financial Health" detail="Comparing authenticated deposit assets and recorded liabilities by currency" status={status} result={status === 'complete' ? 'Recorded leverage snapshot retrieved; this is not a credit score' : undefined}/> }, [])
  useRenderTool({ name: 'search_contacts', parameters: capabilitySchema('banking.contacts.search'), render: ({ parameters, status }) => <AgentToolActivity title="Find saved beneficiary" detail={`Searching customer-owned Contacts for ${parameters.name_query || 'a name'}`} status={status} result={status === 'complete' ? 'Saved contact candidates retrieved through MCP' : undefined}/> }, [])
  useRenderTool({ name: 'navigate_workspace', parameters: capabilitySchema('frontend.workspace.navigate'), render: ({ parameters, status, result }) => <AgentToolActivity title={`Open ${parameters.workspace || 'workspace'}`} detail="Navigating the ORAIflow workspace" status={status} result={result}/> }, [])
  useRenderTool({ name: 'select_account', parameters: capabilitySchema('frontend.account.select'), render: ({ status, result }) => <AgentToolActivity title="Select account" detail="Opening filtered account activity" status={status} result={result}/> }, [])
  useRenderTool({ name: 'clear_account_selection', parameters: capabilitySchema('frontend.account.clear_selection'), render: ({ status, result }) => <AgentToolActivity title="Show all accounts" detail="Clearing the account filter" status={status} result={result}/> }, [])
  useRenderTool({ name: 'prepare_transfer_draft', parameters: capabilitySchema('frontend.transfer.prepare_draft'), render: ({ parameters, status, result }) => <AgentToolActivity title="Prepare editable transfer" detail={`${parameters.amount || ''} ${parameters.currency || ''} to ${parameters.beneficiary || 'recipient'}`} status={status} result={result}/> }, [])

  const agentApplicationContext = validateApplicationContext({
    agent_turn_contract_version: AGENT_TURN_CONTRACT_VERSION,
    workspace: activeNav,
    navigation_epoch: manualNavigationEpochRef.current,
    selected_account_id: selectedAccount?.account_id || null,
    selected_account_name: selectedAccount?.account_name || 'All accounts',
    accounts: accounts.map((account) => ({
      account_id: account.account_id,
      account_name: account.account_name,
      currency: account.currency,
    })),
    contacts: contacts.filter((contact) => contact.status === 'ACTIVE').map((contact) => ({
      contact_id: contact.contact_id,
      full_name: contact.full_name,
      status: contact.status,
    })),
    ...(transferDraftLifecycle ? { transfer_draft_state: transferDraftLifecycle } : {}),
  })
  useAgentContext({
    description: 'Current ORAIflow workspace and account selection. Treat as read-only context; do not infer identity, permissions or financial authorization from it.',
    value: agentApplicationContext,
  })
  // Keep the frontend navigation tool registration stable for the lifetime of
  // the application. The handler reads the latest route/context through this
  // ref, so a navigation state change cannot unregister and re-register an
  // in-flight tool action.
  navigationActionRuntimeRef.current = { navigate, context: agentApplicationContext, navigationEpoch: manualNavigationEpochRef.current }
  // The transfer frontend tool must stay registered while its handler updates
  // draft lifecycle state. CopilotKit refreshes useFrontendTool registration
  // when a dependency changes; keeping the handler dependency-free and reading
  // current state through this ref prevents an in-flight browser action from
  // being unregistered/re-registered mid-call.
  transferActionRuntimeRef.current = {
    accounts, contacts, transferDraftLifecycle, transferDraft, showTransfer,
    context: agentApplicationContext,
  }
  useFrontendTool({
    name: 'navigate_workspace',
    description: 'Navigate only to an existing ORAIflow workspace. The application validates the allowlist independently.',
    parameters: capabilitySchema('frontend.workspace.navigate'),
    handler: (args) => {
      const runtime = navigationActionRuntimeRef.current
      return executeFrontendCapability('frontend.workspace.navigate', args, async ({ workspace, action_id, navigation_epoch }) => (
        executeNavigationOnce(navigationActionReceiptsRef.current, action_id, async () => {
          const destination = resolveWorkspace(workspace)
          if (!destination) return { status: 'rejected', reason: `Unsupported workspace '${workspace}'. Available pages are Overview, Accounts, Payments, Cards, Contacts, Activity, AI assistant, and Controls & audit.`, action_id }
          const navigated = runtime?.navigate?.(destination, { source: 'agent', expectedEpoch: navigation_epoch })
          if (navigated !== true) return { status: 'rejected', reason: 'The customer changed workspace after this navigation was proposed, or the destination is unavailable. Their current page has been kept.', action_id }
          return { status: 'opened', workspace: destination, action_id }
        })
      ), { context: runtime?.context || agentApplicationContext })
    },
  }, [])
  useFrontendTool({
    name: 'select_account',
    description: 'Select an account already available in the current customer context and open its Activity view.',
    parameters: capabilitySchema('frontend.account.select'),
    handler: (args) => executeFrontendCapability('frontend.account.select', args, async ({ account_id }) => {
      const allowed = accounts.some((account) => account.account_id === account_id)
      if (!allowed) return { status: 'rejected', reason: 'Account is not available in this workspace.' }
      setSelectedAccountId(account_id)
      setActivityPage(0)
      setActiveNav('Activity')
      setMobileNav(false)
      return { status: 'selected', account_id, view: 'Activity' }
    }, { context: agentApplicationContext }),
  }, [accounts])
  useFrontendTool({
    name: 'clear_account_selection',
    description: 'Clear the current account filter and show activity across all accounts.',
    parameters: capabilitySchema('frontend.account.clear_selection'),
    handler: (args) => executeFrontendCapability('frontend.account.clear_selection', args, async () => {
      setSelectedAccountId(null)
      setActivityPage(0)
      setActiveNav('Activity')
      setMobileNav(false)
      return { status: 'cleared', view: 'Activity', filter: 'all' }
    }, { context: agentApplicationContext }),
  }, [])
  useFrontendTool({
    name: 'prepare_transfer_draft',
    description: 'Open an editable transfer form only. This action does not preview, approve, authenticate or submit a payment.',
    parameters: capabilitySchema('frontend.transfer.prepare_draft'),
    handler: (args) => {
      const runtime = transferActionRuntimeRef.current || {
        accounts, contacts, transferDraftLifecycle, transferDraft, showTransfer,
        context: agentApplicationContext,
      }
      return executeFrontendCapability('frontend.transfer.prepare_draft', args, async ({ beneficiary, beneficiary_contact_id, amount, currency, from_account_id, workflow_id, action_id }) => {
        const replay = transferDraftReplayDecision({
          receipts: transferActionReceipts.current,
          workflowId: workflow_id,
          actionId: action_id,
          lifecycle: runtime.transferDraftLifecycle,
          visibleDraft: runtime.transferDraft,
          modalOpen: runtime.showTransfer,
        })
        if (replay.kind === 'already_open') {
          console.info('[ORAIflow transfer] duplicate draft action suppressed', { workflow_id, action_id })
          return { status: 'draft_opened', workflow_id, action_id, message: 'The editable transfer draft for this request is already open. No second draft was created.' }
        }
        if (replay.kind !== 'open') {
          console.warn('[ORAIflow transfer] draft replay rejected', { workflow_id, action_id, reason: replay.kind })
          const reason = replay.kind === 'another_draft_open'
            ? 'Another editable transfer draft is already open. Close it before preparing another draft.'
            : replay.kind === 'already_terminal'
              ? 'This transfer draft was already closed or completed and cannot be reopened by a delayed action.'
              : 'The transfer action identifiers do not match the recorded request.'
          return { status: 'rejected', workflow_id, action_id, reason }
        }
        const numericAmount = Number(amount)
        const source = runtime.accounts.find((account) => account.account_id === from_account_id && account.currency === currency)
        if (!source) return { status: 'rejected', workflow_id, action_id, reason: 'Source account is unavailable or has a different currency.' }
        const contact = runtime.contacts.find((item) => item.contact_id === beneficiary_contact_id && item.status === 'ACTIVE')
        if (!contact || contact.full_name.trim().toLocaleLowerCase() !== beneficiary.trim().toLocaleLowerCase() || contact.currency !== currency) {
          return { status: 'rejected', workflow_id, action_id, reason: 'The beneficiary does not match an active saved contact in the requested currency.' }
        }
        if (!Number.isFinite(numericAmount) || numericAmount <= 0 || !workflow_id || !action_id) {
          return { status: 'rejected', workflow_id, action_id, reason: 'The transfer draft parameters or action identifiers are invalid.' }
        }
        const proposal = {
          type: 'transfer_draft_proposal', beneficiary: contact.full_name,
          beneficiary_contact_id: contact.contact_id,
          beneficiary_contact: { full_name: contact.full_name, bank_name: contact.bank_name, account_reference_masked: contact.account_reference_masked },
          amount, currency, from_account_id, workflow_id, action_id,
        }
        // Record the one-shot receipt synchronously before scheduling React
        // state updates. Any concurrent/replayed invocation sees this entry.
        transferActionReceipts.current.set(action_id, { workflow_id, action_id, status: 'open' })
        setTransferDraftLifecycle({ workflow_id, action_id, status: 'open' })
        setTransferDraft(proposal)
        setShowTransfer(true)
        console.info('[ORAIflow transfer] editable draft opened', { workflow_id, action_id })
        return { status: 'draft_opened', workflow_id, action_id, message: 'An editable transfer draft for the verified saved contact is open. Nothing has been previewed, approved or submitted.' }
      }, { context: runtime.context || agentApplicationContext })
    },
  }, [])
  const dateLabel = new Intl.DateTimeFormat('en-GB', { weekday: 'long', day: '2-digit', month: 'long', year: 'numeric' }).format(new Date()).toUpperCase()

  let content
  if (activeNav === 'My profile') {
    content = <ProfilePage profile={profile} heroBackground={heroBackground} onHeroBackgroundChange={changeHeroBackground} heroBannerVisible={heroBannerVisible} onHeroBannerVisibilityChange={changeHeroBannerVisibility} uiSettings={uiSettings} onUiSettingsChange={changeUiSettings} onProfileSaved={(next) => { setProfile(next); refresh() }} onNotice={setNotice} />
  } else if (activeNav === 'Overview') {
    content = <OverviewPage overview={overview} loans={loans} financialHealth={financialHealth} transactions={transactions} config={config} dateLabel={dateLabel}
      onNewTransfer={() => { setTransferDraft(null); setShowTransfer(true) }} onNavigate={navigate} onRefresh={refresh} onTransferDraft={(draft) => { setTransferDraft(draft); setShowTransfer(true) }} selectedAccountId={selectedAccountId} agent={agent} isReady={isReady} copilotkit={copilotkit} heroBackground={heroBackground} heroBannerVisible={heroBannerVisible} onSelectAccount={(id) => { setSelectedAccountId(id); navigate('Activity') }} />
  } else if (activeNav === 'Accounts') {
    content = <AccountsPage accounts={accounts} selectedAccountId={selectedAccountId} onSelectAccount={(id) => { setSelectedAccountId(id); navigate('Activity') }} onNewTransfer={() => { setTransferDraft(null); setShowTransfer(true) }} />
  } else if (activeNav === 'Payments') {
    content = <PaymentsPage transfers={transfers} accounts={accounts} onNewTransfer={() => { setTransferDraft(null); setShowTransfer(true) }} onRefresh={refresh} page={paymentsPage} onPageChange={setPaymentsPage} />
  } else if (activeNav === 'Loans') {
    content = <LoanDetailsPage loans={loans} />
  } else if (activeNav === 'Academy') {
    content = <AcademyPage agent={agent} isReady={isReady} copilotkit={copilotkit} />
  } else if (activeNav === 'Contacts') {
    content = <ContactsPage contacts={contacts} onPrepareTransfer={(contact) => {
      const currency = contact.currency || 'EUR'
      const compatibleAccounts = accounts.filter((account) => account.currency === currency)
      const selectedCompatibleAccount = selectedAccount?.currency === currency ? selectedAccount : null
      const source = selectedCompatibleAccount || (compatibleAccounts.length === 1 ? compatibleAccounts[0] : null)
      setTransferDraft({
        type: 'transfer_draft_proposal', beneficiary: contact.full_name,
        beneficiary_contact_id: contact.contact_id,
        beneficiary_contact: { full_name: contact.full_name, bank_name: contact.bank_name, account_reference_masked: contact.account_reference_masked },
        amount: '', currency, from_account_id: source?.account_id || '',
      })
      setShowTransfer(true)
    }} />
  } else if (activeNav === 'Cards') {
    content = <CardsPage displayName={profile?.display_name || 'Alex Morgan'} />
  } else if (activeNav === 'Activity') {
    content = <ActivityPage transactions={transactions} accounts={accounts} selectedAccountId={selectedAccountId} onSelectAccount={setSelectedAccountId} onRefresh={refresh} page={activityPage} onPageChange={setActivityPage} />
  } else if (activeNav === 'AI assistant') {
    content = <AssistantPage agent={agent} isReady={isReady} copilotkit={copilotkit}/>
  } else if (activeNav === 'Controls & audit') {
    content = <ControlsPage events={auditEvents} verification={auditVerification} total={auditTotal} page={auditPage} onPageChange={changeAuditPage} onRefresh={refresh} />
  } else {
    content = <section className="workspace-card" role="alert"><h2>Workspace unavailable</h2><p>This destination is not part of ORAIflow. Return to Overview or choose a workspace from the navigation.</p><button className="secondary-button" onClick={() => navigate('Overview')}>Return to Overview</button></section>
  }

  const providerLabel = config?.llm_provider === 'demo' ? 'DEMO MODE' : `${config?.llm_provider?.toUpperCase() || 'LLM'} · ${config?.llm_model || ''}`
  const workspaceName = String(config?.workspace_name || 'Atlas Fintech').trim() || 'Atlas Fintech'
  const workspaceInitial = Array.from(workspaceName)[0]?.toLocaleUpperCase() || 'A'

  return <div className="app-shell" data-theme={uiSettings.theme} data-font-size={uiSettings.fontSize} data-density={uiSettings.density} data-reduced-motion={uiSettings.reducedMotion ? 'true' : 'false'}>
    <aside className={`sidebar ${mobileNav ? 'open' : ''}`}>
      <div className="side-brand"><Mark /></div>
      <div className="workspace-switch" title={workspaceName} aria-label={`${workspaceName} sandbox workspace`}><div className="workspace-icon">{workspaceInitial}</div><div><strong>{workspaceName}</strong><small>Sandbox workspace</small></div><ChevronDown size={15} /></div>
      <div className="nav-label">WORKSPACE</div>
      <nav>
        <NavItem icon={Home} label="Overview" active={activeNav === 'Overview'} onClick={() => navClick('Overview')} />
        <NavItem icon={Wallet} label="Accounts" active={activeNav === 'Accounts'} onClick={() => navClick('Accounts')} />
        <NavItem icon={ArrowLeftRight} label="Payments" active={activeNav === 'Payments'} onClick={() => navClick('Payments')} />
        <NavItem icon={Landmark} label="Loans" active={activeNav === 'Loans'} onClick={() => navClick('Loans')} />
        <NavItem icon={CreditCard} label="Cards" active={activeNav === 'Cards'} onClick={() => navClick('Cards')} />
        <NavItem icon={Users} label="Contacts" active={activeNav === 'Contacts'} onClick={() => navClick('Contacts')} />
        <NavItem icon={Activity} label="Activity" active={activeNav === 'Activity'} onClick={() => navClick('Activity')} />
      </nav>
      <div className="nav-label second-label">INTELLIGENCE</div>
      <nav>
        <NavItem icon={GraduationCap} label="Academy" active={activeNav === 'Academy'} onClick={() => navClick('Academy')} />
        <NavItem icon={MessageCircle} label="AI assistant" active={activeNav === 'AI assistant'} onClick={() => navClick('AI assistant')} badge="AI" />
        <NavItem icon={ShieldCheck} label="Controls & audit" active={activeNav === 'Controls & audit'} onClick={() => navClick('Controls & audit')} />
      </nav>
      <div className="sidebar-spacer" />
      <div className="env-card"><div className="env-top"><span className="env-led" /> LOCAL ENVIRONMENT</div><strong>Sandbox is isolated</strong><p>All accounts and payments use synthetic data.</p><div className="env-footer"><LockKeyhole size={13} /> No live payment rails</div></div>
      <button className={`profile-row ${activeNav === 'My profile' ? 'active' : ''}`} onClick={() => { manualNavigationEpochRef.current = advanceManualNavigationEpoch(manualNavigationEpochRef.current); setActiveNav('My profile'); setMobileNav(false) }} title="Open customer settings" aria-current={activeNav === 'My profile' ? 'page' : undefined}><div className="profile-avatar">{(profile?.display_name || 'Demo Customer').split(' ').map(part => part[0]).slice(0,2).join('').toUpperCase()}</div><div><strong>My profile</strong><small>{profile?.display_name || 'Demo Customer'} · Settings</small></div><MoreHorizontal size={18} /></button>
    </aside>

    <main className="main-area">
      <header className="topbar"><div className="topbar-inner"><button className="mobile-menu icon-button" onClick={() => setMobileNav(!mobileNav)} aria-label="Toggle navigation"><Menu size={20} /></button><div className="breadcrumbs"><span>Workspace</span><b>/</b><strong>{activeNav}</strong></div><div className="top-actions"><div className="system-status"><span className="online-dot" /> Local sandbox connected</div><button className="icon-button" onClick={refresh} title="Refresh data" aria-label="Refresh data"><RefreshCw size={17} /></button><button className="icon-button notification" title="Open audit events" aria-label="Open audit events" onClick={() => navigate('Controls & audit')}><Bell size={17} /><i /></button><button className="top-avatar profile-trigger" onClick={() => { manualNavigationEpochRef.current = advanceManualNavigationEpoch(manualNavigationEpochRef.current); setActiveNav('My profile'); setMobileNav(false) }} aria-label="Open customer profile">{(profile?.display_name || 'Demo Customer').split(' ').map(part => part[0]).slice(0,2).join('').toUpperCase()}</button></div></div></header>
      <div className="content-wrap">
        <aside className="demo-disclosure" role="note" aria-label="Important demo and synthetic data notice">
          <span className="demo-disclosure-icon"><AlertTriangle size={18} /></span>
          <div><strong>{DEMO_DISCLOSURE_TITLE}</strong><p>{DEMO_DISCLOSURE_TEXT}</p></div>
        </aside>
        {apiError && <div className="api-error"><CircleHelp size={16} /> Backend connection unavailable: {apiError}. Start the stack with <code>docker compose up -d --build</code>.</div>}
        {content}
        {activeNav !== 'Overview' && <WorkspaceFooter providerLabel={providerLabel} />}
      </div>
    </main>
    {showTransfer && <TransferModal accounts={accounts} initialDraft={transferDraft} onClose={() => {
      const actionId = transferDraft?.action_id
      const workflowId = transferDraft?.workflow_id
      const existingReceipt = actionId ? transferActionReceipts.current.get(actionId) : null
      const alreadyAccepted = existingReceipt?.status === 'accepted' || (
        transferDraftLifecycle?.workflow_id === workflowId &&
        transferDraftLifecycle?.action_id === actionId &&
        transferDraftLifecycle?.status === 'accepted'
      )
      if (actionId && workflowId && !alreadyAccepted) {
        transferActionReceipts.current.set(actionId, { workflow_id: workflowId, action_id: actionId, status: 'dismissed' })
        setTransferDraftLifecycle({ workflow_id: workflowId, action_id: actionId, status: 'dismissed' })
      }
      setShowTransfer(false); setTransferDraft(null)
    }} onAccepted={(id) => {
      const actionId = transferDraft?.action_id
      const workflowId = transferDraft?.workflow_id
      if (actionId && workflowId) {
        transferActionReceipts.current.set(actionId, { workflow_id: workflowId, action_id: actionId, status: 'accepted' })
        setTransferDraftLifecycle({ workflow_id: workflowId, action_id: actionId, status: 'accepted' })
      }
      setCommandId(id); setNotice('Transfer recorded in PostgreSQL and queued for the Temporal dispatcher.')
    }} />}
    {notice && <div className="toast"><CheckCircle2 size={17} />{notice}<button onClick={() => setNotice('')} aria-label="Dismiss"><X size={15} /></button></div>}
  </div>
}

createRoot(document.getElementById('root')).render(<React.StrictMode><CopilotKitProvider runtimeUrl="/api/copilotkit" agentId="oraiflow_agent" onError={({ error }) => console.error('[CopilotKit]', error)}><App /></CopilotKitProvider></React.StrictMode>)
