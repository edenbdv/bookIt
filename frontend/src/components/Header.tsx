import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/useAuth'

export function Header() {
  const { session, logout } = useAuth()
  const navigate = useNavigate()

  function handleLogout() {
    logout()
    navigate('/')
  }

  return (
    <header className="sticky top-0 z-10 flex items-center justify-between bg-accent px-8 py-4">
      <Link to="/" className="flex items-center gap-2 font-display text-xl font-medium tracking-tight text-ground">
        <svg viewBox="0 0 24 24" fill="none" className="h-6 w-6">
          <path
            d="M3 11.5 12 4l9 7.5"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <path
            d="M5 10v9a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1v-9"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        BookIt
      </Link>

      {session ? (
        <div className="flex items-center gap-3">
          <Link
            to="/host/properties"
            className="rounded-full border border-ground/30 px-4 py-2 text-sm font-semibold text-ground hover:bg-ground/10"
          >
            My Properties
          </Link>
          <div className="flex items-center gap-2 text-sm text-ground/85">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-ground text-xs font-bold uppercase text-accent-ink">
              {session.email.charAt(0)}
            </span>
            {session.email}
          </div>
          <button
            onClick={handleLogout}
            className="rounded-full border border-ground/30 px-4 py-2 text-sm font-semibold text-ground hover:bg-ground/10"
          >
            Log out
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-3">
          <Link
            to="/login"
            className="rounded-full border border-ground/30 px-4 py-2 text-sm font-semibold text-ground hover:bg-ground/10"
          >
            Log in
          </Link>
          <Link
            to="/signup"
            className="rounded-full bg-ground px-4 py-2 text-sm font-semibold text-accent-ink hover:bg-white"
          >
            Sign up
          </Link>
        </div>
      )}
    </header>
  )
}
