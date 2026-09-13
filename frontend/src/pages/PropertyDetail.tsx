import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, getProperty, getPropertyReviews } from '../api/client'
import { BookingForm } from '../components/BookingForm'
import { PhotoCarousel } from '../components/PhotoCarousel'
import { ReviewList } from '../components/ReviewList'
import type { PropertyDetail as PropertyDetailType, Review } from '../types'
import { CANCELLATION_POLICY_COPY, CANCELLATION_POLICY_LABEL } from '../utils/cancellationPolicy'
import { formatPrice } from '../utils/format'

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

function StarIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" className={className} fill="currentColor" aria-hidden="true">
      <path d="M10 1l2.6 6.2 6.7.5-5.1 4.4 1.6 6.5L10 15.2 4.2 18.6l1.6-6.5-5.1-4.4 6.7-.5z" />
    </svg>
  )
}

function ShieldIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 22c5-3 8-7 8-12V6l-8-3-8 3v4c0 5 3 9 8 12z" />
    </svg>
  )
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
    return <p className="mx-auto max-w-6xl px-8 py-10 text-sm text-ink-soft">Loading...</p>
  }

  if (state.status === 'error') {
    return (
      <div className="mx-auto max-w-6xl px-8 py-10">
        <Link to="/" className="text-sm text-red-600 underline">
          &larr; Back to properties
        </Link>
        <p className="mt-6 text-red-700">{state.message}</p>
      </div>
    )
  }

  const { property } = state

  return (
    <div className="bg-ground">
      <div className="mx-auto max-w-6xl px-8 py-7">
        <Link to="/" className="inline-flex items-center gap-1.5 text-sm text-ink-soft hover:text-ink">
          &larr; Back to properties
        </Link>

        <div className="mt-5">
          <PhotoCarousel photos={property.photos} alt={property.title} />
        </div>

        <div className="mt-8 grid grid-cols-1 gap-12 lg:grid-cols-[1fr_340px]">
          <div>
            <h1 className="text-balance font-display text-[34px] font-medium tracking-tight text-ink">
              {property.title}
            </h1>
            <div className="mt-2 flex flex-wrap items-center gap-3 text-[14.5px] text-ink-soft">
              <span>
                {property.city}, {property.country}
              </span>
              <span className="h-[3px] w-[3px] rounded-full bg-ink-faint" />
              <span>{property.max_guests} guests max</span>
              {property.rating_avg !== null && (
                <>
                  <span className="h-[3px] w-[3px] rounded-full bg-ink-faint" />
                  <span className="inline-flex items-center gap-1 font-medium text-ink">
                    <StarIcon className="h-3.5 w-3.5 text-gold" />
                    {property.rating_avg.toFixed(1)} &middot; {property.review_count} review
                    {property.review_count === 1 ? '' : 's'}
                  </span>
                </>
              )}
            </div>

            <hr className="my-7 border-line" />

            <p className="text-[12.5px] font-semibold tracking-wide text-ink-soft uppercase">About this place</p>
            <p className="mt-2.5 max-w-[62ch] text-[16px] leading-relaxed text-ink whitespace-pre-wrap">
              {property.description}
            </p>

            <div className="mt-5 flex items-start gap-3.5 rounded-2xl bg-accent-soft p-4">
              <ShieldIcon className="mt-0.5 h-5 w-5 flex-shrink-0 text-accent-ink" />
              <div>
                <div className="text-[14.5px] font-semibold text-accent-ink">
                  {CANCELLATION_POLICY_LABEL[property.cancellation_policy]} cancellation
                </div>
                <p className="mt-0.5 text-sm leading-relaxed text-ink/80">
                  {CANCELLATION_POLICY_COPY[property.cancellation_policy]}
                </p>
              </div>
            </div>

            <hr className="my-7 border-line" />

            <p className="mb-3 text-[12.5px] font-semibold tracking-wide text-ink-soft uppercase">Reviews</p>
            {reviewsState.status === 'loading' && <p className="text-sm text-ink-soft">Loading reviews...</p>}
            {reviewsState.status === 'error' && <p className="text-sm text-ink-soft">Could not load reviews.</p>}
            {reviewsState.status === 'ready' && (
              <ReviewList
                reviews={reviewsState.reviews}
                ratingAvg={property.rating_avg}
                reviewCount={property.review_count}
              />
            )}
          </div>

          <div className="lg:sticky lg:top-6 lg:self-start">
            <div className="rounded-2xl border border-line bg-surface p-5 shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
              <div className="flex items-baseline gap-1.5">
                <span className="font-display text-[26px] font-medium text-ink">
                  {formatPrice(property.price_per_night_cents)}
                </span>
                <span className="text-sm text-ink-soft">/ night</span>
              </div>
              <BookingForm property={property} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
