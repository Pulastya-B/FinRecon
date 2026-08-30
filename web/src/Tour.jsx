import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
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
  const cardRef = useRef(null)
  const [clampTop, setClampTop] = useState(null)

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

  /*
   * placeCard picks a side using an ASSUMED card height, because it runs
   * during render when the card does not exist yet. A step with a long body is
   * taller than the assumption, and the estimator step ran off the bottom of
   * an 800px viewport that way. So measure the real thing once it is on screen
   * and pull it back up if it overflows.
   *
   * Guessing a height and then trusting the guess is the same mistake that put
   * CIRCUMSTANTIAL 6px outside its column.
   */
  useLayoutEffect(() => {
    const el = cardRef.current
    if (!el || card.bottom !== undefined) {
      setClampTop(null)
      return
    }
    const h = el.getBoundingClientRect().height
    const maxTop = window.innerHeight - h - MARGIN
    setClampTop(card.top > maxTop ? Math.max(MARGIN, maxTop) : null)
    // card.top is derived from box; depending on it directly would loop.
  }, [box, i]) // eslint-disable-line react-hooks/exhaustive-deps

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
        ref={cardRef}
        data-tour-card=""
        className="tour-card"
        onClick={(e) => e.stopPropagation()}
        style={
          card.bottom !== undefined
            ? { left: card.left, bottom: card.bottom }
            : { left: card.left, top: clampTop ?? card.top }
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

/*
 * The Evidence page.
 *
 * Figures here are hard-coded, unlike the queue's. That is not an oversight:
 * this page reads one committed report and does not change with the seed
 * selector, so 30 seeds, sd 0.00, the 14.30 gap and the 35x estimator error
 * are fixed properties of the artefact. If build_evidence.py is ever re-run
 * against different data these five sentences have to be re-read, and the
 * comment is here to say so.
 *
 * The order is the argument the page makes, not the order the sections sit in:
 * the claim, the honesty of the claim, the held-out test of it, then the two
 * places the engine was measured against itself.
 */
export function evidenceSteps() {
  return [
    {
      target: '[data-tour="ev-headline"]',
      eyebrow: 'The objection',
      title: 'Thirty datasets the engine had never seen',
      body:
        'A result on one dataset proves the dataset was chosen well. These are ' +
        '30 generated fresh and never looked at during development, and ' +
        'precision came back identical on every one — a standard deviation of ' +
        '0.00, not a mean of near-misses.',
    },
    {
      target: '[data-tour="ev-ranges"]',
      eyebrow: 'The honesty',
      title: 'Ranges, not best case',
      body:
        'Every seed the sweep produced is in this table, including the ones ' +
        'that went worst — coverage bottoms out at 47.30% and exception ' +
        'accuracy at 68.57%, each with the seed named. Nothing was dropped for ' +
        'looking bad.',
    },
    {
      target: '[data-held-out]',
      eyebrow: 'The held-out set',
      title: 'Seed 99, sealed on day one, run once',
      body:
        'Generated before the engine existed and opened only after the numbers ' +
        'were frozen. Precision did not move — 524 claims, zero wrong. ' +
        'Coverage fell 14.30 points, which is what a held-out set exists to ' +
        'expose, and it still lands inside the range the 30 seeds established.',
    },
    {
      target: '[data-tour="ev-tolerance"]',
      eyebrow: 'The tuning',
      title: 'Widening the band buys nothing',
      body:
        'Drag it. From 2 paise to ₹100 — a 5,000× widening — coverage and ' +
        'precision do not move at all. Past that they move together: every ' +
        'extra order the wider band matches is matched wrongly, so the engine ' +
        'buys coverage only by guessing.',
    },
    {
      target: '[data-tour="ev-estimator"]',
      eyebrow: 'Checking itself',
      title: 'It caught its own estimator being wrong',
      body:
        'The formula for how often a match fits by chance assumed amounts ' +
        'spread evenly. Measured against the real distribution it was 35× ' +
        'optimistic — so 9 of 13 attributions dropped to REFUSE.',
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
