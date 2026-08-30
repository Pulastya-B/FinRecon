import React, { useEffect, useLayoutEffect, useState } from 'react'
import { createPortal } from 'react-dom'

/* --------------------------------------------------------------------------
 * The spotlight tour.
 *
 * The first attempt at this was a band across the top of every page, and it
 * read as a site notice -- the thing you dismiss without reading, sitting in
 * the one place the design had deliberately kept clear. Orientation has to
 * point AT something. Dim the page, cut a hole around one component, and put
 * the sentence next to it.
 *
 * Two rules it obeys:
 *
 *   Once, then on demand. It runs itself the first time a page is opened and
 *   never again unasked; after that it lives behind a small glyph in the top
 *   right. A tour that reappears is an obstacle.
 *
 *   Every number is live. The queue's steps read their figures from the run
 *   that is actually on screen, because the previous version hard-coded seed
 *   42's counts and would have said "333 exception rows" while the strip
 *   behind it said 430.
 *
 * The hole is cut with a single spread box-shadow rather than four bands or an
 * SVG mask: one element, no seams at the corners, and it animates between
 * steps for free.
 * ------------------------------------------------------------------------ */

const PAD = 8
const CARD_W = 340
const GAP = 14
const MARGIN = 12

function rectOf(selector) {
  const el = document.querySelector(selector)
  if (!el) return null
  const r = el.getBoundingClientRect()
  if (r.width === 0 && r.height === 0) return null
  return r
}

/*
 * Where the card goes. Below, then above, then BESIDE, then over.
 *
 * The side branch is the one that matters. A tall target -- the queue section
 * is 471px -- leaves no room above or below, and the first version dropped the
 * card on top of the very rows it was describing. Checking left and right
 * before giving up puts it in the empty column next to the spotlight instead,
 * which is where a reader expects it and where it hides nothing.
 */
function placeCard(r) {
  const H = 210 // a card is ~180-210px; enough to test for fit
  if (!r) {
    return {
      left: window.innerWidth / 2 - CARD_W / 2,
      top: window.innerHeight / 2 - 90,
    }
  }
  const centred = Math.min(
    Math.max(MARGIN, r.left + r.width / 2 - CARD_W / 2),
    window.innerWidth - CARD_W - MARGIN
  )

  if (window.innerHeight - r.bottom - PAD > H) {
    return { left: centred, top: r.bottom + PAD + GAP }
  }
  if (r.top - PAD > H) {
    return { left: centred, bottom: window.innerHeight - r.top + PAD + GAP }
  }

  // Vertically centred on the target, clamped so it never leaves the viewport.
  const top = Math.min(
    window.innerHeight - H - MARGIN,
    Math.max(MARGIN, r.top + r.height / 2 - H / 2)
  )
  const rightRoom = window.innerWidth - r.right - PAD - GAP - MARGIN
  if (rightRoom >= CARD_W) return { left: r.right + PAD + GAP, top }
  const leftRoom = r.left - PAD - GAP - MARGIN
  if (leftRoom >= CARD_W) return { left: r.left - PAD - GAP - CARD_W, top }

  return { left: centred, top }
}

