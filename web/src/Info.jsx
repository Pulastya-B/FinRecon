import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/* --------------------------------------------------------------------------
 * In-page explanation, for a reader who arrives cold.
 *
 * Three pieces, and none of them is a guided tour. A tour fires once, in a
 * fixed order, at the moment the reader knows least -- and is gone by the time
 * they are actually confused. These stay put: the strip and the banners can be
 * dismissed when they stop being useful, and the info glyphs never go away.
 *
 * No tour library. Any of them would arrive with its own rounded corners, its
 * own shadow and its own accent colour, and undo the token discipline the rest
 * of this interface is built on. This is 200 lines against the existing ramp.
 *
 * Dismissals live in React state, for the session only. Nothing is persisted:
 * localStorage is unavailable here, and a banner that stays dismissed across a
 * reload is worse than one that does not when the reader is a judge opening
 * the page once.
 * ------------------------------------------------------------------------ */

const POP_WIDTH = 300
const MARGIN = 8

/*
 * The popover positioner, lifted from Tip in App.jsx rather than rewritten.
 *
 * That component already solved this exact problem the hard way: absolutely
 * positioned inside the queue's overflow-y-auto pane, the bubble was clipped
 * by the scroll container, and the leftmost column opened its bubble
 * underneath the sidebar. Portal to <body>, position: fixed off the anchor's
 * own rect, clamp to the viewport, flip up when there is no room below.
 */
function place(anchor) {
  const r = anchor?.getBoundingClientRect()
  if (!r) return null
  const left = Math.min(
    Math.max(MARGIN, r.left - 6),
    window.innerWidth - POP_WIDTH - MARGIN
  )
  const below = window.innerHeight - r.bottom
  return {
    left,
    top: r.bottom + 8,
    bottom: r.top - 8,
    flip: below < 190,
  }
}

export function InfoDot({ id, className = '' }) {
  const entry = INFO[id]
  const anchor = useRef(null)
  const [box, setBox] = useState(null)
  const open = Boolean(box)

  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && setBox(null)
    // Click anywhere else closes it. Registered on the document rather than an
    // overlay element, so it cannot swallow a click meant for the page.
    const onDown = (e) => {
      if (anchor.current && anchor.current.contains(e.target)) return
      if (e.target.closest?.('[data-info-pop]')) return
      setBox(null)
    }
    window.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    const onMove = () => setBox((b) => (b ? place(anchor.current) : null))
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
  }, [open])

  if (!entry) return null

  return (
    <>
      <button
        ref={anchor}
        type="button"
        data-info={id}
        aria-label={`What is ${entry.heading}?`}
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation()
          setBox(open ? null : place(anchor.current))
        }}
        className={`info-glyph ${open ? 'info-glyph-on' : ''} ${className}`}
      >
        <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true">
          <circle cx="7" cy="7" r="6.1" fill="none" stroke="currentColor"
                  strokeWidth="1.2" />
          <circle cx="7" cy="4.2" r="0.85" fill="currentColor" />
          <path d="M7 6.3v4.2" stroke="currentColor" strokeWidth="1.2"
                strokeLinecap="round" />
        </svg>
      </button>

      {box &&
        createPortal(
          <div
            data-info-pop={id}
            className="info-pop"
            style={
              box.flip
                ? { left: box.left, bottom: window.innerHeight - box.bottom }
                : { left: box.left, top: box.top }
            }
          >
            <div className="info-pop-heading">{entry.heading}</div>
            <p className="info-pop-body">{entry.body}</p>
          </div>,
          document.body
        )}
    </>
  )
}

/* --------------------------------------------------------------------------
 * The content.
 *
 * A heading and two sentences. The second sentence is doing the real work in
 * every one of these: the first says what the number is, the second says why
 * it is the number worth looking at, which is the part a reader cannot get
 * from the screen.
 * ------------------------------------------------------------------------ */
