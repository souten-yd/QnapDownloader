# Security architecture and mandatory test boundaries

Status: design specification; no browser, backend or threat filters implemented. No software can guarantee detection of every malicious site.

## Hub-managed JD2 engine (v0.1.11)

The JD2 engine is installed only by an explicit admin action from pinned official URLs (HTTPS only, redirects only to HTTPS, fixed SHA-256 and size, tar members checked for absolute paths, `..`, devices and escaping links). JD2's unauthenticated RemoteAPI is used only on `127.0.0.1` after the Hub verifies, before every call, that all LISTEN sockets on the port are loopback and owned by the Java process the Hub started (pid, start time, command line). Only a fixed method allowlist is callable and the browser can never reach JD2 directly. The engine runs as the Hub's (non-root) user with `start_new_session`; stop signals are sent only after re-verifying pid, start time and command line. The in-RAM log (300 lines) scrubs password/cookie/token-like text. Residual risk: other root-level processes on the same host can reach the API (same trust boundary as the Hub). MyJDownloader integration was removed. Real-device behaviour is unverified.

## Threat model

Untrusted inputs: third-party sites/HTML/JS, advertisements, tracking scripts, redirect destinations, Click'n'Load containers, host/plugin output, filenames, credentials embedded in URLs and downloaded file contents. Trusted controls: authenticated QNAP management UI/API, permission policy engine, NAS folder picker, credential store and local JD2 adapter.

- Untrusted website scripts MUST NOT run with the QNAP administration UI origin, token or privileges. The lightweight mode only displays sanitized article/link data. Use a separated, patched and sandboxed interactive browser when needed. If its sandbox cannot be maintained on TS-253Be, disable the mode and offer standard Safari/Chrome integration instead.
- Use HTTPS or private VPN transport; authenticate every management request even on LAN. Use strict sessions, CSRF tokens, origin checks, restrictive CSP, sanitized rendering, secure/HttpOnly/SameSite cookies, allowlist CORS and operation-specific authorization.
- Prevent SSRF from fetched URL, browser proxy, CNL import and redirect destinations. Check all IPv4/IPv6 addresses, DNS rebinding and private, link-local or loopback ranges, including QTS local administration services.
- Default-deny unsolicited tabs/popups, automatic redirect chains, push notifications, unsolicited downloads, geolocation, camera/microphone access, clipboard writing, outside app launches and persistent background site execution. Explicit user actions may grant narrowly scoped exceptions for legitimate login flows.
- Block known malicious domains, ad/tracker network requests and deceptive fake download links. Maintain validated, memory-resident filterlists; persist only changed filter data and keep event statistics in a bounded RAM ring. Provide clear errors for false positives without blanket disabling security.
- Rapidgator auto collection/download rules are OFF globally. Any exception must bind exact source origin, exact allowed target host, content eligibility, maximum links (20 per page default, 100 hard ceiling), TTL, rate limit and explicit user grant. Hidden/advertising/auto-opened links never qualify.
- Cloudflare JavaScript checks, Turnstile and CAPTCHAs: allow legitimate scripts to load in a compatible real browser and ask users to pass interactive challenges themselves. Do NOT impersonate fingerprints, bypass site controls, share auth cookies across distinct users/IPs, defeat CAPTCHA or suppress TLS errors. Provide standard-browser URL/share fallback.
- Limit memory, tabs, crawl depth, CPU and transfer concurrency, especially on 2GB TS-253Be. Do not disable Chromium sandbox flags to make features appear to work.
- JD2 headless control endpoints and CNL localhost ports are not publicly reachable and never bound to unrestricted LAN interfaces. Require credentialed internal bridge and verify real client origin; browser localhost != QNAP localhost.
- Enforce allowed save-root and real-path validation, prevent traversal and symlink escapes, block executable auto-launch, auto-extract, ZIP Slip, archive bombs, unsafe file overwrites and downloaded malware execution.
- Restrict credentials at rest, redact logs, never expose full secrets/temporary tokens in diagnostics. Manage sites and credentials only via explicit user operations.
- External Docker JD2/other QPKGs are never stopped, replaced, reconfigured or deleted without explicit consent.

## Security acceptance

Negative tests must cover SSRF and redirects, DNS rebinding, private addresses, malicious HTML/XSS, CSRF, cross-origin API access, ad-origin auto-start attempts, popup/navigation blocking, Cloudflare fail-safe, file traversal and symlink attacks, excessive link discovery, unauthorized CNL, queue double-add, restart, mounting and RAM pressure. Do not claim a scan provides perfect malware protection.
