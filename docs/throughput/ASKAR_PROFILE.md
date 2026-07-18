# Stock Askar / ACA-Py Send-Path Profile

## Question

Can optimizing DIDComm v1 packing—especially repeated key conversion or ECDH—push one
stock ACA-Py/Askar process materially beyond the observed ~50 messages/second?

## Method

- ACA-Py 1.3.0, stock `wallet-type: askar`
- Postgres wallet pool: 30
- No mediator, Redis, or ledger
- 20 holders × 1 direct connection
- `admin_only`: time the ACA-Py `/connections/{id}/send-message` path without waiting
  for Credo receipt
- Native-aware `py-spy 0.4.1` attached with `SYS_PTRACE`
- Primary profile: 4,000 messages, 20 Hz native sampling for 25 seconds
- Control GIL profile: 2,000 messages, 50 Hz nonblocking sampling for 25 seconds

The native-profiled run sustained **47.84 msg/s**, close to the previous unprofiled
admin-only baseline of **48.30 msg/s**, so the 20 Hz profile had little observable
throughput distortion. A 200 Hz blocking trial distorted throughput to 34.84 msg/s
and is retained only as corroborating data.

Artifacts:

- `askar-profile/py-spy-native-20hz.raw` (793 samples, 1 unwind error)
- `askar-profile/py-spy-gil-50hz.raw` (606 samples, 251 copy errors; directional)
- `askar-profile/py-spy-native-50hz-blocking.raw` (high-overhead corroboration)
- corresponding `run-meta-*.txt`

## What stock ACA-Py actually does

ACA-Py 1.3.0 already offloads DIDComm v1 packing to the default thread executor:

```python
from_key_entry = await self._session.handle.fetch_key(from_verkey)
return await asyncio.get_event_loop().run_in_executor(
    None, pack_message, to_verkeys, from_key, message
)
```

The synchronous worker then generates a fresh CEK, converts Ed25519 keys to X25519,
wraps the CEK with `crypto_box`, seals the sender verkey, performs AEAD encryption,
and serializes the JWE.

This corrects the earlier hypothesis that stock Askar performs all crypto directly
on the event-loop thread. It does not; the event loop still coordinates storage,
pack scheduling, the admin route, and outbound HTTP.

## Native profile

The native raw profile contained 793 weighted samples. Percentages below overlap
where stacks contain both a parent and child category, so they are evidence of hot
paths rather than additive wall-clock accounting.

| Inclusive path | Samples | Share |
|---|---:|---:|
| Any Askar FFI/native path | 360 | 45.4% |
| DIDComm pack worker | 105 | 13.2% |
| Admin basic-message route | 93 | 11.7% |
| Outbound HTTP | 56 | 7.1% |
| Connection-target lookup | 19 | 2.4% |

Within the 105 pack-worker samples:

| Pack stage | Samples | Share of pack |
|---|---:|---:|
| Ed25519→X25519 conversion | 24 | 22.9% |
| Construct recipient public key | 7 | 6.7% |
| `crypto_box_seal` sender verkey | 26 | 24.8% |
| Authcrypt `crypto_box` CEK wrap | 17 | 16.2% |
| AEAD payload encryption | 7 | 6.7% |
| Fresh CEK generation | 4 | 3.8% |

Stable key construction/conversion plus asymmetric wrapping therefore account for
roughly 70% of sampled pack work. AEAD for the four-byte payload is small.

## Python/GIL profile

The GIL-only profile kept throughput at **48.36 msg/s**. It was noisier due to
Python 3.12 frame-copy errors, but showed that significant Python work remains
outside the crypto worker:

- admin authentication/validation middleware
- Askar FFI argument/callback/destructor plumbing
- profile/session setup and teardown
- connection-record/model deserialization
- outbound aiohttp handling
- event-loop callbacks and cross-thread wakeups

Thus, faster crypto cannot remove the whole single-process ceiling.

## Cache feasibility

### Safe, straightforward cache

An in-memory LRU can cache:

- parsed recipient Ed25519 public `Key`
- recipient X25519 converted `Key`
- sender X25519 converted `Key`

The cache key would be `(sender_verkey, recipient_verkey)` with bounded size and
process-local lifetime. It must account for key rotation and ensure native key
handles remain valid and thread-safe.

### What cannot be reused

DIDComm v1 still requires per-message:

- fresh CEK
- fresh nonce/IV
- AEAD encryption
- `crypto_box_seal` ephemeral sender-verkey encryption

The current `aries_askar.crypto_box` API does not expose libsodium-style
`beforenm`/`afternm` precomputation. Reusing the authcrypt ECDH shared secret would
require an aries-askar API addition or deeper native fork—not just an ACA-Py LRU.

## Expected gain

A converted-key LRU could remove about 30% of sampled pack work. Because the pack
worker represented about 13% of all sampled stacks, its optimistic first-order
impact is only around **4% of the measured process path**, before cache locking and
handle-management overhead.

Sampling is not exact enough to promise a number, but it does rule out the earlier
idea that a simple key-conversion cache is likely to turn 50 msg/s into 100+ msg/s.
A realistic target is a single-digit to low-teens percentage improvement.

## Conclusion

The DIDComm implementation contributes to the limit, but it is **not the sole
limiting factor**:

- asymmetric pack operations dominate inside `pack_message`
- stock ACA-Py already runs pack in a thread executor
- substantial Askar session/FFI, admin middleware, model, and HTTP work remains
- the stock API cannot cache the actual `crypto_box` shared-secret computation

An LRU prototype is technically justified as a small optimization experiment, but
not as the primary scaling strategy. The higher-value stock-path investigations are:

1. reduce per-message Askar profile/session open-close and FFI churn
2. cache sender key fetch and connection targets with correct invalidation
3. avoid controller/admin validation overhead for internal high-rate dispatch
4. add native `crypto_box` precomputation only if aries-askar maintainers accept it
5. use multiple ACA-Py processes for material capacity growth

