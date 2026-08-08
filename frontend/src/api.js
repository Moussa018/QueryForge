const BASE = '/api'

async function request(path, options) {
  const response = await fetch(`${BASE}${path}`, options)
  const payload = await response.json().catch(() => null)

  if (!response.ok) {
    // FastAPI renvoie `detail`, tantot chaine tantot objet structure.
    const detail = payload?.detail
    if (detail && typeof detail === 'object') {
      const error = new Error(detail.message || 'Requete refusee.')
      error.stage = detail.stage
      error.attempts = detail.attempts || []
      throw error
    }
    throw new Error(detail || `Erreur ${response.status}`)
  }
  return payload
}

export function ask(question, { dryRun = false } = {}) {
  return request('/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, dry_run: dryRun }),
  })
}

export function health() {
  return request('/health')
}
