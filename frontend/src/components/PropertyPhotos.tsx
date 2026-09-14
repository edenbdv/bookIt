import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { addPhoto, ApiError, getProperty, removePhoto } from '../api/client'
import type { PropertyPhoto } from '../types'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; photos: PropertyPhoto[] }

// BOOK-102: no dedicated "list this property's photos" endpoint -- reuses
// the same GET /properties/{id} the guest-facing detail page calls, since
// PropertyDetailRead already carries the full gallery (property/router.py)
export function PropertyPhotos({ propertyId }: { propertyId: string }) {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [url, setUrl] = useState('')
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  // ids whose <img> failed to load -- shown as a visible placeholder rather
  // than silently disappearing, so a bad URL (e.g. a webpage link instead of
  // a direct image file) is diagnosable instead of just an empty box
  const [failedIds, setFailedIds] = useState<Set<string>>(new Set())

  const load = useCallback(() => {
    setState({ status: 'loading' })
    getProperty(propertyId)
      .then((property) => setState({ status: 'ready', photos: property.photos }))
      .catch((err: unknown) => {
        const message = err instanceof ApiError ? err.message : 'Could not load photos.'
        setState({ status: 'error', message })
      })
  }, [propertyId])

  useEffect(load, [load])

  async function handleAdd(e: FormEvent) {
    e.preventDefault()
    setAdding(true)
    setAddError(null)
    try {
      await addPhoto(propertyId, url)
      setUrl('')
      load()
    } catch (err) {
      setAddError(err instanceof ApiError ? err.message : 'Could not add this photo.')
    } finally {
      setAdding(false)
    }
  }

  async function handleRemove(photoId: string) {
    if (state.status !== 'ready') return
    // optimistic -- there's no meaningful way for the host to react to a
    // remove failing beyond seeing the thumbnail come back, so roll back
    // locally on error rather than build a separate error UI for it
    const previous = state.photos
    setState({ status: 'ready', photos: previous.filter((p) => p.id !== photoId) })
    try {
      await removePhoto(propertyId, photoId)
    } catch {
      setState({ status: 'ready', photos: previous })
    }
  }

  return (
    <div className="mt-3">
      {state.status === 'loading' && <p className="text-sm text-ink-soft">Loading photos...</p>}
      {state.status === 'error' && (
        <p role="alert" className="text-sm text-red-700">
          {state.message}
        </p>
      )}

      {state.status === 'ready' && (
        <>
          {state.photos.length === 0 ? (
            <p className="text-sm text-ink-soft">No photos yet.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {state.photos.map((photo) => (
                <div
                  key={photo.id}
                  className="group relative h-20 w-28 overflow-hidden rounded-lg bg-accent-soft"
                >
                  {failedIds.has(photo.id) ? (
                    <div className="flex h-full w-full flex-col items-center justify-center gap-0.5 px-1.5 text-center">
                      <span className="text-xs font-semibold text-ink-soft">Couldn't load</span>
                      <span className="w-full truncate text-[10px] text-ink-faint">{photo.url}</span>
                    </div>
                  ) : (
                    <img
                      src={photo.url}
                      alt=""
                      className="h-full w-full object-cover"
                      onError={() => setFailedIds((prev) => new Set(prev).add(photo.id))}
                    />
                  )}
                  <button
                    type="button"
                    onClick={() => handleRemove(photo.id)}
                    aria-label="Remove photo"
                    className="absolute top-1 right-1 flex h-5 w-5 items-center justify-center rounded-full bg-black/60 text-xs text-white opacity-0 hover:bg-black/80 group-hover:opacity-100"
                  >
                    &times;
                  </button>
                </div>
              ))}
            </div>
          )}

          <form onSubmit={handleAdd} className="mt-2.5 flex gap-2">
            <input
              type="url"
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://... photo URL"
              className="flex-1 rounded-lg border border-line px-3 py-1.5 text-sm text-ink focus:border-accent focus:outline-none"
            />
            <button
              type="submit"
              disabled={adding}
              className="rounded-lg bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent-ink disabled:opacity-60"
            >
              {adding ? 'Adding...' : 'Add'}
            </button>
          </form>
          {addError && (
            <p role="alert" className="mt-1.5 text-sm text-red-700">
              {addError}
            </p>
          )}
        </>
      )}
    </div>
  )
}
