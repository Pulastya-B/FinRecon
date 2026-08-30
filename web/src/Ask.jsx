import React, { useEffect, useRef, useState } from 'react'
import { InfoDot } from './Info.jsx'

/* --------------------------------------------------------------------------
 * The Ask panel.
 *
 * The one place in this app that makes a live model call. Everything else is
 * cached and offline and stays that way.
 *
 * Two things are deliberately visible that a chat box would normally hide.
 *
 * THE TOOL CALLS. Watching the agent query the engine -- why_not(bank_000013),
 * settlement_breakdown(setl_...) -- is most of the point. An answer with its
 * working shown is a different artifact from an answer, and it is the
 * difference between "the model said so" and "the engine said so and here is
 * where it was asked".
 *
 * THE GROUNDING CHECK. Every figure, id and reason code in the reply is
 * checked against what the tools returned before it is rendered, and the
 * verdict is shown next to the answer. A guardrail nobody can see fire is
 * indistinguishable from one that is not there.
 * ------------------------------------------------------------------------ */

function ToolCall({ call, i }) {
  const args = Object.entries(call.arguments || {})
  return (
    <div className="flex items-baseline gap-2 py-[3px]">
      <span className="tnum w-[16px] shrink-0 text-right text-[10px] text-n-300">
        {i + 1}
      </span>
      <span
        className={`h-[6px] w-[6px] shrink-0 translate-y-[-1px] rounded-full ${
          call.ok ? 'bg-accent' : 'bg-warn'
        }`}
      />
      <code className="min-w-0 font-mono text-[11.5px] leading-[16px] text-n-700">
        <span className="font-semibold text-n-900">{call.tool}</span>
        <span className="text-n-500">
          (
          {args.map(([k, v], n) => (
            <span key={k}>
              {n > 0 && ', '}
              {k}=<span className="text-accent">{String(v)}</span>
            </span>
          ))}
          )
        </span>
      </code>
    </div>
  )
}

function Grounding({ grounding, corrected }) {
  const ok = grounding.ok
  const n = grounding.numbers?.verified?.length || 0
  // An invented figure and an unsupported inference are different failures.
  // A reader should be able to tell which one fired without parsing prose.
  const claims = grounding.unsupported_claims || []
  const label = ok
    ? 'grounded'
    : claims.length
    ? 'unsupported claim'
    : 'ungrounded'
  return (
    <div className="mt-3 border-t border-n-100 pt-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span
          className={`inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.04em] ${
            ok ? 'bg-accent/10 text-accent' : 'bg-danger/10 text-danger'
          }`}
        >
          <span
            className={`h-[6px] w-[6px] rounded-full ${
              ok ? 'bg-accent' : 'bg-danger'
            }`}
          />
          {label}
        </span>
        <InfoDot id="grounded" />
        <span className="text-body-sm text-n-500">
          {n > 0
            ? `${n} figure${n === 1 ? '' : 's'} traced back to engine output`
            : 'no figures in this answer required checking'}
        </span>
      </div>

      {!ok && (
        <ul className="mt-1.5 space-y-0.5">
          {grounding.problems.map((p) => (
            <li key={p} className="text-body-sm text-danger">
              · {p}
            </li>
          ))}
        </ul>
      )}

      {corrected && (
        <div className="mt-2 rounded border border-warn/30 bg-warn/[0.06] px-2.5 py-1.5 text-[11.5px] leading-[16px] text-n-600">
          <span className="font-semibold text-warn">
            A first answer was rejected and sent back.
          </span>{' '}
          {corrected.problems.join('; ')}
        </div>
      )}
    </div>
  )
}

