# Research: MCP SDK choice — official `mcp` vs FastMCP

Ticket: `.scratch/mcp-server/issues/01-mcp-sdk-choice.md` ("MCP SDK choice: official `mcp` vs FastMCP")
Checked: 2026-11-21 (all PyPI/PyPI-JSON/GitHub/docs data fetched this date)
Deliverable for: ticket 03 (runtime architecture), ticket 09 (testing strategy), ticket 07 (auth/transport posture)

## Verdict

**Recommend the official `mcp` Python SDK (v2.x, the `MCPServer` high-level API) as the
single runtime dependency.** FastMCP 4 remains a capable framework and loses on no single
capability dimension — but for this project (Python >=3.11, FastAPI app with ~92 HTML
routes, typer CLI, LAN-trust streamable HTTP + stdio, thin tools over an existing store
layer) it wins on none either, while adding a heavier dependency tree (it transitively
depends on the official SDK anyway), a commercial-steward versioning cadence (v3→v4 within
~8 months, deprecated APIs already scheduled for removal in 4.3), and vendor lock-in to a
Prefect product line. The official SDK is the protocol's reference implementation under the
neutral LF Projects stewardship, is lighter, covers every requirement on the ticket with
first-class documentation, and its ergonomics are the same decorator style both projects
share (FastMCP's high-level API was merged into the official SDK in 2024 and now ships as
`mcp.server.MCPServer`).

## Comparison table

| Dimension | Official `mcp` (modelcontextprotocol/python-sdk) | FastMCP (PrefectHQ/fastmcp) |
| --- | --- | --- |
| Latest release | 2.3.0 (2026-10-02); 1.x line in maintenance | 4.2.4 (2026-11-21 — released today); 3.4.x maintenance line alongside |
| 2026 release velocity | 21 releases; 2.0.0 stable Jul 2026; ~1–2/month since | 75 releases; v3 early 2026, v4 GA Aug 2026, 4.2 Nov 2026 |
| Governance / steward | LF Projects ("Model Context Protocol, a Series of LF Projects, LLC") — protocol reference implementation | Prefect Technologies (commercial open-core: FastMCP Gateway hosting); repo moved jlowin → PrefectHQ |
| Python support | `requires_python >=3.10` (PyPI); README/docs state "Python 3.11+" | 4.2 `requires_python >=3.11` exactly (3.10 projects stay on 4.1.x) |
| stdio | Yes (`run()` transport overload / `mcp run` CLI) | Yes (`fastmcp run --transport stdio` / `Client` stdio transport) |
| Streamable HTTP | Yes, plus SSE (`sse_app()`); WebSocket removed in v2 | Yes (`http_app()`, HTTP deployment docs); SSE + stdio client transports |
| Protocol revision | v2 speaks 2026-07-28 and serves **both** eras (2025-11-25 handshake + sessionless) from one server | Same: negotiates both eras per connection (built on official SDK v2) |
| FastAPI coexistence (mount inside existing app) | `mcp.streamable_http_app()` → Starlette ASGI app; documented "Add to an existing app" page: `Mount` into Starlette **or FastAPI**; host app lifespan must enter `mcp.session_manager.run()`; `mount_path` removed — mounting is the supported prefix mechanism | `mcp.http_app(path='/mcp')` → ASGI app; dedicated FastAPI integration doc: `FastAPI(lifespan=mcp_app.lifespan)` + `app.mount(...)`; ships `combine_lifespans` helper |
| Standalone run | `mcp.run("streamable-http")` / `mcp run` CLI (`[cli]` extra) | `fastmcp run server.py --transport http` / `fastmcp.json` config |
| Authoring ergonomics | `@mcp.tool()` / `@mcp.resource()` / `@mcp.prompt()` decorators, type-hint schemas, docstring descriptions, `Context`, `Resolve()` dependencies, `@mcp.custom_route()`, middleware (provisional) | Same decorator core plus richer extras: transforms, providers, DI (`Depends`), visibility, versioning, background tasks, apps/skills |
| Auth primitives (HTTP) | `TokenVerifier` protocol (one async method → `AccessToken \| None`), `AuthSettings` (issuer/resource URL, scopes), RFC 9728 protected-resource metadata, 401 + `WWW-Authenticate`, `get_access_token()` context | Richer menu: `StaticTokenVerifier`, JWT providers, OAuth/OIDC proxy, full OAuth server, multi-auth, per-provider packages (Auth0, Keycloak, Entra, …) |
| LAN-trust posture fit | No auth needed: just don't set `token_verifier=`; default Host allowlist is localhost-only → pass `TransportSecuritySettings(allowed_hosts=[...])` when serving on a real hostname | Same "no auth by default"; hostname exposure handled at the web layer |
| Test utilities (pytest, in-process) | `Client(mcp)` — same `Client` class, in-memory, no wire; pytest + anyio fixtures; `raise_exceptions=True`; era-neutral client; docs tested with this pattern | `Client(mcp)` in-memory (`FastMCPTransport`); pytest-asyncio (`auto` mode) recommended; **plus** documented ASGI-level mounted-app testing via `StreamingASGITransport` + `run_asgi_lifespan` |
| License | MIT | Apache-2.0 |
| Runtime dependency weight | `mcp-types` (pydantic-only), anyio, httpx2, jsonschema, opentelemetry-api, pydantic, pyjwt[crypto], python-multipart, sse-starlette, starlette, uvicorn, typing-extensions (+pywin32 on Windows) — ~13 dists | `fastmcp` = thin meta-package → `fastmcp-slim[client,server]` → official `mcp>=2.3.0` **transitively**, plus ~2 dozen more dists (authlib, joserfc, websockets, watchfiles, openapi-pydantic, pyyaml, cyclopts, rich, py-key-value-aio, pyperclip, …) |
| "Exactly one SDK in pyproject" | Yes: `mcp` only | Satisfied nominally (`fastmcp`), but the official SDK still lands in the tree |

