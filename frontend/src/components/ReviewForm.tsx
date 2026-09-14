import { useState, type FormEvent } from 'react'
import { ApiError, createReview } from '../api/client'

// BOOK-86: shared by PropertyReservations (host reviews guest) and
// MyBookings (guest reviews property) -- same endpoint, same shape, only
// the placeholder copy differs by direction
interface Props {
  reservationId: string
  placeholder: string
  onDone: () => void
}

export function ReviewForm({ reservationId, placeholder, onDone }: Props) {
  const [rating, setRating] = useState(5)
  const [comment, setComment] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await createReview(reservationId, { rating, comment })
      onDone()
    } catch (err) {
      // covers the backend's duplicate-review 409 too -- its message is
      // clear enough to show as-is, no special-casing needed
      setError(err instanceof ApiError ? err.message : 'Could not submit this review.')
      setSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-2.5 flex flex-col gap-2.5 rounded-lg border border-line bg-ground p-3"
    >
      <label className="flex items-center gap-2 text-sm text-ink">
        Rating
        <select
          value={rating}
          onChange={(e) => setRating(Number(e.target.value))}
          className="rounded-md border border-line bg-white px-2 py-1 text-sm focus:border-accent focus:outline-none"
        >
          {[5, 4, 3, 2, 1].map((n) => (
            <option key={n} value={n}>
              {n} star{n === 1 ? '' : 's'}
            </option>
          ))}
        </select>
      </label>
      <textarea
        required
        rows={2}
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder={placeholder}
        className="rounded-md border border-line bg-white px-2.5 py-2 text-sm text-ink focus:border-accent focus:outline-none"
      />
      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={submitting}
        className="self-start rounded-lg bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent-ink disabled:opacity-60"
      >
        {submitting ? 'Submitting...' : 'Submit review'}
      </button>
    </form>
  )
}
