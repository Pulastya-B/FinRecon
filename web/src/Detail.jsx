import React, { useState } from 'react'
import { InfoDot } from './Info.jsx'

// Flat fills, no gradient, 4px radius. Severity is carried by hue alone, and
// the neutral badges draw from the ramp rather than from a fourth grey nobody
// else uses.
const CODE_STYLE = {
  MISSING_IN_BANK: 'bg-[#FDECEA] text-danger',
  CHARGEBACK_UNPOSTED: 'bg-[#FBF1E3] text-warn',
  AMOUNT_VARIANCE_UNEXPLAINED: 'bg-[#FBF1E3] text-warn',
  DUPLICATE_PAYMENT: 'bg-n-100 text-n-600',
  ORDER_UNPAID: 'bg-n-100 text-n-600',
  AMBIGUOUS_MULTI_CANDIDATE: 'bg-n-100 text-n-600',
  TIMING_PENDING: 'bg-n-100 text-n-600',
  UNKNOWN: 'bg-n-100 text-n-600',
}

/*
 * `short` abbreviates the code to one word for the queue, where the full
 * 27-character codes were crushing the headline column. The full code stays on
 * the title attribute: this one is a genuine last-resort lookup, not something
 * a reader needs, so the browser's own tooltip is the right weight for it.
 */
export function CodeBadge({ code, short }) {
  return (
    <span
      title={short ? code.replace(/_/g, ' ') : undefined}
      className={`inline-block align-middle rounded px-1.5 py-0.5 text-[10.5px] font-semibold uppercase leading-[14px] tracking-[0.04em] ${
        CODE_STYLE[code] || CODE_STYLE.UNKNOWN
      }`}
    >
      {short || code.replace(/_/g, ' ')}
    </span>
  )
}

/*
 * The band, spelled out, in the queue.
 *
 * It was a coloured dot, because CIRCUMSTANTIAL is 100px of text at 10.5px and
 * the headline column was absorbing every spare pixel on the screen. A dot
 * makes the reader learn a colour key to read a word, which is a poor trade
 * for a column whose whole job is to say how much to trust the row.
 *
 * The width comes from a MEASUREMENT, not an estimate: CIRCUMSTANTIAL renders
 * at 96.36px in Inter at 10px/700/0.04em, so the column is 118px -- 96 of text,
 * 16 of padding and 6 of cushion. Inter is about 11% wider than the system
 * fallback and this column has been clipped twice by arithmetic done on the
 * wrong font. The cushion is there because it was clipped a third time by
 * padding that was 12px when the markup asked for 8.
 *
 * `truncate` is deliberate on a column sized to never truncate. Without
 * nowrap the word would WRAP if it ever outgrew the column -- the row would
 * silently grow to two lines and the clipping probe would pass, because
 * scrollWidth equals clientWidth when text wraps. Nowrap is what makes the
 * failure detectable.
 */
const BAND_TEXT = {
  STRONG: 'text-accent',
  CIRCUMSTANTIAL: 'text-warn',
  REFUSE: 'text-n-500',
}

export function BandTag({ band }) {
  if (!band) return <span className="text-n-300">—</span>
  return (
    <span
      className={`block truncate text-[10px] font-bold uppercase leading-[18px] tracking-[0.04em] ${
        BAND_TEXT[band] || 'text-n-500'
      }`}
    >
      {band}
    </span>
  )
}

// In the detail pane, where the width exists, the band is the word.
export function Band({ band }) {
  if (!band) return <span className="text-n-500">—</span>
  if (band === 'STRONG')
    return <span className="font-semibold text-n-900">STRONG</span>
  if (band === 'REFUSE')
    return <span className="italic text-n-500">REFUSE</span>
  return <span className="text-n-500">{band}</span>
}

/*
 * Which raw file each kind of id lives in.
 *
 * Order ids are deliberately absent. In prose an order id is almost always a
 * bystander -- an example of the pattern, not the thing to open -- and
 * linkifying every one of them would turn a paragraph into a link farm and
 * bury the ids that ARE worth opening. Order ids get links in the order list
 * below, where opening them is the point.
 */
const ID_TABLE = [
  [/^setl_/, 'settlements'],
  [/^bank_/, 'bank'],
  [/^rfnd_/, 'refunds'],
  [/^cb_/, 'chargebacks'],
  [/^pay_/, 'payments'],
]

const ID_RE = /\b(?:setl_[0-9]{8}_[0-9]+|bank_[0-9]+|rfnd_[0-9]+|cb_[0-9]+|pay_[0-9]+)\b/g

function tableFor(id) {
  for (const [re, table] of ID_TABLE) if (re.test(id)) return table
  return null
}