## Detail behind the decisive rows

### Maintenance and velocity

- Official: PyPI shows 21 releases in 2026 (Apr: 1, May: 2, Jun: 6, Jul: 4, Aug: 4, Sep: 2,
  Oct: 1). 2.0.0 stable shipped 2026-07-28 with the protocol's 2026-07-28 revision; latest
  2.3.0 on 2026-10-02. Repo pushed 2026-11-17, ~24.9k stars, MIT. v1.x stays on maintenance
  with security fixes ("v1.x is not going anywhere"). Slower cadence post-launch, but the
  project *owns the protocol* — the spec and the SDK move together.
- FastMCP: PyPI shows 75 releases in 2026, multiple per week in Oct–Nov (4.2.0 → 4.2.4
  within 8 days). High velocity means fast fixes but also frequent upgrades and churn:
  v3→v4 within ~8 months, a 4.1→4.2 upgrade guide, deprecated skills APIs already slated
  for removal in 4.3, and packaging restructured into a `fastmcp` meta-package +
  `fastmcp-slim` core + optional `fastmcp-tasks`. Stewardship moved to Prefect
  (self-reported 28.7k stars, 2.6k forks, 410+ contributors) with a commercial Gateway
  product layered on top.

### Coexistence with FastAPI (one uvicorn process serves UI + MCP)

Both SDKs return an ASGI app that FastAPI can mount; both explicitly support FastAPI as
the host. The patterns are near-identical:

- Official: `mcp.streamable_http_app()` (default endpoint `/mcp`, override via
  `streamable_http_path="/"`) mounted with `Mount`; the **host app's lifespan must enter
  `mcp.session_manager.run()`** because a mounted sub-app's own lifespan never runs.
  `@mcp.custom_route("/health")` adds plain HTTP endpoints next to `/mcp`.
- FastMCP: `mcp.http_app(path='/mcp')` mounted with `app.mount(...)` and
  `FastAPI(lifespan=mcp_app.lifespan)`; `combine_lifespans(app_lifespan, mcp_app.lifespan)`
  when the FastAPI app already has one. Slightly friendlier wiring, same underlying idea.

Standalone-from-typer: official `mcp.run("streamable-http")` or the `mcp` CLI extra;
FastMCP `fastmcp run --transport http` or `mcp.run()` in `if __name__ == "__main__"`. Both
work equally well as a `mcp_serve` typer subcommand that just calls the SDK's run/app
entrypoint. Neither depends on FastAPI, so no version pinning conflicts with the app's
FastAPI dependency.

### Testing (ticket 09)

- Official: `Client(mcp)` connects in memory — "No subprocess. No port. Nothing on a
  wire. It's the same idea as FastAPI's TestClient." Works from pytest via anyio fixtures;
  `raise_exceptions=True` shows real server errors in tests; the client is era-neutral by
  default. The SDK's own documented examples are tested through exactly this client.
- FastMCP: same in-memory `Client(mcp)` idea (pytest-asyncio auto-mode recommended), and
  additionally documents an **ASGI-level test path for the mounted app**
  (`StreamingASGITransport` + `run_asgi_lifespan`) which exercises the real FastAPI +
  mount + auth stack in-process. Nice-to-have; the in-memory client covers most of what
  ticket 09 needs either way.

### Auth revisit path (ticket 07; v1 is LAN-trust, no new auth)

- Official: the whole integration surface is one protocol — implement `TokenVerifier`
  (async `verify_token(token) -> AccessToken | None`), pass `token_verifier=` +
  `AuthSettings(issuer_url=..., resource_server_url=..., required_scopes=[...])`, and the
  app gains RFC 9728 discovery at `/.well-known/oauth-protected-resource/mcp`, 401 +
  `WWW-Authenticate` on unauthenticated requests, and `get_access_token()` inside
  handlers. It never issues tokens (your IdP does). Notably `auth_server_provider=` (the
  embedded-AS route) is discouraged for new servers.
- FastMCP: same shape but a bigger catalog (`StaticTokenVerifier`, JWT, OAuth/OIDC proxy,
  full OAuth server, multi-auth). For a household app the official surface is enough.
