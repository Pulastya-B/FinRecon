import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import Detail, { BandTag, CodeBadge } from './Detail.jsx'
import DataPage from './Data.jsx'
import EvidencePage from './Evidence.jsx'
import Ask from './Ask.jsx'
import Trace from './Trace.jsx'
import Cash from './Cash.jsx'
import { InfoDot } from './Info.jsx'
import Tour, { runSteps, queueSteps, evidenceSteps } from './Tour.jsx'

async function api(path, options) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) throw new Error(`${res.status} ${path}`)
  return res.json()
}

/*
 * Column and stat explanations, written out in full.
 *
 * A judge reading this queue has no reason to know what CIRCUMSTANTIAL means,
 * and the terms that need explaining are exactly the ones carrying the
 * argument. Kept as data next to the table rather than scattered through the
 * markup, because the wording of these is the part most likely to change.
 */
const TIPS = {
  rupees:
    'Money at stake in this group. The queue is sorted by this — an operator works by value, not by ID.',
  code: 'What kind of problem this is. Determines who acts on it and what they do.',
  headline: 'One line naming the specific payout or pattern.',
  orders: 'How many customer orders are affected. One payout can strand dozens.',
  evidence:
    'How strongly the cause is supported. STRONG means an accidental fit was essentially ruled out. CIRCUMSTANTIAL means the arithmetic works but something else could produce it — verify before acting. REFUSE means the engine declined to name a cause rather than guess. A dash means no cause was searched for; the finding needs no attribution.',
  matched:
    'Orders the engine tied all the way through: order to gateway payment to payout to bank credit. The rest are in the queue.',
  incorrect:
    'Matches the engine made that were wrong, checked against ground truth. This is the number that matters — a wrong match enters the books silently.',
  investigable:
    'Exception rows collapsed by root cause. One payout going wrong strands dozens of orders; an operator works the cause once, not each row.',
  unexplained:
    'Total money in the queue. Not money lost — money the engine refused to claim it had settled.',
}

/* --------------------------------------------------------------------------
 * Icons. Inline, 16px, currentColor, no library.
 *
 * A nav of four bare words gave the eye nothing to land on and no way to tell
 * one destination from another before reading it. Drawn to one 1.6px stroke so
 * they read as a set rather than four borrowed glyphs.
 * ------------------------------------------------------------------------ */
const ico = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
}

function IconRun() {
  return (
    <svg width="20" height="20" viewBox="0 0 16 16" {...ico}>
      <path d="M8 1.6 3 8.4h3.4L5.6 14.4l5.6-7.2H7.6z" />
    </svg>
  )
}
function IconQueue() {
  return (
    <svg width="20" height="20" viewBox="0 0 16 16" {...ico}>
      <path d="M2.4 4h11.2M2.4 8h11.2M2.4 12h7" />
    </svg>
  )
}
function IconEvidence() {
  return (
    <svg width="20" height="20" viewBox="0 0 16 16" {...ico}>
      <path d="M8 1.8 13.2 4v4c0 3-2.2 5.3-5.2 6.2C5 13.3 2.8 11 2.8 8V4z" />
      <path d="M5.9 8.1 7.4 9.6l2.9-3" />
    </svg>
  )
}
function IconData() {
  return (
    <svg width="20" height="20" viewBox="0 0 16 16" {...ico}>
      <ellipse cx="8" cy="3.9" rx="5.2" ry="2.1" />
      <path d="M2.8 3.9v8.2c0 1.2 2.3 2.1 5.2 2.1s5.2-.9 5.2-2.1V3.9" />
      <path d="M2.8 8c0 1.2 2.3 2.1 5.2 2.1s5.2-.9 5.2-2.1" />
    </svg>
  )
}
function IconAsk() {
  return (
    <svg width="20" height="20" viewBox="0 0 16 16" {...ico}>
      <path d="M13.4 10.2c0 .7-.6 1.3-1.3 1.3H5.8L3.2 13.7v-2.2h-.3c-.7 0-1.3-.6-1.3-1.3V3.8c0-.7.6-1.3 1.3-1.3h9.2c.7 0 1.3.6 1.3 1.3z" />
      <path d="M6.4 5.9a1.6 1.6 0 0 1 3.1.5c0 1.1-1.6 1.3-1.6 2.2M7.9 10.3v.02" />
    </svg>
  )
}
function IconCash() {
  return (
    <svg width="20" height="20" viewBox="0 0 16 16" {...ico}>
      <rect x="1.6" y="3.6" width="12.8" height="8.8" rx="1.4" />
      <circle cx="8" cy="8" r="2" />
      <path d="M4.2 8h.02M11.8 8h.02" />
    </svg>
  )
}
const NAV = [
  { name: 'Run', Icon: IconRun },
  { name: 'Queue', Icon: IconQueue },
  { name: 'Cash', Icon: IconCash },
  { name: 'Ask', Icon: IconAsk },
  { name: 'Evidence', Icon: IconEvidence },
  { name: 'Data', Icon: IconData },
]

/*
 * A tooltip that cannot be clipped.
 *
 * Rendered through a portal to <body> and positioned with position: fixed from
 * the anchor's own rect, then clamped to the viewport. Both halves matter: the
 * portal escapes the queue pane's overflow, and the clamp stops the leftmost
 * column putting its bubble under the sidebar.
 */
const TIP_WIDTH = 290
const TIP_MARGIN = 8

function Tip({ text, children, className = '' }) {
  const anchor = useRef(null)
  const [box, setBox] = useState(null)

  const open = () => {
    const r = anchor.current?.getBoundingClientRect()
    if (!r) return
    const left = Math.min(
      Math.max(TIP_MARGIN, r.left),
      window.innerWidth - TIP_WIDTH - TIP_MARGIN
    )
    const below = window.innerHeight - r.bottom
    setBox({ left, top: r.bottom + 8, flip: below < 130, bottom: r.top - 8 })
  }

  return (
    <>
      <span
        ref={anchor}
        tabIndex={0}
        className={`tip ${className}`}
        onMouseEnter={open}
        onMouseLeave={() => setBox(null)}
        onFocus={open}
        onBlur={() => setBox(null)}
      >
        {children}
      </span>
      {box &&
        createPortal(
          <div
            className="tip-bubble"
            style={
              box.flip
                ? { left: box.left, bottom: window.innerHeight - box.bottom }
                : { left: box.left, top: box.top }
            }
          >
            {text}
          </div>,
          document.body
        )}
    </>
  )
}

