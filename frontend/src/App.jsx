import { useEffect, useState } from 'react'
import { ask, health } from './api.js'
import ResultChart from './components/ResultChart.jsx'
import ResultTable from './components/ResultTable.jsx'

const EXAMPLES = [
  "Les clients qui ont commande plus de 3 fois ce mois sans ouvrir aucun email",
  "Chiffre d'affaires par mois sur les 6 derniers mois",
  "Top 10 des produits les plus vendus par quantite",
  "Repartition des commandes par statut",
]

function StatusBadge({ state }) {
  const label = {
    ok: 'Base et modele connectes',
    degraded: 'Service degrade',
    down: 'API injoignable',
    loading: 'Verification…',
  }[state]
  return (
    <span className={`status ${state}`}>
      <span className="dot" />
      {label}
    </span>
  )
}

function ErrorPanel({ error }) {
  const stageLabel = {
    generation: 'Question non traduisible',
    validation: 'Requete refusee par la securite',
    explain: 'Requete trop couteuse',
    llm: 'Service de generation indisponible',
  }[error.stage] || 'Erreur'

  return (
    <div className="panel error">
      <span className="stage">{stageLabel}</span>
      <p>{error.message}</p>
      {error.attempts?.length > 0 && (
        <details className="attempts">
          <summary>Voir les {error.attempts.length} tentative(s) refusee(s)</summary>
          {error.attempts.map((attempt, i) => (
            <div key={i}>
              <pre>{attempt.sql}</pre>
              <div>Motif : {attempt.error}</div>
            </div>
          ))}
        </details>
      )}
    </div>
  )
}

export default function App() {
  const [question, setQuestion] = useState('')
  const [outcome, setOutcome] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState('loading')

  useEffect(() => {
    health()
      .then((h) => setStatus(h.status === 'ok' ? 'ok' : 'degraded'))
      .catch(() => setStatus('down'))
  }, [])

  async function submit(text, { dryRun = false } = {}) {
    const value = (text ?? question).trim()
    if (value.length < 3 || loading) return

    setLoading(true)
    setError(null)
    setOutcome(null)
    try {
      setOutcome(await ask(value, { dryRun }))
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }

  function onKeyDown(event) {
    // Entree envoie, Maj+Entree passe a la ligne : la question tient en general
    // sur une ligne, mais on n'empeche pas d'en ecrire plusieurs.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="app">
      <div className="header">
        <h1>QueryForge</h1>
        <StatusBadge state={status} />
      </div>
      <p className="tagline">
        Posez votre question en francais. Le SQL est genere, verifie, borne, puis
        execute en lecture seule sur votre base.
      </p>

      <div className="ask">
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ex : montre-moi les clients qui ont commande plus de 3 fois ce mois sans ouvrir aucun email"
          aria-label="Question en langage naturel"
        />
        <div className="ask-actions">
          <span className="hint">Entree pour envoyer · Maj+Entree pour un retour a la ligne</span>
          <span style={{ display: 'flex', gap: 8 }}>
            <button onClick={() => submit(undefined, { dryRun: true })} disabled={loading}>
              Simuler
            </button>
            <button className="primary" onClick={() => submit()} disabled={loading}>
              {loading && <span className="spinner" />}
              {loading ? 'Generation…' : 'Interroger'}
            </button>
          </span>
        </div>
      </div>

      {!outcome && !error && !loading && (
        <div className="examples">
          {EXAMPLES.map((example) => (
            <button key={example} onClick={() => { setQuestion(example); submit(example) }}>
              {example}
            </button>
          ))}
        </div>
      )}

      {error && <ErrorPanel error={error} />}

      {outcome && (
        <>
          <div className="panel">
            <h2>Explication</h2>
            <p className="explanation">{outcome.explanation}</p>
            {outcome.assumptions?.length > 0 && (
              <ul className="notes">
                {outcome.assumptions.map((a, i) => <li key={i}>Hypothese : {a}</li>)}
              </ul>
            )}
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2>SQL genere</h2>
              <div className="meta">
                {outcome.estimated_cost != null && (
                  <span>cout estime {Math.round(outcome.estimated_cost).toLocaleString('fr-FR')}</span>
                )}
                {outcome.attempts > 1 && <span>{outcome.attempts} tentatives</span>}
              </div>
            </div>
            <pre className="sql">{outcome.sql}</pre>
            {outcome.notes?.length > 0 && (
              <ul className="notes">
                {outcome.notes.map((note, i) => <li key={i}>{note}</li>)}
              </ul>
            )}
          </div>

          <ResultChart
            columns={outcome.columns}
            columnTypes={outcome.column_types}
            rows={outcome.rows}
            chartHint={outcome.chart_hint}
          />

          <ResultTable
            columns={outcome.columns}
            columnTypes={outcome.column_types}
            rows={outcome.rows}
            rowCount={outcome.row_count}
            durationMs={outcome.duration_ms}
          />
        </>
      )}
    </div>
  )
}
