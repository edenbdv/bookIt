import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ApiError, createProperty, deleteProperty, listPropertiesByHost } from '../api/client'
import { PropertyReservations } from '../components/PropertyReservations'
import { useAuth } from '../context/useAuth'
import type { CancellationPolicy, PropertyListItem } from '../types'
import { CANCELLATION_POLICY_LABEL } from '../utils/cancellationPolicy'
import { formatPrice } from '../utils/format'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; properties: PropertyListItem[] }

const inputClass =
  'w-full rounded-lg border border-line px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none'

function AddPropertyForm({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [city, setCity] = useState('')
  const [country, setCountry] = useState('')
  const [maxGuests, setMaxGuests] = useState('2')
  const [pricePerNight, setPricePerNight] = useState('')
  const [cancellationPolicy, setCancellationPolicy] = useState<CancellationPolicy>('moderate')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await createProperty({
        title,
        description,
        city,
        country,
        max_guests: Number(maxGuests),
        // dollars in the form, cents over the wire -- same boundary as
        // BookingForm's formatPrice, just inverted
        price_per_night_cents: Math.round(Number(pricePerNight) * 100),
        cancellation_policy: cancellationPolicy,
      })
      onCreated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create this property.')
      setSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 grid grid-cols-2 gap-3 rounded-2xl border border-line bg-white p-5"
    >
      <label className="col-span-2 flex flex-col gap-1 text-sm font-semibold text-ink">
        Title
        <input required value={title} onChange={(e) => setTitle(e.target.value)} className={inputClass} />
      </label>
      <label className="col-span-2 flex flex-col gap-1 text-sm font-semibold text-ink">
        Description
        <textarea
          required
          rows={3}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className={inputClass}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm font-semibold text-ink">
        City
        <input required value={city} onChange={(e) => setCity(e.target.value)} className={inputClass} />
      </label>
      <label className="flex flex-col gap-1 text-sm font-semibold text-ink">
        Country
        <input required value={country} onChange={(e) => setCountry(e.target.value)} className={inputClass} />
      </label>
      <label className="flex flex-col gap-1 text-sm font-semibold text-ink">
        Max guests
        <input
          required
          type="number"
          min={1}
          value={maxGuests}
          onChange={(e) => setMaxGuests(e.target.value)}
          className={inputClass}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm font-semibold text-ink">
        Price / night ($)
        <input
          required
          type="number"
          min={0}
          step="1"
          value={pricePerNight}
          onChange={(e) => setPricePerNight(e.target.value)}
          className={inputClass}
        />
      </label>
      <label className="col-span-2 flex flex-col gap-1 text-sm font-semibold text-ink">
        Cancellation policy
        <select
          value={cancellationPolicy}
          onChange={(e) => setCancellationPolicy(e.target.value as CancellationPolicy)}
          className={inputClass}
        >
          {(Object.keys(CANCELLATION_POLICY_LABEL) as CancellationPolicy[]).map((policy) => (
            <option key={policy} value={policy}>
              {CANCELLATION_POLICY_LABEL[policy]}
            </option>
          ))}
        </select>
      </label>

      {error && (
        <p role="alert" className="col-span-2 text-sm text-red-700">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="col-span-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white hover:bg-accent-ink disabled:opacity-60"
      >
        {submitting ? 'Creating...' : 'Create property'}
      </button>
    </form>
  )
}

export function HostProperties() {
  const { session } = useAuth()
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [showForm, setShowForm] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  // id pending a second click to actually delete, and any error from that
  // attempt -- inline confirmation, not a browser confirm()/alert() dialog,
  // to match how every other action in this app surfaces state and errors
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const load = useCallback(() => {
    if (!session) return
    setState({ status: 'loading' })
    listPropertiesByHost(session.userId)
      .then((properties) => setState({ status: 'ready', properties }))
      .catch((err: unknown) => {
        const message = err instanceof ApiError ? err.message : 'Could not load your properties.'
        setState({ status: 'error', message })
      })
  }, [session])

  useEffect(load, [load])

  async function handleDelete(id: string) {
    setDeleteError(null)
    try {
      await deleteProperty(id)
      setConfirmDeleteId(null)
      load()
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.message : 'Could not delete this property.')
    }
  }

  if (!session) {
    return (
      <main className="mx-auto max-w-4xl px-8 py-10">
        <p className="text-sm text-ink-soft">Log in to manage your properties.</p>
      </main>
    )
  }

  return (
    <main className="mx-auto max-w-4xl px-8 py-7">
      <div className="flex items-center justify-between">
        <h1 className="font-display text-2xl font-medium text-ink">My Properties</h1>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-ink"
        >
          {showForm ? 'Cancel' : 'Add property'}
        </button>
      </div>

      {showForm && (
        <AddPropertyForm
          onCreated={() => {
            setShowForm(false)
            load()
          }}
        />
      )}

      {state.status === 'loading' && <p className="mt-6 text-sm text-ink-soft">Loading your properties...</p>}
      {state.status === 'error' && (
        <p role="alert" className="mt-6 text-sm text-red-700">
          {state.message}
        </p>
      )}
      {state.status === 'ready' && state.properties.length === 0 && (
        <p className="mt-6 text-sm text-ink-soft">You haven't listed any properties yet.</p>
      )}

      {state.status === 'ready' && state.properties.length > 0 && (
        <ul className="mt-6 space-y-4">
          {state.properties.map((property) => (
            <li key={property.id} className="rounded-2xl border border-line bg-white p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-display text-lg font-medium text-ink">{property.title}</h2>
                  <p className="text-sm text-ink-soft">
                    {property.city}, {property.country}
                  </p>
                  <p className="mt-1 text-sm text-ink">
                    <span className="font-semibold">{formatPrice(property.price_per_night_cents)}</span> / night
                    <span className="text-ink-soft">
                      {' '}
                      &middot; {CANCELLATION_POLICY_LABEL[property.cancellation_policy]} cancellation
                    </span>
                  </p>
                </div>

                {confirmDeleteId === property.id ? (
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-ink-soft">Delete this listing?</span>
                    <button
                      type="button"
                      onClick={() => handleDelete(property.id)}
                      className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-red-700"
                    >
                      Yes, delete
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmDeleteId(null)}
                      className="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold text-ink hover:bg-ground"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setExpandedId(expandedId === property.id ? null : property.id)}
                      className="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold text-ink hover:bg-ground"
                    >
                      {expandedId === property.id ? 'Hide bookings' : 'View bookings'}
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmDeleteId(property.id)}
                      className="rounded-lg border border-red-200 px-3 py-1.5 text-sm font-semibold text-red-700 hover:bg-red-50"
                    >
                      Delete
                    </button>
                  </div>
                )}
              </div>

              {deleteError && confirmDeleteId === property.id && (
                <p role="alert" className="mt-2 text-sm text-red-700">
                  {deleteError}
                </p>
              )}

              {expandedId === property.id && <PropertyReservations propertyId={property.id} />}
            </li>
          ))}
        </ul>
      )}
    </main>
  )
}
