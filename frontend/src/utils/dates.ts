/**
 * Today as yyyy-mm-dd in the *viewer's* timezone, to compare against `type="date"`
 * inputs (which are timezone-less wall dates).
 *
 * Deliberately not toISOString().slice(0, 10): that converts to UTC first, so
 * anywhere ahead of it -- e.g. UTC+3 between midnight and 03:00 -- would call
 * today "yesterday" and offer a date the user can't actually book.
 *
 * Shared by SearchBar and BookingForm -- both need the same "today" to bound
 * their date pickers and validate against.
 */
export function todayIso(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}