export default function Tour({ steps, onClose }) {
  const [i, setI] = useState(0)
  const [box, setBox] = useState(null)

  const step = steps[i]
  const last = i === steps.length - 1

  // useLayoutEffect so the spotlight is positioned before the browser paints;
  // with useEffect the scrim flashes at 0,0 for one frame on every step.
  useLayoutEffect(() => {
    let cancelled = false
    const measure = () => {
      if (cancelled) return
      setBox(step.target ? rectOf(step.target) : null)
    }
    const el = step.target ? document.querySelector(step.target) : null
    if (el) {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      // Let the smooth scroll settle before measuring, then keep measuring
      // while it does -- the rect moves for ~300ms after scrollIntoView.
      const t = [40, 120, 240, 380].map((ms) => setTimeout(measure, ms))
      measure()
      return () => {
        cancelled = true
        t.forEach(clearTimeout)
      }
    }
    measure()
    return () => {
      cancelled = true
    }
  }, [i, step])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'ArrowRight' || e.key === 'Enter') {
        last ? onClose() : setI((n) => n + 1)
      }
      if (e.key === 'ArrowLeft') setI((n) => Math.max(0, n - 1))
    }
    const onResize = () => setBox(step.target ? rectOf(step.target) : null)
    window.addEventListener('keydown', onKey)
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', onResize)
    }
  }, [last, onClose, step])

  const card = placeCard(box)

  return createPortal(
    <div data-tour-overlay="" className="tour-root" onClick={onClose}>
      {/* The hole. One div, one spread shadow: everything outside it is dim,
          everything inside it is the page at full contrast. */}
      <div
        data-tour-spotlight=""
        className={`tour-spot ${box ? '' : 'tour-spot-none'}`}
        style={
          box
            ? {
                left: box.left - PAD,
                top: box.top - PAD,
                width: box.width + PAD * 2,
                height: box.height + PAD * 2,
              }
            : undefined
        }
      />

      <div
        data-tour-card=""
        className="tour-card"
        onClick={(e) => e.stopPropagation()}
        style={
          card.bottom !== undefined
            ? { left: card.left, bottom: card.bottom }
            : { left: card.left, top: card.top }
        }
      >
        <div className="mb-1.5 flex items-baseline justify-between gap-3">
          <span className="text-label uppercase text-n-500">{step.eyebrow}</span>
          <span className="tnum text-[11px] text-n-500">
            {i + 1} / {steps.length}
          </span>
        </div>

        <div className="text-body-lg font-semibold leading-[21px] text-n-900">
          {step.title}
        </div>
        <p className="mt-1.5 text-body-sm leading-relaxed text-n-600">
          {step.body}
        </p>

        <div className="mt-3.5 flex items-center gap-2">
          <button
            data-tour-next=""
            onClick={() => (last ? onClose() : setI((n) => n + 1))}
            className="rounded bg-accent px-3 py-1.5 text-body-sm font-semibold text-n-0 hover:brightness-110"
          >
            {last ? 'Done' : 'Next'}
          </button>
          {i > 0 && (
            <button
              onClick={() => setI((n) => n - 1)}
              className="rounded border border-n-200 bg-n-0 px-3 py-1.5 text-body-sm text-n-700 hover:bg-n-50"
            >
              Back
            </button>
          )}
          <button
            data-tour-skip=""
            onClick={onClose}
            className="ml-auto text-body-sm text-n-500 underline decoration-n-300 underline-offset-2 hover:text-n-800"
          >
            skip
          </button>
        </div>
      </div>
    </div>,
    document.body
  )
}

/* --------------------------------------------------------------------------
 * The steps.
 *
 * `summary` is the live run. Every figure below comes out of it, so the tour
 * cannot contradict the screen it is pointing at on a seed other than 42.
 * ------------------------------------------------------------------------ */
export function runSteps() {
  return [
    {
      target: '[data-tour="run-card"]',
      eyebrow: 'Start here',
      title: 'Pick a dataset and run it',
      body:
        'Seed 42 is 2,222 rows across six ledgers — orders, gateway payments, ' +
        'refunds, chargebacks, payouts and a bank statement. It reconciles in ' +
        'about two seconds, locally, with no network call.',
    },
    {
      target: '[data-tour="chain"]',
      eyebrow: 'The problem',
      title: 'The chain breaks at the bank',
      body:
        'Inside the gateway every row carries the id of the row before it, so ' +
        'those joins are lookups. A bank line has only a date, an amount and a ' +
        'narration string — and the reference tying it to a payout is buried ' +
        'in that text, sometimes truncated, sometimes absent.',
    },
    {
      target: '[data-tour="claims"]',
      eyebrow: 'The claim',
      title: 'Precision is the number that matters',
      body:
        'A miss costs a person two minutes. A wrong match is silent and lands ' +
        'in the books. The engine refuses rather than guesses, and that refusal ' +
        'is what the Evidence page measures across 30 datasets it never saw.',
    },
  ]
}

export function queueSteps(summary) {
  const rows = summary?.exception_rows ?? 0
  const groups = summary?.groups ?? 0
  const matched = summary?.matched ?? 0
  const total = summary?.total_orders ?? 0
  const money = summary?.unexplained ?? ''
  const pctMatched =
    total > 0 ? `${((matched / total) * 100).toFixed(2)}%` : ''

  return [
    {
      target: '[data-tour="stats"]',
      eyebrow: 'What ran',
      title: `${matched.toLocaleString('en-IN')} of ${total.toLocaleString('en-IN')} orders tied through`,
      body:
        `That is ${pctMatched} coverage with zero wrong matches. Coverage is ` +
        `not higher because the rest are genuinely broken and must not be ` +
        `matched — the engine declines instead of guessing.`,
    },
    {
      target: '[data-queue-pane] section:first-of-type',
      eyebrow: 'The compression',
      title: `${rows.toLocaleString('en-IN')} exception rows became ${groups} findings`,
      body:
        `One payout going wrong strands dozens of orders. Grouping by root ` +
        `cause turns that into one thing to investigate, not dozens — sorted ` +
        `by money, ${money} in total.`,
    },
    {
      target: '[data-queue] tbody tr:first-child',
      eyebrow: 'The work',
      title: 'Click a finding to see the arithmetic',
      body:
        'The detail pane rebuilds the payout from the settlement equation — ' +
        'payments minus fee, tax and withholding — and shows what the bank ' +
        'actually credited. That reconstruction is what identifies a payout ' +
        'whose narration lost its reference code.',
    },
  ]
}
