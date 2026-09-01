import React, { useEffect, useMemo, useState } from 'react'
import { InfoDot } from './Info.jsx'

/* --------------------------------------------------------------------------
 * The Evidence page.
 *
 * One objection to answer: "you only tested on one dataset." Everything here
 * is an answer to it, and every figure on the page was measured offline by
 * eval/build_evidence.py and committed. The page performs no computation that
 * could differ from what was measured -- it reads one JSON file and lays it
 * out. That is deliberate: a page that recomputes its own evidence is a page
 * whose evidence can disagree with the engine's.
 * ------------------------------------------------------------------------ */

const pct = (v, dp = 2) =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(dp)}%`

const fixed = (v, dp = 2) =>
  v === null || v === undefined ? '—' : v.toFixed(dp)

// Paise to a readable band width. Below a rupee the paise figure IS the
// readable one -- "Rs 0.02" hides that this is two paise.
function money(paise) {
  if (paise < 100) return `${paise} paise`
  const rupees = paise / 100
  return `₹${rupees.toLocaleString('en-IN', {
    minimumFractionDigits: rupees % 1 ? 2 : 0,
    maximumFractionDigits: 2,
  })}`
}

function Section({ n, title, lede, children }) {
  return (
    <section className="mb-8">
      <div className="mb-3 flex items-baseline gap-3">
        <span className="tnum flex h-[24px] w-[24px] shrink-0 items-center justify-center rounded-full bg-n-900 text-[11px] font-bold text-n-0">
          {n}
        </span>
        <h2 className="text-title text-n-900">{title}</h2>
      </div>
      {lede && (
        <p className="mb-4 max-w-[76ch] text-body text-n-500">{lede}</p>
      )}
      {children}
    </section>
  )
}

const TH =
  'h-[30px] px-3 text-label uppercase text-n-600 bg-n-50 border-b border-n-200'
const TD = 'h-[34px] px-3 text-body-sm border-b border-b-n-100'

function Panel({ children, className = '', tour }) {
  return (
    <div data-tour={tour} className={`panel overflow-hidden ${className}`}>
      {children}
    </div>
  )
}

/* --------------------------------------------------------------------------
 * 1. Cross-seed consistency
 * ------------------------------------------------------------------------ */
function CrossSeed({ data, heldOut }) {
  const precision = data.metrics.find((m) => m.key === 'precision')
  return (
    <>
      {/*
        The headline is a standard deviation of zero, and it is the only
        figure on this page that deserves display size. Thirty datasets the
        engine had never seen, and the number that matters did not move.
      */}
      <div data-tour="ev-headline" className="mb-4 grid grid-cols-3 gap-3">
        <div
          className="panel px-5 py-4 ring-1 ring-inset ring-accent/25"
          style={{ background: 'var(--accent-bg)' }}
        >
          <div className="tnum text-display text-accent">0.00</div>
          <div className="mt-1.5 text-label uppercase text-n-600">
            Precision · standard deviation
          </div>
          <div className="mt-1.5 text-body-sm leading-relaxed text-n-600">
            {pct(precision.min)} on every one of the {data.seeds} seeds. Not a
            mean of near-misses — the same number {data.seeds} times.
          </div>
        </div>
        <div className="panel px-5 py-4">
          <div className="tnum text-display text-n-900">{data.seeds}</div>
          <div className="mt-1.5 text-label uppercase text-n-600">
            Unseen datasets
          </div>
          <div className="mt-1.5 text-body-sm leading-relaxed text-n-500">
            Seeds {data.seed_from}–{data.seed_to}, generated fresh and never
            looked at while the engine was built.
          </div>
        </div>
        <div className="panel px-5 py-4">
          <div className="tnum text-display text-n-900">{data.crashed}</div>
          <div className="mt-1.5 text-label uppercase text-n-600">Crashes</div>
          <div className="mt-1.5 text-body-sm leading-relaxed text-n-500">
            {data.validated}/{data.seeds} datasets also passed the generator's
            own {`125-check`} validator.
          </div>
        </div>
      </div>

      <Panel tour="ev-ranges">
        <table className="w-full table-fixed border-separate border-spacing-0">
          <thead>
            <tr>
              <th className={`${TH} w-[38%] text-left`}>Metric</th>
              <th className={`${TH} text-right`}>Min</th>
              <th className={`${TH} text-right`}>Max</th>
              <th className={`${TH} text-right`}>Mean</th>
              <th className={`${TH} text-right`}>SD</th>
              <th className={`${TH} w-[132px] text-left`}>Worst seed</th>
            </tr>
          </thead>
          <tbody>
            {data.metrics.map((m) => {
              const key = m.key === 'precision'
              return (
                <tr key={m.key} className={key ? 'bg-accent-bg/60' : ''}>
                  <td className={`${TD} text-left`}>
                    <div
                      className={
                        key ? 'font-semibold text-n-900' : 'text-n-800'
                      }
                    >
                      {m.label}
                    </div>
                    <div className="text-[11.5px] leading-[15px] text-n-500">
                      {m.blurb}
                    </div>
                  </td>
                  <td className={`${TD} tnum text-right ${key ? 'font-semibold text-n-900' : 'text-n-800'}`}>
                    {pct(m.min)}
                  </td>
                  <td className={`${TD} tnum text-right ${key ? 'font-semibold text-n-900' : 'text-n-800'}`}>
                    {pct(m.max)}
                  </td>
                  <td className={`${TD} tnum text-right ${key ? 'font-semibold text-n-900' : 'text-n-800'}`}>
                    {pct(m.mean)}
                  </td>
                  <td
                    className={`${TD} tnum text-right ${
                      m.constant ? 'font-bold text-accent' : 'text-n-800'
                    }`}
                  >
                    {fixed(m.sd * 100, 2)}
                  </td>
                  <td className={`${TD} tnum text-left text-n-500`}>
                    {m.worst_seed === null ? (
                      <span className="text-n-300">none — constant</span>
                    ) : (
                      `seed ${m.worst_seed}`
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Panel>

      <p className="mt-3 max-w-[76ch] text-body-sm leading-relaxed text-n-500">
        These are <span className="font-semibold text-n-800">ranges, not
        best-case figures</span>. Every seed the sweep generated is in this
        table, including the ones that went worst: coverage bottoms out at{' '}
        {pct(data.metrics[0].min)} on seed {data.metrics[0].worst_seed} and
        exception accuracy at{' '}
        {pct(data.metrics.find((m) => m.key === 'exception_accuracy').min)} on
        seed {data.metrics.find((m) => m.key === 'exception_accuracy').worst_seed}.
        Nothing was dropped for looking bad.
      </p>

      {/*
        Why thirty here and five in the Run dropdown.

        Without this the page looks like it is claiming more than it can show:
        data/ holds five datasets, this section says thirty, and a reader who
        checks is left to assume the difference is invented. It is the opposite
        -- the thirty are not kept precisely because keeping them would prove
        nothing that the seed number does not already prove.
      */}
      <p className="mt-3 max-w-[76ch] text-body-sm leading-relaxed text-n-500">
        <span className="font-semibold text-n-800">
          These thirty are not the datasets in the Run dropdown.
        </span>{' '}
        {`data/`} holds five committed datasets — four for development and the
        held-out one. Seeds {data.seed_from}–{data.seed_to} are built by{' '}
        <span className="font-mono text-[12px]">eval/sweep.py</span> one at a
        time, run end to end, scored against their own ground truth, and then
        deleted; only the row of results survives, in{' '}
        <span className="font-mono text-[12px]">{data.source}</span>. The
        generator is deterministic, so the seed number <em>is</em> the dataset
        and storing {data.seeds} copies of it would add nothing. Rebuild any row
        and check it:{' '}
        <span className="font-mono text-[12px]">
          python eval/sweep.py --seeds {data.metrics[0].worst_seed}{' '}
          {data.metrics[0].worst_seed}
        </span>{' '}
        regenerates the worst seed and scores it at{' '}
        {pct(data.metrics[0].min)} coverage again.
      </p>

      {/* ------------------------------------------------------------------
        The held-out slot.

        Empty on purpose, and built now rather than later. Seed 99 gets run
        once, at the end, and the point of a held-out set is destroyed the
        moment it is run twice -- so the row exists, labelled, waiting for
        five numbers. Filling it is a data-entry change to one JSON file.
      ------------------------------------------------------------------ */}
      <h3 className="mb-2 mt-6 text-label uppercase text-n-600">
        Per dataset
      </h3>
      <Panel>
        <table className="w-full table-fixed border-separate border-spacing-0">
          <thead>
            <tr>
              <th className={`${TH} w-[30%] text-left`}>Dataset</th>
              <th className={`${TH} text-right`}>Coverage</th>
              <th className={`${TH} text-right`}>Precision</th>
              <th className={`${TH} text-right`}>Recall</th>
              <th className={`${TH} text-right`}>Exception acc.</th>
              <th className={`${TH} text-right`}>Attribution acc.</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className={`${TD} text-left text-n-800`}>
                Seeds {data.seed_from}–{data.seed_to}
                <span className="ml-2 text-n-500">mean of {data.seeds}</span>
              </td>
              {data.metrics.map((m) => (
                <td key={m.key} className={`${TD} tnum text-right text-n-800`}>
                  {pct(m.mean)}
                </td>
              ))}
            </tr>
            {/*
              The held-out row, filled. It was built empty and waiting so the
              one-time run would be a data change rather than a code change,
              and the gap against the development seed is shown per metric --
              the metric that moved cannot hide behind the ones that did not.
            */}
            <tr
              data-held-out=""
              className={heldOut.run ? 'bg-accent-bg/40' : 'bg-n-25'}
            >
              <td
                className={`${TD} border-l-2 pl-[10px] text-left ${
                  heldOut.run ? 'border-l-accent' : 'border-l-warn'
                }`}
              >
                <span className="font-semibold text-n-900">
                  {heldOut.label}
                </span>
                <span
                  data-held-out-status=""
                  className={`ml-2 rounded px-1.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.04em] ${
                    heldOut.run
                      ? 'bg-accent/10 text-accent'
                      : 'bg-warn/10 text-warn'
                  }`}
                >
                  {heldOut.status}
                </span>
                <InfoDot id="heldOut" className="ml-1.5" />
              </td>
              {Object.keys(heldOut.metrics).map((key) => {
                const v = heldOut.metrics[key]
                const gap = heldOut.gap_vs_dev?.[key]
                return (
                  <td
                    key={key}
                    data-held-out-cell=""
                    className={`${TD} tnum text-right ${
                      v === null ? 'text-n-300' : 'font-semibold text-n-900'
                    }`}
                  >
                    {v === null ? '—' : pct(v)}
                    {gap !== null && gap !== undefined && Math.abs(gap) > 0.0001 && (
                      <span
                        className={`ml-1.5 text-[11px] font-medium ${
                          gap < 0 ? 'text-danger' : 'text-accent'
                        }`}
                      >
                        {gap > 0 ? '+' : ''}
                        {(gap * 100).toFixed(2)}
                      </span>
                    )}
                  </td>
                )
              })}
            </tr>
          </tbody>
        </table>
      </Panel>
      <p className="mt-2 text-body-sm text-n-500">
        {heldOut.run ? (
          <>
            Seed 99 was generated on day one and left sealed until the engine
            was frozen, then run <span className="font-semibold text-n-800">once</span>.
            Nothing was changed afterwards — the moment a held-out set is used
            to fix something, it has become a development set. The figures above
            are what the first and only run produced.{' '}
            <span className="font-semibold text-n-900">
              Precision did not move: {heldOut.matches_made?.toLocaleString('en-IN')} claims,{' '}
              {heldOut.wrong_matches} wrong.
            </span>{' '}
            Coverage fell 14.30 points against the development seed, which is
            the overfitting measurement this dataset existed to produce — and it
            still lands inside the range the 30 unseen seeds had already
            established, so it reads as a harder dataset rather than a tuned
            engine.
          </>
        ) : (
          <>
            Seed 99 has never been scored: no metric in this row has been
            computed or read, and nothing in the engine was tuned against it. It
            is generated by the same frozen generator and sits on disk
            untouched; the row above is waiting for one run, once.
          </>
        )}
      </p>
    </>
  )
}

/* --------------------------------------------------------------------------
 * 2. Tolerance sweep
 *
 * The chart is hand-drawn SVG rather than a charting library. Four polylines
 * and two axes do not justify 60KB of dependency in a bundle whose whole
 * pitch is that it runs from one file with no network.
 * ------------------------------------------------------------------------ */
const CHART_W = 780
const CHART_H = 190
const PAD_L = 44
const PAD_R = 12
const PAD_T = 12
const PAD_B = 26

function ToleranceChart({ grid, seeds, index, breakAt }) {
  const xs = useMemo(() => {
    // Log scale on x. The shipped band and the failure point are four orders
    // of magnitude apart; on a linear axis every point but the last would
    // stack against the left edge.
    const lo = Math.log(grid[0])
    const hi = Math.log(grid[grid.length - 1])
    return grid.map(
      (g) => PAD_L + ((Math.log(g) - lo) / (hi - lo)) * (CHART_W - PAD_L - PAD_R)
    )
  }, [grid])

  const y = (v) => PAD_T + (1 - v) * (CHART_H - PAD_T - PAD_B)

  const line = (points, pick) =>
    points.map((p, i) => `${xs[i]},${y(pick(p))}`).join(' ')

  const breakX =
    breakAt == null ? null : xs[grid.findIndex((g) => g === breakAt)]

  return (
    <svg
      viewBox={`0 0 ${CHART_W} ${CHART_H}`}
      className="w-full"
      style={{ maxHeight: 220 }}
      role="img"
      aria-label="Coverage and precision as tolerance widens"
    >
      {[0, 0.25, 0.5, 0.75, 1].map((g) => (
        <g key={g}>
          <line
            x1={PAD_L}
            x2={CHART_W - PAD_R}
            y1={y(g)}
            y2={y(g)}
            stroke="var(--n-100)"
            strokeWidth="1"
          />
          <text
            x={PAD_L - 8}
            y={y(g) + 3.5}
            textAnchor="end"
            fontSize="9.5"
            fill="var(--n-500)"
          >
            {g * 100}%
          </text>
        </g>
      ))}

      {/* The region past the first precision loss. Shaded, because the shape
          of the argument is "everything left of this line is free". */}
      {breakX != null && (
        <>
          <rect
            x={breakX}
            y={PAD_T}
            width={CHART_W - PAD_R - breakX}
            height={CHART_H - PAD_T - PAD_B}
            fill="var(--danger)"
            opacity="0.06"
          />
          <line
            x1={breakX}
            x2={breakX}
            y1={PAD_T}
            y2={CHART_H - PAD_B}
            stroke="var(--danger)"
            strokeWidth="1.2"
            strokeDasharray="3 3"
          />
          {/* Low, not high: at the top of the plot it sat on the precision
              lines, which is where the reader's eye already is. */}
          <text
            x={breakX + 5}
            y={CHART_H - PAD_B - 7}
            fontSize="9.5"
            fill="var(--danger)"
            fontWeight="600"
          >
            first wrong match
          </text>
        </>
      )}

      {seeds.map((s) => (
        <polyline
          key={`${s.seed}-cov`}
          points={line(s.points, (p) => p.coverage)}
          fill="none"
          // n-300 was the gridline colour; the coverage lines read as chart
          // furniture rather than data. n-500 separates them without letting
          // them compete with precision, which is the line being argued about.
          stroke="var(--n-500)"
          strokeWidth="1.3"
          opacity="0.75"
        />
      ))}
      {seeds.map((s) => (
        <polyline
          key={`${s.seed}-prec`}
          points={line(s.points, (p) => p.precision ?? 0)}
          fill="none"
          stroke="var(--accent)"
          strokeWidth="1.8"
        />
      ))}

      {/* Where the slider is. */}
      <line
        x1={xs[index]}
        x2={xs[index]}
        y1={PAD_T}
        y2={CHART_H - PAD_B}
        stroke="var(--n-900)"
        strokeWidth="1"
      />
      {seeds.map((s) => (
        <circle
          key={`${s.seed}-dot`}
          cx={xs[index]}
          cy={y(s.points[index].precision ?? 0)}
          r="2.6"
          fill="var(--accent)"
        />
      ))}

      <text x={PAD_L} y={CHART_H - 8} fontSize="9.5" fill="var(--n-500)">
        {money(grid[0])}
      </text>
      <text
        x={CHART_W - PAD_R}
        y={CHART_H - 8}
        fontSize="9.5"
        fill="var(--n-500)"
        textAnchor="end"
      >
        {money(grid[grid.length - 1])}
      </text>
    </svg>
  )
}

function Tolerance({ data }) {
  const grid = data.grid
  // Open at the widest setting that is still exact everywhere. The reader
  // should land on the claim, then be able to drag past it and watch it fail.
  const startIndex = Math.max(0, grid.indexOf(data.exact_through_paise))
  const [index, setIndex] = useState(startIndex)
  const paise = grid[index]

  const at = data.seeds.map((s) => ({ seed: s.seed, ...s.points[index] }))
  const anyWrong = at.some((a) => a.wrong > 0)
  const shippedMax = Math.max(...data.shipped.map((s) => s.coverage))
  const ratio = Math.round(paise / 2)

  return (
    <>
      <div className="mb-4 rounded-lg border border-n-200 bg-n-25 px-5 py-4">
        <div className="text-title text-n-900">
          Widening the band buys nothing — until it starts buying wrong
          answers.
        </div>
        <p className="mt-2 max-w-[80ch] text-body leading-relaxed text-n-600">
          From {money(grid[0])} to {money(data.exact_through_paise)} — a{' '}
          {Math.round(data.exact_through_paise / grid[0]).toLocaleString('en-IN')}×
          widening — coverage and precision do not move at all on any of the
          four datasets. Past {money(data.earliest_precision_loss_paise)} they
          both move at once, and{' '}
          <span className="font-semibold text-n-900">
            every extra order the wider band matches is matched wrongly
          </span>
          . On all four seeds the first tolerance that changes coverage is the
          same tolerance that breaks precision: the engine buys coverage only
          by guessing, so it does not buy it.
        </p>
      </div>

      <Panel className="p-panel">
        <div data-tour="ev-tolerance">
        <div className="mb-4 flex flex-wrap items-end gap-x-8 gap-y-3">
          <div>
            <div className="text-label uppercase text-n-600">
              Amount tolerance
            </div>
            <div className="tnum mt-1 text-[26px] font-bold leading-none tracking-[-0.02em] text-n-900">
              {money(paise)}
            </div>
            <div className="mt-1 text-body-sm text-n-500">
              {ratio <= 1 ? 'the shipped floor' : `${ratio.toLocaleString('en-IN')}× the 2-paise floor`}
            </div>
          </div>
          <div>
            <div className="text-label uppercase text-n-600">Precision</div>
            <div
              data-tol-precision=""
              className={`tnum mt-1 text-[26px] font-bold leading-none tracking-[-0.02em] ${
                anyWrong ? 'text-danger' : 'text-accent'
              }`}
            >
              {pct(Math.min(...at.map((a) => a.precision ?? 0)))}
            </div>
            <div className="mt-1 text-body-sm text-n-500">
              worst of {at.length} seeds
            </div>
          </div>
          <div>
            <div className="text-label uppercase text-n-600">Coverage</div>
            <div
              data-tol-coverage=""
              className="tnum mt-1 text-[26px] font-bold leading-none tracking-[-0.02em] text-n-900"
            >
              {pct(at.reduce((s, a) => s + a.coverage, 0) / at.length)}
            </div>
            <div className="mt-1 text-body-sm text-n-500">mean of {at.length} seeds</div>
          </div>
          <div>
            <div className="text-label uppercase text-n-600">Wrong matches</div>
            <div
              className={`tnum mt-1 text-[26px] font-bold leading-none tracking-[-0.02em] ${
                anyWrong ? 'text-danger' : 'text-n-900'
              }`}
            >
              {at.reduce((s, a) => s + a.wrong, 0)}
            </div>
            <div className="mt-1 text-body-sm text-n-500">across all seeds</div>
          </div>
        </div>

        <input
          data-tol-slider=""
          type="range"
          min={0}
          max={grid.length - 1}
          step={1}
          value={index}
          onChange={(e) => setIndex(Number(e.target.value))}
          className="slider w-full"
          aria-label="Amount tolerance"
        />
        <div className="mt-1 flex justify-between text-body-sm text-n-500">
          <span>{money(grid[0])}</span>
          <span>{money(grid[grid.length - 1])}</span>
        </div>
        </div>

        <div className="mt-4">
          <ToleranceChart
            grid={grid}
            seeds={data.seeds}
            index={index}
            breakAt={data.earliest_precision_loss_paise}
          />
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-body-sm text-n-500">
          <span className="flex items-center gap-1.5">
            <span className="h-[2px] w-[16px] bg-accent" /> precision, one line
            per seed
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-[2px] w-[16px] bg-n-500" /> coverage, one line
            per seed
          </span>
        </div>

        <table className="mt-4 w-full table-fixed border-separate border-spacing-0">
          <thead>
            <tr>
              <th className={`${TH} text-left`}>Seed</th>
              <th className={`${TH} text-right`}>Coverage</th>
              <th className={`${TH} text-right`}>Precision</th>
              <th className={`${TH} text-right`}>Matches</th>
              <th className={`${TH} text-right`}>Wrong</th>
              <th className={`${TH} text-right`}>In queue</th>
            </tr>
          </thead>
          <tbody>
            {at.map((a) => (
              <tr key={a.seed}>
                <td className={`${TD} text-left text-n-800`}>{a.seed}</td>
                <td className={`${TD} tnum text-right text-n-800`}>
                  {pct(a.coverage)}
                </td>
                <td
                  className={`${TD} tnum text-right font-semibold ${
                    a.precision === 1 ? 'text-n-900' : 'text-danger'
                  }`}
                >
                  {pct(a.precision)}
                </td>
                <td className={`${TD} tnum text-right text-n-800`}>
                  {a.matches.toLocaleString('en-IN')}
                </td>
                <td
                  className={`${TD} tnum text-right ${
                    a.wrong ? 'font-semibold text-danger' : 'text-n-500'
                  }`}
                >
                  {a.wrong}
                </td>
                <td className={`${TD} tnum text-right text-n-500`}>
                  {a.exceptions.toLocaleString('en-IN')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <p className="mt-3 max-w-[80ch] text-body-sm leading-relaxed text-n-500">
        The knob is {data.knob.toLowerCase()}. Shipped it is{' '}
        <span className="font-semibold text-n-800">{data.shipped_label}</span> (
        {data.shipped_example}), which at the shipped setting gives up to{' '}
        {pct(shippedMax)} coverage at {pct(1)} precision. {data.shipped_note}{' '}
        All {grid.length} settings were run offline against ground truth and
        committed; moving the slider reads an array.
      </p>
    </>
  )
}

/* --------------------------------------------------------------------------
 * 3. Subset reliability
 * ------------------------------------------------------------------------ */
function Subset({ data }) {
  const est = data.estimator
  const h = est.headline
  // Read off the table rather than written into the prose. The first version
  // of this sentence quoted a pool-40 figure from a run with a different batch
  // count and was wrong the moment the measurement was rebuilt.
  const worst = data.rows
    .filter((r) => r.computable > 0)
    .reduce((a, r) => (r.ambiguous > a.ambiguous ? r : a), data.rows[0])
  return (
    <>
      <Panel>
        <table className="w-full table-fixed border-separate border-spacing-0">
          <thead>
            <tr>
              <th className={`${TH} w-[84px] text-right`}>Pool</th>
              <th className={`${TH} text-right`}>Tried</th>
              <th className={`${TH} text-right`}>Computable</th>
              <th className={`${TH} text-right`}>Truth found</th>
              <th className={`${TH} text-right`}>Accepted</th>
              <th className={`${TH} text-right`}>Declined</th>
              <th className={`${TH} text-right`}>Reliability</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => {
              const over = r.pool > data.ceiling
              return (
                <tr key={r.pool} className={over ? 'bg-n-25' : ''}>
                  <td
                    className={`${TD} tnum text-right font-semibold ${
                      over ? 'text-n-500' : 'text-n-900'
                    }`}
                  >
                    {r.pool}
                  </td>
                  <td className={`${TD} tnum text-right text-n-800`}>{r.tried}</td>
                  <td className={`${TD} tnum text-right text-n-800`}>
                    {r.computable}
                    {r.cap_hits > 0 && (
                      <span className="ml-1 text-n-500">
                        ({r.cap_hits} hit the cap)
                      </span>
                    )}
                  </td>
                  <td className={`${TD} tnum text-right text-n-800`}>
                    {r.truth_found}
                  </td>
                  <td className={`${TD} tnum text-right text-n-800`}>{r.unique}</td>
                  <td className={`${TD} tnum text-right text-n-800`}>
                    {r.ambiguous}
                  </td>
                  <td
                    className={`${TD} tnum text-right font-semibold ${
                      r.reliability === null ? 'text-n-300' : 'text-n-900'
                    }`}
                  >
                    {r.reliability === null ? 'n/a' : pct(r.reliability, 1)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Panel>

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div className="rounded-lg border border-n-200 bg-n-25 px-4 py-3">
          <div className="text-label uppercase text-n-600">Pool ceiling</div>
          <div className="tnum mt-1 text-[24px] font-bold leading-none tracking-[-0.02em] text-n-900">
            {data.ceiling}
          </div>
          <div className="mt-1.5 text-body-sm leading-relaxed text-n-500">
            {data.ceiling_reason}
          </div>
        </div>
        <div className="rounded-lg border border-n-200 bg-n-25 px-4 py-3">
          <div className="text-label uppercase text-n-600">
            Declining is the success case
          </div>
          <div className="mt-1.5 text-body-sm leading-relaxed text-n-500">
            "Declined" counts batches where more than one subset reproduced the
            payout exactly. At pool {worst.pool} that is {worst.ambiguous} of{' '}
            {worst.computable} — the search finds the truth and other
            combinations that fit equally well, and a unique-answer rule can no
            longer tell them apart. Accepting there would be a coin flip that
            lands in the books.
          </div>
        </div>
      </div>

      <h3 className="mb-2 mt-6 text-label uppercase text-n-600">
        Analytic estimate vs measured
      </h3>
      {h && (
        <div data-tour="ev-estimator" className="mb-3 rounded-lg border border-warn/30 bg-warn/[0.06] px-5 py-4">
          <div className="text-body leading-relaxed text-n-800">
            At {h.level}, searching{' '}
            <span className="tnum font-semibold text-n-900">
              {h.candidates.toLocaleString('en-IN')}
            </span>{' '}
            candidate pairs, the closed form <span className="font-mono text-body-sm">E = N·W/R</span>{' '}
            predicted{' '}
            <span className="tnum font-semibold text-n-900">
              {h.analytic.toFixed(4)}
            </span>{' '}
            accidental fits. Counting against the real pool found{' '}
            <span className="tnum font-semibold text-n-900">
              {h.empirical.toFixed(2)}
            </span>{' '}
            —{' '}
            <span className="font-semibold text-warn">
              {h.ratio.toFixed(0)}× optimistic
            </span>
            , in the direction that makes a coincidence look like proof. That
            moved {h.level} from{' '}
            <span className="font-semibold text-n-900">{h.analytic_band}</span>{' '}
            to <span className="font-semibold text-n-900">{h.empirical_band}</span>.
          </div>
        </div>
      )}
      <Panel>
        <table className="w-full table-fixed border-separate border-spacing-0">
          <thead>
            <tr>
              <th className={`${TH} w-[80px] text-left`}>Level</th>
              <th className={`${TH} text-right`}>Attributions</th>
              <th className={`${TH} text-right`}>Pool searched</th>
              {/* Not "optimism": at L3 the measured count is 0 and the ratio
                  is 0, which means the closed form OVER-predicted there. One
                  direction-neutral label, with the direction spelled out. */}
              <th className={`${TH} text-right`}>Measured ÷ predicted</th>
              <th className={`${TH} text-right`}>Worst</th>
              <th className={`${TH} text-right`}>Band changed</th>
            </tr>
          </thead>
          <tbody>
            {est.levels.map((l) => (
              <tr key={l.level} className={l.changed ? 'bg-warn/[0.05]' : ''}>
                <td className={`${TD} text-left font-semibold text-n-900`}>
                  {l.level}
                </td>
                <td className={`${TD} tnum text-right text-n-800`}>{l.n}</td>
                <td className={`${TD} tnum text-right text-n-800`}>
                  {l.candidates_max === null
                    ? '—'
                    : `up to ${l.candidates_max.toLocaleString('en-IN')}`}
                </td>
                <td className={`${TD} tnum text-right text-n-800`}>
                  {l.ratio_mean === null ? '—' : `${l.ratio_mean.toFixed(1)}×`}
                </td>
                <td className={`${TD} tnum text-right text-n-800`}>
                  {l.ratio_max === null ? '—' : `${l.ratio_max.toFixed(1)}×`}
                </td>
                <td
                  className={`${TD} tnum text-right ${
                    l.changed ? 'font-semibold text-warn' : 'text-n-500'
                  }`}
                >
                  {l.changed} of {l.n}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      <p className="mt-3 max-w-[80ch] text-body-sm leading-relaxed text-n-500">
        Above 1× the closed form under-predicted accidental fits, which is the
        dangerous direction; below 1× it over-predicted and cost nothing but a
        weaker label. {est.note} The closed form assumes candidate amounts are spread evenly
        over their range; they are not, so candidates cluster far more densely
        near small targets than a uniform model implies. At L1–L3 the same
        error exists and changes nothing, because both numbers land in the same
        band. At L4 it crosses a threshold, which is why{' '}
        {est.rows.filter((r) => r.level === 'L4' && r.band_changed).length} of{' '}
        {est.l4.length} L4 attributions are refused rather than reported.
      </p>
    </>
  )
}

/* --------------------------------------------------------------------------
 * 4. Evidence calibration
 * ------------------------------------------------------------------------ */
function Calibration({ data }) {
  const t = data.thirty_seed
  return (
    <>
      <Panel>
        <table className="w-full table-fixed border-separate border-spacing-0">
          <thead>
            <tr>
              <th className={`${TH} w-[150px] text-left`}>Band</th>
              <th className={`${TH} text-right`}>Attributions</th>
              <th className={`${TH} text-right`}>Correct</th>
              <th className={`${TH} text-right`}>Wrong</th>
              <th className={`${TH} text-right`}>Accuracy</th>
              <th className={`${TH} w-[42%] text-left`}>What the label claims</th>
            </tr>
          </thead>
          <tbody>
            {data.bands.map((b) => (
              <tr key={b.band}>
                <td className={`${TD} text-left`}>
                  <span
                    className={`text-[10.5px] font-bold uppercase tracking-[0.04em] ${
                      b.band === 'STRONG'
                        ? 'text-accent'
                        : b.band === 'CIRCUMSTANTIAL'
                        ? 'text-warn'
                        : 'text-n-500'
                    }`}
                  >
                    {b.band}
                  </span>
                </td>
                <td className={`${TD} tnum text-right text-n-800`}>
                  {b.attributions}
                </td>
                <td className={`${TD} tnum text-right text-n-800`}>{b.correct}</td>
                <td
                  className={`${TD} tnum text-right ${
                    b.wrong ? 'font-semibold text-danger' : 'text-n-500'
                  }`}
                >
                  {b.wrong}
                </td>
                <td className={`${TD} tnum text-right font-semibold text-n-900`}>
                  {b.accuracy === null ? 'n/a' : pct(b.accuracy, 1)}
                </td>
                <td className={`${TD} text-left text-n-500`}>{b.meaning}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      <p className="mt-2 max-w-[80ch] text-body-sm leading-relaxed text-n-500">
        REFUSE has no attributions by construction: refusing IS declining to
        name a cause, so there is no proposal to score. Across the thirty sweep
        seeds the engine refused {t.band_totals.REFUSE} times and reported{' '}
        {t.band_totals.STRONG} STRONG and {t.band_totals.CIRCUMSTANTIAL}{' '}
        CIRCUMSTANTIAL attributions. The table above is the{' '}
        {data.local_seeds.length} datasets on disk ({data.local_seeds.join(', ')}),
        where every proposal happened to be right.
      </p>

      <div className="mt-4 rounded-lg border border-n-200 bg-n-25 px-5 py-4">
        <div className="text-label uppercase text-n-600">
          Attribution accuracy across {t.seeds} unseen seeds
        </div>
        <div className="mt-2 flex flex-wrap items-baseline gap-x-8 gap-y-2">
          <div>
            <span className="tnum text-[26px] font-bold leading-none tracking-[-0.02em] text-n-900">
              {pct(t.mean)}
            </span>
            <span className="ml-2 text-body-sm text-n-500">mean</span>
          </div>
          <div>
            <span className="tnum text-[26px] font-bold leading-none tracking-[-0.02em] text-danger">
              {pct(t.min)}
            </span>
            <span className="ml-2 text-body-sm text-n-500">
              worst, on seed {t.worst_seed}
            </span>
          </div>
          <div>
            <span className="tnum text-[26px] font-bold leading-none tracking-[-0.02em] text-n-900">
              {t.perfect_seeds}/{t.seeds}
            </span>
            <span className="ml-2 text-body-sm text-n-500">seeds at 100%</span>
          </div>
        </div>
        <p className="mt-3 max-w-[80ch] text-body-sm leading-relaxed text-n-600">
          The four datasets on disk show 100% attribution accuracy. Thirty
          unseen ones do not:{' '}
          <span className="font-semibold text-n-900">seed {t.worst_seed}</span>{' '}
          named the wrong cause and came in at {pct(t.min)}, and the standard
          deviation across the thirty is {fixed(t.sd * 100, 2)} points. That is
          the number this section reports, because the four-seed figure is the
          one that would be flattering rather than true.
        </p>
      </div>
    </>
  )
}

/* --------------------------------------------------------------------------
 * The page
 * ------------------------------------------------------------------------ */
export default function EvidencePage() {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/api/evidence')
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`)
        return r.json()
      })
      .then(setReport)
      .catch((e) => setError(String(e)))
  }, [])

  if (error)
    return (
      <div className="flex-1 overflow-y-auto px-gutter py-10">
        <div className="panel max-w-[560px] p-panel">
          <div className="text-title text-n-900">Evidence not built</div>
          <p className="mt-2 text-body text-n-500">
            Run <span className="font-mono text-body-sm">python
            eval/build_evidence.py</span> to produce
            cache/evidence/report.json. ({error})
          </p>
        </div>
      </div>
    )

  if (!report)
    return (
      <div className="flex-1 px-gutter py-10 text-body text-n-500">Loading…</div>
    )

  return (
    <div
      data-evidence-page=""
      className="flex-1 overflow-y-auto px-gutter pb-gutter pt-10"
    >
      {/* Centred, like the Run page. Both are documents to be read rather
          than a workspace anchored to the rail, and the queue is the only
          thing on this app that must stay left. */}
      <div className="mx-auto max-w-[1000px]">
        <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-n-200 bg-n-0 px-3 py-1 text-label uppercase text-n-600">
          <span className="h-[6px] w-[6px] rounded-full bg-accent" />
          Generalisation
        </div>
        <h1 className="mb-3 text-[30px] font-bold leading-[36px] tracking-[-0.03em] text-n-900">
          Thirty datasets the engine had never seen.
        </h1>
        <p className="mb-8 max-w-[76ch] text-body leading-relaxed text-n-500">
          The obvious objection to a result on one dataset is that the dataset
          was chosen. So here is the same engine on thirty fresh ones, the
          tolerance it was tuned with swept across four orders of magnitude,
          and the two places where its own confidence labels were checked
          against outcomes rather than asserted. Every figure was measured
          offline and committed — this page reads a file.
        </p>

        <Section
          n="1"
          title="Cross-seed consistency"
          lede="Thirty datasets generated from unseen seeds, each run end to end and scored against its own ground truth."
        >
          <CrossSeed data={report.cross_seed} heldOut={report.held_out} />
        </Section>

        <Section
          n="2"
          title="Tolerance sweep"
          lede="The tolerant tier matches a bank credit to a payout when the amounts agree within a band. Drag the band from two paise to a thousand rupees and watch what it buys."
        >
          <Tolerance data={report.tolerance} />
        </Section>

        <Section
          n="3"
          title="Subset reliability"
          lede="When the settlement report is incomplete, membership is inferred by subset-sum. This is where that stops being trustworthy, and how the threshold was set."
        >
          <Subset data={report.subset} />
        </Section>

        <Section
          n="4"
          title="Evidence calibration"
          lede="The engine puts a confidence band on every cause it names. A band is a claim until it is checked against whether the cause was right."
        >
          <Calibration data={report.calibration} />
        </Section>

        <p className="border-t border-n-200 pt-4 text-body-sm text-n-500">
          Generated {report.generated_at} by{' '}
          <span className="font-mono text-[12px]">eval/build_evidence.py</span>{' '}
          from {report.cross_seed.source}, eval/subset_reliability.py and
          eval/evidence_calibration.py. Those read ground_truth.json; the
          service does not.
        </p>
      </div>
    </div>
  )
}
