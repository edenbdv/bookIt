/** Money is integer cents everywhere (app/property, app/reservation) --
 * this is display-only formatting, never a value that round-trips back
 * through a float. Shown as whole dollars: no listing/booking amount in
 * the seed or test data has cents worth displaying.
 */
export function formatPrice(cents: number): string {
  return `$${(cents / 100).toFixed(0)}`
}
