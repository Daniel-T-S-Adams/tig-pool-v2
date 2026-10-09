# Pool structure

Checked 9 October 2026. Cloudflare proxies both addresses; the website and backend
run on the same primary Hetzner server. The code is implemented and tested;
public HTTPS is active. Browser access and the normal worker client both pass
connectivity checks; see the [setup checklist](CLOUDFLARE_SETUP.md).

```mermaid
flowchart TD
    Dev["Development machine<br/>Edit and test changes"] --> Git["GitHub repositories"]
    Git -.->|Deploy and activate release| Website
    Git -.->|Deploy and activate release| API

    Browser["Member / operator browser"] <-->|Load website| WebProxy
    Browser <-->|Live balances, login and actions| ApiProxy
    Workers["Members' CPU / GPU workers"] <-->|Request work and return results| ApiProxy

    subgraph Cloudflare["Cloudflare — proxied HTTPS"]
        WebProxy["pool.tig.foundation<br/>Cache public website files"]
        ApiProxy["pool-api.tig.foundation<br/>Never cache API responses"]
    end

    WebProxy <-->|HTTPS| Website
    ApiProxy <-->|HTTPS| API

    subgraph Primary["Primary Hetzner server"]
        Website["Website"]
        API["Pool backend / API"]
        DB[("Pool database")]
        API <--> DB
    end

    Coordinator["Primary coordinator<br/>New work paused"] <--> DB
    Coordinator <-->|Existing submissions / reconciliation| TIG["TIG protocol"]
    TIG -->|Block and report responses| Observers["Primary protocol observers"]
    Base["Base RPC<br/>Historical access failing"] --> Wallet["Primary wallet observer"]
    Observers --> Spool["Local immutable protocol spool"]
    Wallet --> Spool
    Spool -->|Import saved evidence| DB
    DB -->|Verified daily bases and continuous WAL| Backup["Recovery Hetzner server<br/>Germany"]
```

- **Two addresses, one primary server.** Both DNS records use **Proxied**
  (orange cloud), with HTTPS on both connections. Daniel reports that
  **Full (strict)** is already selected. A verified Cloudflare Origin CA certificate
  protects the Cloudflare-to-Hetzner connection. Browser requests and normal
  worker API requests work. The scoped Browser Integrity Check exception
  applies only to `/api/v2/` on the API hostname.
- **Cache public website files; keep live data uncached.** The browser loads
  the website from `pool.tig.foundation`, then calls
  `pool-api.tig.foundation/api/v2` for login, balances and actions. Workers use
  that same API address for their work. Member and operator permissions remain
  distinct from worker permissions. API traffic must not receive browser
  challenge pages.
- **Updates become live after deployment and activation.** Saving changes to
  GitHub alone doesn't update the website. Public CSS/JavaScript now use
  content-hashed URLs and can be cached for a year; HTML and all API responses
  use `no-store`. Deployment retains older public asset hashes so pages already
  open can finish loading. Public checks verified cached assets and uncached
  HTML/API responses on 2 October.
- **The coordinator and observers talk directly to TIG and Base.** Outgoing protocol connections and
  backup transfers do not pass through the website's Cloudflare proxy.
- **Backup takeover is manual.** The German server preserves recovery data until
  needed. Recovery includes switching Cloudflare's origin to the backup after
  stopping the old primary and verifying the restored pool.
- **Recovery currently receives database backups, WAL and encrypted settings.**
  Its independent TIG collectors are inactive; the full primary raw-evidence
  copy is disabled after a disk-reserve failure. Pending raw files are outside
  database protection until imported. The storage proposal is recorded in
  [operations status](OPERATIONS_STATUS.md).

The existing API version remains `/api/v2`; the hostname split does not require
renaming it to `/api/v1` or creating a second backend.

The diagram shows current connections. It does not establish a completed
mainnet reward cycle. See [operations status](OPERATIONS_STATUS.md) for the
pause, unresolved block gaps, custody access and remaining cleanup.

To preview this Markdown file in VS Code, press **Ctrl+Shift+V**
(**Cmd+Shift+V** on Mac). To open the preview beside the source, press
**Ctrl+K**, then **V** (**Cmd+K**, then **V** on Mac).