/*
 * Prose with its record ids made clickable.
 *
 * The explanation names the refund it is consistent with; being able to open
 * that refund in the source is the difference between a claim and a claim you
 * can check. Split on the id pattern rather than replacing into HTML: this
 * text comes from a template or a language model, and building markup out of
 * it by string substitution is how prose becomes an injection point.
 */
function LinkedProse({ text, onOpenRow, className = '' }) {
  const parts = []
  let last = 0
  for (const m of text.matchAll(ID_RE)) {
    const table = tableFor(m[0])
    if (m.index > last) parts.push(text.slice(last, m.index))
    parts.push(
      table ? (
        <button
          key={`${m.index}-${m[0]}`}
          onClick={() => onOpenRow?.(table, m[0])}
          className="font-mono text-[0.92em] text-accent hover:underline"
        >
          {m[0]}
        </button>
      ) : (
        m[0]
      )
    )
    last = m.index + m[0].length
  }
  parts.push(text.slice(last))
  return <div className={`whitespace-pre-line ${className}`}>{parts}</div>
}

/*
 * The shape of an order-side finding, stated as counts.
 *
 * This replaced a sentence apologising for the absent arithmetic ("there is no
 * settlement equation to reconstruct"). The absence is not a shortcoming to
 * excuse -- it is the finding. An order with no payment and no bank row is
 * exactly what only a three-way comparison can see, and three counts say that
 * faster and with more authority than an apology did.
 */
function ShapeLine({ shape }) {
  if (!shape) return null
  const cells = [
    ['orders', shape.orders],
    ['payments', shape.payments],
    ['bank rows', shape.bank_rows],
  ]
  return (
    <div className="flex items-stretch rounded-md border border-n-200 bg-n-25">
      {cells.map(([label, n], i) => (
        <div
          key={label}
          className={`flex-1 px-4 py-3 ${i > 0 ? 'border-l border-n-200' : ''}`}
        >
          <div className="tnum text-[24px] font-bold leading-none tracking-[-0.02em] text-n-900">
            {n}
          </div>
          <div className="mt-1 text-label uppercase text-n-500">
            {label}
          </div>
        </div>
      ))}
    </div>
  )
}

/*
 * The arithmetic block. The single most important element in the product.
 *
 * A spreadsheet can show a settlement total. What it cannot do is show the
 * total DECOMPOSED, every term aligned on the decimal, with the bank figure
 * set directly beneath the computed one so the eye lands on the gap without
 * being told where to look. That comparison is the whole argument of the
 * engine, so it gets monospace, tabular figures, real vertical room, the top
 * of the pane, and a border one step darker than anything else on it.
 *
 * The terms are 13px and quiet; the three lines that follow them are 14px and
 * loud. The decomposition is supporting evidence, and expected-versus-bank is
 * the claim.
 */
