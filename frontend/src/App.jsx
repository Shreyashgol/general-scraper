import { useCallback, useEffect, useRef, useState } from 'react'

// Served from the API host in production; proxied by Vite in dev.
const API = ''
const POLL_MS = 1500

const TERMINAL = ['done', 'error', 'blocked']

async function postJob(body) {
  const response = await fetch(`${API}/api/jobs`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    // FastAPI validation errors arrive as a list of {msg, loc}.
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => d.msg).join('; ')
      : data.detail
    throw new Error(detail || `Request failed (${response.status})`)
  }
  return data
}

export default function App() {
  const [url, setUrl] = useState('')
  const [maxProducts, setMaxProducts] = useState(200)
  const [ignoreRobots, setIgnoreRobots] = useState(false)
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const pollRef = useRef(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  // Poll while the job is in flight; always tear the timer down on unmount.
  useEffect(() => {
    if (!job || TERMINAL.includes(job.status)) {
      stopPolling()
      return
    }
    if (pollRef.current) return

    pollRef.current = setInterval(async () => {
      try {
        const response = await fetch(`${API}/api/jobs/${job.id}`)
        if (!response.ok) throw new Error('Lost track of the job')
        setJob(await response.json())
      } catch (e) {
        setError(e.message)
        stopPolling()
      }
    }, POLL_MS)

    return stopPolling
  }, [job, stopPolling])

  async function onSubmit(event) {
    event.preventDefault()
    setError(null)
    setJob(null)
    setSubmitting(true)
    try {
      setJob(await postJob({ url, max_products: Number(maxProducts), ignore_robots: ignoreRobots }))
    } catch (e) {
      setError(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  const running = job && !TERMINAL.includes(job.status)

  return (
    <main className="wrap">
      <header>
        <h1>Scrape Platform</h1>
        <p className="sub">
          Paste a product or category URL. We detect the site, extract every product, and hand
          you a CSV.
        </p>
      </header>

      <form onSubmit={onSubmit} className="card">
        <label htmlFor="url">Product or listing URL</label>
        <input
          id="url"
          type="url"
          required
          placeholder="https://example.com/collections/bags"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          disabled={running || submitting}
        />

        <div className="row">
          <div className="field">
            <label htmlFor="max">Max products</label>
            <input
              id="max"
              type="number"
              min="1"
              max="5000"
              value={maxProducts}
              onChange={(e) => setMaxProducts(e.target.value)}
              disabled={running || submitting}
            />
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={ignoreRobots}
              onChange={(e) => setIgnoreRobots(e.target.checked)}
              disabled={running || submitting}
            />
            <span>
              Ignore <code>robots.txt</code>
              <em>Only if you are authorized to scrape this site.</em>
            </span>
          </label>
        </div>

        <button type="submit" disabled={running || submitting || !url}>
          {running ? 'Scraping…' : submitting ? 'Starting…' : 'Scrape'}
        </button>
      </form>

      {error && <div className="card alert error">{error}</div>}
      {job && <JobPanel job={job} />}
    </main>
  )
}

function JobPanel({ job }) {
  const running = !TERMINAL.includes(job.status)

  return (
    <section className="card">
      <div className="statusline">
        <span className={`badge ${job.status}`}>{job.status}</span>
        {job.adapter ? (
          <span className="badge adapter" title="A hand-built adapter handles this site exactly">
            adapter: {job.adapter}
          </span>
        ) : (
          <span className="badge generic" title="No adapter for this site — results are best effort">
            generic crawler
          </span>
        )}
        {job.detected_pattern && <code className="pattern">{job.detected_pattern}</code>}
      </div>

      {job.status === 'blocked' && (
        <div className="alert warn">
          <strong>Blocked by robots.txt.</strong> {job.error}
          <br />
          Tick “Ignore robots.txt” only if you have authorization for this site.
        </div>
      )}
      {job.status === 'error' && <div className="alert error">{job.error}</div>}

      {running && (
        <p className="progress">
          Working… {job.exported} product{job.exported === 1 ? '' : 's'} so far
          <span className="dots" aria-hidden="true" />
        </p>
      )}

      {job.status === 'done' && (
        <dl className="stats">
          <div><dt>Exported</dt><dd>{job.exported}</dd></div>
          <div><dt>Discovered</dt><dd>{job.discovered}</dd></div>
          <div><dt>Failed</dt><dd>{job.failed}</dd></div>
          <div><dt>Invalid</dt><dd>{job.invalid}</dd></div>
        </dl>
      )}

      {job.warnings?.length > 0 && (
        <ul className="alert warn">
          {job.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      {job.has_csv && (
        <a className="download" href={`${API}/api/jobs/${job.id}/csv`} download>
          Download CSV ({job.exported} rows)
        </a>
      )}

      {job.preview?.length > 0 && <Preview rows={job.preview} total={job.exported} />}
    </section>
  )
}

function Preview({ rows, total }) {
  return (
    <>
      <h2>
        Preview <small>first {rows.length} of {total}</small>
      </h2>
      <div className="tablewrap">
        <table>
          <thead>
            <tr>
              <th>Image</th>
              <th>Name</th>
              <th>Category</th>
              <th>Subcategory</th>
              <th>MRP</th>
              <th>ASP</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.product_url}>
                <td>
                  <img src={row.image_url} alt="" loading="lazy" />
                </td>
                <td>{row.name}</td>
                <td>{row.category || '—'}</td>
                <td>{row.subcategory || '—'}</td>
                <td className="num">{row.mrp || '—'}</td>
                <td className="num">{row.asp || '—'}</td>
                <td>
                  <a href={row.product_url} target="_blank" rel="noreferrer">
                    open
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
