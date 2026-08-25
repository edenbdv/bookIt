import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, getProperty, getPropertyReviews } from '../api/client'
import { PhotoCarousel } from '../components/PhotoCarousel'
import { ReviewList } from '../components/ReviewList'
import type { PropertyDetail as PropertyDetailType, Review } from '../types'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; property: PropertyDetailType }

// reviews load independently of the property itself -- a slow or failing
// reviews fetch shouldn't block the page's primary content from rendering
type ReviewsState =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; reviews: Review[] }

function formatPrice(cents: number): string {
  return `$${(cents / 100).toFixed(0)}`
}

export function PropertyDetail() {
  const { id } = useParams<{ id: string }>()
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [reviewsState, setReviewsState] = useState<ReviewsState>({ status: 'loading' })

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setState({ status: 'loading' })

    getProperty(id)
      .then((property) => {
        if (!cancelled) setState({ status: 'ready', property })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const message = err instanceof ApiError ? err.message : 'Could not load this property.'
        setState({ status: 'error', message })
      })

    return () => {
      cancelled = true
    }
  }, [id])

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setReviewsState({ status: 'loading' })

    getPropertyReviews(id)
      .then((reviews) => {
        if (!cancelled) setReviewsState({ status: 'ready', reviews })
      })
      .catch(() => {
        if (!cancelled) setReviewsState({ status: 'error' })
      })

    return () => {
      cancelled = true
    }
  }, [id])

  if (state.status === 'loading') {
    return <p className="mx-auto max-w-4xl px-8 py-10 text-sm text-gray-500">Loading...</p>
  }

  if (state.status === 'error') {
    return (
      <div className="mx-auto max-w-4xl px-8 py-10">
        <Link to="/" className="text-sm text-red-600 underline">
          &larr; Back to properties
        </Link>
        <p className="mt-6 text-red-700">{state.message}</p>
      </div>
    )
  }

  const { property } = state

  return (
    <div className="mx-auto max-w-4xl px-8 py-6">
      <Link to="/" className="text-sm text-gray-500 hover:text-gray-800">
        &larr; Back to properties
      </Link>

      <div className="mt-4">
        <PhotoCarousel photos={property.photos} alt={property.title} />
      </div>

      <h1 className="mt-6 text-2xl font-bold">{property.title}</h1>
      <p className="mt-1 text-gray-500">
        {property.city}, {property.country}
      </p>

      <div className="mt-4 text-sm text-gray-700">
        <span className="font-semibold">{property.max_guests}</span> guests max
      </div>

      <p className="mt-5 whitespace-pre-wrap text-[15px] leading-relaxed text-gray-800">
        {property.description}
      </p>

      <div className="mt-6 border-t border-gray-200 pt-5 text-lg">
        <span className="text-xl font-bold">{formatPrice(property.price_per_night_cents)}</span> / night
      </div>

      <div className="mt-8 border-t border-gray-200 pt-6">
        <h2 className="mb-4 text-lg font-bold">Reviews</h2>
        {reviewsState.status === 'loading' && (
          <p className="text-sm text-gray-500">Loading reviews...</p>
        )}
        {reviewsState.status === 'error' && (
          <p className="text-sm text-gray-500">Could not load reviews.</p>
        )}
        {reviewsState.status === 'ready' && <ReviewList reviews={reviewsState.reviews} />}
      </div>
    </div>
  )
}
