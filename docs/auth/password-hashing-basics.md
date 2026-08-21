# Password hashing basics (BOOK-30)

## 1. The problem: why not just store the password as-is

If `User.hashed_password` stored the literal password, then anyone who can read that column — a leaked backup, a compromised admin account, an attacker who dumps the database through an SQL injection bug elsewhere in the app — reads every user's actual password in plain text. That's bad on its own, and worse because people reuse passwords across sites: leaking BookIt's database would hand the attacker working logins for those same users' email and banking accounts too.

The goal of password hashing is: **even someone who steals the entire `users` table should not be able to recover the passwords.**

## 2. What hashing actually is

A hash function takes an input of any size and produces a fixed-size output (the "hash" or "digest"), with three properties that matter here:

- **Deterministic** — the same input always produces the same output.
- **One-way** — there is no function that goes from output back to input. Given a hash, you cannot compute the password that produced it; the only way to find out is to guess a password, hash it, and see if the output matches.
- **Avalanche effect** — changing one character of the input produces a completely different output, so hashes don't leak partial information about "how close" a guess was.

So instead of storing the password, we store `hash(password)`. Login later checks `hash(entered_password) == stored_hash` — it never needs the original password back, only to confirm a guess produces the same hash.

This is also why hashing is not the same as **encoding** (base64, URL-encoding, etc.) — encoding is reversible by design (`decode(encode(x)) == x`), meant to make data safe to transport, not to hide it. Hashing has no matching "decode."

## 3. Why a bare hash (e.g. plain SHA-256) still isn't enough

Being one-way sounds like it should be sufficient, but a plain cryptographic hash has two remaining weaknesses for passwords specifically:

**It's fast.** SHA-256 is designed to hash gigabytes of data quickly (that's what makes it good for e.g. verifying file integrity). That speed is a liability here: an attacker who steals a table of hashes can simply *guess* — hash every password in a breach dictionary (real leaked passwords number in the billions) and check for a match. Modern hardware can compute billions of SHA-256 hashes per second, so guessing every common password against every stolen hash takes minutes, not years.

**It's the same output for the same input, with nothing else mixed in.** If two users both pick `"password123"`, a plain hash of each produces the *identical* string. That has two consequences:
1. Anyone with read access to the table can see which accounts share a password, just by spotting duplicate hash values.
2. It enables a **rainbow table attack** — described next.

### Rainbow table attack, concretely

A rainbow table is a *precomputed* lookup table an attacker builds once, ahead of time, independent of any specific breach:

| Candidate password | `SHA-256(password)` |
|---|---|
| `123456` | `8d969eef6ecad3c29a3a629280e686cf...` |
| `password` | `5e884898da28047151d0e56f8dc62927...` |
| `qwerty` | `65e84be33532fb784c48129675f9eff3...` |
| ... millions more ... | ... |

This only works because plain `SHA-256(password)` is **deterministic and unsalted** (section 2) — it produces one fixed output for a given password, period, with nothing user-specific or system-specific mixed in. That means `SHA-256("123456")` is the *exact same string* no matter who typed that password, on what site, or when:

- Every user on Company A's system who chose `"123456"` has that identical hash stored.
- Every user on Company B's *completely unrelated* system who also chose `"123456"` has that same identical hash stored too.
- A table built by hashing `"123456"` once, today, still matches both of those, plus every future system that makes the same unsalted-hashing choice.

So the attacker builds the table **once** (using every password from known leaks, dictionaries, common patterns) and reuses it against *any* stolen hash table, from any company, at any point in the future — not because companies share a hash function on purpose, but because an unsalted hash of the same password is *always* identical, everywhere, forever. When a new breach surfaces, the attacker doesn't compute anything new — they just look up each stolen hash in the table and instantly read off the matching password, the same way you'd look up a word in a dictionary. This is what makes rainbow tables dangerous: the expensive part (building the table) is done once, and cracking each new leak afterward is nearly instant.

