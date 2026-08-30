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

/*
 * `body` overrides the stored text.
 *
 * Two of these explanations quote figures from the run -- coverage and the
 * exception-row count -- and the stored versions were seed 42's. On seed 7 the
 * INVESTIGABLE popover said "333 exception rows" while the strip two
 * centimetres above it said 430. Callers that have the live summary pass the
 * sentence in; the rest are seed-independent and use the constant.
 */
export function InfoDot({ id, className = '', body }) {
  const stored = INFO[id]
  const entry = stored && body ? { ...stored, body } : stored
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