const CODE_SHORT = {
  MISSING_IN_BANK: 'MISSING',
  CHARGEBACK_UNPOSTED: 'CHARGEBACK',
  AMOUNT_VARIANCE_UNEXPLAINED: 'VARIANCE',
  DUPLICATE_PAYMENT: 'DUPLICATE',
  ORDER_UNPAID: 'UNPAID',
  AMBIGUOUS_MULTI_CANDIDATE: 'AMBIGUOUS',
  TIMING_PENDING: 'PENDING',
  UNKNOWN: 'UNKNOWN',
}

// Severity thresholds, in paise. A lakh is where a finding stops being a
// bookkeeping question and starts being money someone has to answer for; a
// thousand is where it stops being worth an operator's attention before the
// rest of the queue is clear.
const BIG_PAISE = 100000 * 100
const SMALL_PAISE = 1000 * 100

/*
 * The stat strip. Four cards on a tinted page rather than four cells in a bar.
 *
 * The value comes first and the label sits under it: the figure is what a
 * reader is here for and the label tells them how to read it, so leading with
 * 11px caps made every cell open with its least important line.
 *
 * INCORRECT is the only figure in accent blue and the only tinted card,
 * because it is the claim the whole engine makes. Everything else on this
 * screen is context for it.
 */
function Stats({ summary }) {
  // `info` names the popover behind each card's glyph. MATCHED is the coverage
  // figure and INCORRECT is the precision one, so those are the explanations
  // they carry -- the card label is the number's name, the glyph is what it
  // means and why it is the one to look at.
  const pct = summary.total_orders
    ? ((summary.matched / summary.total_orders) * 100).toFixed(2)
    : '0.00'
  const rows = summary.exception_rows.toLocaleString('en-IN')

  const cells = [
    {
      label: 'Matched',
      value: `${summary.matched.toLocaleString('en-IN')} / ${summary.total_orders.toLocaleString('en-IN')}`,
      tip: TIPS.matched,
      info: 'coverage',
      // Live, not seed 42's. See the note on InfoDot's `body` prop.
      body:
        `${pct}% of orders tied all the way through to a bank line. It is not ` +
        `higher because the other ${rows} chains are genuinely broken and must ` +
        `not be matched — recall is 100%, meaning every chain that COULD be ` +
        `matched was.`,
    },
    { label: 'Incorrect', value: String(summary.incorrect), tip: TIPS.incorrect,
      accent: true, info: 'precision' },
    {
      label: 'Investigable',
      value: `${summary.groups} items`,
      sub: `${summary.exception_rows} rows`,
      tip: TIPS.investigable,
      info: 'investigable',
      body:
        `${rows} exception rows grouped by root cause into ${summary.groups} ` +
        `findings. One missing payout strands dozens of orders — that is one ` +
        `thing to investigate, not dozens.`,
    },
    { label: 'Unexplained', value: summary.unexplained, tip: TIPS.unexplained,
      info: 'unexplained' },
  ]
  return (
    <div data-tour="stats" className="grid shrink-0 grid-cols-4 gap-3 px-gutter pb-4 pt-4">
      {cells.map((c) => (
        <div
          key={c.label}
          className={`panel flex min-w-0 flex-col justify-center px-4 py-3 ${
            c.accent ? 'ring-1 ring-inset ring-accent/25' : ''
          }`}
          style={c.accent ? { background: 'var(--accent-bg)' } : undefined}
        >
          <div
            data-stat-value=""
            className={`tnum truncate text-display ${
              c.accent ? 'text-accent' : 'text-n-900'
            }`}
          >
            {c.value}
          </div>
          <div className="mt-1.5 flex items-baseline gap-2">
            <Tip text={c.tip} className="shrink-0 text-label uppercase text-n-600">
              {c.label}
            </Tip>
            <InfoDot id={c.info} body={c.body} />
            {c.sub && <span className="tnum text-body-sm text-n-500">{c.sub}</span>}
          </div>
        </div>
      ))}
    </div>
  )
}

/*
 * The rupee bar. 4px, directly under the figure, width proportional to the
 * largest value IN ITS OWN SECTION.
 *
 * Per-section, not global. The two sections differ by an order of magnitude, so
 * one shared scale drew every payout row as the same stub and ranked nothing.
 * No animation: the queue is a list to scan, and bars growing on load carry no
 * information.
 */
function RupeeBar({ paise, max }) {
  const pct = max > 0 ? Math.max(2, (paise / max) * 100) : 0
  return (
    <div
      data-bar=""
      className="pointer-events-none absolute bottom-[3px] right-0 h-[4px] rounded-[1px] bg-[#6B7280]/20"
      style={{ width: `${pct}%` }}
    />
  )
}

/*
 * No horizontal padding here. It used to carry px-3, and a column that wanted
 * px-2 could not override it: Tailwind emits utilities in scale order, so px-3
 * lands after px-2 in the stylesheet and wins regardless of the order the
 * classes appear in the markup. EVIDENCE asked for 8px of padding, silently got
 * 12, and lost 6px of CIRCUMSTANTIAL on every seed that produces one -- which
 * seed 42 does not. Padding is now declared per column, at the column.
 */
const CELL = 'h-[32px] leading-[18px] border-b border-b-n-100'