export default function Ask({ seed, onOpenTrace, subject, onClearSubject }) {
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [turns, setTurns] = useState([])
  const [meta, setMeta] = useState(null)
  const endRef = useRef(null)

  useEffect(() => {
    fetch(`/api/ask/${seed}/suggested`)
      .then((r) => r.json())
      .then(setMeta)
      .catch(() => setMeta(null))
  }, [seed])

  // A new dataset invalidates every answer on screen: they were about the
  // previous run and nothing in them says so.
  useEffect(() => setTurns([]), [seed])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, busy])

  const send = async (text) => {
    const q = (text ?? question).trim()
    if (!q || busy) return
    setQuestion('')
    setBusy(true)
    const started = performance.now()
    try {
      const res = await fetch(`/api/ask/${seed}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, subject: subject?.id || null }),
      })
      const data = await res.json()
      setTurns((t) => [
        ...t,
        { q, data, seconds: ((performance.now() - started) / 1000).toFixed(1) },
      ])
    } catch (e) {
      setTurns((t) => [
        ...t,
        { q, data: { ok: false, error: 'network', message: String(e) } },
      ])
    } finally {
      setBusy(false)
    }
  }

  const noKey = meta && meta.key_present === false

  return (
    <div className="flex min-h-0 flex-1 flex-col px-gutter pb-gutter pt-6">
      <div className="mx-auto flex min-h-0 w-full max-w-[880px] flex-1 flex-col">
        <div className="mb-1 flex items-baseline gap-3">
          <h1 className="text-title text-n-900">Ask the run</h1>
          {meta?.model && (
            <span className="font-mono text-[11px] text-n-500">{meta.model}</span>
          )}
        </div>
        <p className="mb-4 max-w-[76ch] text-body-sm leading-relaxed text-n-500">
          A live model with tools that query this reconciliation run. It cannot
          see the ledgers — it can only ask the engine, and every figure it
          reports is checked against what the engine returned before you see it.
          It is for what the queue <em>cannot</em> show you: totals across all
          findings, patterns over hundreds of declined rows, and what a decline
          would have needed to match.
        </p>

        {noKey && (
          <div className="mb-4 rounded-lg border border-warn/30 bg-warn/[0.06] px-4 py-3 text-body-sm leading-relaxed text-n-700">
            <span className="font-semibold">No API key configured.</span> This is
            the only feature that needs one — set{' '}
            <span className="font-mono text-[12px]">MISTRAL_API_KEY</span>, or
            put it in a <span className="font-mono text-[12px]">.env</span> file.
            The rest of the app runs without it.
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {turns.length === 0 && !busy && (
            <div className="panel p-panel">
              <div className="mb-2.5 text-label uppercase text-n-600">
                Try one of these
              </div>
              <div className="flex flex-col items-start gap-1.5">
                {(meta?.questions || []).map((q) => (
                  <button
                    key={q}
                    onClick={() => send(q)}
                    className="rounded border border-n-200 bg-n-0 px-2.5 py-1.5 text-left text-body-sm text-n-700 transition-colors hover:border-accent hover:text-accent"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((turn, i) => (
            <div key={i} className="mb-4">
              <div className="mb-2 flex items-baseline gap-2">
                <span className="text-label uppercase text-n-500">You</span>
                <span className="text-body text-n-900">{turn.q}</span>
              </div>

              {turn.data.ok === false ? (
                <div className="panel border-danger/30 p-panel">
                  <div className="text-body-sm font-semibold text-danger">
                    {turn.data.error === 'no_api_key'
                      ? 'No API key'
                      : 'The question could not be answered'}
                  </div>
                  <div className="mt-1 text-body-sm text-n-600">
                    {turn.data.message}
                  </div>
                </div>
              ) : (
                <div className="panel p-panel">
                  {turn.data.tool_calls?.length > 0 && (
                    <div className="mb-3 rounded border border-n-100 bg-n-25 px-2.5 py-2">
                      <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-n-500">
                        asked the engine
                      </div>
                      {turn.data.tool_calls.map((c, n) => (
                        <ToolCall key={n} call={c} i={n} />
                      ))}
                    </div>
                  )}

                  <div className="whitespace-pre-wrap text-body leading-relaxed text-n-800">
                    {renderAnswer(turn.data.answer, onOpenTrace)}
                  </div>

                  {turn.data.note && (
                    <div className="mt-2 text-body-sm italic text-n-500">
                      {turn.data.note}
                    </div>
                  )}

                  {turn.data.grounding && (
                    <Grounding
                      grounding={turn.data.grounding}
                      corrected={turn.data.corrected}
                    />
                  )}

                  <div className="mt-2 text-[11px] text-n-300">
                    {turn.seconds}s · {turn.data.steps} tool call
                    {turn.data.steps === 1 ? '' : 's'}
                  </div>
                </div>
              )}
            </div>
          ))}

          {busy && (
            <div className="panel flex items-center gap-2.5 p-panel">
              <span className="h-[7px] w-[7px] animate-pulse rounded-full bg-accent" />
              <span className="text-body-sm text-n-500">
                querying the engine…
              </span>
            </div>
          )}
          <div ref={endRef} />
        </div>

        {/* The referenced finding, carried in from the queue.
            Typing setl_20260722_019 by hand is the difference between a tool
            and a toy, and "this one" is how a person refers to the row in
            front of them. */}
        {subject && (
          <div
            data-ask-subject=""
            className="mt-3 flex items-center gap-2 rounded-md border border-accent/30 bg-accent-bg px-3 py-2"
          >
            <span className="text-[10px] font-semibold uppercase tracking-[0.06em] text-accent">
              About
            </span>
            <span className="min-w-0 flex-1 truncate text-body-sm text-n-800">
              {subject.headline || subject.id}
              {subject.amount && (
                <span className="tnum ml-2 font-semibold text-n-900">
                  {subject.amount}
                </span>
              )}
            </span>
            <button
              onClick={onClearSubject}
              title="Ask about the whole run instead"
              className="shrink-0 rounded px-1.5 text-body-sm text-n-500 hover:bg-n-0 hover:text-n-900"
            >
              ✕
            </button>
          </div>
        )}

        {(subject ? meta?.questions_for_subject : null)?.length > 0 &&
          turns.length === 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {meta.questions_for_subject.map((q) => (
                <button
                  key={q}
                  onClick={() => send(q)}
                  className="rounded-full border border-n-200 bg-n-0 px-2.5 py-1 text-[11.5px] text-n-600 transition-colors hover:border-accent hover:text-accent"
                >
                  {q}
                </button>
              ))}
            </div>
          )}

        <form
          onSubmit={(e) => {
            e.preventDefault()
            send()
          }}
          className="mt-3 flex gap-2"
        >
          <input
            data-ask-input=""
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={
              subject
                ? 'What would it have taken for this to match?'
                : 'Which cause accounts for most of the queue?'
            }
            className="min-w-0 flex-1 rounded border border-n-200 bg-n-0 px-3 py-2 text-body text-n-900 outline-none placeholder:text-n-300 focus:border-accent"
          />
          <button
            data-ask-send=""
            type="submit"
            disabled={busy || !question.trim()}
            className="shrink-0 rounded bg-accent px-4 py-2 text-body-sm font-semibold text-n-0 shadow-[0_1px_2px_rgba(11,102,239,0.35)] hover:brightness-110 disabled:opacity-40"
          >
            Ask
          </button>
        </form>
      </div>
    </div>
  )
}

/* Ids in the answer become links into the trace, because the next question
   after "why wasn't setl_x matched" is always "show me". Built by splitting on
   a pattern rather than by injecting markup into model output -- prose from a
   model is untrusted text and string-replacing HTML into it is how that
   becomes an injection point. */
const ID_RE = /\b(?:ord|pay|setl|bank|rfnd|cb)_[A-Za-z0-9_]+\b/g

// The model writes markdown whether or not it is asked to, and **bold** was
// rendering as literal asterisks around every figure -- the exact numbers the
// eye should land on were the ugliest things on screen. Only bold and inline
// code are honoured; anything else stays literal, because this is untrusted
// text and a general markdown renderer is a larger surface than the feature
// needs.
const MD_RE = /\*\*([^*]+)\*\*|`([^`]+)`/g

function linkify(text, onOpenTrace, keyPrefix) {
  const out = []
  let last = 0
  for (const m of text.matchAll(ID_RE)) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const id = m[0]
    out.push(
      <button
        key={`${keyPrefix}-${id}-${m.index}`}
        onClick={() => onOpenTrace?.(id)}
        title={`Why wasn't ${id} matched?`}
        className="font-mono text-[12.5px] text-accent underline decoration-accent/30 underline-offset-2 hover:decoration-accent"
      >
        {id}
      </button>
    )
    last = m.index + id.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function renderAnswer(text, onOpenTrace) {
  if (!text) return null
  const out = []
  let last = 0
  let i = 0
  for (const m of text.matchAll(MD_RE)) {
    if (m.index > last) {
      out.push(...linkify(text.slice(last, m.index), onOpenTrace, `p${i}`))
    }
    const [raw, bold, code] = m
    if (bold !== undefined) {
      out.push(
        <span key={`b${i}`} className="font-semibold text-n-900">
          {linkify(bold, onOpenTrace, `b${i}`)}
        </span>
      )
    } else {
      out.push(
        <span key={`c${i}`} className="font-mono text-[12.5px] text-n-800">
          {linkify(code, onOpenTrace, `c${i}`)}
        </span>
      )
    }
    last = m.index + raw.length
    i += 1
  }
  if (last < text.length) {
    out.push(...linkify(text.slice(last), onOpenTrace, `p${i}`))
  }
  return out
}
