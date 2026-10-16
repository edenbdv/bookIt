# BookIt frontend

A React + TypeScript SPA for BookIt: sign up / log in, browse the property feed (search by city, guests, price), view a property's detail page, book a stay, and manage bookings (view status, cancel) from the My Bookings page.

## Stack

- Vite + React 19 + TypeScript
- React Router (client-side routing)
- Tailwind CSS v4

## Setup

```bash
npm install
cp .env.example .env
```

`.env` sets `VITE_API_BASE_URL`, which must point at a running BookIt API (see the root [README](../README.md) / [CLAUDE.md](../CLAUDE.md) for backend setup). The backend only allows cross-origin requests from `http://localhost:5173` when it's running with `ENVIRONMENT=development` (see the CORS middleware in `app/main.py`) — if you change the dev server's port, the backend will reject the requests.

## Development

```bash
npm run dev       # dev server on :5173, with HMR
npm run build     # type-check (tsc -b) then production build
npm run lint      # eslint
npm run preview   # serve the production build locally
```

## Structure

```
src/
  api/client.ts       typed fetch wrapper — one function per endpoint, throws ApiError on non-2xx
  types.ts             mirrors app/auth/schemas.py and app/property/schemas.py (kept in sync by hand)
  context/             auth state: AuthContext + AuthProvider (localStorage-backed session) + useAuth hook
  components/          Header, PropertyCard, SearchBar, AuthForm (shared by Login and Signup)
  pages/                Feed, PropertyDetail, Login, Signup
```

Each domain module in the backend (`auth`, `property`, ...) has its own `schemas.py`; `types.ts` mirrors those by hand since there's no shared codegen. If a backend schema changes, update `types.ts` (and `api/client.ts` if the endpoint's request/response shape changed) to match.

## Design

Airbnb-inspired but stripped down: white background, a red-600 accent (logo, primary buttons, active links), gray-900/gray-500 text hierarchy, generous whitespace, `rounded-2xl` cards and photos, `rounded-full` pill buttons for auth actions. No design system or component library — just Tailwind utility classes inline, since the surface area is small enough that an abstraction would cost more than it saves.

Every async page (`Feed`, `PropertyDetail`) models its data as an explicit `LoadState` union (`loading | error | ready`) instead of separate boolean flags, so the JSX can't render an impossible combination (e.g. an error and results at once). Both also guard their `useEffect` with a `cancelled` flag so a stale response from a superseded request (e.g. the user changes search filters mid-fetch) can't overwrite newer state.

## Pages

### Feed (`/`)

The landing page and default route.

- **Search bar** (sticky under the header): city (case-insensitive, with autocomplete — see below), numeric guests, and min/max price-per-night ($ input, converted to cents before hitting the API). "Search" applies all four as combined filters; "Clear" resets and re-fetches the unfiltered list. Filters live in `Feed`'s state and are passed down, not URL-synced — a refresh loses the current search.
- **Grid**: responsive auto-fill grid of `PropertyCard`s (min 240px per card), each showing cover photo (or a "No photo" placeholder), title, city/country, price/night, and max guests. Clicking a card routes to its detail page.
- **States**: a loading message while the fetch is in flight, a friendly "Could not reach the API. Is it running?" message on network failure (vs. the server's actual error message for a 4xx/5xx via `ApiError`), and a distinct "No properties match these filters" empty state so a bad search doesn't look like a broken app. A result count ("N properties") sits above the grid.

### Property detail (`/properties/:id`)

- Large `aspect-video` cover photo (first photo in the list, gradient placeholder if none), title, city/country, max guests, full description (`whitespace-pre-wrap` so line breaks in the listing text are preserved), and price/night.
- "&larr; Back to properties" link at the top, kept visible in both the loaded and error states.
- Same `loading` / `error` / `ready` pattern as `Feed`; an invalid or deleted property id surfaces the backend's error message rather than a blank page.
- Booking is explicitly not here — this page is read-only.

### Login (`/login`) and Signup (`/signup`)

Both render the same `AuthForm` component with a `mode` prop, differing only in copy, the password `minLength` (8 on signup, none on login — the backend already enforces the minimum, this is just earlier feedback), and which `useAuth` method they call.

- Email + password fields, a submit button that shows "Please wait..." and disables while the request is in flight, and an inline red error banner on failure (wrong credentials, duplicate email, etc. — whatever message the API returns).
- A footer link cross-navigates between the two ("New here? Create an account" / "Already have an account? Log in").
- On success, both redirect to `/`. Signup has no separate confirmation step: it calls `POST /auth/signup` and then immediately logs in with the same credentials, since the signup endpoint doesn't return tokens itself.

### Header (all pages)

Sticky top bar with the BookIt logo/home link on the left. Right side is auth-state-dependent: logged out shows "Log in" / "Sign up" pill buttons; logged in shows an avatar (first letter of the email) plus the email itself and a "Log out" button. Session state comes from `useAuth`, so this is the one component that re-renders on every login/logout.

## City search

Matching is case-insensitive on the backend (`GET /properties?city=...` lowercases both sides before comparing), so "tel aviv" and "Tel Aviv" return the same results. The city input also gets a native `<datalist>` of every distinct city that has a listing (`GET /properties/cities`), so the browser offers autocomplete suggestions as you type — no custom dropdown component, just the platform's built-in typeahead UI wired to a `list` attribute.

## Auth state

`AuthProvider` wraps the whole app (in `main.tsx`, outside the router) and persists `{ email, accessToken }` to `localStorage` under `bookit_session`, so a page refresh doesn't log the user out. There's no session expiry check or token refresh — see Known gaps.

## Known gaps

- `session.accessToken` is stored after login but nothing attaches it as an `Authorization` header yet, and the refresh token from `/auth/login` is discarded — fine today since only public endpoints (`/properties`, `/auth/*`) are called, but needs wiring up before any authenticated request is added.
- No automated frontend tests yet.
