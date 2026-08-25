import { useEffect, useState, type FormEvent } from 'react'
import { listCities } from '../api/client'
import type { PropertyFilters } from '../types'

interface Props {
  onSearch: (filters: PropertyFilters) => void
}

export function SearchBar({ onSearch }: Props) {
  const [city, setCity] = useState('')
  const [guests, setGuests] = useState('')
  const [minPrice, setMinPrice] = useState('')
  const [maxPrice, setMaxPrice] = useState('')
  const [cityOptions, setCityOptions] = useState<string[]>([])

  useEffect(() => {
    listCities()
      .then(setCityOptions)
      // suggestions are a nice-to-have -- if this fails, the city input
      // still works as a plain text field, so fail silently
      .catch(() => {})
  }, [])

  function submit(e: FormEvent) {
    e.preventDefault()
    onSearch({
      city: city.trim() || undefined,
      guests: guests ? Number(guests) : undefined,
      min_price_cents: minPrice ? Math.round(Number(minPrice) * 100) : undefined,
      max_price_cents: maxPrice ? Math.round(Number(maxPrice) * 100) : undefined,
    })
  }

  function clear() {
    setCity('')
    setGuests('')
    setMinPrice('')
    setMaxPrice('')
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
    </form>
  )
}
