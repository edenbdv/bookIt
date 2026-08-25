import { useEffect, useState, type FormEvent } from 'react'
import { listCities } from '../api/client'
import type { PropertyFilters, PropertySort } from '../types'

interface Props {
  onSearch: (filters: PropertyFilters) => void
}

export function SearchBar({ onSearch }: Props) {
  const [city, setCity] = useState('')
  const [guests, setGuests] = useState('')
  const [minPrice, setMinPrice] = useState('')
  const [maxPrice, setMaxPrice] = useState('')
  const [cityOptions, setCityOptions] = useState<string[]>([])
  // matches the API's own default (BOOK-77), so the dropdown reflects the
  // order actually being shown before the user touches it
  const [sort, setSort] = useState<PropertySort>('recommended')

  useEffect(() => {
    listCities()
      .then(setCityOptions)
      // suggestions are a nice-to-have -- if this fails, the city input
      // still works as a plain text field, so fail silently
      .catch(() => {})
  }, [])

  function buildFilters(nextSort: PropertySort): PropertyFilters {
    return {
      city: city.trim() || undefined,
      guests: guests ? Number(guests) : undefined,
      min_price_cents: minPrice ? Math.round(Number(minPrice) * 100) : undefined,
      max_price_cents: maxPrice ? Math.round(Number(maxPrice) * 100) : undefined,
      sort: nextSort,
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    onSearch(buildFilters(sort))
  }

  function changeSort(next: PropertySort) {
    setSort(next)
    // re-search immediately rather than waiting for Search to be pressed --
    // a sort control that does nothing until you press another button reads
    // as broken. Carries the currently-typed filters along with it.
    onSearch(buildFilters(next))
  }

  function clear() {
    setCity('')
    setGuests('')
    setMinPrice('')
    setMaxPrice('')
    setSort('recommended')
    onSearch({})
  }

  const inputClass =
    'rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-sm focus:border-gray-400 focus:outline-none'

  return (
    <form onSubmit={submit} className="flex flex-wrap items-center gap-2.5 border-b border-gray-200 bg-gray-50 px-8 py-5">
      <input
        className={`${inputClass} w-44`}
        placeholder="City"
        list="city-options"
        value={city}
        onChange={(e) => setCity(e.target.value)}
      />
      <datalist id="city-options">
        {cityOptions.map((option) => (
          <option key={option} value={option} />
        ))}
      </datalist>
      <input
        className={`${inputClass} w-24`}
        type="number"
        min={1}
        placeholder="Guests"
        value={guests}
        onChange={(e) => setGuests(e.target.value)}
      />
      <input
        className={`${inputClass} w-28`}
        type="number"
        min={0}
        placeholder="Min $/night"
        value={minPrice}
        onChange={(e) => setMinPrice(e.target.value)}
      />
      <input
        className={`${inputClass} w-28`}
        type="number"
        min={0}
        placeholder="Max $/night"
        value={maxPrice}
        onChange={(e) => setMaxPrice(e.target.value)}
      />
      <button type="submit" className="rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-semibold text-white hover:opacity-90">
        Search
      </button>

      <button type="button" onClick={clear} className="text-sm text-gray-500 underline">
        Clear
      </button>

      {/* ml-auto pushes the sort control to the far right, away from the
          filter inputs -- it acts on results, it isn't another filter */}
      <label className="ml-auto flex items-center gap-2 text-sm text-gray-500">
        Sort
        <select
          className={inputClass}
          value={sort}
          onChange={(e) => changeSort(e.target.value as PropertySort)}
        >
          <option value="recommended">Recommended</option>
          <option value="price">Price: low to high</option>
        </select>
      </label>
    </form>
  )
}
