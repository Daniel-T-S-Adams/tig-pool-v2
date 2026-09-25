# Cloudflare proxy setup

Selected on 25 September 2026: **members → Cloudflare → primary Hetzner server**.
The recommended refinement is a website at **pool.tig.foundation** and an
uncached API at **pool-api.tig.foundation**, used by both the dashboard and
CPU/GPU workers. Both addresses use the same primary **46.62.249.188**; recovery
is **2.28.230.81** in Germany. The application split is implemented and passed
255 isolated tests, including the browser's full financial fixture across two
HTTPS origins. Release `d3b9d1a` is installed on the primary and German recovery
VMs; public HTTPS activation and the checks below remain separate.
See the [structure diagram](POOL_STRUCTURE.md).

## Current position

Read-only checks at **10:03 UTC on 25 September 2026** confirmed that public DNS
already returns Cloudflare addresses and HTTP requests pass through Cloudflare.
HTTP shows the pool's setup message. HTTPS returns **521**; the primary server
has no HTTPS certificate installed and is not yet listening on port 443.
Daniel reported **Full (strict)** and confirmed both DNS records and API cache
bypass were configured. Both hostnames resolved through Cloudflare at 12:21 UTC
on 25 September. Automated certificate-path probes then returned Cloudflare
**403 / error 1010 (Browser Integrity Check)**. The primary's HTTP configuration
accepts both hostnames, but certificate issuance awaits the scoped exception
below. Mainnet work and financial operations remain paused.

The tested release was installed on recovery at **12:56 UTC** and primary at
**12:57 UTC**. The primary API remains private on loopback, with both configured
origins verified. Fourteen migration checksums match; no migrations or financial
changes occurred. Both collectors retain continuous, conflict-free history.
The new nginx routing and Cloudflare-only origin access configuration are
syntax-checked but not activated. A temporary loopback nginx instance also
passed routing, caching, API authentication/CORS, redirects and untrusted-peer
rejection checks; its test certificate/listener were removed. Actual public
TLS and Cloudflare behavior still need verification.
[Draft PR 21](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/21).

A fresh preparation backup passed verification on Germany at **13:27 UTC**:
207,779 file checksums, the exact application archive, retained public assets
and decrypted settings all passed. Certificates have not yet been issued;
the final TLS backup remains a later step.

## Your next steps — pool owner/operator, local browser

The DNS, Full (strict) and API cache-bypass steps below are **reported complete**.
The new remaining operator step is the automated-request exception.

1. Open **Cloudflare → tig.foundation → DNS → Records**. Confirm the website
   record and add the API record if it does not already exist. Edit existing
   records rather than adding duplicates.

   | Type | Name | IPv4 address / Content | Proxy status | TTL |
   |---|---|---|---|---|
   | A | **pool** | **46.62.249.188** | **Proxied — orange cloud** | Auto |
   | A | **pool-api** | **46.62.249.188** | **Proxied — orange cloud** | Auto |

2. **Complete: Full (strict) reported by Daniel.** Keep that setting. It verifies
   the certificate on Hetzner, which still needs installing for both hostnames.
   Codex will check for hostname-specific overrides during validation.

3. Add the permanent **API Cache Rule**: open **Cache Rules → Create rule**,
   name it `Pool API cache bypass`, select **Custom filter expression**, match
   **Hostname equals pool-api.tig.foundation**, and set **Cache eligibility →
   Bypass cache**. Place it after other matching cache rules, then deploy.
   If the earlier `pool.tig.foundation` rollout bypass has already been created,
   keep it during the migration. Enable website caching after the split and
   release-aware asset URLs are tested. API responses still served on the old
   website hostname must also remain uncached during the transition.

