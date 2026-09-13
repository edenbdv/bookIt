import type { Review } from '../types'

function Stars({ rating }: { rating: number }) {
  return (
    <span className="text-gold" aria-label={`${rating} out of 5 stars`}>
      {'★'.repeat(rating)}
      <span className="text-line">{'★'.repeat(5 - rating)}</span>
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
    return <p className="text-sm text-ink-soft">No reviews yet.</p>
  }

  return (
    <div>
      {ratingAvg !== null && (
        <div className="mb-4 flex items-center gap-2 text-sm">
          <Stars rating={Math.round(ratingAvg)} />
          <span className="font-semibold text-ink">{ratingAvg.toFixed(1)}</span>
          <span className="text-ink-soft">
            &middot; {reviewCount} review{reviewCount === 1 ? '' : 's'}
          </span>
        </div>
      )}

      <ul>
        {reviews.map((review) => (
          <li key={review.id} className="border-t border-line py-4.5 first:border-0 first:pt-0">
            <div className="flex items-center gap-2.5">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-accent-soft text-sm font-semibold text-accent-ink">
                {(review.author_name ?? 'G').charAt(0)}
              </span>
              <div>
                <div className="flex items-center gap-2 text-sm">
                  {/* accounts predating display_name have none -- "Guest"
                      keeps the row looking intentional rather than broken */}
                  <span className="font-semibold text-ink">{review.author_name ?? 'Guest'}</span>
                  <span className="text-ink-faint">{formatDate(review.created_at)}</span>
                </div>
                <Stars rating={review.rating} />
              </div>
            </div>
            <p className="mt-2 text-[14.5px] leading-relaxed text-ink-soft">{review.comment}</p>
          </li>
        ))}
      </ul>
    </div>
  )
}