## 4. The salt: how it defeats rainbow tables

A **salt** is a random value generated fresh for every password, mixed into the hash computation: `hash(password + salt)` instead of `hash(password)`. `bcrypt.gensalt()` generates that random value automatically.

This changes the math in two ways:
- Two users who both pick `"password123"` now get *different* hashes, because each got a different random salt.
- A precomputed rainbow table (built for plain `hash(password)`) is useless — the attacker would need a *separate* table per possible salt value, and with a random salt per password, that's effectively infinite tables. Precomputation stops being a shortcut.

The salt itself isn't secret — bcrypt stores it in plain view, embedded directly inside the hash string it returns:

```
$2b$12$N9qo8uLOickgx2ZMRZoMy.MrOWK.gVj4qF/ZKZ8XlZH7C6bJ8v5eK
└┬─┘└┬┘└──────────┬──────────┘└─────────────┬─────────────┘
 │   │            │                          └─ hash output
 │   │            └─ salt (22 chars, randomly generated)
 │   └─ cost factor (2^12 rounds — see below)
 └─ algorithm version
```

There's no separate `salt` column on `User` — the one `hashed_password` string is self-contained; hiding the salt would add nothing, since its entire job is to make precomputation useless, not to be secret.

### The salt being visible is fine — because it was never meant to be secret

This trips people up, so it's worth spelling out: **the salt isn't a secret, and that's by design.** An attacker who steals the database can read every salt in plain sight. That doesn't undo any of the protection above, because the salt defends against a different attacker cost than the one people usually picture.

There are two separate costs an attacker faces:

1. **Precomputing a table in advance**, before ever seeing a specific breach — this is what a rainbow table is.
2. **Brute-forcing one specific stolen hash** after a breach — guess a password, hash it *with that hash's own salt*, compare, repeat.

The salt only kills cost #1. It does nothing to hide cost #2, and was never trying to. Before the breach, the attacker doesn't know what salt a not-yet-stolen password will get, so there's nothing to precompute against — that's what makes advance preparation pointless (as covered above). *After* the breach, sure, they can read the salt for each row — but knowing it doesn't save them any work. They still must redo the full guess-hash-compare cycle **separately, from scratch, per user**, because each user's salt is different — there is no table, no shortcut, and no way to crack two users at once anymore. Compare to no salt at all: one leaked database would let a single precomputed table crack every user sharing a common password *simultaneously, for free*. That gap — reusable-across-everyone vs. must-redo-per-person — is the entire value the salt buys. It was never about hiding anything from an attacker who already has the database.

## 5. Cost factor: slowing brute force down further

