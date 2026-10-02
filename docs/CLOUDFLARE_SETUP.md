# Cloudflare proxy setup

The website is **pool.tig.foundation**. Both the browser dashboard and CPU/GPU
workers use **pool-api.tig.foundation/api/v2** for live requests. Cloudflare
proxies both addresses to the primary Hetzner server, **46.62.249.188**.
The German recovery server is **2.28.230.81**.
[Structure diagram](POOL_STRUCTURE.md).

## Current position — 2 October 2026

**The Origin CA certificate is installed and public HTTPS works.** Daniel
provided the signed PEM, which matches the server key and covers exactly the
two pool hostnames. It was activated at **09:14 UTC**. Its expiry is
**28 September 2041, 09:06 UTC** (the issued certificate uses a 15-year term).
Certbot renewal is disabled; the existing health check now reports origin
certificate and trust-root expiry, with a 60-day warning. External alert
notifications remain part of the wider launch work.

A fresh Chromium browser loaded the website, both hashed assets and the API.
Browser API capabilities returned **200**, and an invalid worker token returned
**401**, with working CORS and no JavaScript errors. HTML/API responses are
`no-store`; public CSS/JavaScript produced Cloudflare cache **HIT** responses.
Public TLS validates normally, and direct origin HTTP access is rejected even
when the caller supplies a fake Cloudflare forwarding header. SSH still works.
The encrypted certificate/key/configuration backup passed verification on
Germany at **09:22 UTC**. No financial features or paid work were enabled.

**A real worker-client incompatibility is now confirmed.** At **09:16 UTC**,
the unchanged `worker_v2.client.Client` received **403 / error 1010** for its
normal capabilities and benchmark requests. It used its normal headers, an
invalid test execution token and no browser User-Agent override. This was a
transport check, not a paid benchmark. Browser requests passed; the worker's
normal HTTP requests were rejected by Cloudflare before pool authentication.
Example Ray ID: **a4429b6f5f4c82d1-ARN**.

The earlier September HTTP/ACME probe did not establish this worker failure.
The new worker evidence supports the narrow configuration rule below. No
website or ACME-path security exception is needed for certificate issuance.
[Cloudflare error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).

## Next step — pool owner/operator, local browser

Codex has server access but no access to your Cloudflare account. Please add
this rule so the worker's ordinary HTTP client can reach the pool API:

1. Open **Cloudflare → tig.foundation → Rules → Overview → Create rule →
   Configuration Rule**.
2. Name it **Pool API worker requests**. Choose **Custom filter expression →
   Edit expression**, and paste:

   ```text
   (http.host eq "pool-api.tig.foundation" and starts_with(http.request.uri.path, "/api/v2/"))
   ```

3. Add the setting **Browser Integrity Check → Off**. Deploy the rule after
   any conflicting configuration rule. Keep **Full (strict)**, API cache bypass
   and other security settings unchanged.
4. Tell Codex when it is saved. Codex will repeat the same worker-client calls
   to confirm capabilities succeed and an invalid execution token is still
   refused by the pool.

This disables that browser-specific check only for `/api/v2/` on
`pool-api.tig.foundation`. Pool authentication and other attack protections
still apply. [Cloudflare's selective BIC setting](https://developers.cloudflare.com/waf/tools/browser-integrity-check/),
[configuration rule instructions](https://developers.cloudflare.com/rules/configuration-rules/create-dashboard/).

Certificate signing is complete; there is no CSR/certificate action to repeat.
The public [original CSR](POOL_ORIGIN_CERTIFICATE.csr) remains a record of the
request. The private key stays protected on the primary and in encrypted
recovery backups.

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
responses, including errors, use `no-store`. Live checks confirmed asset cache
hits and uncached HTML/API responses; no additional caching change is needed
for the tested paths.
[Cloudflare cache rules](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/).

## Server work and public checks

- [x] Verify proxied DNS; record the operator's Full (strict) and API cache-bypass
  confirmations. Host-specific dashboard overrides are not independently read.
- [x] Implement/test the website/API split and deploy `d3b9d1a` on both VMs.
  Newer repository work has not been deployed as part of this TLS change.
- [x] Generate the server key/CSR and receive Daniel's signed public PEM.
- [x] Verify the issuing chain, hostnames, dates and key match; activate HTTPS
  with saved rollback settings. Recheck official Cloudflare peer ranges.
- [x] Verify public TLS, redirects and real-browser loading/API CORS. Confirm
  cached public assets, uncached HTML/API responses and invalid-token rejection.
- [x] Verify direct origin web access is denied and SSH remains available.
- [x] Test the unchanged worker HTTP client and capture its **403 / 1010** failure.
- [ ] **Operator, local browser:** deploy the scoped Browser Integrity Check
  rule above; **Codex:** repeat the worker transport check without changing
  its headers or treating a browser success as a worker success.
- [ ] **Codex and operator:** complete wallet login, authenticated worker work,
  uploads and size/timeout checks in their authorized validation phase.
- [x] Back up and verify the final certificate, key and nginx settings on
  Germany. Supplement `configuration-origin-ca-2026-10-02T092242Z` augments the
  `2026-09-25T125901Z` database snapshot; it does not advance that DB backup.
- [x] Record expiry and add local monitoring of the origin certificate and
  RSA trust root. Replace the certificate before expiry; refresh the stored
  trust root when required. Origin CA does not use automatic ACME renewal.
- [ ] Connect health/expiry failures to external operator notifications as
  part of the mainnet launch work.

Origin CA protects the **Cloudflare → Hetzner** connection. Visitors see
Cloudflare's public certificate. Keep the proxy enabled: a browser connecting
directly would not trust an Origin CA certificate.
[Origin CA](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/),
[Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/).

Public HTTPS preparation does not complete the mainnet launch. The remaining
payment, reward, recovery and CPU/GPU validation checks still apply.
