import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useAuth } from '../context/useAuth'

interface Props {
  mode: 'login' | 'signup'
}

export function AuthForm({ mode }: Props) {
  const { login, signup } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const isLogin = mode === 'login'

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      if (isLogin) {
        await login(email, password)
      } else {
        await signup(email, password, displayName)
      }
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mx-auto mt-16 max-w-sm px-6">
      <h1 className="font-display text-2xl font-medium text-ink">
        {isLogin ? 'Welcome back' : 'Create your account'}
      </h1>
      <p className="mt-1 text-sm text-ink-soft">
        {isLogin ? 'Log in to continue' : 'Join BookIt to start browsing'}
      </p>

      <form onSubmit={handleSubmit} className="mt-6 space-y-4">
        {error && <div className="rounded-lg bg-red-50 px-3 py-2.5 text-sm text-red-700">{error}</div>}

        {!isLogin && (
          <div>
            <label className="mb-1.5 block text-sm font-semibold text-ink">Name</label>
            <input
              type="text"
              required
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Shown on your reviews"
              className="w-full rounded-lg border border-line px-3 py-2.5 text-sm text-ink focus:border-accent focus:outline-none"
            />
          </div>
        )}

        <div>
          <label className="mb-1.5 block text-sm font-semibold text-ink">Email</label>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            className="w-full rounded-lg border border-line px-3 py-2.5 text-sm text-ink focus:border-accent focus:outline-none"
          />
        </div>

        <div>
          <label className="mb-1.5 block text-sm font-semibold text-ink">Password</label>
          <input
            type="password"
            required
            minLength={isLogin ? undefined : 8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={isLogin ? '' : 'At least 8 characters'}
            className="w-full rounded-lg border border-line px-3 py-2.5 text-sm text-ink focus:border-accent focus:outline-none"
          />
        </div>

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-lg bg-accent py-2.5 font-semibold text-white hover:bg-accent-ink disabled:opacity-60"
        >
          {submitting ? 'Please wait...' : isLogin ? 'Log in' : 'Sign up'}
        </button>
      </form>

      <p className="mt-5 text-center text-sm text-ink-soft">
        {isLogin ? (
          <>
            New here?{' '}
            <Link to="/signup" className="font-semibold text-accent-ink">
              Create an account
            </Link>
          </>
        ) : (
          <>
            Already have an account?{' '}
            <Link to="/login" className="font-semibold text-accent-ink">
              Log in
            </Link>
          </>
        )}
      </p>
    </div>
  )
}
