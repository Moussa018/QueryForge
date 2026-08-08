import {
  Bar, BarChart, CartesianGrid, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

// Ordre fixe des teintes : le slot depend de la position de la serie, jamais de
// son rang. Un filtre qui retire une serie ne doit pas repeindre les autres.
// Les couleurs sont lues en CSS var pour suivre le theme clair/sombre.
const SERIES = [
  'var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)',
  'var(--series-5)', 'var(--series-6)', 'var(--series-7)', 'var(--series-8)',
]

const MAX_SERIES = 8
const MAX_CATEGORIES = 30

/**
 * Choisit la forme a partir de la *forme des donnees*, l'indication du modele ne
 * servant que de departage. Le modele se trompe sur le type de graphique bien
 * plus souvent qu'il ne se trompe sur le SQL.
 */
export function planChart({ columns, columnTypes, rows, chartHint }) {
  if (!rows?.length || columns.length < 2) return null

  const numeric = columns.filter((c) => columnTypes[c] === 'number')
  const temporal = columns.filter((c) => columnTypes[c] === 'temporal')
  const categorical = columns.filter(
    (c) => columnTypes[c] === 'string' || columnTypes[c] === 'boolean',
  )

  if (!numeric.length) return null

  // Une serie temporelle se lit en ligne : l'axe X est continu et ordonne.
  if (temporal.length) {
    return {
      type: 'line',
      xKey: temporal[0],
      series: numeric.slice(0, MAX_SERIES),
    }
  }

  if (categorical.length) {
    // Trop de categories : un axe illisible. Le tableau reste la source complete.
    if (rows.length > MAX_CATEGORIES) return null
    return {
      type: 'bar',
      xKey: categorical[0],
      // 'pie' est volontairement rendu en barres : comparer des longueurs sur
      // une base commune est plus fiable que comparer des angles.
      series: numeric.slice(0, MAX_SERIES),
      downgradedFrom: chartHint === 'pie' ? 'pie' : null,
    }
  }

  return null
}

function formatValue(value) {
  if (typeof value !== 'number') return String(value ?? '—')
  if (Number.isInteger(value)) return value.toLocaleString('fr-FR')
  return value.toLocaleString('fr-FR', { maximumFractionDigits: 2 })
}

function shortenLabel(value) {
  const text = String(value ?? '')
  if (text.length <= 14) return text
  // Une date ISO se reduit a sa partie calendaire.
  if (/^\d{4}-\d{2}-\d{2}T/.test(text)) return text.slice(0, 10)
  return `${text.slice(0, 13)}…`
}

function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="chart-tooltip">
      <div className="label">{shortenLabel(label)}</div>
      {payload.map((entry) => (
        <div className="row" key={entry.dataKey}>
          <span className="swatch" style={{ background: entry.color }} />
          <span>{entry.dataKey} : {formatValue(entry.value)}</span>
        </div>
      ))}
    </div>
  )
}

const AXIS = {
  stroke: 'var(--baseline)',
  tick: { fill: 'var(--text-muted)', fontSize: 11 },
  tickLine: false,
}

export default function ResultChart({ columns, columnTypes, rows, chartHint }) {
  const plan = planChart({ columns, columnTypes, rows, chartHint })
  if (!plan) return null

  const data = rows.map((row) => ({ ...row, __x: shortenLabel(row[plan.xKey]) }))
  const multi = plan.series.length > 1
  const Chart = plan.type === 'line' ? LineChart : BarChart

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Visualisation</h2>
        {plan.downgradedFrom && (
          <span className="hint">camembert rendu en barres (lecture plus fiable)</span>
        )}
      </div>

      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height="100%">
          <Chart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
            {/* Grille horizontale seule : les verticales n'aident pas la lecture
                d'une magnitude et ajoutent du bruit. */}
            <CartesianGrid stroke="var(--gridline)" strokeWidth={1} vertical={false} />
            <XAxis dataKey="__x" {...AXIS} interval="preserveStartEnd" minTickGap={12} />
            <YAxis {...AXIS} width={56} tickFormatter={formatValue} />
            <Tooltip
              content={<ChartTooltip />}
              cursor={{ fill: 'var(--gridline)', fillOpacity: 0.35 }}
            />

            {plan.series.map((key, i) =>
              plan.type === 'line' ? (
                <Line
                  key={key}
                  type="monotone"
                  dataKey={key}
                  stroke={SERIES[i]}
                  strokeWidth={2}
                  dot={false}
                  // Le point survole doit rester lisible par-dessus la ligne :
                  // anneau de 2px a la couleur de la surface.
                  activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--surface-1)' }}
                />
              ) : (
                <Bar
                  key={key}
                  dataKey={key}
                  fill={SERIES[i]}
                  // Extremite arrondie de 4px cote valeur, base ancree a zero.
                  radius={[4, 4, 0, 0]}
                  maxBarSize={38}
                />
              ),
            )}
          </Chart>
        </ResponsiveContainer>
      </div>

      {/* L'identite d'une serie ne repose jamais sur la couleur seule. */}
      {multi && (
        <div className="legend">
          {plan.series.map((key, i) => (
            <span className="item" key={key}>
              <span className="swatch" style={{ background: SERIES[i] }} />
              {key}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
