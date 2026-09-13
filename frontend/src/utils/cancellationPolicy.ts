import type { CancellationPolicy } from '../types'

// Mirrors app/reservation/cancellation_policy.py's calculate_refund_cents()
// cutoffs exactly -- keep these two in sync by hand (BOOK-76/BOOK-99).
export const CANCELLATION_POLICY_LABEL: Record<CancellationPolicy, string> = {
  flexible: 'Flexible',
  moderate: 'Moderate',
  strict: 'Strict',
}

export const CANCELLATION_POLICY_COPY: Record<CancellationPolicy, string> = {
  flexible: 'Full refund up to 24 hours before check-in. No refund after.',
  moderate:
    'Full refund up to 5 days before check-in, 50% refund up to 24 hours before, no refund after.',
  strict: '50% refund up to 7 days before check-in. No refund after.',
}