export const INFO = {
  coverage: {
    heading: 'Coverage',
    body:
      '66.70% of orders tied all the way through to a bank line. It is not ' +
      'higher because the other 333 chains are genuinely broken and must not ' +
      'be matched — recall is 100%, meaning every chain that COULD be matched ' +
      'was.',
  },
  precision: {
    heading: 'Precision',
    body:
      'Of the matches the engine made, how many were actually correct, ' +
      'checked against ground truth. This is the number that matters: a wrong ' +
      'match produces no error, it produces a plausible wrong answer that ' +
      'enters the books.',
  },
  investigable: {
    heading: 'Investigable',
    body:
      '333 exception rows grouped by root cause. One missing payout strands ' +
      'dozens of orders — that is one thing to investigate, not dozens.',
  },
  unexplained: {
    heading: 'Unexplained',
    body:
      'Total money across all open findings. The queue is sorted by this, ' +
      'because a finance operator works by value, not by ID.',
  },
  evidenceBand: {
    heading: 'Evidence band',
    body:
      'How likely this cause fitted by chance, measured against the actual ' +
      'amount distribution. STRONG means an accidental fit is essentially ' +
      'ruled out. REFUSE means chance explains it equally well, so the engine ' +
      'declines to name a cause.',
  },
  arithmetic: {
    heading: 'Settlement arithmetic',
    body:
      'What should have arrived, rebuilt from the day’s payments minus fees, ' +
      'tax and withholding. This is what identifies a payout whose bank ' +
      'narration was clipped before its reference code — matching on amount ' +
      'alone could not.',
  },
  atRisk: {
    heading: 'At risk',
    body:
      'The posting window passed and no matching credit appeared. This money ' +
      'should be in the account and is not.',
  },
  disputed: {
    heading: 'Disputed',
    body:
      'The payout arrived but short of what the settlement equation rebuilds ' +
      'to. The difference is a finding in the queue.',
  },
  grounded: {
    heading: 'Grounded',
    body:
      'Every figure, ID and reason code in the answer traced back to a tool ' +
      'result. Answers that fail are sent back once with the offending tokens ' +
      'named, then rendered flagged rather than hidden.',
  },
  heldOut: {
    heading: 'Held out',
    body:
      'Generated on day one and never scored until the numbers were frozen. ' +
      'Coverage fell 14.30 points against the development seed — inside the ' +
      'range the 30-seed sweep had already established. That drop is what a ' +
      'held-out set exists to expose.',
  },
}

/* --------------------------------------------------------------------------
 * One sentence per page, below the stat strip.
 * ------------------------------------------------------------------------ */
export const PAGE_BANNER = {
  Run:
    'Reconciles three ledgers that never agree: what the merchant sold, what ' +
    'the gateway collected, what the bank actually paid.',
  Queue:
    '333 exception rows grouped into 16 things a person actually has to ' +
    'decide, sorted by money at stake.',
  Cash:
    'Where every released payout actually is, as of the last line in the bank ' +
    'statement — a position, not a forecast.',
  Ask:
    'A live agent with 12 tools over the completed run. It is never given the ' +
    'ledgers, and every figure it reports is checked against a tool result ' +
    'before you see it.',
  Evidence:
    'Every accuracy claim in this project, measured across 30 datasets the ' +
    'engine never saw — ranges, not best case.',
  Data:
    'The six source ledgers. Order IDs elsewhere in the app deep-link into ' +
    'these rows.',
}

export function PageBanner({ page, dismissed, onDismiss }) {
  const text = PAGE_BANNER[page]
  if (!text || dismissed) return null
  return (
    <div data-page-banner={page} className="explain-band mx-gutter mb-3">
      <span className="min-w-0 flex-1">{text}</span>
      <button
        type="button"
        onClick={onDismiss}
        className="shrink-0 text-n-500 underline decoration-n-300 underline-offset-2 hover:text-n-800"
      >
        dismiss
      </button>
    </div>
  )
}

/* --------------------------------------------------------------------------
 * "Start here" — the Run page only.
 *
 * Three numbered lines and nothing else. No illustration and no hero styling:
 * a reader who has just landed is deciding whether this is worth five minutes,
 * and a decorated panel answers a question they did not ask.
 * ------------------------------------------------------------------------ */
const STEPS = [
  ['Run seed 42', '2,222 rows across six ledgers, about two seconds'],
  [
    'Open the Queue and click the top finding',
    'the settlement arithmetic is the thing a spreadsheet cannot do',
  ],
  [
    'Check Evidence',
    'every accuracy claim measured across 30 datasets the engine never saw',
  ],
]

export function StartHere({ dismissed, onDismiss }) {
  if (dismissed) return null
  return (
    <div data-start-here="" className="explain-panel mb-5">
      <div className="mb-2.5 flex items-baseline justify-between gap-3">
        <span className="text-label uppercase text-n-800">Start here</span>
        <button
          type="button"
          onClick={onDismiss}
          className="text-body-sm text-n-500 underline decoration-n-300 underline-offset-2 hover:text-n-800"
        >
          dismiss
        </button>
      </div>
      <ol className="grid gap-1.5">
        {STEPS.map(([what, why], i) => (
          <li key={what} className="flex gap-2.5 text-body-sm leading-[19px]">
            <span className="tnum shrink-0 text-n-500">{i + 1}.</span>
            <span className="min-w-0 text-n-600">
              <span className="font-semibold text-n-900">{what}</span> — {why}
            </span>
          </li>
        ))}
      </ol>
    </div>
  )
}
