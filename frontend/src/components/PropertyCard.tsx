import { Link } from 'react-router-dom'
import type { PropertyListItem } from '../types'

function formatPrice(cents: number): string {
  return `$${(cents / 100).toFixed(0)}`
}

export function PropertyCard({ property }: { property: PropertyListItem }) {
  return (
    <Link to={`/properties/${property.id}`} className="group block">
      <div className="aspect-4/3 overflow-hidden rounded-2xl bg-gradient-to-br from-gray-100 to-gray-200">
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
          <div className="flex h-full w-full items-center justify-center text-sm text-gray-400">
            No photo
          </div>
        )}
      </div>
      <div className="pt-2">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="truncate text-[15px] font-semibold text-gray-900">{property.title}</h3>
          {property.rating_avg === null ? (
            // no reviews yet -- "New" reads better than "0.0", which would
            // look like a terrible score rather than an absent one
            <span className="shrink-0 text-xs font-semibold text-gray-500">New</span>
          ) : (
            <span className="shrink-0 text-sm text-gray-700">
              <span className="text-amber-500">★</span> {property.rating_avg.toFixed(1)}
              <span className="text-gray-500"> ({property.review_count})</span>
            </span>
          )}
        </div>
        <p className="text-sm text-gray-500">
          {property.city}, {property.country}
        </p>
        <p className="mt-1 text-sm">
          <span className="font-bold">{formatPrice(property.price_per_night_cents)}</span> / night
        </p>
        <p className="text-xs text-gray-500">Up to {property.max_guests} guests</p>
      </div>
    </Link>
  )
}