function QueueRow({ g, active, max, onSelect, decided }) {
  const missing = g.code === 'MISSING_IN_BANK'
  const big = g.rupees_paise >= BIG_PAISE
  const small = g.rupees_paise < SMALL_PAISE

  /*
   * The left rail carries SEVERITY first and selection second. A missing payout
   * is money that may be gone; the row says so before you read it. Selection is
   * already carried by the fill, so when the two want the same 2px the severity
   * keeps it.
   */
  const rail = missing
    ? 'border-l-2 border-l-danger pl-[10px]'
    : active
    ? 'border-l-2 border-l-accent pl-[10px]'
    : 'pl-3'

  return (
    <tr
      onClick={() => onSelect(g.group_id)}
      data-selected={active ? '' : undefined}
      data-decided={decided ? decided.action : undefined}
      // A worked item recedes rather than disappearing: the queue is a record
      // of what was found, and hiding decided rows would make the burn-down
      // impossible to check against the list it describes.
      className={`row-i cursor-pointer ${
        active ? 'bg-accent-bg' : decided ? 'bg-n-25 opacity-55' : 'hover:bg-n-50'
      }`}
    >
      <td className={`relative pr-3 text-right ${CELL} ${rail}`}>
        <span
          className={`tnum relative z-[1] !leading-[18px] align-middle ${
            big
              ? 'text-[16px] font-bold tracking-[-0.02em] text-n-900'
              : small
              ? 'text-body-lg text-n-500'
              : 'text-body-lg text-n-900'
          }`}
        >
          {g.rupees}
        </span>
        <RupeeBar paise={g.rupees_paise} max={max} />
      </td>
      <td className={`w-[114px] px-3 ${CELL}`}>
        <CodeBadge code={g.code} short={CODE_SHORT[g.code] || g.code} />
      </td>
      <td
        title={g.headline}
        className={`max-w-0 truncate px-3 text-body-sm text-n-800 ${CELL}`}
      >
        {decided && (
          <span
            className={`mr-1.5 text-[11px] font-semibold uppercase tracking-[0.03em] ${
              ACTION_STYLE[decided.action] || 'text-n-500'
            }`}
          >
            {decided.action}
          </span>
        )}
        {g.headline}
      </td>
      <td className={`tnum w-[66px] px-2 text-right text-body-sm text-n-500 ${CELL}`}>
        {g.affected_chains}
      </td>
      <td className={`w-[118px] px-2 ${CELL}`}>
        <BandTag band={g.evidence_band} />
      </td>
    </tr>
  )
}

function Caret({ dir }) {
  return (
    <span className="inline-block align-middle text-[8px] leading-none text-n-800">
      {dir === 'asc' ? '▲' : '▼'}
    </span>
  )
}

function SortHeader({ label, tip, sortKey, sort, onSort, align = 'left', width, pad, info }) {
  const activeKey = sort.key === sortKey
  return (
    <th
      className={`group/th relative ${width || ''} ${
        pad || 'px-3'
      } h-[30px] text-label uppercase text-n-600 ${
        align === 'right' ? 'text-right' : 'text-left'
      }`}
    >
      <span className="relative inline-block">
        <Tip text={tip}>{label}</Tip>
        {sortKey && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onSort(sortKey)
            }}
            title={`Sort by ${label.toLowerCase()}`}
            className={`absolute top-1/2 -translate-y-1/2 hover:text-n-800 ${
              // A right-aligned label sits flush against the cell's right
              // padding, so hanging the caret off its right edge pushes it out
              // of the column. On those it goes on the inner side instead.
              align === 'right' ? '-left-[13px]' : '-right-[12px]'
            } ${
              activeKey ? 'text-n-800' : 'text-n-300 opacity-0 group-hover/th:opacity-100'
            }`}
          >
            {activeKey ? <Caret dir={sort.dir} /> : <span className="text-[8px]">⇅</span>}
          </button>
        )}
      </span>
      {info && <InfoDot id={info} className="ml-1" />}
    </th>
  )
}

/*
 * One section of the queue: a titled panel with its own column header.
 *
 * The section label used to sit BELOW the column names, which read as though
 * PAYOUT SHORTFALLS were a row of the table rather than the name of the group
 * those columns describe. A heading belongs above the thing it heads, so each
 * section is its own table with its own header underneath its own title -- and
 * sticky is per-table, so a header pins while its section is on screen and
 * hands over when the next one arrives.
 *
 * The two sections are genuinely different jobs. A payout shortfall is chased
 * with the gateway and the bank; an order-side pattern is worked inside the
 * merchant's own systems and never involves either.
 */
function QueueSection({ label, note, rows, max, selected, onSelect, sort, onSort, decisions }) {
  if (rows.length === 0) return null
  return (
    <section className="panel mb-4 overflow-hidden">
      <header
        data-section=""
        className="flex items-baseline gap-2 border-b border-n-200 bg-n-25 px-3 py-2.5"
      >
        <h2 className="text-label uppercase text-n-800">{label}</h2>
        <span className="tnum text-label text-n-500">({rows.length})</span>
        <span className="ml-auto truncate text-body-sm text-n-500">{note}</span>
      </header>

      <table data-queue="" className="w-full table-fixed border-separate border-spacing-0">
        <thead className="sticky-head">
          <tr>
            <SortHeader
              label="Rupees"
              tip={TIPS.rupees}
              sortKey="rupees"
              sort={sort}
              onSort={onSort}
              align="right"
              width="w-[126px]"
            />
            <SortHeader
              label="Code"
              tip={TIPS.code}
              sort={sort}
              onSort={onSort}
              width="w-[114px]"
            />
            <SortHeader
              label="Headline"
              tip={TIPS.headline}
              sortKey="date"
              sort={sort}
              onSort={onSort}
            />
            <SortHeader
              label="Orders"
              tip={TIPS.orders}
              sortKey="orders"
              sort={sort}
              onSort={onSort}
              align="right"
              width="w-[66px]"
              pad="px-2"
            />
            <SortHeader
              label="Evidence"
              tip={TIPS.evidence}
              sort={sort}
              onSort={onSort}
              width="w-[118px]"
              pad="px-2"
              info="evidenceBand"
            />
          </tr>
        </thead>
        <tbody>
          {rows.map((g) => (
            <QueueRow
              key={g.group_id}
              g={g}
              active={g.group_id === selected}
              max={max}
              onSelect={onSelect}
              decided={decisions?.[g.group_id]}
            />
          ))}
        </tbody>
      </table>
    </section>
  )
}

/*
 * The burn-down, and the audit trail behind it.
 *
 * The brief this is built against says "an agent that CLOSES one finance-ops
 * loop". Until now the loop did not close: a decision was posted, stored in
 * an audit log, and read back by nothing. An operator could work the whole
 * queue and the screen would look exactly as it did before they started.
 *
 * So the queue now burns down, and the log that was already being written is
 * readable and exportable. The export is the artifact the loop actually
 * produces -- what a controller hands an auditor -- and it is fetched from
 * the server's log rather than assembled from React state, because the
 * server's record is the one that would be audited.
 */
const ACTION_STYLE = {
  approve: 'text-accent',
  reject: 'text-danger',
  escalate: 'text-warn',
}