Salting stops precomputation, but an attacker can still try brute-forcing one specific stolen hash directly (guess passwords, hash each with *that* hash's known salt, compare). `bcrypt` counters this by being deliberately **slow**, with a tunable cost factor (the `12` in `$2b$12$...` above means 2^12 = 4096 rounds of internal hashing per attempt). Hashing one password takes a fraction of a second — trivial for a real login — but multiplied across billions of guess attempts, it turns brute-forcing a single password from minutes into years. The cost factor can be raised over time as hardware gets faster.

### What a "round" actually is

A round is one repetition of an internal mixing step, where each round's output feeds in as the *next* round's input — a chain, not one bigger computation done once. `bcrypt` isn't "hash quickly" — it's "run the same internal step (based on the Blowfish cipher's key setup) over and over, in sequence":

```
state = initialize(password, salt)
for i in range(2 ** cost_factor):   # cost_factor=12 -> 4096 iterations
    state = expensive_mixing_step(state)
final_hash = state
```

Each loop iteration is one round. With `cost=12` that's 4096 rounds — one `hash_password` call does 4096 sequential mixing steps, not one. For a single real login this is still milliseconds (imperceptible to a user), but for an attacker trying a billion password guesses, it's a billion × 4096 mixing steps instead of a billion × 1 — that multiplier, repeated over every single guess, is what turns brute-forcing from minutes into years.

## 6. The algorithm is public — and that's fine (Kerckhoffs's principle)

`bcrypt.hashpw`/`bcrypt.checkpw` is the exact same function for us, for an attacker, for anyone — it's a published, open algorithm, not a secret we invented. That's deliberate, not an oversight: security should never depend on an algorithm being secret, only on some piece of *data* being hard to guess. If security relied on "nobody knows our hash function," the moment that leaked (or got reverse-engineered), every password would be crackable at once. Public, heavily-scrutinized algorithms are actually more trustworthy — decades of cryptographers trying to break bcrypt and failing is a far stronger guarantee than an algorithm we kept secret ourselves. This is a well-known principle called **Kerckhoffs's principle**.

So if the function is identical for everyone, what makes it secure? Two things, both per-password *data*, not a secret algorithm:

- **The salt** — random per password (section 4), so even with the same public function, `hash("password123", salt_A)` ≠ `hash("password123", salt_B)`.
- **The password itself** — the one genuinely secret input. An attacker can see the algorithm and the salt (both embedded in the stored string), but still has to guess the *password* correctly — that's the only unknown left.

Notice `bcrypt.hashpw` takes no secret key at all — just `(password, salt, cost)`. Contrast with something like HMAC, which *does* take a secret key specifically so that knowing the algorithm still isn't enough to compute a valid output without it. bcrypt doesn't need that, because its security model is "make guessing expensive," not "hide how it works."

## 7. Verifying: re-hash and compare, never decode

Putting it together — `verify_password(plain, hashed)` does not decode `hashed` back into a password (nothing can). Instead:

1. It reads the salt and cost factor back out of the `hashed` string (they're stored in plain view, as shown above).
2. It hashes the *candidate* plaintext again, using that same salt and cost factor.
3. It compares the two resulting hash strings for equality.

Same input + same salt + same cost factor always produces the same output (property 1 in section 2) — so a match proves the candidate password was correct, without the original password ever being reconstructed.

```python
# app/auth/security.py
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
```

The `.encode("utf-8")`/`.decode("utf-8")` calls here are a different, unrelated "encode/decode" from the one in section 2 — they just convert text ↔ bytes so bcrypt's byte-only C function can run, not reversing the hash or recovering the password.
## 8. Why `bcrypt` directly, not `passlib`

`passlib`'s `CryptContext` used to be the standard wrapper around `bcrypt`, but it's effectively unmaintained and has known compatibility breaks with `bcrypt >= 4.1` (it inspects an internal `bcrypt.__about__` attribute that newer `bcrypt` releases removed). Calling `bcrypt.hashpw`/`bcrypt.checkpw` directly avoids that dependency entirely — one well-maintained library instead of an unmaintained wrapper around it.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Hashing | One-way, deterministic transform — no operation recovers the input from the output |
| Encoding | A *different, reversible* transform (base64, etc.) — meant for transport, not secrecy; not a substitute for hashing |
| Dictionary attack | Hashing a list of common/leaked passwords and checking for matches against stolen hashes |
| Rainbow table | A dictionary attack's hash lookup table, precomputed once and reused against any future leak — cracking becomes instant lookup instead of computation |
| Salt | Random value mixed into the hash per-password, so identical passwords produce different hashes; stored inside the bcrypt string itself, not separately; defeats rainbow tables since one precomputed table can no longer match every salt |
| Cost factor | How many internal rounds bcrypt runs per hash — tunable to keep brute-forcing slow as hardware improves |
| Kerckhoffs's principle | Security should depend only on secret *data* (the password), never on the algorithm being secret — bcrypt is public and identical for everyone |
| `verify_password` | Re-hashes the candidate with the *stored* salt and cost factor, then compares outputs — never decodes anything |
