function format(value, type) {
  if (value === null || value === undefined) return null
  if (type === 'number' && typeof value === 'number') {
    return Number.isInteger(value)
      ? value.toLocaleString('fr-FR')
      : value.toLocaleString('fr-FR', { maximumFractionDigits: 2 })
  }
  if (typeof value === 'boolean') return value ? 'vrai' : 'faux'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function ResultTable({ columns, columnTypes, rows, rowCount, durationMs }) {
  if (!columns.length) {
    return (
      <div className="panel">
        <h2>Resultat</h2>
        <p className="empty">Aucune colonne retournee.</p>
      </div>
    )
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Resultat</h2>
        <div className="meta">
          <span>{rowCount.toLocaleString('fr-FR')} ligne{rowCount > 1 ? 's' : ''}</span>
          <span>{durationMs} ms</span>
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="empty">
          La requete est valide mais ne renvoie aucune ligne. Le filtre est peut-etre
          trop restrictif, ou la periode demandee est vide.
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((col) => (
                  <th key={col} className={columnTypes[col] === 'number' ? 'number' : ''}>
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {columns.map((col) => {
                    const formatted = format(row[col], columnTypes[col])
                    const isNumber = columnTypes[col] === 'number'
                    return (
                      <td
                        key={col}
                        className={[isNumber ? 'number' : '', formatted === null ? 'null' : '']
                          .filter(Boolean)
                          .join(' ')}
                      >
                        {formatted === null ? 'NULL' : formatted}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
