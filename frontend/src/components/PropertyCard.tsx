import { Link } from 'react-router-dom'
import type { PropertyListItem } from '../types'
import { formatPrice } from '../utils/format'

export function PropertyCard({ property }: { property: PropertyListItem }) {
  return (
    <Link to={`/properties/${property.id}`} className="group block">
      <div className="aspect-4/3 overflow-hidden rounded-2xl bg-accent-soft">
        {property.cover_photo ? (
          <img
            src={property.cover_photo.url}
            alt={property.title}
            className="h-full w-full object-cover transition group-hover:scale-105"
            onError={(e) => {
              e.currentTarget.style.display = 'none'
            }}
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-sm text-ink-faint">
            No photo
          </div>
        )}
      </div>
      <div className="pt-2">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="truncate font-display text-[16px] font-medium text-ink">{property.title}</h3>
          {property.rating_avg === null ? (
            // no reviews yet -- "New" reads better than "0.0", which would
            // look like a terrible score rather than an absent one
            <span className="shrink-0 text-xs font-semibold text-ink-soft">New</span>
          ) : (
            <span className="shrink-0 text-sm text-ink">
              <span className="text-gold">★</span> {property.rating_avg.toFixed(1)}
              <span className="text-ink-soft"> ({property.review_count})</span>
            </span>
          )}
        </div>
        <p className="text-sm text-ink-soft">
          {property.city}, {property.country}
        </p>
        <p className="mt-1 text-sm text-ink">
          <span className="font-semibold">{formatPrice(property.price_per_night_cents)}</span> / night
        </p>
        <p className="text-xs text-ink-faint">Up to {property.max_guests} guests</p>
      </div>
    </Link>
  )
}
