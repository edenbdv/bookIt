import { useState } from 'react'
import type { PropertyPhoto } from '../types'

export function PhotoCarousel({ photos, alt }: { photos: PropertyPhoto[]; alt: string }) {
  const [index, setIndex] = useState(0)

  if (photos.length === 0) {
    return (
      <div className="aspect-[21/9] overflow-hidden rounded-2xl bg-accent-soft">
        <div className="flex h-full w-full items-center justify-center text-ink-faint">No photo</div>
      </div>
    )
  }

  function goTo(next: number) {
    setIndex((next + photos.length) % photos.length)
  }

  return (
    <div className="relative aspect-[21/9] overflow-hidden rounded-2xl bg-ground">
      <img src={photos[index].url} alt={alt} className="h-full w-full object-cover" />

      {photos.length > 1 && (
        <>
          <button
            type="button"
            onClick={() => goTo(index - 1)}
            aria-label="Previous photo"
            className="absolute top-1/2 left-3 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full bg-white/90 text-gray-800 shadow hover:bg-white"
          >
            &larr;
          </button>
          <button
            type="button"
            onClick={() => goTo(index + 1)}
            aria-label="Next photo"
            className="absolute top-1/2 right-3 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full bg-white/90 text-gray-800 shadow hover:bg-white"
          >
            &rarr;
          </button>

          <div className="absolute bottom-3 left-1/2 flex -translate-x-1/2 gap-1.5">
            {photos.map((photo, i) => (
              <button
                key={photo.id}
                type="button"
                onClick={() => goTo(i)}
                aria-label={`Go to photo ${i + 1}`}
                className={`h-1.5 rounded-full transition-all ${
                  i === index ? 'w-5 bg-white' : 'w-1.5 bg-white/60'
                }`}
              />
            ))}
          </div>

          <div className="absolute top-3 right-3 rounded-full bg-black/50 px-2 py-0.5 text-xs text-white">
            {index + 1} / {photos.length}
          </div>
        </>
      )}
    </div>
  )
}