4. In **Rules → Overview → Create rule → Configuration Rule**, create
   `Pool automated requests`. Select **Custom filter expression → Edit
   expression** and paste:

   ```text
   (http.host eq "pool-api.tig.foundation") or ((http.host eq "pool.tig.foundation") and starts_with(http.request.uri.path, "/.well-known/acme-challenge/"))
   ```

   Add **Browser Integrity Check → Off** and deploy after any conflicting
   configuration rule. This exception covers the API and the website's
   certificate-validation path; it preserves browser checks on other website
   paths and does not disable authentication, rate limits or other protections.
   Confirm when it is saved. [Cloudflare error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/),
   [scoped Browser Integrity Check settings](https://developers.cloudflare.com/rules/configuration-rules/settings/#browser-integrity-check).

[Cloudflare proxy status](https://developers.cloudflare.com/dns/proxy-status/),
[Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/),
[creating a cache rule](https://developers.cloudflare.com/cache/how-to/cache-rules/create-dashboard/),
[cache bypass setting](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/#bypass-cache).

If you do not administer the `tig.foundation` zone, its Cloudflare administrator
can make these hostname-specific changes. No Cloudflare API token is required
for the browser steps.

## Codex's server work and checks

- [x] Confirm website DNS and HTTP responses pass through Cloudflare.
- [x] Record Daniel's report that Full (strict) is selected.
- [x] Confirm both hostnames resolve through Cloudflare.
- [x] Make the browser API address configurable; allow only the website origin
  for cross-origin browser access and in its content security policy. Keep
  wallet signatures bound to the website origin. Set installer commands,
  downloads and artifact links to the API address, retaining `/api/v2` and
  the existing authentication/permission checks. Test both browser and worker
  clients, including denied origins and permissions.
- [x] Use content-hashed URLs for public CSS/JavaScript, with immutable cache
  headers. Keep HTML and all API responses (including errors) `no-store`.
  Tests check content changes, origin isolation and the live dashboard flow.
- [x] Install the tested release on both VMs; configure the primary's private
  API and retain previous public asset hashes across releases. Restarts preserve
  financial state, pause controls, migration checksums and observer continuity.
- [ ] Activate the staged [nginx configuration](../deploy/v2-mainnet/nginx.conf.example)
  after certificates are issued. Verify actual Cloudflare cache behavior and
  permit website asset caching after rollout.
- [ ] Verify the certificate-validation path through the proxy; obtain and
  install certificates for both hostnames on the **primary Hetzner VM**; activate
  HTTPS and test renewal. If existing Cloudflare rules interfere, identify the
  exact rule requiring an operator adjustment.
- [ ] **Operator, local Cloudflare browser, after the certificate is ready:**
  confirm the effective mode is **Full (strict)** for both hostnames. If it is
  not, open **Rules → Overview → Create rule → Configuration Rule**. Name it
  `Pool strict HTTPS`, match **Hostname equals pool.tig.foundation OR Hostname
  equals pool-api.tig.foundation**, add the
  **SSL** setting and choose **Full (strict)** (shown as **Strict** in some
  rule interfaces), then deploy. Check for conflicting matching rules.
- [ ] Verify Cloudflare's public certificate, the Hetzner certificate, HTTPS
  redirects, website loading, wallet login and operator access. A working web
  page does not by itself prove that Full (strict) is selected.
- [ ] Verify cache bypass and real worker API requests, result/proof uploads,
  request size and timeout limits. Worker requests must not receive browser
  challenge pages; browser API calls must also receive API responses. Scope any necessary security-rule adjustments to the
  affected pool routes; retain authentication and attack protection.
- [ ] Configure the primary to accept public web traffic only from Cloudflare
  and trust forwarded client addresses only from Cloudflare. Preserve SSH
  administration and certificate renewal, and test that direct web access
  cannot bypass the proxy.
- [x] Back up the new release, retained public assets and pending web settings
  on Germany; verify every checksum and decrypt protected settings privately.
- [ ] Back up the final web/certificate configuration on the German recovery
  server and record the Cloudflare hostname rules for a manual takeover.

[Hostname-specific SSL rules](https://developers.cloudflare.com/rules/configuration-rules/settings/#ssl),
[creating a configuration rule](https://developers.cloudflare.com/rules/configuration-rules/create-dashboard/),
[challenge pages and API compatibility](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/),
[protecting the origin](https://developers.cloudflare.com/fundamentals/security/protect-your-origin-server/).

Cache only public website content under this plan; balances, wallet login,
withdrawals, operator actions and work responses remain uncached.
[Cloudflare cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/).

Completing this setup establishes the public website and API. Mainnet funding
and work still require the remaining payment, reward, recovery and CPU/GPU
validation checks in the launch plan.
