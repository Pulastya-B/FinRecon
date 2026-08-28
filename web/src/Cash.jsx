import React, { useEffect, useState } from 'react'

/* --------------------------------------------------------------------------
 * The cash position.
 *
 * "Run the books AND THE CASH POSITION" -- the second half of the job. The
 * queue answers "what went wrong"; this answers the question a finance person
 * actually opens the laptop for: is the money in the account?
 *
 * It is a POSITION, not a forecast, and the page says so twice. The statement
 * is historical and ends on a fixed date. Calling a re-presentation of past
 * state a forecast would invite backtest error and calibration intervals that
 * do not exist here, and inviting a question you cannot answer is a bad trade
 * for a word.
 *
 * Every bucket is a verdict the engine already reached. No window arithmetic
 * happens on this page.
 * ------------------------------------------------------------------------ */

const STYLE = {
  confirmed: {
    ring: 'ring-1 ring-inset ring-n-200',
    value: 'text-n-900',
    dot: 'bg-n-300',
  },
  expected: {
    ring: 'ring-1 ring-inset ring-n-200',
    value: 'text-n-900',
    dot: 'bg-accent',
  },
  at_risk: {
    ring: 'ring-1 ring-inset ring-danger/30',
    value: 'text-danger',
    dot: 'bg-danger',
    tint: 'rgba(217, 48, 37, 0.05)',
  },
  disputed: {
    ring: 'ring-1 ring-inset ring-warn/30',
    value: 'text-warn',
    dot: 'bg-warn',
    tint: 'rgba(178, 106, 0, 0.05)',
  },
}

const TD = 'h-[34px] px-3 text-body-sm border-b border-b-n-100'
const TH =
  'h-[30px] px-3 text-label uppercase text-n-600 bg-n-50 border-b border-n-200'

