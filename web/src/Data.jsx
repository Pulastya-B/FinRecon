import React, { useEffect, useState } from 'react'

const PAGE = 50

async function api(path) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' } })
  if (!res.ok) throw new Error(`${res.status} ${path}`)
  return res.json()
}

const TABLES = [
  { key: 'orders', label: 'Orders' },
  { key: 'payments', label: 'Payments' },
  { key: 'refunds', label: 'Refunds' },
  { key: 'chargebacks', label: 'Chargebacks' },
  { key: 'settlements', label: 'Settlements' },
  { key: 'bank', label: 'Bank' },
]

/*
 * The linked-rows panel: the same money as it appears in each other ledger.
 *
 * This is the product's claim shown on one order. Each hop says HOW it was
 * made, because they are not the same kind of fact: order-to-payment and
 * payment-to-payout are declared in the gateway's own columns, and
 * payout-to-bank is a UTR recovered from free-text narration. That last hop is
 * the boundary the engine exists to cross, and a panel that drew all of them
 * identically would hide the only interesting thing on it.
 */
function Links({ links, onOpen, onFilter, table }) {
  if (!links) return null
  if (links.length === 0)
    return (
      <div className="px-4 py-3 text-body-sm text-n-500">
        {table === 'orders'
          ? 'Nothing in the gateway or the bank references this order. That IS the finding: an order that produced no payment is invisible to a gateway-against-bank comparison.'
          : 'Nothing else in the ledgers references this row.'}
      </div>
    )
  return (
    <div className="divide-y divide-n-100">
      {links.map((l, i) => (
        <div key={`${l.table}-${l.id}-${i}`} className="px-4 py-2">
          <div className="flex items-baseline gap-2">
            <span className="w-[86px] shrink-0 text-label uppercase text-n-500">
              {l.table}
            </span>
            {l.id ? (
              <button
                onClick={() => onOpen(l.table, l.id)}
                className="font-mono text-body-sm text-accent hover:underline"
              >
                {l.id}
              </button>
            ) : l.settlement ? (
              /* A payout contains many rows, so the link is to the filtered
                 set rather than to one id picked out of it. */
              <button
                onClick={() => onFilter(l.table, l.settlement)}
                className="text-body-sm text-accent hover:underline"
              >
                view {l.count} in {l.table}
              </button>
            ) : (
              <span className="text-body-sm italic text-n-500">not found</span>
            )}
          </div>
          <div className="mt-0.5 pl-[94px] text-body-sm text-n-500">
            {l.how}
          </div>
        </div>
      ))}
    </div>
  )
}

/*
 * The Data page: the six CSVs of the selected seed, as they sit on disk.
 *
 * Fifty rows at a time. orders.csv is a thousand rows and rendering all of
 * them so one can be highlighted spends the DOM on 999 nobody asked for -- and
 * the deep link already knows which row it wants, so the server returns the
 * window containing it rather than the client searching for it.
 *
 * Values are printed exactly as the file holds them: unparsed, unrounded, not
 * reformatted into rupees. This page exists so a reader can check the engine
 * against the source, and a viewer that prettifies what it shows cannot settle
 * an argument about what the source says.
 */
