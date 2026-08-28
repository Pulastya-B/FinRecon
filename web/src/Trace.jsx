import React, { useEffect, useState } from 'react'

/* --------------------------------------------------------------------------
 * "Why not?" -- the tier-by-tier decline trace for one entity.
 *
 * The engine has always recorded a reason for every entity it declined; until
 * now they were merged for Tier 5 and dropped. This renders them in the order
 * the tiers ran, because the SEQUENCE is the story: on seed 42, 251 entities
 * carry a different reason in a later tier than they did in Tier 1, and
 * "Tier 1 found no UTR, then Tier 2 found the amount outside its band, then
 * Tier 3 rebuilt the payout and found no credit" is a different statement
 * from any one of those alone.
 *
 * Rendered as a modal over whatever the reader was looking at, because it is
 * an aside answering a question about one row, not a place to navigate to.
 * ------------------------------------------------------------------------ */

const OUTCOME_STYLE = {
  matched: 'bg-accent/10 text-accent',
  exception: 'bg-danger/10 text-danger',
  ignored: 'bg-n-100 text-n-600',
  undecided: 'bg-warn/10 text-warn',
}

function Step({ step, last }) {
  return (
    <div className="relative flex gap-3 pb-4">
      {!last && (
        <div className="absolute left-[11px] top-[24px] h-full w-[1.5px] bg-n-200" />
      )}
      <div className="relative z-[1] flex h-[23px] w-[23px] shrink-0 items-center justify-center rounded-full border-[1.5px] border-danger/40 bg-n-0 text-[10.5px] font-bold text-danger">
        {step.tier}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-body-sm font-semibold text-n-900">
            {step.title}
          </span>
          <span className="text-[11.5px] text-n-500">— {step.goal}</span>
        </div>
        <div className="mt-1 inline-block rounded bg-danger/[0.08] px-1.5 py-0.5 font-mono text-[10.5px] font-semibold uppercase tracking-[0.03em] text-danger">
          {step.reason}
        </div>
        <p className="mt-1.5 text-body-sm leading-relaxed text-n-700">
          {step.sentence}
        </p>
        {step.caused_by && (
          <p className="mt-1 text-[11.5px] text-n-500">
            Inherited from{' '}
            <span className="font-mono text-n-700">{step.caused_by}</span> —
            this entity did not fail on its own.
          </p>
        )}
      </div>
    </div>
  )
}

export default function Trace({ seed, entityId, onClose, onOpen }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!entityId) return
    setData(null)
    setError(null)
    fetch(`/api/trace/${seed}/${encodeURIComponent(entityId)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [seed, entityId])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  if (!entityId) return null

  return (
    <div
      className="fixed inset-0 z-[300] flex items-start justify-center bg-n-900/25 px-6 py-10"
      onClick={onClose}
    >
      <div
        data-trace-panel=""
        onClick={(e) => e.stopPropagation()}
        className="pane-raised max-h-full w-full max-w-[620px] overflow-y-auto"
      >
        <div className="flex items-start justify-between gap-3 border-b border-n-200 bg-n-25 px-panel py-3">
          <div className="min-w-0">
            <div className="text-label uppercase text-n-600">Why not?</div>
            <div className="mt-0.5 truncate text-body font-semibold text-n-900">
              {data?.headline || entityId}
            </div>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded px-2 py-1 text-body-sm text-n-500 hover:bg-n-100 hover:text-n-900"
          >
            Esc
          </button>
        </div>

        <div className="px-panel py-4">
          {error && (
            <div className="text-body-sm text-n-500">
              No trace for <span className="font-mono">{entityId}</span>. It is
              not an entity in this dataset.
            </div>
          )}
          {!data && !error && (
            <div className="text-body-sm text-n-500">Loading…</div>
          )}

          {data && (
            <>
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <span
                  className={`rounded px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.04em] ${
                    OUTCOME_STYLE[data.outcome.state] || OUTCOME_STYLE.undecided
                  }`}
                >
                  {data.outcome.code || data.outcome.state}
                </span>
                {data.outcome.tier && (
                  <span className="text-body-sm text-n-500">
                    decided at tier {data.outcome.tier}
                  </span>
                )}
                {data.outcome.note && (
                  <span className="text-body-sm text-n-500">
                    {data.outcome.note}
                  </span>
                )}
              </div>

              {data.steps.length === 0 ? (
                <p className="text-body-sm leading-relaxed text-n-600">
                  No tier declined this one. It was matched on the evidence the
                  ledgers already agreed on, so there is nothing to explain.
                </p>
              ) : (
                <>
                  <div className="mb-2 text-label uppercase text-n-600">
                    What each tier tried
                  </div>
                  <div>
                    {data.steps.map((s, i) => (
                      <Step
                        key={s.tier}
                        step={s}
                        last={i === data.steps.length - 1}
                      />
                    ))}
                  </div>
                </>
              )}

              <div className="mt-2 border-t border-n-100 pt-3">
                <div className="mb-1.5 text-label uppercase text-n-600">
                  The record the tiers read
                </div>
                <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1">
                  {data.facts.map((f) => (
                    <React.Fragment key={f.label + f.value}>
                      <dt className="text-body-sm text-n-500">{f.label}</dt>
                      <dd className="min-w-0 break-words font-mono text-[12px] text-n-800">
                        {f.value}
                      </dd>
                    </React.Fragment>
                  ))}
                </dl>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
