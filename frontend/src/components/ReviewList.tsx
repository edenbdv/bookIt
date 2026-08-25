import type { Review } from '../types'

function Stars({ rating }: { rating: number }) {
  return (
    <span className="text-amber-500" aria-label={`${rating} out of 5 stars`}>
      {'★'.repeat(rating)}
      <span className="text-gray-300">{'★'.repeat(5 - rating)}</span>
    </span>
  )
}

function formatDate(iso: string): string {
  // fixed to en-US, not the browser's locale -- the rest of the UI is
  // hardcoded English, so a Hebrew/etc. locale would render "אוג 2026"
  // next to English labels
  return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short' })
}

interface Props {
  reviews: Review[]
  // the server's aggregate over ALL reviews. Preferred over averaging
  // `reviews` here, which only holds the fetched page (limit=50) and would
  // silently drift once a property has more than that.
  ratingAvg: number | null
  reviewCount: number
}

export function ReviewList({ reviews, ratingAvg, reviewCount }: Props) {
  if (reviews.length === 0) {
    return <p className="text-sm text-gray-500">No reviews yet.</p>
  }

  return (
    <div>
      {ratingAvg !== null && (
        <div className="mb-4 flex items-center gap-2 text-sm">
          <Stars rating={Math.round(ratingAvg)} />
          <span className="font-semibold">{ratingAvg.toFixed(1)}</span>
          <span className="text-gray-500">
            &middot; {reviewCount} review{reviewCount === 1 ? '' : 's'}
          </span>
        </div>
      )}

      <ul className="space-y-5">
        {reviews.map((review) => (
          <li key={review.id} className="border-b border-gray-100 pb-5 last:border-0 last:pb-0">
            <div className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-full bg-gray-900 text-xs font-bold uppercase text-white">
                {(review.author_name ?? 'G').charAt(0)}
              </span>
              {/* accounts predating display_name have none -- "Guest" keeps
                  the row looking intentional rather than broken */}
              <span className="text-sm font-semibold">{review.author_name ?? 'Guest'}</span>
              <Stars rating={review.rating} />
              <span className="text-xs text-gray-500">{formatDate(review.created_at)}</span>
            </div>
            <p className="mt-1.5 text-sm leading-relaxed text-gray-800">{review.comment}</p>
          </li>
        ))}
      </ul>
    </div>
  )
}
