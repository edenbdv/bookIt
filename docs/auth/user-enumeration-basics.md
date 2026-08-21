# User enumeration basics (BOOK-30)

## 1. The mistake: different errors for "no such email" vs. "wrong password"

It's tempting to return a more specific error from `/auth/login` — `"no account with that email"` if the lookup fails, `"wrong password"` if it fails verification. That specificity is exactly the bug: it turns a login attempt into a way to check *whether an email has an account at all*, without ever needing to guess the right password. This is called **user (or account) enumeration**.

## 2. Why knowing "this email exists here" is useful to an attacker on its own

An attacker doesn't need to crack anything to benefit from enumeration — they just need a distinguishable response:

- **Targeted phishing** — "I know you have a BookIt account" makes a phishing email far more convincing than a blind guess.
- **Credential-stuffing prioritization** — attackers hold huge dumps of `email:password` pairs leaked from *other*, unrelated breaches. Enumeration lets them first filter a mass email list down to only the ones that actually have a BookIt account, then spend their (rate-limited, noisy) login attempts only on those — instead of blindly trying every leaked pair against every email.
- **Privacy leak on its own** — confirming "this specific person has an account on this platform" can be sensitive information by itself, independent of ever accessing the account.

## 3. The fix: one identical response for both failure cases

`app/auth/service.py`'s `login()`:

```python
async def login(uow: UnitOfWork, email: str, password: str) -> User:
    user = await uow.users.get_by_email(email)
    # same error for "no such email" and "wrong password" -- confirming
    # which one it was would tell an attacker whether a given email is
    # registered at all
    if user is None or not verify_password(password, user.hashed_password):
        raise InvalidCredentialsError
    return user
```

Both failure paths — email not found, and email found but password wrong — collapse into the exact same `InvalidCredentialsError`, which the router (`router.py`) maps to the exact same `401` with the exact same message (`"Invalid email or password"`). A failed login attempt reveals nothing beyond "something about this pair was wrong" — one guess and a thousand guesses leak the same amount of information: none.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| User enumeration | Using a system's distinguishable responses to determine which accounts/emails exist, without needing valid credentials |
| Credential stuffing | Trying `email:password` pairs leaked from one breach against a *different* site, hoping for password reuse |
| Fix | Return one identical error (message + status code) for every login failure reason, regardless of which check actually failed |
