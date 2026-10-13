# Community Pool branding

Community Pool is the name of the member-run pool for The Innovation Game (TIG)
served by this repository. It replaces the earlier "InnoPool v2" name on the
website, in wallet sign-in messages and in the API title. The identity is
deliberately simple: one accent colour, system typography and a small mark.

## Name

- Write **Community Pool** as two capitalised words. Do not write
  "CommunityPool", "community pool" or "CP" in member-facing text.
- Say "the pool" after the first mention. Add "TIG" only where the context is
  not already clear, such as a page description.
- Member-facing text does not call the pool "v2" or "InnoPool". Protocol and
  deployment identifiers keep their existing names because deployed workers,
  services and backups depend on them: the `X-InnoPool-Version` header, the
  `innopool-v2-*` service, directory and container names, the
  `innopool-slave-v2` repository and the database names. The member API is
  still version `2.0`; that is a protocol number, not a product name.

## Mark

The mark is a ring holding three dots: members pooled around one benchmark.
It is drawn inline in the page shell (`pool_manager/pool_v2/web/index.html`)
so the website needs no extra asset, and the favicon is the same mark on an
accent tile as a data URI. Standalone copies are in `docs/brand/`:

- `mark.svg`: the mark alone, in the accent colour, for avatars and icons.
- `logo.svg`: the mark with the wordmark, for documents and announcements.

Keep clear space of at least half the mark's width around it. Do not recolour
the dots individually or add gradients, shadows or outlines.

## Colour

| Token | Value | Use |
| --- | --- | --- |
| Ink | `#1c2a2d` | Headings and body text |
| Muted | `#5e6f72` | Secondary text and table headings |
| Accent | `#0f6b73` | Mark, primary buttons, eyebrows, text buttons |
| Accent strong | `#0b555b` | Button hover and text on accent tints |
| Accent soft | `#e3f0ef` | Highlighted metric, active pills, notices |
| Accent line | `#c3dedc` | Borders on accent tints |
| Background | `#f3f6f6` | Page background |
| Paper | `#ffffff` | Cards, header and inputs |
| Line | `#dbe3e3` | Card and table borders |
| Danger | `#a23d35` | Errors and destructive actions |

Accent on white and ink on the background both exceed the 4.5:1 contrast
ratio for normal text. The stylesheet defines these as custom properties on
`:root` in `pool_manager/pool_v2/web/style.css`; change them there, not in
individual rules.

## Type and layout

- Inter where installed, otherwise the system sans-serif stack. No web fonts
  are loaded, which keeps the content security policy unchanged.
- Page titles are large and tightly tracked; section headings are small. The
  eyebrow above a page title is uppercase, letter-spaced and in the accent.
- Cards are white on the tinted background with 12px corners. Status pills are
  fully rounded. A 3px accent rule runs along the top of the page and the
  welcome card repeats it.
- The brand changes nothing about how TIG amounts are formatted or laid out.

## Where the brand text lives

| Place | File |
| --- | --- |
| Page title, description, favicon, header and footer | `pool_manager/pool_v2/web/index.html` |
| Colours, type and layout | `pool_manager/pool_v2/web/style.css` |
| Wallet sign-in and withdrawal-wallet messages | `pool_manager/pool_v2/auth.py` |
| API title in the OpenAPI document | `pool_manager/pool_v2/api.py` |
| Repository and guide titles | `README.md`, `docs/OWNER_GUIDE.md`, `docs/MEMBER_SETUP_V2.md` |

`tests/v2/test_api_origins.py` checks that the served shell and API carry the
name. Dated operations records keep the wording they had when they were written.