function Arithmetic({ arithmetic, shape }) {
  const { terms, expected, actual, gap, exact, n_payments } = arithmetic
  if (!terms || terms.length === 0) return <ShapeLine shape={shape} />

  return (
    <div className="rounded-md border border-n-200 bg-n-25 px-5 py-4">
      <div className="mb-3 flex items-center gap-1.5 text-label uppercase text-n-500">
        <span>Settlement arithmetic · {n_payments} payments</span>
        <InfoDot id="arithmetic" />
      </div>
      <table className="w-full font-mono">
        <tbody>
          {terms.map((t) => (
            <tr key={t.label}>
              <td className="py-[3px] pr-6 text-body-sm text-n-500">{t.label}</td>
              <td className="tnum py-[3px] text-right text-body-sm text-n-800">
                {t.display}
              </td>
            </tr>
          ))}
          <tr>
            <td colSpan={2} className="pt-2">
              <div className="border-t border-n-200" />
            </td>
          </tr>
          <tr>
            <td className="py-[5px] pr-6 text-body font-semibold text-n-900">
              expected
            </td>
            <td className="tnum py-[5px] text-right text-body font-semibold text-n-900">
              {expected}
            </td>
          </tr>
          <tr>
            <td className="py-[5px] pr-6 text-body font-semibold text-n-900">
              bank
            </td>
            <td
              className={`tnum py-[5px] text-right text-body font-semibold ${
                exact ? 'text-n-900' : 'text-danger'
              }`}
            >
              <span className="inline-flex items-center justify-end gap-1.5">
                {/* The check sits ON the bank line, where the claim is made:
                    this figure and the computed one are the same to the paisa.
                    As a footnote underneath it was a comment about the block;
                    here it is a verdict on the comparison. */}
                {exact && (
                  <svg
                    width="13"
                    height="13"
                    viewBox="0 0 16 16"
                    fill="none"
                    className="text-accent"
                  >
                    <path
                      d="M3 8.5l3.2 3.2L13 5"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                )}
                {actual === null ? 'not found' : actual}
              </span>
            </td>
          </tr>
          {!exact && gap !== null && (
            <tr>
              <td className="py-[5px] pr-6 text-body font-semibold text-danger">
                short by
              </td>
              <td className="tnum py-[5px] text-right text-body font-bold text-danger">
                {gap}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {exact && (
        <div className="mt-2 text-body-sm font-medium text-accent">
          Reconstructed to the paisa
        </div>
      )}
    </div>
  )
}

// Which raw file a source record lives in. The roles are produced by
// service/engine.py; anything not listed here stays inert text rather than
// becoming a link to a page that would 404.
const ROLE_TABLE = {
  settlement: 'settlements',
  'bank row': 'bank',
  'attributed refund': 'refunds',
  'attributed chargeback': 'chargebacks',
  'attributed payment': 'payments',
}

function EvidenceLine({ evidence }) {
  const bits = []
  if (evidence.identified_by) bits.push(`Identified by ${evidence.identified_by}`)
  if (evidence.level) bits.push(`${evidence.level} ${evidence.attributed_item || ''}`.trim())
  if (evidence.candidates_searched != null)
    bits.push(`${evidence.candidates_searched.toLocaleString('en-IN')} candidates searched`)
  if (evidence.expected_accidental_fits != null)
    bits.push(`expected accidental fits ${evidence.expected_accidental_fits}`)
  if (evidence.band) bits.push(evidence.band)
  if (bits.length === 0) return null
  return (
    <div className="text-body-sm text-n-500">
      {bits.join(' · ')}
    </div>
  )
}

export default function Detail({
  detail, onDecision, decided, onOpenRow, onWhyNot, onAsk,
}) {
  const [note, setNote] = useState('')
  if (!detail)
    return (
      <div className="flex h-full items-center justify-center px-8 text-center text-body-sm text-n-500">
        Select an item from the queue to see the arithmetic behind it.
      </div>
    )

  const act = (action) => {
    onDecision(detail.group_id, action, note)
    setNote('')
  }

  const isPayout = Boolean(detail.settlement_id)

  return (
    <div className="h-full overflow-y-auto px-panel py-panel">
      <div className="mb-1 flex items-center gap-2">
        <CodeBadge code={detail.code} />
        {detail.identified_by && (
          <span className="text-body-sm text-n-500">
            identified by {detail.identified_by}
          </span>
        )}
      </div>
      <h2 className="text-title text-n-900">
        {detail.headline}
      </h2>
      <div className="mb-4 mt-0.5 text-body-sm text-n-500">
        <span className="tnum font-medium text-n-900">{detail.rupees}</span>
        {' · '}
        {detail.affected_chains} order{detail.affected_chains === 1 ? '' : 's'} affected
      </div>

      <div className="mb-4">
        <Arithmetic arithmetic={detail.arithmetic} shape={detail.shape} />
      </div>

      {/*
        The prose ends with its own "Suggested action:" sentence and the pane
        renders the engine's canonical action in a block of its own below, so
        the same instruction was appearing twice, forty words apart, in a pane
        whose problem is that too many things compete with the arithmetic.
        The prose keeps the finding and the mechanism; the action is shown once,
        where it is styled as an action.
      */}
      {detail.explanation?.text && (
        <LinkedProse
          text={detail.explanation.text.split(/\n?Suggested action:/i)[0].trim()}
          onOpenRow={onOpenRow}
          className="mb-3 max-w-[62ch] text-body text-n-800"
        />
      )}

      <div className="mb-4">
        <EvidenceLine evidence={detail.evidence || {}} />
      </div>

      {/* The finding says WHAT is wrong; this says what the engine tried and
          why each attempt failed. It is the first question anyone asks after
          reading the arithmetic, and the answer already existed. */}
      <div className="mb-4 grid gap-1.5">
        {isPayout && (
          <button
            data-why-not=""
            onClick={() => onWhyNot?.(detail.settlement_id)}
            className="flex w-full items-center justify-between rounded-md border border-n-200 bg-n-25 px-3 py-2 text-left transition-colors hover:border-accent hover:bg-accent-bg"
          >
            <span className="text-body-sm font-semibold text-n-800">
              Why didn't this match?
            </span>
            <span className="text-[11.5px] text-n-500">
              tier-by-tier trace →
            </span>
          </button>
        )}
        {/* Carries the finding to the Ask page as its subject, so the next
            question can say "this" instead of an id nobody wants to type. */}
        <button
          data-ask-about=""
          onClick={() =>
            onAsk?.({
              id: detail.group_id,
              headline: detail.headline,
              amount: detail.rupees,
            })
          }
          className="flex w-full items-center justify-between rounded-md border border-n-200 bg-n-25 px-3 py-2 text-left transition-colors hover:border-accent hover:bg-accent-bg"
        >
          <span className="text-body-sm font-semibold text-n-800">
            Ask about this
          </span>
          <span className="text-[11.5px] text-n-500">
            compare, count, what-if →
          </span>
        </button>
      </div>

      {detail.records?.length > 0 && (
        <div className="mb-4">
          <div className="mb-1 text-label uppercase text-n-500">
            Source records
          </div>
          {/*
            Two lines per record, not four columns.

            Role, id, date and amount side by side needed 335px in a 320px pane
            and pushed the whole pane into horizontal overflow -- the id is a
            17-character mono string and the amount is tabular, so neither can
            be squeezed. Stacked, the same four facts fit with room left, and
            the pairing is better anyway: role with amount is the summary line,
            id with date is the reference underneath it.

            Secondary by construction. These are where the figures above came
            from, not the finding.
          */}
          <div className="divide-y divide-n-100 border-t border-n-100">
            {detail.records.map((r) => (
              <div key={r.id} className="py-1.5">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-body-sm text-n-500">{r.role}</span>
                  <span className="tnum shrink-0 text-body-sm text-n-800">
                    {r.amount}
                  </span>
                </div>
                <div className="mt-0.5 flex items-baseline justify-between gap-3">
                  <span className="min-w-0 truncate font-mono text-[11px]">
                    {ROLE_TABLE[r.role] ? (
                      <button
                        onClick={() => onOpenRow?.(ROLE_TABLE[r.role], r.id)}
                        className="text-accent hover:underline"
                      >
                        {r.id}
                      </button>
                    ) : (
                      <span className="text-n-800">{r.id}</span>
                    )}
                  </span>
                  <span className="tnum shrink-0 text-[11px] text-n-500">
                    {r.date}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/*
        A payout gets a COUNT, not a list.
        Its orders are bystanders: they were in the batch when the batch came
        up short, and the work is finding why the payout was short, not reading
        25 healthy orders. The list was the largest thing on the pane and the
        least useful thing on it. One line, and a way through to all of them.
      */}
      {isPayout && detail.shape?.orders > 0 && (
        <div className="mb-4 text-body-sm text-n-500">
          <span className="tnum font-medium text-n-900">
            {detail.shape.orders}
          </span>{' '}
          orders in this payout ·{' '}
          <button
            onClick={() => onOpenRow?.('orders', null, detail.settlement_id)}
            className="text-accent hover:underline"
          >
            view in Data
          </button>
        </div>
      )}

      {/*
        An order-side group gets every one of them.
        Here the orders ARE the finding, and "30 orders were captured twice" is
        worth what a reader can check: one click reaches the order and the two
        payment rows that capture it.
      */}
      {!isPayout && detail.orders_sample?.length > 0 && (
        <div className="mb-4">
          <div className="mb-1 text-label uppercase text-n-500">
            Orders ({detail.orders_sample.length})
          </div>
          <div className="flex flex-wrap gap-x-2.5 gap-y-1 font-mono text-body-sm">
            {detail.orders_sample.map((id) => (
              <button
                key={id}
                onClick={() => onOpenRow?.('orders', id)}
                className="text-accent hover:underline"
              >
                {id}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Reads as an action, not as a pull quote. */}
      <div className="mb-4 rounded-md border border-n-100 bg-n-25 px-3 py-2.5 text-body-sm leading-relaxed text-n-600">
        {detail.suggested_action}
      </div>

      <div className="border-t border-n-200 pt-3">
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Note (optional)"
          className="mb-2 w-full rounded border border-n-200 px-2 py-1.5 text-body-sm outline-none placeholder:text-n-500 focus:border-accent"
        />
        <div className="flex items-center gap-2">
          {/* data-action, so a probe can address the BUTTON. Selecting these
              by their label matched the "recorded: approve" line below once a
              decision existed, and two of three clicks in a test went to a
              span. */}
          <button
            data-action="approve"
            onClick={() => act('approve')}
            className="rounded bg-accent px-3 py-1.5 text-body-sm font-medium text-n-0 hover:brightness-110"
          >
            Approve
          </button>
          <button
            data-action="reject"
            onClick={() => act('reject')}
            className="rounded border border-n-200 px-3 py-1.5 text-body-sm font-medium text-n-900 hover:bg-n-50"
          >
            Reject
          </button>
          <button
            data-action="escalate"
            onClick={() => act('escalate')}
            className="rounded border border-n-200 px-3 py-1.5 text-body-sm font-medium text-n-900 hover:bg-n-50"
          >
            Escalate
          </button>
          {decided && (
            <span className="text-body-sm text-n-500">
              recorded: {decided.action}
              {decided.note ? ` — ${decided.note}` : ''}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
