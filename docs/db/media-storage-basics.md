# Where and how images actually get stored — not just theory

Grew out of a real design question: `PropertyPhoto.url` (BOOK-74) stores a URL string, not the image itself. Why, and what does a real production system actually do instead of a human hand-typing a stock photo link?

## 1. Why the database never stores the image bytes

Postgres *can* technically hold binary data (a `bytea` column), but nobody does this for user photos at real scale, for a few concrete reasons:

- **It bloats the database.** Every backup, every replica, every `pg_dump` now has to move megabytes per photo instead of a ~100-byte string.
- **No caching is possible.** A row sitting inside Postgres can't be cached by a browser or a CDN the way a URL-addressable resource can (see section 4).
- **It couples image-serving load to your database.** Serving a 2MB photo is a completely different kind of work (bandwidth-bound) than answering "what properties are in Tel Aviv" (compute-bound) — mixing them means image traffic can slow down unrelated queries.

So the database stores a **URL** — a pointer to where the actual bytes live — never the bytes themselves. This project's `PropertyPhoto.url: Mapped[str]` already reflects this; it's not a shortcut that needs redoing later.

## 2. Binary and base64 are heavier than a URL — concretely

A URL is a tiny string (`https://images.unsplash.com/photo-...&w=1200&q=80` — a couple hundred bytes). The image it points to is hundreds of KB to several MB.

**Base64 makes embedding binary in JSON *worse*, not better.** JSON is text-only — it can't contain raw binary — so base64 re-encodes binary as text to smuggle it through. That encoding has real overhead: every 3 bytes of binary becomes 4 characters of text, roughly **+33% size** on top of the original.

**Where this bites in practice:** `GET /properties` (list, up to 100 results) with base64-embedded `cover_photo` would ship up to 100 × ~1.3MB of encoded image data in *every single response* — even to a caller that never renders anything (a mobile app checking availability, an admin script). With a URL, that same response is a few KB of text; whoever actually needs the image fetches it separately. Same "don't transfer more than needed" reasoning as `LIMIT`/`OFFSET` (`sql-basics.md` section 5), just applied to bytes-per-row instead of rows-per-page.

## 3. The real upload flow — what produces the URL in production

Right now, in this project, a human hand-picks a stock photo URL and `POST`s it to `/properties/{id}/photos`. In a real product, a host uploads their own photo, and the URL gets produced by an actual upload pipeline instead:

1. **Client asks the API for permission to upload** — not the file itself yet.
2. **The API returns a presigned URL** — a short-lived, cryptographically signed URL granting temporary permission to write directly into a specific path in an object storage bucket (S3, Google Cloud Storage, Cloudflare R2), without ever handing out real storage credentials.
3. **The client uploads the file bytes directly to that storage**, bypassing the API server entirely for the heavy part. This is why it scales — the app server never touches the actual image data, only brokers permission.
4. **The client tells the API "done, it's at this path"** — and *that's* the moment the API does exactly what `add_photo` already does here: create a `PropertyPhoto` row, with `url` now pointing at real S3/CDN infrastructure instead of a hand-picked link.

**A CDN (CloudFront, Cloudflare) usually sits in front of the storage** — caching images at edge locations physically close to whoever's requesting them, often resizing on the fly instead of storing every size separately.

So the schema shape (`url: str`) doesn't change between a dev seed script and a real product — only what's *upstream* of that URL (a presigned-upload endpoint, file-type/size validation, an actual bucket) is missing, and that's realistically its own ticket, not a redesign of what's already built.

## 4. Caching — why "call an external URL" isn't the same cost as embedding bytes

A fair question: doesn't fetching from S3/a CDN every time also cost something? Yes — but it's a fundamentally cheaper *kind* of cost than baking bytes into every API response:

- **Who pays, and when.** A URL is only fetched by whoever actually needs to *see* the image, only at the moment they need it. Base64-in-response forces that cost on every single caller of the endpoint, whether or not they render anything.
- **Caching by URL.** A browser caches a resource by its URL. The same photo shown on two property cards, or revisited later, is served from the browser's own cache — zero network requests the second time. Base64 baked into JSON can never be cached this way; it's not a distinct fetchable resource, just bytes glued into that one response, refetched in full every time.
- **Parallelism.** A browser fetches several image URLs at once, over multiple connections. One JSON response with embedded photos is one big sequential blob — nothing can start rendering until the whole thing arrives.
- **Purpose-built infrastructure.** S3 + a CDN exist specifically to serve static files at scale, from edge locations near the requester worldwide — genuinely faster than one origin API server, and it takes zero load off the database or app server.

## 5. Cache in RAM vs. the database on disk — it's about durability, not a fixed rule

**Why the database has to live on disk, fundamentally:** RAM loses everything the instant power is lost or a process restarts. A database's whole job is being the durable source of truth — a row can't vanish because the server rebooted. So Postgres's real data lives on disk (or cloud-managed disk — AWS RDS runs on EBS volumes underneath). Object storage (S3) is the same idea: distributed disk, replicated across multiple physical machines/regions, so one disk failure can't lose the only copy of a photo.

**Postgres does use RAM too — but only as a speed layer sitting in front of that durable disk copy.** `shared_buffers` (plus the OS's own page cache) keeps frequently-read pages in memory so repeated reads skip disk. If that RAM gets cleared, nothing is lost — Postgres just re-reads from disk, which still has everything.

**A browser/CDN cache is the same pattern, pushed further out.** It is never the source of truth — the real copy always still lives in S3/the DB. Losing a cached copy (RAM evicted, CDN edge cache expired) just means one slower re-fetch from the origin, never actual data loss.

**The real rule:** whatever must survive a crash goes on disk (the DB, object storage). Whatever is just a disposable, faster copy of something durable elsewhere can live in RAM — because losing it is recoverable, not catastrophic.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Why DB stores a URL, not bytes | Binary bloats backups/replicas, can't be cached, and couples bandwidth-heavy image serving to database load |
| Base64 overhead | +33% size versus raw binary — every 3 bytes of binary becomes 4 characters of text |
| Presigned URL | A short-lived, signed URL letting a client upload directly to object storage (S3/GCS) without real storage credentials |
| Why direct-to-storage upload scales | The app server never touches the file bytes — only brokers permission; bandwidth-heavy work moves off the app server entirely |
| CDN | Sits in front of object storage; caches images at edge locations near the requester, often resizes on the fly |
| Why a URL fetch ≠ embedding bytes | URL fetches are paid only by whoever needs the image, are cacheable by the browser, and run in parallel; embedded bytes cost every caller, every time, sequentially |
| Why the DB lives on disk | RAM is volatile — a crash/restart must not lose durable data; disk (or cloud-managed disk) survives that |
| Why Postgres also uses RAM (`shared_buffers`) | A speed layer in front of the durable disk copy — losing it just means a re-read from disk, not data loss |
| Why caches can live in RAM | A cache is never the source of truth — losing it costs one slower re-fetch from the real origin, never actual data |
