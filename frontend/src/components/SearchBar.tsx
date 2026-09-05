import { useEffect, useState, type FormEvent } from 'react'
import { listCities } from '../api/client'
import type { PropertyFilters, PropertySort } from '../types'
import { todayIso } from '../utils/dates'

interface Props {
  onSearch: (filters: PropertyFilters) => void
}

export function SearchBar({ onSearch }: Props) {
  const [city, setCity] = useState('')
  const [guests, setGuests] = useState('')
  const [minPrice, setMinPrice] = useState('')
  const [maxPrice, setMaxPrice] = useState('')
  const [checkIn, setCheckIn] = useState('')
  const [checkOut, setCheckOut] = useState('')
  const [dateError, setDateError] = useState<string | null>(null)
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
      check_in: checkIn || undefined,
      check_out: checkOut || undefined,
      sort: nextSort,
    }
  }

  /**
   * Mirrors the two cross-field rules GET /properties enforces on the date
   * range (app/property/router.py), so a bad range reads as a form message
   * instead of arriving as a 422 in the results area.
   *
   * The past-date rule is the exception: it has no backend counterpart --
   * the API will happily report who was free last March -- but "available"
   * is only meaningful for dates you can still book, so the UI refuses it
   * rather than returning a result that means nothing.
   *
   * Both inputs are type="date", whose value is always ISO yyyy-mm-dd --
   * fixed-width and zero-padded, so a plain string compare orders them
   * correctly and no Date parsing is needed.
   */
  function validateDates(): string | null {
    if (!checkIn && !checkOut) return null
    if (!checkIn || !checkOut) return 'Pick both a check-in and a check-out date.'
    if (checkIn < todayIso()) return 'Check-in cannot be in the past.'
    if (checkOut <= checkIn) return 'Check-out must be after check-in.'
    return null
  }

  function search(nextSort: PropertySort) {
    const error = validateDates()
    setDateError(error)
    if (error) return
    onSearch(buildFilters(nextSort))
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    search(sort)
  }

  function changeSort(next: PropertySort) {
    setSort(next)
    // re-search immediately rather than waiting for Search to be pressed --
    // a sort control that does nothing until you press another button reads
    // as broken. Carries the currently-typed filters along with it. An
    // incomplete date range still blocks the request, but the message says so.
    search(next)
  }

  function clear() {
    setCity('')
    setGuests('')
    setMinPrice('')
    setMaxPrice('')
    setCheckIn('')
    setCheckOut('')
    setDateError(null)
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
      {/* type="date" carries no placeholder (the browser renders its own
          format hint), so the label lives in title/aria-label instead */}
      {/* Visibly labelled, unlike the inputs around them: a bare date field
          shows only its format hint, so two of them side by side are
          indistinguishable. Same label-wraps-control shape as Sort below.

          dir="ltr" only normalises where the browser puts the calendar
          button and how it aligns the text, so these sit consistently with
          the other fields under an RTL locale. It deliberately does NOT
          touch the displayed date format -- that stays locale-driven
          (dd/mm/yyyy here), which is what a local user expects, and the
          value read back is ISO yyyy-mm-dd either way.

          min greys out impossible days in the picker, but validateDates
          still re-checks every rule: a date typed straight into the field
          isn't constrained by min in every browser. */}
      <label className="flex items-center gap-2 text-sm text-gray-500">
        Check in
        <input
          className={`${inputClass} w-36`}
          type="date"
          dir="ltr"
          min={todayIso()}
          value={checkIn}
          onChange={(e) => setCheckIn(e.target.value)}
        />
      </label>
      <label className="flex items-center gap-2 text-sm text-gray-500">
        Check out
        <input
          className={`${inputClass} w-36`}
          type="date"
          dir="ltr"
          // tracks check-in once it's set, so the picker can't offer a
          // check-out before it (same-day still reaches validateDates)
          min={checkIn || todayIso()}
          value={checkOut}
          onChange={(e) => setCheckOut(e.target.value)}
        />
      </label>
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

      {/* w-full breaks the flex-wrap row so the message sits under the
          inputs rather than squeezing between them */}
      {dateError && (
        <p role="alert" className="w-full text-sm text-red-700">
          {dateError}
        </p>
      )}
    </form>
  )
}
