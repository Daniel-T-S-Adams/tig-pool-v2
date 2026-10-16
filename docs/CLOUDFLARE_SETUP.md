# Cloudflare proxy setup

The website is **pool.example**. Both the browser dashboard and CPU/GPU
workers use **pool-api.example/api/v2** for live requests. These two
hostnames are placeholders for the operator's own; substitute them wherever
they appear in this documentation. Cloudflare proxies both addresses to the
primary host. [Structure diagram](POOL_STRUCTURE.md).

## Deployed configuration

The HTTPS setup and the scoped API rule below are the recorded configuration.
Member sign-in and mainnet CPU assignment/result upload through the proxy are
validated after these transport checks. Database/WAL/settings/pending-file
recovery protection is described in [backup operations](BACKUP_OPERATIONS.md).
External alerting is the operator's decision ([roadmap](ROADMAP.md)); without
a configured destination, manual inspection is the process. The checks below
are transport/TLS evidence, not a statement about the release or fund flags.

## Transport and TLS validation

**Worker connectivity passes once the scoped rule is saved.** The unchanged
worker client receives capabilities **200** and an invalid-token response
**401** from the pool. API preflight succeeds, responses stay `no-store`, and
HTTP API requests redirect to HTTPS. This verifies transport and
invalid-token rejection; authenticated work/uploads remain separate checks.
No benchmark or funds movement is involved.

**The Origin CA certificate enables public HTTPS.** The operator provides the
signed PEM, which must match the server key and cover exactly the two pool
hostnames. Origin CA certificates can be issued with a 15-year term; record
the expiry. Certbot renewal is disabled; the health check reports origin
certificate and trust-root expiry, with a 60-day warning. Forwarding those
notifications externally is part of the wider launch work.

A fresh Chromium browser loads the website, both hashed assets and the API.
Browser API capabilities return **200**, and an invalid worker token returns
**401**, with working CORS and no JavaScript errors. HTML/API responses are
`no-store`; public CSS/JavaScript produce Cloudflare cache **HIT** responses.
Public TLS validates normally, and direct origin HTTP access is rejected even
when the caller supplies a fake Cloudflare forwarding header. SSH still works.
The encrypted certificate/key/configuration backup is verified on the recovery
server. No financial features or paid work are enabled by this step.

**Failure mode the rule fixes:** without the scoped rule, the unchanged
`worker_v2.client.Client` receives **403 / error 1010** for its normal
capabilities and benchmark requests. It uses its normal headers, an invalid
test execution token and no browser User-Agent override. This is a transport
check, not a paid benchmark. Browser requests pass; the worker's normal HTTP
requests are rejected by Cloudflare before pool authentication.

An HTTP/ACME probe does not establish this worker failure. The worker evidence
supports the narrow configuration rule below. No website or ACME-path security
exception is needed for certificate issuance.
[Cloudflare error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).

## Configuration rule — keep for configuration reference

The pool operator creates this rule in the Cloudflare browser dashboard. Once
it is saved, the normal worker client passes. These are the rule
instructions:

1. Open **Cloudflare → your zone → Rules → Overview → Create rule →
   Configuration Rule**.
2. Name it **Pool API worker requests**. Choose **Custom filter expression →
   Edit expression**, and paste:

   ```text
   (http.host eq "pool-api.example" and starts_with(http.request.uri.path, "/api/v2/"))
   ```

3. Add the setting **Browser Integrity Check → Off**. Deploy the rule after
   any conflicting configuration rule. Keep **Full (strict)**, API cache bypass
   and other security settings unchanged.
4. **Verify:** the same worker-client calls now succeed for capabilities, and
   invalid execution tokens are still refused by the pool.

This disables that browser-specific check only for `/api/v2/` on
`pool-api.example`. Pool authentication and other attack protections
still apply. [Cloudflare's selective BIC setting](https://developers.cloudflare.com/waf/tools/browser-integrity-check/),
[configuration rule instructions](https://developers.cloudflare.com/rules/configuration-rules/create-dashboard/).

Keep the CSR used for the certificate request as a record. The private key
stays protected on the primary and in encrypted recovery backups.

## Existing DNS and cache settings

The required DNS records and cache settings:

| Type | Name | Content | Proxy status | TTL |
|---|---|---|---|---|
| A | pool | `<primary host address>` | Proxied — orange cloud | Auto |
| A | pool-api | `<primary host address>` | Proxied — orange cloud | Auto |

The **Pool API cache bypass** Cache Rule matches
`http.host eq "pool-api.example"` and sets **Cache eligibility → Bypass
cache**. It must take precedence over conflicting cache rules. Only public,
content-hashed CSS/JavaScript receive long-lived caching. HTML and all API
responses, including errors, use `no-store`. Live checks should confirm asset
cache hits and uncached HTML/API responses; no additional caching change is
needed for these paths.
[Cloudflare cache rules](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/).

## Server work and public checks

- Verify proxied DNS; record the operator's Full (strict) and API cache-bypass
  confirmations. Host-specific dashboard overrides are not independently read.
- Deploy the release with the website/API split on both VMs.
- Generate the server key/CSR and receive the operator's signed public PEM.
- Verify the issuing chain, hostnames, dates and key match; activate HTTPS
  with saved rollback settings. Recheck official Cloudflare peer ranges.
- Verify public TLS, redirects and real-browser loading/API CORS. Confirm
  cached public assets, uncached HTML/API responses and invalid-token rejection.
- Verify direct origin web access is denied and SSH remains available.
- Test the unchanged worker HTTP client; without the scoped rule it fails with
  **403 / 1010**.
- **Operator, local browser:** deploy the scoped Browser Integrity Check rule
  above; then repeat the unchanged worker client and confirm capabilities 200,
  invalid token 401, preflight and API no-store checks pass.
- Complete wallet login, authenticated worker work, uploads and size/timeout
  checks in their authorized validation phase.
- Back up and verify the final certificate, key and nginx settings on the
  recovery server. A configuration supplement augments the existing database
  snapshot; it does not advance that DB backup.
- Record expiry and add local monitoring of the origin certificate and
  RSA trust root. Replace the certificate before expiry; refresh the stored
  trust root when required. Origin CA does not use automatic ACME renewal.
- Connect health/expiry failures to external operator notifications if the
  operator chooses an alert destination.

Origin CA protects the **Cloudflare → origin** connection. Visitors see
Cloudflare's public certificate. Keep the proxy enabled: a browser connecting
directly would not trust an Origin CA certificate.
[Origin CA](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/),
[Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/).

Public HTTPS preparation does not complete the mainnet launch. The remaining
payment, reward, recovery and CPU/GPU validation checks still apply.