- LAN-trust specifics for either SDK: no auth = simply don't configure one. But the
  official streamable-HTTP app is **localhost-only by default** (DNS-rebinding protection
  via the Host allowlist); serving on a real LAN hostname needs
  `TransportSecuritySettings(allowed_hosts=[...])` or every request gets `421
  Misdirected Request`. This is a spec-visible fact for ticket 07, not a reason to prefer
  FastMCP.

### Dependency weight and the "exactly one SDK" constraint

`fastmcp` 4.2.4 is a meta-package: `fastmcp-slim[client,server]==4.2.4`. Its `server` and
`client` extras list `mcp>=2.3.0` directly — **FastMCP 4 is built on the official SDK v2**
(the FastMCP docs say so too: "FastMCP 4, which builds on the MCP Python SDK v2"). So
choosing FastMCP doesn't avoid the official SDK; it adds it as a transitive dependency
alongside roughly two dozen more packages. Choosing `mcp` alone keeps `pyproject.toml` to
one declared runtime dependency and the smaller tree (~13 distributions, dominated by
pydantic/starlette/uvicorn/anyio which the FastAPI app already ships with anyway).

## Risks / caveats to carry into the spec

1. **Mount + lifespan trap (official SDK)**: a mounted `streamable_http_app()`'s own
   lifespan is dead code; the FastAPI host lifespan must enter `mcp.session_manager.run()`
   or the first `/mcp` request fails with `RuntimeError: Task group is not initialized`.
   The spec must spell this out.
2. **Host allowlist**: default localhost-only; LAN hostname serving needs
   `TransportSecuritySettings(allowed_hosts=...)`. Ticket 07 must confirm the LAN posture
   with this setting.
3. **CORS for browser clients** (the "family phone assistants" fog item): `Mcp-*` request
   headers must be allowed and `Mcp-Session-Id` exposed, or preflighted requests never
   send.
4. **v1→v2 is a hard break in the official SDK**: `from mcp.server.fastmcp import
   FastMCP` is gone (renamed `mcp.server.MCPServer`); wire types moved to a separate
   `mcp-types` distribution with snake_case fields. Any tutorial/blog code from the 1.x
   era will not import. Fresh code (this effort) is unaffected.
5. **Error semantics differ in v2**: unexpected exceptions become sanitized `-32603`
   protocol errors; only `ToolError` messages reach the model. Tool descriptions/flows
   (tickets 04/05) should return structured errors rather than relying on raised
   exceptions.
6. **stdio has no auth in either SDK** — the stdio boundary is the process that launched
   it; auth primitives apply only to the HTTP transport.
7. If the effort later wants FastMCP's richer transforms/middleware/auth-provider
   catalog, migration official→FastMCP is cheap (FastMCP ships an
   "Upgrading from MCP SDK v2" guide). The reverse is costlier. Starting official keeps
   both doors open.

## Sources

All checked 2026-11-21.

- PyPI JSON: `mcp` 2.3.0 metadata + release history — https://pypi.org/pypi/mcp/json
- PyPI JSON: `fastmcp` 4.2.4 metadata + release history — https://pypi.org/pypi/fastmcp/json
- PyPI JSON: `fastmcp-slim` 4.2.4 dependency metadata — https://pypi.org/pypi/fastmcp-slim/json
- PyPI JSON: `mcp-types` 2.3.0 metadata — https://pypi.org/pypi/mcp-types/json
- GitHub API: modelcontextprotocol/python-sdk (stars, push date, license) — https://api.github.com/repos/modelcontextprotocol/python-sdk
- Official SDK README (Python 3.11+, transports, `MCPServer`, `Client`) — https://github.com/modelcontextprotocol/python-sdk (raw README)
- Official SDK docs: What's new in v2 (protocol revision, renames, removed transports, httpx2) — https://py.sdk.modelcontextprotocol.io/whats-new/
- Official SDK docs: Add to an existing app (ASGI mounting, lifespan, transport security, CORS, custom routes) — https://py.sdk.modelcontextprotocol.io/run/asgi/
- Official SDK docs: Testing (in-memory `Client`, pytest + anyio, `raise_exceptions`) — https://py.sdk.modelcontextprotocol.io/get-started/testing/
- Official SDK docs: Authorization (`TokenVerifier`, `AuthSettings`, RFC 9728, `get_access_token`) — https://py.sdk.modelcontextprotocol.io/run/authorization/
- FastMCP homepage (Prefect stewardship, 28.7k stars, Apache-2.0, "high-level API went into the official SDK in 2024", FastMCP 4 GA Aug 2026) — https://gofastmcp.com/
- FastMCP docs: What's new in FastMCP 4 (protocol negotiation, 4.2 Python 3.11+ requirement, upgrade cadence) — https://gofastmcp.com/docs/getting-started/whats-new
- FastMCP docs: FastAPI integration (`from_fastapi`, `http_app` mounting, lifespans, combine_lifespans) — https://gofastmcp.com/docs/integrations/fastapi.md
- FastMCP docs: Testing your FastMCP Server (in-memory client, pytest-asyncio, inline-snapshot) — https://gofastmcp.com/docs/servers/testing.md
- FastMCP docs index (auth providers, transforms, middleware, upgrades incl. "from MCP SDK v2") — https://gofastmcp.com/llms.txt