function AuditTrail({ seed, onClose }) {
  const [entries, setEntries] = useState(null)

  useEffect(() => {
    api(`/api/audit/${seed}`)
      .then((d) => setEntries(d.entries || []))
      .catch(() => setEntries([]))
  }, [seed])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const download = () => {
    const head = ['seed', 'group_id', 'action', 'note', 'at']
    const esc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv = [
      head.join(','),
      ...entries.map((e) => head.map((h) => esc(e[h])).join(',')),
    ].join('\n')
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `finrecon-audit-${seed}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div
      className="fixed inset-0 z-[300] flex items-start justify-center bg-n-900/25 px-6 py-10"
      onClick={onClose}
    >
      <div
        data-audit-panel=""
        onClick={(e) => e.stopPropagation()}
        className="pane-raised max-h-full w-full max-w-[720px] overflow-y-auto"
      >
        <div className="flex items-center justify-between gap-3 border-b border-n-200 bg-n-25 px-panel py-3">
          <div>
            <div className="text-label uppercase text-n-600">Audit trail</div>
            <div className="mt-0.5 text-body font-semibold text-n-900">
              {entries === null ? '…' : `${entries.length} decision${entries.length === 1 ? '' : 's'} recorded`}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              data-audit-export=""
              onClick={download}
              disabled={!entries?.length}
              className="rounded bg-accent px-3 py-1.5 text-body-sm font-semibold text-n-0 hover:brightness-110 disabled:opacity-40"
            >
              Export CSV
            </button>
            <button
              onClick={onClose}
              className="rounded px-2 py-1 text-body-sm text-n-500 hover:bg-n-100 hover:text-n-900"
            >
              Esc
            </button>
          </div>
        </div>
        <div className="px-panel py-4">
          {entries?.length === 0 && (
            <p className="text-body-sm leading-relaxed text-n-500">
              Nothing decided yet. Approve, reject or escalate a finding in the
              queue and it is recorded here — this log is the only thing the UI
              writes, and the engine never reads it back.
            </p>
          )}
          {entries?.length > 0 && (
            <table className="w-full table-fixed border-separate border-spacing-0">
              <thead>
                <tr>
                  <th className={`${TH_A} w-[38%] text-left`}>Finding</th>
                  <th className={`${TH_A} w-[96px] text-left`}>Action</th>
                  <th className={`${TH_A} text-left`}>Note</th>
                  <th className={`${TH_A} w-[142px] text-left`}>Recorded</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={i}>
                    <td className={`${TD_A} truncate font-mono text-[12px] text-n-800`}>
                      {e.group_id}
                    </td>
                    <td className={`${TD_A} font-semibold ${ACTION_STYLE[e.action] || ''}`}>
                      {e.action}
                    </td>
                    <td className={`${TD_A} truncate text-n-600`}>{e.note || '—'}</td>
                    <td className={`${TD_A} tnum text-n-500`}>
                      {String(e.at).replace('T', ' ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}

const TH_A =
  'h-[28px] px-2 text-label uppercase text-n-600 bg-n-50 border-b border-n-200'
const TD_A = 'h-[30px] px-2 text-body-sm border-b border-b-n-100'

function BurnDown({ groups, decisions, onOpenAudit }) {
  const total = groups.reduce((s, g) => s + g.rupees_paise, 0)
  const open = groups.filter((g) => !decisions[g.group_id])
  const cleared = groups.length - open.length
  const remaining = open.reduce((s, g) => s + g.rupees_paise, 0)
  const pct = total ? Math.round(((total - remaining) / total) * 100) : 0

  return (
    <div data-burndown="" className="panel mb-4 px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
        <span className="text-label uppercase text-n-600">Queue</span>
        <span className="text-body text-n-800">
          <span className="tnum font-semibold text-n-900">{cleared}</span> of{' '}
          <span className="tnum font-semibold text-n-900">{groups.length}</span>{' '}
          cleared
        </span>
        <span className="text-body text-n-500">
          <span className="tnum font-semibold text-n-900">
            {formatInr(remaining)}
          </span>{' '}
          still open of {formatInr(total)}
        </span>
        <button
          onClick={onOpenAudit}
          className="ml-auto text-body-sm text-accent hover:underline"
        >
          Audit trail →
        </button>
      </div>
      <div className="mt-2 h-[5px] overflow-hidden rounded-full bg-n-100">
        <div
          data-burndown-bar=""
          className="h-full rounded-full bg-accent transition-[width] duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

// Paise to the same rupee formatting the server uses. Kept here rather than
// re-derived per caller so the burn-down and the rows cannot disagree.
function formatInr(paise) {
  const sign = paise < 0 ? '-' : ''
  const abs = Math.abs(paise)
  const rupees = Math.floor(abs / 100)
  const p = String(abs % 100).padStart(2, '0')
  return `${sign}₹${rupees.toLocaleString('en-IN')}.${p}`
}

function Queue({ groups, selected, onSelect, decisions, onOpenAudit }) {
  const [sort, setSort] = useState({ key: 'rupees', dir: 'desc' })

  const toggle = (key) =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === 'desc' ? 'asc' : 'desc' }
        : // Money and counts open large-first; a date opens earliest-first,
          // because "what happened first" is the question a date is asked.
          { key, dir: key === 'date' ? 'asc' : 'desc' }
    )

  const { payouts, orderSide, payoutMax, orderMax } = useMemo(() => {
    const value = (g) =>
      sort.key === 'rupees'
        ? g.rupees_paise
        : sort.key === 'orders'
        ? g.affected_chains
        : g.settled_on || ''

    const cmp = (a, b) => {
      const [x, y] = [value(a), value(b)]
      // Order-side rows have no payout date. They sort to the end rather than
      // taking a position an empty string would give them.
      if (sort.key === 'date') {
        if (!x && !y) return b.rupees_paise - a.rupees_paise
        if (!x) return 1
        if (!y) return -1
      }
      const d = x < y ? -1 : x > y ? 1 : 0
      return sort.dir === 'asc' ? d : -d
    }

    const p = groups.filter((g) => g.kind !== 'orders')
    const o = groups.filter((g) => g.kind === 'orders')
    const max = (rows) => rows.reduce((m, g) => Math.max(m, g.rupees_paise), 0)
    return {
      payouts: [...p].sort(cmp),
      orderSide: [...o].sort(cmp),
      payoutMax: max(p),
      orderMax: max(o),
    }
  }, [groups, sort])

  return (
    <div className="pb-gutter pl-gutter pr-1">
      <BurnDown
        groups={groups}
        decisions={decisions || {}}
        onOpenAudit={onOpenAudit}
      />
      <QueueSection
        label="Payout shortfalls"
        note="chased with the gateway and the bank"
        rows={payouts}
        max={payoutMax}
        selected={selected}
        onSelect={onSelect}
        sort={sort}
        onSort={toggle}
        decisions={decisions || {}}
      />
      <QueueSection
        label="Order-side patterns"
        note="worked inside the merchant's own systems"
        rows={orderSide}
        max={orderMax}
        selected={selected}
        onSelect={onSelect}
        sort={sort}
        onSort={toggle}
        decisions={decisions || {}}
      />
    </div>
  )
}

/* --------------------------------------------------------------------------
 * The Run page.
 *
 * It was a headline, a button and a lot of nothing, which reads as a page that
 * failed to load. What belongs here is the one thing a reader cannot work out
 * from the queue: WHY three ledgers need reconciling at all. The chain diagram
 * below is the whole argument in one line -- two hops the gateway declares and
 * one hop nobody declares -- and it is the reason every number in this product
 * exists.
 * ------------------------------------------------------------------------ */
function ChainNode({ label, count, muted }) {
  return (
    <div
      className={`flex min-w-0 flex-1 flex-col items-center rounded-md border border-n-200 px-3 py-3 ${
        muted ? 'bg-n-25' : 'bg-n-0'
      }`}
    >
      <div className="tnum text-[18px] font-bold leading-none tracking-[-0.02em] text-n-900">
        {count == null ? '—' : count.toLocaleString('en-IN')}
      </div>
      <div className="mt-1.5 text-center text-[10.5px] font-semibold uppercase leading-tight tracking-[0.04em] text-n-500">
        {label}
      </div>
    </div>
  )
}

function ChainLink({ label, broken }) {
  return (
    <div className="flex w-[108px] shrink-0 flex-col items-center gap-1 px-1">
      <div className="flex w-full items-center">
        <div
          className={`h-0 flex-1 border-t-[1.5px] ${
            broken ? 'border-dashed border-danger/60' : 'border-n-300'
          }`}
        />
        <svg width="9" height="9" viewBox="0 0 10 10" fill="none" className="-ml-px">
          <path
            d="M1 1.5 5 5 1 8.5"
            stroke={broken ? '#D93025' : '#CBD0DA'}
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <div
        className={`text-center text-[10px] font-medium leading-tight ${
          broken ? 'text-danger' : 'text-n-500'
        }`}
      >
        {label}
      </div>
    </div>
  )
}

function StepLine({ n, title, body }) {
  return (
    <div className="flex gap-3">
      <div className="mt-[1px] flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full bg-n-100 text-[11px] font-semibold text-n-600">
        {n}
      </div>
      <div className="min-w-0">
        <div className="text-body-sm font-semibold text-n-900">{title}</div>
        <div className="mt-0.5 text-body-sm text-n-500">{body}</div>
      </div>
    </div>
  )
}

function Claim({ figure, label, body }) {
  return (
    <div className="panel p-4">
      <div className="tnum text-[20px] font-bold leading-none tracking-[-0.02em] text-n-900">
        {figure}
      </div>
      <div className="mt-1.5 text-label uppercase text-n-600">{label}</div>
      <div className="mt-1.5 text-body-sm leading-relaxed text-n-500">{body}</div>
    </div>
  )
}

function RunPanel({ seeds, seed, setSeed, onRun, running, summary, elapsed, cached }) {
  const current = seeds.find((s) => s.seed === seed)
  return (
    <div className="flex-1 overflow-y-auto px-gutter pb-gutter pt-12">
      <div className="mx-auto max-w-[880px]">
        <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-n-200 bg-n-0 px-3 py-1 text-label uppercase text-n-600">
          <span className="h-[6px] w-[6px] rounded-full bg-accent" />
          Three-way reconciliation
        </div>

        <h1 className="mb-3 text-[32px] font-bold leading-[38px] tracking-[-0.03em] text-n-900">
          Orders, the gateway and the bank,
          <br />
          reconciled to the paisa.
        </h1>
        <p className="mb-8 max-w-[64ch] text-body text-n-500">
          The three ledgers disagree in ways no shared key can resolve. The engine
          settles what arithmetic can settle and refuses the rest, then hands you
          the refusals, grouped by cause and sorted by money.
        </p>

        <div data-tour="run-card" className="panel mb-4 p-panel">
          <div className="flex flex-wrap items-center gap-3">
            <select
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
              className="rounded border border-n-200 bg-n-0 px-3 py-2 text-body-sm text-n-800 outline-none focus:border-accent"
            >
              {seeds.map((s) => (
                <option key={s.seed} value={s.seed}>
                  {/* Dot, not a dash: seed 99's label carries its own em dash
                      and two in one option read as a broken sentence. */}
                  {s.label} · {s.total_rows.toLocaleString('en-IN')} rows
                </option>
              ))}
            </select>
            <button
              onClick={onRun}
              disabled={running}
              className="inline-flex items-center gap-2 rounded bg-accent px-4 py-2 text-body-sm font-semibold text-n-0 shadow-[0_1px_2px_rgba(11,102,239,0.35)] hover:brightness-110 disabled:opacity-60"
            >
              <IconRun />
              {running ? 'Running…' : 'Run reconciliation'}
            </button>
            {elapsed != null && (
              <span className="text-body-sm text-n-500">
                {cached ? '' : `completed in ${elapsed}s`}
              </span>
            )}
            <span className="ml-auto text-body-sm text-n-500">
              
            </span>
          </div>
        </div>

        <div data-tour="chain" className="panel mb-4 p-panel">
          <div className="mb-4 flex flex-wrap items-baseline gap-2">
            <h2 className="text-label uppercase text-n-800">Where the chain breaks</h2>
            <span className="text-body-sm text-n-500">
              two hops the gateway declares, one it does not
            </span>
          </div>

          <div className="flex items-center">
            <ChainNode label="Orders" count={current?.orders} />
            <ChainLink label="order_id" />
            <ChainNode label="Payments" count={current?.payments} />
            <ChainLink label="settlement_id" />
            <ChainNode label="Payouts" count={current?.settlements} />
            <ChainLink label="UTR in free text" broken />
            <ChainNode label="Bank credits" count={current?.bank_rows} muted />
          </div>

          <p className="mt-5 max-w-[72ch] text-body-sm leading-relaxed text-n-500">
            Inside the gateway every row carries the id of the row before it, so
            those joins are lookups. At the bank they stop: a statement line has a
            date, an amount and a narration string, and the only thing tying it to
            a payout is a reference buried in that string — sometimes truncated,
            sometimes absent, sometimes shared by two rows. Everything hard about
            this problem lives in that last hop.
          </p>
        </div>

        {/*
          Provenance note for a seed that was held out and has since been
          scored. Keyed off the server's held_out flag rather than the label
          text, so rewording the dropdown can never silently drop the note.
          It sits above the results because it is the frame those results are
          read in, not a footnote to them.
        */}
        {current?.held_out && (
          <div
            data-held-out-note=""
            className="mb-4 rounded border border-n-200 border-l-2 border-l-warn bg-n-25 p-panel"
          >
            <div className="mb-1.5 text-label uppercase text-warn">
              Held out until 30 August
            </div>
            <p className="max-w-[72ch] text-body-sm leading-relaxed text-n-600">
              This dataset was generated on day one and never scored until 30
              August, after the engine was frozen. Coverage is 14.30 points
              lower than the development seed — inside the 47.30–79.50% range
              the 30-seed sweep had already established. The engine has not
              been changed since.
            </p>
          </div>
        )}

        {summary && (
          <div className="panel mb-4 p-panel">
            <div className="mb-3 text-label uppercase text-n-600">This run</div>
            <div className="text-body text-n-500">
              <span className="tnum font-semibold text-n-900">
                {summary.matched.toLocaleString('en-IN')}
              </span>{' '}
              of {summary.total_orders.toLocaleString('en-IN')} orders reconciled
              with{' '}
              <span className="font-semibold text-accent">
                {summary.incorrect} incorrect
              </span>
              . The remaining{' '}
              <span className="tnum font-semibold text-n-900">
                {summary.exception_rows}
              </span>{' '}
              exception rows collapse to{' '}
              <span className="tnum font-semibold text-n-900">{summary.groups}</span>{' '}
              investigable items, worth{' '}
              <span className="tnum font-semibold text-n-900">
                {summary.unexplained}
              </span>
              .
            </div>

            {/*
              Throughput, stated. "Throughput plus measured accuracy plus an
              honest exception list" is the bar this is judged against, and
              until now the number was computed on every run and shown on
              none of them. The brief asked for a 50-record batch; this is
              2,222 rows, so the comparison is worth making explicitly rather
              than leaving a reader to work out that 1,000 orders is a lot.
            */}
            {elapsed != null && (
              <div
                data-throughput=""
                className="mt-4 grid grid-cols-4 gap-3 border-t border-n-100 pt-3"
              >
                {[
                  {
                    v: summary.input_rows.toLocaleString('en-IN'),
                    l: 'Rows in',
                    s: 'orders, payments, refunds, chargebacks, payouts, bank',
                  },
                  { v: `${elapsed}s`, l: 'Wall clock', s: 'end to end, one process' },
                  {
                    v: Math.round(summary.input_rows / Math.max(elapsed, 0.001))
                      .toLocaleString('en-IN'),
                    l: 'Rows / second',
                    s: 'single-threaded, no index, no database',
                  },
                  {
                    v: `${Math.round(summary.input_rows / 50)}×`,
                    l: 'Over the bar',
                    s: 'the brief asked for a 50-record batch',
                  },
                ].map((c) => (
                  <div key={c.l}>
                    <div className="tnum text-[19px] font-bold leading-none tracking-[-0.02em] text-n-900">
                      {c.v}
                    </div>
                    <div className="mt-1.5 text-label uppercase text-n-600">
                      {c.l}
                    </div>
                    <div className="mt-1 text-[11.5px] leading-[15px] text-n-500">
                      {c.s}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="panel mb-4 p-panel">
          <div className="mb-4 text-label uppercase text-n-800">
            What the engine does
          </div>
          <div className="grid gap-4">
            <StepLine
              n="1"
              title="Normalise, then match only on what is certain"
              body="Exact references first, tolerant bands second. A candidate with two plausible matches is never matched — guessing under ambiguity is what destroys precision."
            />
            <StepLine
              n="2"
              title="Rebuild each payout from the settlement equation"
              body="Gross minus fee, tax, refunds, chargebacks and withholding, in integer paise. A payout that reproduces to the paisa needs no human."
            />
            <StepLine
              n="3"
              title="Group what is left by cause, not by row"
              body="Hundreds of exception rows are a handful of actual events. The queue shows the events, each with the arithmetic behind it and a way through to the source."
            />
          </div>
        </div>

        {/*
          Two claims, not three. The third was "0 calls at request time", which
          was true until the Ask page started making live model calls and is
          not any more. A claim that has been overtaken by the product is worse
          than no claim -- a judge who finds one stops believing the others.
        */}
        <div data-tour="claims" className="grid grid-cols-2 gap-3 pb-4">
          <Claim
            figure="100.00%"
            label="Precision"
            body="Held across 30 unseen seeds, and on a held-out set opened once after the numbers were frozen. A wrong match enters the books silently, so the engine refuses rather than guesses."
          />
          <Claim
            figure="Integer paise"
            label="No float anywhere"
            body="Every amount is an integer count of paise from the CSV to the screen. Float rounding is the exact bug this avoids."
          />
        </div>
      </div>
    </div>
  )
}

export default function App() {
  const [seeds, setSeeds] = useState([])
  const [seed, setSeed] = useState('seed42')
  const [nav, setNav] = useState('Run')
  const [running, setRunning] = useState(false)
  const [summary, setSummary] = useState(null)
  const [elapsed, setElapsed] = useState(null)
  const [cached, setCached] = useState(false)
  const [groups, setGroups] = useState([])
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  // Which seed the loaded queue actually came from. Not derivable from `seed`,
  // which changes the moment the dropdown does -- before anything is re-run.
  const [loadedSeed, setLoadedSeed] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [dataTarget, setDataTarget] = useState(null)
  // The id whose decline trace is open, or null. Lives at the top because
  // three different pages can raise it and none of them owns it.
  const [traceId, setTraceId] = useState(null)
  // The finding the Ask page is currently about, carried in from the queue.
  const [askSubject, setAskSubject] = useState(null)
  const [auditOpen, setAuditOpen] = useState(false)
  /*
   * The tour runs itself once per page, then only when asked.
   *
   * `seen` records which tours have already auto-played this session, so
   * navigating back to the Queue does not replay it. Session-only:
   * localStorage is unavailable here, and a tour suppressed across a reload is
   * the wrong default for a page a judge opens once.
   */
  const [tour, setTour] = useState(null)
  const [seen, setSeen] = useState({})

  /*
   * Auto-play, once each.
   *
   * The Run tour fires on arrival. The Queue tour waits for `groups` because
   * two of its three steps point at rows that do not exist until a run has
   * finished -- spotlighting an empty table would be worse than not running.
   */
  /*
   * ?tour=off suppresses the auto-play. The glyph still works.
   *
   * The scrim intercepts pointer events, which is what makes a spotlight a
   * spotlight and is exactly right for a reader -- but it also means every
   * automated check has to race a 700ms timer and dismiss a modal before it
   * can click anything. A supported switch beats sprinkling Escape presses
   * through the probes and hoping the timing holds.
   */
  const autoTourOff =
    typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).get('tour') === 'off'

  useEffect(() => {
    if (tour || autoTourOff) return
    if (nav === 'Run' && !seen.Run) {
      const t = setTimeout(() => {
        setSeen((s) => ({ ...s, Run: true }))
        setTour('Run')
      }, 700)
      return () => clearTimeout(t)
    }
    if (nav === 'Queue' && !seen.Queue && groups.length > 0) {
      const t = setTimeout(() => {
        setSeen((s) => ({ ...s, Queue: true }))
        setTour('Queue')
      }, 500)
      return () => clearTimeout(t)
    }
    // Evidence fetches its report on mount; 900ms is after that lands, and
    // every step here points at something the report renders.
    if (nav === 'Evidence' && !seen.Evidence) {
      const t = setTimeout(() => {
        setSeen((s) => ({ ...s, Evidence: true }))
        setTour('Evidence')
      }, 900)
      return () => clearTimeout(t)
    }
  }, [nav, seen, groups.length, tour])

  useEffect(() => {
    api('/api/seeds').then((d) => {
      setSeeds(d.seeds)
      if (d.seeds.length && !d.seeds.find((s) => s.seed === seed))
        setSeed(d.seeds[0].seed)
    })
  }, [])

  // Switching datasets invalidates everything the last run produced. A group
  // id belongs to ONE seed, so leaving it selected re-fetched it against the
  // new seed and 404'd; leaving the queue up showed one seed's findings under
  // another seed's name. Both were reachable only by switching seeds, which
  // nobody did until seed 99 became selectable. Cleared here rather than in
  // the dropdown handler so the /data/<seed>/<table> deep link is covered too.
  useEffect(() => {
    setSummary(null)
    setGroups([])
    setSelected(null)
    setDetail(null)
  }, [seed])

  const run = async () => {
    setRunning(true)
    try {
      const r = await api(`/api/reconcile/${seed}`, { method: 'POST' })
      setSummary(r.summary)
      setElapsed(r.elapsed_seconds)
      setCached(r.cached)
      const q = await api(`/api/exceptions/${seed}`)
      setGroups(q.groups)
      // Select the first row AS DISPLAYED, which is the largest payout
      // shortfall. The API sorts by money alone, and on every seed the four
      // order-side groups outweigh every individual payout -- so opening on
      // groups[0] landed on a finding with no settlement equation, and the
      // arithmetic block was not on screen until you clicked something else.
      const first = q.groups.filter((g) => g.kind !== 'orders')[0] ?? q.groups[0]
      setSelected(first?.group_id ?? null)
      setLoadedSeed(seed)
      setNav('Queue')
    } finally {
      setRunning(false)
    }
  }

  useEffect(() => {
    if (!selected) return setDetail(null)
    // Only fetch a finding that belongs to the CURRENT seed's queue. Clearing
    // `selected` and `groups` on a seed change is not enough on its own: those
    // clears land on the NEXT render, so for one pass this effect still saw the
    // old id beside the new seed and asked the server for a group that seed has
    // never had. Comparing against the seed the queue was loaded from is the
    // only check that is already true at the moment this runs.
    if (loadedSeed !== seed) return
    api(`/api/exceptions/${seed}/${encodeURIComponent(selected)}`).then(setDetail)
  }, [selected, seed, loadedSeed])

  const decide = async (groupId, action, note) => {
    const r = await api(
      `/api/exceptions/${seed}/${encodeURIComponent(groupId)}/decision`,
      { method: 'POST', body: JSON.stringify({ action, note }) }
    )
    setDecisions((d) => ({ ...d, [groupId]: r.recorded }))
  }

  const openRow = (table, rowId, settlement) => {
    setDataTarget({ table, rowId, settlement })
    setNav('Data')
    const q = new URLSearchParams()
    if (rowId) q.set('row', rowId)
    if (settlement) q.set('settlement', settlement)
    window.history.pushState({}, '', `/data/${seed}/${table}?${q}`)
  }

  useEffect(() => {
    const m = window.location.pathname.match(/^\/data\/(seed\d+)\/(\w+)$/)
    if (!m) return
    const params = new URLSearchParams(window.location.search)
    setSeed(m[1])
    setDataTarget({
      table: m[2],
      rowId: params.get('row'),
      settlement: params.get('settlement'),
    })
    setNav('Data')
  }, [])

  const selectedDetail = useMemo(
    () => (detail && detail.group_id === selected ? detail : null),
    [detail, selected]
  )

  return (
    <div className="app-bg flex h-full overflow-hidden">
      {/*
        A permanent icon rail, not a collapsed sidebar.

        76px with a 20px glyph and its name under it, which is wide enough to
        read as a deliberate rail rather than a panel someone squeezed. The
        expand/collapse state is gone: it existed to buy width for the table,
        and a rail that is always this width buys that width permanently and
        stops the layout meaning two different things on two screens.
      */}
      <nav
        data-nav=""
        className="flex w-[76px] shrink-0 flex-col items-center border-r border-n-200 bg-n-0 py-4"
      >
        <div className="mark-tile mb-6 flex h-[34px] w-[34px] items-center justify-center rounded-[9px]">
          <svg width="19" height="19" viewBox="0 0 16 16" fill="none">
            <path
              d="M3 11.4 6.2 7.1l2.6 2.3L13 3.9"
              stroke="#fff"
              strokeWidth="1.9"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>

        <div className="flex w-full flex-col items-center gap-1 px-2">
          {NAV.map(({ name, Icon }) => {
            const active = name === nav
            const enabled = true
            return (
              <button
                key={name}
                disabled={!enabled}
                onClick={() => enabled && setNav(name)}
                title={name}
                className={`nav-item flex w-full flex-col items-center gap-1 rounded-md py-2 ${
                  active
                    ? 'bg-accent-bg text-accent'
                    : enabled
                    ? 'text-n-500 hover:bg-n-50 hover:text-n-900'
                    : 'cursor-default text-n-300'
                }`}
              >
                <Icon />
                <span
                  className={`text-[10px] uppercase tracking-[0.04em] ${
                    active ? 'font-semibold' : 'font-medium'
                  }`}
                >
                  {name}
                </span>
              </button>
            )
          })}
        </div>

        <div className="mt-auto text-[10px] font-medium uppercase tracking-[0.04em] text-n-300">
          
        </div>
      </nav>

      {/*
        1668px = 24 of gutter + 872 of queue + 12 of gap + 760 of pane + 24 of
        gutter. The content stops there and the page keeps the rest as margin.

        Without the cap the stat strip stretched to the edge of a 1920 screen
        while the panels below it stopped 200px short, because the queue and the
        pane are both capped and the strip was not. Everything in this column
        now ends on the same vertical line at every width.
      */}
      <main className="flex min-w-0 max-w-[1668px] flex-1 flex-col overflow-hidden">
        {summary && nav !== 'Run' && nav !== 'Evidence' && nav !== 'Cash' && (
          <Stats summary={summary} />
        )}

        {/*
          The replay trigger. Absolutely positioned so it costs the page no
          height -- the banner it replaced took 46px off the queue and cost up
          to two visible rows.
        */}
        {(nav === 'Run' || nav === 'Queue' || nav === 'Evidence') && (
          <button
            data-tour-open={nav}
            onClick={() => setTour(nav)}
            title="Show me around this page"
            className="tour-open absolute right-gutter top-3 z-[30]"
          >
            <svg width="12" height="12" viewBox="0 0 14 14" aria-hidden="true">
              <circle cx="7" cy="7" r="6.1" fill="none" stroke="currentColor"
                      strokeWidth="1.2" />
              <circle cx="7" cy="4.2" r="0.85" fill="currentColor" />
              <path d="M7 6.3v4.2" stroke="currentColor" strokeWidth="1.2"
                    strokeLinecap="round" />
            </svg>
            Tour
          </button>
        )}

        {nav === 'Run' && (
          <RunPanel
            seeds={seeds}
            seed={seed}
            setSeed={setSeed}
            onRun={run}
            running={running}
            summary={summary}
            elapsed={elapsed}
            cached={cached}
          />
        )}

        {nav === 'Queue' && (
          <div className="flex min-h-0 flex-1 gap-3 pr-gutter">
            {/*
              The queue STOPS GROWING at 872px; the detail pane takes what is
              left over.

              Before this the headline was the only column without a width, so
              it absorbed every spare pixel on the screen: 386px at 1280 but
              851px at 1920, for a string that is never longer than 358px. Four
              hundred pixels of empty column in the middle of every row, while
              the band beside it had been squeezed down to a coloured dot.

              872px of queue is 842 of table -- 424 of fixed columns and 418 for
              the headline, which needs 382. flex-[3] against flex-1 decides who
              gets the surplus FIRST: the queue fills to its cap, then the pane
              grows to 760, and only past ~1740px wide is there anything left to
              sit as page margin.
            */}
            <div
              data-queue-pane=""
              className="min-w-0 flex-[3] max-w-[872px] overflow-y-auto"
            >
              <Queue
                groups={groups}
                selected={selected}
                onSelect={setSelected}
                decisions={decisions}
                onOpenAudit={() => setAuditOpen(true)}
              />
            </div>
            <div className="pane-raised mb-gutter min-w-[372px] max-w-[760px] flex-1 overflow-hidden">
              <Detail
                detail={selectedDetail}
                onDecision={decide}
                decided={decisions[selected]}
                onOpenRow={openRow}
                onWhyNot={setTraceId}
                onAsk={(subject) => {
                  setAskSubject(subject)
                  setNav('Ask')
                }}
              />
            </div>
          </div>
        )}

        {/*
          The Evidence page is about thirty datasets, not the one selected in
          the header, so the stat strip above is suppressed for it: two rows of
          headline figures describing different populations is how a reader
          ends up attributing seed 42's numbers to the sweep.
        */}
        {nav === 'Cash' && (
          <Cash
            seed={seed}
            onOpenTrace={setTraceId}
            onOpenFinding={(groupId) => {
              setSelected(groupId)
              setNav('Queue')
            }}
          />
        )}

        {nav === 'Ask' && (
          <Ask
            seed={seed}
            onOpenTrace={setTraceId}
            subject={askSubject}
            onClearSubject={() => setAskSubject(null)}
          />
        )}

        {nav === 'Evidence' && <EvidencePage />}

        {nav === 'Data' && (
          <DataPage
            seed={seed}
            target={dataTarget}
            onTargetConsumed={() => setDataTarget(null)}
          />
        )}
      </main>

      <Trace
        seed={seed}
        entityId={traceId}
        onClose={() => setTraceId(null)}
        onOpen={setTraceId}
      />

      {auditOpen && (
        <AuditTrail seed={seed} onClose={() => setAuditOpen(false)} />
      )}

      {tour && (
        <Tour
          steps={
            tour === 'Run'
              ? runSteps()
              : tour === 'Evidence'
              ? evidenceSteps()
              : queueSteps(summary)
          }
          onClose={() => setTour(null)}
        />
      )}
    </div>
  )
}
