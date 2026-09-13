import { useEffect, useState } from 'react'
import { ApiError, listProperties } from '../api/client'
import { PropertyCard } from '../components/PropertyCard'
import { SearchBar } from '../components/SearchBar'
import type { PropertyFilters, PropertyListItem } from '../types'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; properties: PropertyListItem[] }

export function Feed() {
  const [filters, setFilters] = useState<PropertyFilters>({})
  const [state, setState] = useState<LoadState>({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })

    listProperties(filters)
      .then((properties) => {
        if (!cancelled) setState({ status: 'ready', properties })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const message =
          err instanceof ApiError ? err.message : 'Could not reach the API. Is it running?'
        setState({ status: 'error', message })
      })

    return () => {
      cancelled = true
    }
  }, [filters])

  return (
    <>
      <SearchBar onSearch={setFilters} />
      <main className="mx-auto max-w-6xl px-8 py-7">
        {state.status === 'loading' && <p className="text-sm text-ink-soft">Loading properties...</p>}

        {state.status === 'error' && (
          <div className="py-20 text-center text-red-700">
            {state.message}
            <div className="mt-1 text-sm text-ink-soft">Is the API running?</div>
          </div>
        )}

        {state.status === 'ready' && state.properties.length === 0 && (
          <div className="py-20 text-center text-ink-soft">No properties match these filters.</div>
        )}

        {state.status === 'ready' && state.properties.length > 0 && (
          <>
            <p className="mb-4 text-sm text-ink-soft">
              {state.properties.length} propert{state.properties.length === 1 ? 'y' : 'ies'}
            </p>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-6">
              {state.properties.map((p) => (
                <PropertyCard key={p.id} property={p} />
              ))}
            </div>
          </>
        )}
      </main>
    </>
  )
}