export default function DataPage({ seed, target, onTargetConsumed }) {
  const [table, setTable] = useState(target?.table || 'orders')
  const [offset, setOffset] = useState(0)
  const [rowId, setRowId] = useState(target?.rowId || null)
  // A payout filter, set when the detail pane sends "view in Data". Orders
  // carry no settlement id, so the server walks order -> payment -> settlement
  // to build this set; the client only names the payout.
  const [settlement, setSettlement] = useState(target?.settlement || null)
  const [page, setPage] = useState(null)
  const [links, setLinks] = useState(null)
  const [counts, setCounts] = useState({})

  useEffect(() => {
    api(`/api/data/${seed}`)
      .then((d) =>
        setCounts(Object.fromEntries(d.tables.map((t) => [t.key, t.rows])))
      )
      .catch(() => setCounts({}))
  }, [seed])

  // A target arriving from the queue overrides whatever was being browsed.
  useEffect(() => {
    if (!target) return
    setTable(target.table)
    setRowId(target.rowId)
    setSettlement(target.settlement || null)
    setOffset(0)
    onTargetConsumed?.()
  }, [target])

  useEffect(() => {
    const q = new URLSearchParams({ limit: String(PAGE) })
    if (rowId) q.set('row', rowId)
    else q.set('offset', String(offset))
    if (settlement) q.set('settlement', settlement)
    api(`/api/data/${seed}/${table}?${q}`).then((p) => {
      setPage(p)
      setOffset(p.offset)
    })
  }, [seed, table, rowId, offset, settlement])

  useEffect(() => {
    if (!rowId) return setLinks(null)
    api(`/api/data/${seed}/${table}/${encodeURIComponent(rowId)}/links`)
      .then((d) => setLinks(d.links))
      .catch(() => setLinks([]))
  }, [seed, table, rowId])

  const open = (nextTable, nextId) => {
    setTable(nextTable)
    setRowId(nextId)
    // Following a link out of a filtered view leaves the filter: the row being
    // opened need not be in the payout that was being browsed.
    setSettlement(null)
    setOffset(0)
    window.history.pushState(
      {},
      '',
      `/data/${seed}/${nextTable}?row=${encodeURIComponent(nextId)}`
    )
  }

  const browse = (nextOffset) => {
    setRowId(null)
    setOffset(nextOffset)
  }

  if (!page)
    return (
      <div className="px-6 py-6 text-body-sm text-n-500">Loading {table}…</div>
    )

  const money = new Set(page.money_columns)
  const last = Math.max(0, page.total - PAGE)

  return (
    <div className="flex min-h-0 flex-1 gap-3 px-gutter pb-gutter pt-4">
      <div className="panel flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* File switcher and pager, one row. */}
        <div className="flex shrink-0 items-center gap-1 border-b border-n-200 bg-n-25 px-3 py-2">
          {TABLES.map((t) => (
            <button
              key={t.key}
              onClick={() => {
                setTable(t.key)
                setRowId(null)
                setSettlement(null)
                setOffset(0)
              }}
              className={`rounded px-2 py-1 text-body-sm ${
                t.key === table
                  ? 'bg-accent-bg font-medium text-accent'
                  : 'text-n-500 hover:text-n-900'
              }`}
            >
              {t.label}
              {counts[t.key] != null && (
                <span className="tnum ml-1.5 text-[10.5px] opacity-70">
                  {counts[t.key].toLocaleString('en-IN')}
                </span>
              )}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-2">
            <span className="tnum text-body-sm text-n-500">
              {page.file} · rows {(page.offset + 1).toLocaleString('en-IN')}–
              {Math.min(page.offset + page.rows.length, page.total).toLocaleString('en-IN')} of{' '}
              {page.total.toLocaleString('en-IN')}
            </span>
            <button
              onClick={() => browse(Math.max(0, page.offset - PAGE))}
              disabled={page.offset === 0}
              className="rounded border border-n-200 px-2 py-0.5 text-body-sm text-n-900 disabled:opacity-40"
            >
              ←
            </button>
            <button
              onClick={() => browse(Math.min(last, page.offset + PAGE))}
              disabled={page.offset >= last}
              className="rounded border border-n-200 px-2 py-0.5 text-body-sm text-n-900 disabled:opacity-40"
            >
              →
            </button>
          </div>
        </div>

        {page.settlement && (
          <div className="flex shrink-0 items-center gap-2 border-b border-n-200 bg-accent-bg px-4 py-1.5 text-body-sm">
            <span className="text-n-500">filtered to payout</span>
            <span className="font-mono text-body-sm text-n-900">
              {page.settlement}
            </span>
            <span className="tnum text-n-500">
              · {page.total.toLocaleString('en-IN')} rows
            </span>
            <button
              onClick={() => {
                setSettlement(null)
                setOffset(0)
              }}
              className="ml-1 text-accent hover:underline"
            >
              show all
            </button>
          </div>
        )}

        {rowId && page.row_found === false && (
          <div className="shrink-0 border-b border-n-200 bg-[#FDF3E2] px-4 py-1.5 text-body-sm text-warn">
            {rowId} is not in {page.file}.
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full text-body-sm">
            <thead className="sticky top-0 bg-n-50">
              <tr className="border-b border-n-200 text-label uppercase text-n-600">
                <th className="w-[52px] py-1.5 pl-4 pr-2 text-right font-semibold">
                  #
                </th>
                {page.columns.map((c) => (
                  <th
                    key={c}
                    className={`py-1.5 pr-3 font-semibold ${
                      money.has(c) ? 'text-right' : 'text-left'
                    }`}
                  >
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {page.rows.map((r, i) => {
                const id = r[page.id_column]
                const hit = id === rowId
                return (
                  <tr
                    key={id}
                    onClick={() => open(table, id)}
                    className={`row-i cursor-pointer border-b border-n-100 ${
                      hit
                        ? 'bg-[#FEF6E0]'
                        : i % 2
                        ? 'bg-n-25 hover:bg-n-50'
                        : 'hover:bg-n-50'
                    }`}
                  >
                    <td
                      className={`tnum py-[5px] pr-2 text-right text-body-sm text-n-500 ${
                        hit ? 'border-l-2 border-warn pl-[14px]' : 'pl-4'
                      }`}
                    >
                      {page.offset + i + 1}
                    </td>
                    {page.columns.map((c) => (
                      <td
                        key={c}
                        className={`py-[5px] pr-3 ${
                          money.has(c)
                            ? 'tnum text-right text-n-900'
                            : c === page.id_column
                            ? 'font-mono text-[11px] text-n-900'
                            : 'text-n-500'
                        }`}
                      >
                        {r[c]}
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="pane-raised w-[330px] shrink-0 overflow-y-auto">
        <div className="border-b border-n-200 px-4 py-2.5">
          <div className="text-label uppercase text-n-500">
            Same money, other ledgers
          </div>
          <div className="mt-0.5 font-mono text-body-sm text-n-900">
            {rowId || 'select a row'}
          </div>
        </div>
        {rowId ? (
          <Links
            links={links}
            onOpen={open}
            table={table}
            onFilter={(nextTable, sid) => {
              setTable(nextTable)
              setRowId(null)
              setSettlement(sid)
              setOffset(0)
            }}
          />
        ) : (
          <div className="px-4 py-3 text-body-sm text-n-500">
            Click any row, or an order id in the queue, to follow it through the
            gateway to the bank statement.
          </div>
        )}
      </div>
    </div>
  )
}
