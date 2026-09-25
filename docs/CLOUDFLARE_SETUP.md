# Cloudflare proxy setup

The website is **pool.tig.foundation**. Both the browser dashboard and CPU/GPU
workers use **pool-api.tig.foundation/api/v2** for live requests. Cloudflare
proxies both addresses to the primary Hetzner server, **46.62.249.188**.
The German recovery server is **2.28.230.81**.
[Structure diagram](POOL_STRUCTURE.md).

## Current position — 25 September 2026

DNS, **Full (strict)** and the API cache-bypass rule are reported complete.
The tested website/API split (`d3b9d1a`) is installed on both VMs. Its 255
isolated tests passed, including full browser flows across separate HTTPS
origins. The primary API is private; public HTTPS awaits the signed certificate.
Mainnet work and financial operations remain paused.

The earlier generic HTTP script received **403 / error 1010** when checking a
Let's Encrypt validation path. Certificate issuance had not started, and this
was **not a real worker test**. Daniel has now approved **Cloudflare Origin CA**.
This removes the HTTP certificate-validation step. **The earlier request for a
Browser Integrity Check exception is superseded.** Test actual worker requests
after HTTPS is active; use their results before proposing a scoped rule change.

The Origin CA private key and CSR have been generated on the primary. The CSR
signature and key match are verified. The revised nginx configuration passed
syntax and private HTTPS routing, caching, CORS, authentication and direct-peer
rejection checks at **14:50 UTC**. It is staged, not publicly activated.
The existing full preparation backup was verified on Germany at **13:27 UTC**;
the new key/CSR and TLS configuration supplement passed encrypted recovery
verification on Germany at **14:54 UTC**, without activating any credentials.

## Next step — pool owner/operator, on your local computer in your browser

Codex has server access, but no access to your Cloudflare account. The one
Cloudflare action needed now is signing the prepared public certificate request:

1. Open **Cloudflare → tig.foundation → SSL/TLS → Origin Server → Create
   Certificate** (under **Origin Certificates**).
2. Choose **Use my private key and CSR**. Open
   [the prepared CSR](POOL_ORIGIN_CERTIFICATE.csr) as text and paste its entire
   contents, including the BEGIN and END lines. This is a public request;
   the private key already exists on Hetzner.
3. Set the hostnames to exactly **pool.tig.foundation** and
   **pool-api.tig.foundation**. Remove the default apex/wildcard entries if
   Cloudflare shows them. Choose **1 year** validity, then **Create**.
4. Select **PEM** format. Copy the public **Origin Certificate**, including
   `-----BEGIN CERTIFICATE-----` and `-----END CERTIFICATE-----`, and paste it
   into this chat. Codex will verify and install it. Do not send a private key.
   With the CSR option, a new private key is unnecessary.

Keep both DNS records **Proxied** and keep **Full (strict)**. No API token is
needed for these browser steps. If you do not administer `tig.foundation`,
its Cloudflare administrator can sign this same public CSR.
[Cloudflare's Origin CA instructions](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/).

## Existing DNS and cache settings

These settings are reported complete; there is no DNS action to repeat:

| Type | Name | Content | Proxy status | TTL |
|---|---|---|---|---|
| A | pool | 46.62.249.188 | Proxied — orange cloud | Auto |
| A | pool-api | 46.62.249.188 | Proxied — orange cloud | Auto |

The **Pool API cache bypass** Cache Rule matches
`http.host eq "pool-api.tig.foundation"` and sets **Cache eligibility → Bypass
cache**. It must take precedence over conflicting cache rules. Only public,
content-hashed CSS/JavaScript receive long-lived caching. HTML and all API
responses, including errors, use `no-store`. Keep any temporary website cache
bypass until public asset caching has been checked.
[Cloudflare cache rules](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/).

## Server work and public checks

- [x] Verify proxied DNS for both hosts; record the operator's Full (strict)
  and API cache-bypass confirmation.
- [x] Implement and test separate website/API origins, exact browser CORS,
  website-bound wallet signatures, API installer addresses and hashed assets.
- [x] Deploy the tested release on both VMs, retaining previous releases and
  public asset hashes. Verify pause controls and observer continuity.
- [x] Generate a root-only Origin CA private key and a public CSR for both hosts.
- [x] Stage Origin CA nginx paths and verify routing/caching/access rules on
  temporary loopback HTTPS. Keep the production API private pending issuance.
- [ ] **Operator, local browser:** sign the CSR above and return the public PEM.
- [ ] **Codex, primary:** verify Cloudflare's issuing chain, both names, expiry
  and key match; activate the checked nginx configuration with rollback.
- [ ] **Codex:** verify public browser-trusted HTTPS, redirects, website loading,
  API responses and Cloudflare caching. Confirm the effective Full (strict)
  setting with the operator; successful HTTPS alone does not prove that mode.
- [ ] **Codex:** use the unchanged worker HTTP client for a read-only capabilities
  call, plus permission-denied API requests. Record status, response type,
  Cloudflare Ray ID and cache headers. This checks transport without assigning
  work, sending tokens or enabling funds.
- [ ] **Codex and operator:** complete wallet login, authenticated worker flows,
  upload sizes/timeouts and public browser checks in the appropriate validation
  phase. No real worker compatibility result is claimed before those checks.
  If Cloudflare challenges a real request, use its evidence to identify and
  adjust only the applicable security rule; preserve API authentication.
- [ ] **Codex:** verify direct origin web access is denied while SSH remains
  usable. Trust forwarded client addresses only from official Cloudflare peers.
- [x] **Codex:** verify the encrypted key/CSR and configuration supplement on
  Germany, including decryption and key match. Its DB recovery point remains
  the 12:59 snapshot; the supplement does not advance that recovery point.
- [ ] **Codex:** refresh and verify the supplement after the signed certificate
  is installed, including the activated nginx configuration.
- [ ] **Codex:** record certificate expiry and add expiry monitoring before
  launch. Renew by obtaining/installing a replacement Origin CA certificate;
  this certificate does not use Certbot renewal. Disable the unused Certbot
  timer on activation only if no other certificates depend on it.

Origin CA protects the **Cloudflare → Hetzner** connection. Visitors see
Cloudflare's public certificate. Keep the proxy enabled: a browser connecting
directly would not trust an Origin CA certificate.
[Origin CA](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/),
[Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/).

Public HTTPS preparation does not complete the mainnet launch. The remaining
payment, reward, recovery and CPU/GPU validation checks still apply.