export default function Cash({ seed, onOpenFinding, onOpenTrace }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [filter, setFilter] = useState(null)

  useEffect(() => {
    setData(null)
    setError(null)
    fetch(`/api/cash/${seed}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [seed])

  if (error)
    return (
      <div className="flex-1 px-gutter py-10 text-body-sm text-n-500">
        Run a reconciliation first — the cash position is assembled from it.
      </div>
    )
  if (!data)
    return (
      <div className="flex-1 px-gutter py-10 text-body-sm text-n-500">Loading…</div>
    )

  const rows = filter ? data.rows.filter((r) => r.bucket === filter) : data.rows
  const atRisk = data.buckets.find((b) => b.key === 'at_risk')

  return (
    <div
      data-cash-page=""
      className="flex-1 overflow-y-auto px-gutter pb-gutter pt-8"
    >
      <div className="max-w-[1000px]">
        <div className="mb-1 flex flex-wrap items-baseline gap-3">
          <h1 className="text-title text-n-900">Cash position</h1>
          <span data-cash-asof="" className="text-body-sm text-n-500">
            {data.as_of_label}
          </span>
        </div>
        <p className="mb-5 max-w-[76ch] text-body-sm leading-relaxed text-n-500">
          Of the {data.payouts_total} payouts the gateway says it released, where
          each one actually is. {data.note}
        </p>

        {/* The sentence someone acts on. One line, the engine's own figure. */}
        {atRisk.payouts > 0 && (
          <div
            data-cash-headline=""
            className="mb-5 rounded-lg border border-danger/30 px-5 py-4"
            style={{ background: 'rgba(217, 48, 37, 0.05)' }}
          >
            <div className="tnum text-[28px] font-bold leading-none tracking-[-0.02em] text-danger">
              {atRisk.amount}
            </div>
            <div className="mt-2 text-body text-n-800">
              should be in the account and is not — {atRisk.payouts} payout
              {atRisk.payouts === 1 ? '' : 's'} released by the gateway with no
              matching credit in the statement.
            </div>
          </div>
        )}

        <div className="mb-5 grid grid-cols-4 gap-3">
          {data.buckets.map((b) => {
            const s = STYLE[b.key]
            const on = filter === b.key
            return (
              <button
                key={b.key}
                data-cash-bucket={b.key}
                onClick={() => setFilter(on ? null : b.key)}
                title={on ? 'Show all payouts' : `Show only ${b.label}`}
                className={`panel px-4 py-3 text-left transition-colors ${s.ring} ${
                  on ? 'border-accent' : ''
                }`}
                style={{ background: on ? 'var(--accent-bg)' : s.tint }}
              >
                <div className={`tnum text-[19px] font-bold leading-none tracking-[-0.02em] ${s.value}`}>
                  {b.amount}
                </div>
                <div className="mt-1.5 flex items-center gap-1.5">
                  <span className={`h-[6px] w-[6px] rounded-full ${s.dot}`} />
                  <span className="text-label uppercase text-n-700">{b.label}</span>
                  <span className="tnum text-body-sm text-n-500">
                    {b.payouts}
                  </span>
                </div>
                <div className="mt-1 text-[11.5px] leading-[15px] text-n-500">
                  {b.meaning}
                </div>
              </button>
            )
          })}
        </div>

        {/*
          An empty bucket with no explanation reads as a broken feature. This
          one is empty for a reason worth stating: "in transit" needs a payout
          that is BOTH absent from the statement AND still inside its window,
          and the statement ends on the last settlement date, so nearly every
          released payout is already either credited or overdue.
        */}
        {data.buckets.find((b) => b.key === 'expected').payouts === 0 && (
          <p className="-mt-2 mb-5 max-w-[76ch] text-body-sm leading-relaxed text-n-500">
            <span className="font-semibold text-n-700">Nothing is in transit.</span>{' '}
            A payout lands in Expected only if it is missing from the statement{' '}
            <em>and</em> its posting window is still open on{' '}
            {data.as_of.split('-').reverse().join('-')}. Since the statement runs
            to the last settlement date, a released payout is almost always
            either credited or already overdue — across 30 unseen seeds this
            bucket is occupied on 4 of them.
          </p>
        )}

        <div className="mb-2 flex items-baseline gap-2">
          <h2 className="text-label uppercase text-n-800">Every payout</h2>
          {filter && (
            <button
              onClick={() => setFilter(null)}
              className="text-body-sm text-accent hover:underline"
            >
              showing {rows.length} of {data.rows.length} — show all
            </button>
          )}
        </div>

        <div className="panel overflow-hidden">
          <table className="w-full table-fixed border-separate border-spacing-0">
            <thead>
              <tr>
                <th className={`${TH} w-[190px] text-left`}>Payout</th>
                {/* 100px wrapped the ISO date onto two lines and broke the
                    32px row rhythm. 10 digits of tnum at 13px plus 24 of
                    padding needs 116. */}
                <th className={`${TH} w-[116px] text-left`}>Settled</th>
                <th className={`${TH} w-[118px] text-left`}>State</th>
                <th className={`${TH} w-[128px] text-right`}>Net</th>
                <th className={`${TH} w-[128px] text-right`}>At stake</th>
                <th className={`${TH} text-left`}>What the engine found</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const s = STYLE[r.bucket]
                return (
                  <tr
                    key={r.settlement_id}
                    onClick={() =>
                      r.group_id
                        ? onOpenFinding?.(r.group_id)
                        : onOpenTrace?.(r.settlement_id)
                    }
                    className="row-i cursor-pointer hover:bg-n-50"
                  >
                    <td className={`${TD} font-mono text-[12px] text-accent`}>
                      {r.settlement_id}
                    </td>
                    <td className={`${TD} tnum text-n-600`}>{r.settled_on}</td>
                    <td className={TD}>
                      <span className="inline-flex items-center gap-1.5">
                        <span className={`h-[6px] w-[6px] rounded-full ${s.dot}`} />
                        <span className="text-[11px] font-semibold uppercase tracking-[0.03em] text-n-700">
                          {r.bucket.replace('_', ' ')}
                        </span>
                      </span>
                    </td>
                    <td className={`${TD} tnum text-right text-n-800`}>{r.net}</td>
                    <td
                      className={`${TD} tnum text-right font-semibold ${
                        r.at_stake ? s.value : 'text-n-300'
                      }`}
                    >
                      {r.at_stake || '—'}
                    </td>
                    <td className={`${TD} truncate text-n-600`} title={r.note}>
                      {r.note}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        <p className="mt-3 max-w-[76ch] text-body-sm leading-relaxed text-n-500">
          "At risk" and "Expected" are the engine's own distinction: Tier 3
          decides whether a missing credit is still inside the posting window
          (<span className="font-mono text-[12px]">TIMING_PENDING</span>) or past
          it (<span className="font-mono text-[12px]">MISSING_IN_BANK</span>).
          This page does not re-derive it. Days are counted against the last
          statement line, not today, so a fixed dataset reports a fixed number.
        </p>
      </div>
    </div>
  )
}
