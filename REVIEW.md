# Code Review — TempMail + Higgsfield Auto-Registration Bot

## Summary

The project is a Telegram bot that provides temporary email addresses via mail.tm and automates Higgsfield account registration with Playwright. It includes a page structure monitor, a pluggable notification system, and a watchdog sidecar for infrastructure monitoring. The architecture is well thought-out (overlay/underlay separation, notification bus, health checks), but there are several bugs, security concerns, and reliability issues that should be addressed before production use.

---

## Critical Issues

### 1. Blocking Docker event loop (BUG)
**File:** `watchdog/docker_monitor.py:249-254`

```python
events = client.events(decode=True, filters={...})

loop = asyncio.get_event_loop()
for event in events:
    if not self._running:
        break
    await self._process_event(event)
```

`client.events()` returns a **blocking generator**. Iterating over it with a `for` loop blocks the entire asyncio event loop, freezing all other coroutines (health checker, host monitor, status reports). This must be offloaded to a thread:

```python
loop = asyncio.get_running_loop()
for event in events:
    if not self._running:
        break
    await loop.run_in_executor(None, lambda: None)  # yield to loop
    await self._process_event(event)
```

Or better, iterate in an executor and push events through an `asyncio.Queue`.

Additionally, `asyncio.get_event_loop()` is deprecated since Python 3.10 and the assigned `loop` variable is never used.

### 2. Broken PID metadata in lifecycle startup (BUG)
**File:** `bot/notifications/crash_handler.py:181-182`

```python
"pid": str(sys.modules.get("os", type("", (), {"getpid": lambda: "?"})).__class__),
```

This doesn't return the PID — it returns the string representation of the `<class 'module'>` type. The intended code is:

```python
import os
"pid": str(os.getpid()),
```

### 3. Missing `.env.example` file
**File:** `README.md:13`

The README instructs `cp .env.example .env`, but no `.env.example` file exists in the repository. Users have no reference for what environment variables to set.

### 4. No concurrency guard on Chromium instances
**Files:** `bot/higgsfield_service.py`, `bot/monitor_service.py`

Multiple simultaneous `/register` commands or a `/register` + monitor check will launch multiple Chromium instances. With the 1GB container memory limit and Chromium using ~500MB each, two concurrent registrations will trigger OOM. Add an `asyncio.Semaphore(1)` to serialize browser usage.

---

## Security Issues

### 5. Credentials displayed in Telegram messages
**File:** `bot/main.py:389-393`

```python
f"🔑 Password: <code>{escape(result.password)}</code>\n"
```

The generated password is sent as a Telegram message. If the Telegram chat is compromised, forwarded, or screenshotted, credentials are exposed. Consider sending a one-time viewable message or requiring the user to retrieve credentials through a separate secure channel.

### 6. Screenshot overwrites and potential info leak
**File:** `bot/higgsfield_service.py:186`

```python
await page.screenshot(path="/tmp/higgs_error.png")
```

A fixed path means concurrent registrations overwrite each other's debug screenshots. Use a unique filename (e.g., with timestamp or UUID). The screenshot may also contain sensitive page content. The `/tmp/debug` volume mount in `docker-compose.yml` could expose these externally.

### 7. No HTTPS validation on webhook URL
**File:** `bot/notifications/webhook_notifier.py`

The `WEBHOOK_URL` from the environment is used as-is. If an HTTP (non-TLS) URL is provided, all notification events — including error details, tracebacks, and metadata — are transmitted in cleartext. Consider validating that the URL uses HTTPS, or at minimum logging a warning.

### 8. Bot token appears in notification URLs
**File:** `bot/notifications/telegram_notifier.py:73`

```python
url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
```

This is standard Telegram Bot API usage, but if HTTP errors are logged with full URLs (by aiohttp or custom error handlers), the bot token will appear in log files. Consider masking the token in error messages.

---

## Bugs

### 9. Stale proxy in mail service
**File:** `bot/mail_service.py:41-47`

```python
async def _get_session(self) -> aiohttp.ClientSession:
    if self._session is None or self._session.closed:
        proxy = get_random_proxy()
        self._session = aiohttp.ClientSession()
        self._proxy = proxy
    return self._session
```

The proxy is selected once when the session is first created and then reused for all subsequent requests. If proxy rotation is desired (as documented), the proxy should be re-selected per-request or the session should be periodically refreshed. Currently, proxy rotation only happens if the session gets closed and recreated.

### 10. No retry logic for mail.tm API calls
**File:** `bot/mail_service.py:53-59`

The `_request` method raises on any HTTP error with no retry. mail.tm has documented rate limits (8 req/sec/IP) and transient failures. The registration flow depends heavily on reliable mail API access. A simple retry with backoff for 429 and 5xx responses would significantly improve reliability.

### 11. `_change_log` grows unbounded
**File:** `bot/monitor_service.py:46,174-180`

The `_change_log` list grows forever. The `change_log` property returns only the last 20 entries, but the underlying list is never trimmed. Over time (months), this will consume memory. Use a `collections.deque(maxlen=20)` instead.

### 12. Error keyword detection is unreliable
**File:** `bot/higgsfield_service.py:341-346`

```python
error_keywords = ["already exists", "error", "invalid", "failed"]
for kw in error_keywords:
    if kw.lower() in page_text.lower():
        logger.warning(f"Possibile errore nel form: trovato '{kw}'")
        break
```

The word "error" will match on almost any page (e.g., "error handling", links containing "error", etc.). The method always returns `True` regardless of whether error keywords are found, making this check purely informational via logging. If the intent is to detect form submission errors, this needs tighter scoping (e.g., check only within the form container) and should influence the return value.

### 13. Event log trimming uses list slicing
**File:** `bot/notifications/bus.py:83-84`

```python
self._event_log.append(event)
if len(self._event_log) > self._max_log_size:
    self._event_log = self._event_log[-self._max_log_size:]
```

This creates a new list every time the cap is exceeded. Use `collections.deque(maxlen=100)` for O(1) fixed-size behavior instead of O(n) slicing.

---

## Design & Reliability Observations

### 14. No rate limiting on bot commands
**File:** `bot/main.py`

Any allowed user can spam `/register` (launching Chromium), `/monitor_check` (launching Chromium), `/newemail` (hitting mail.tm API), or `/check` (hitting mail.tm API) without any throttling. Add per-user cooldowns, especially for resource-intensive commands.

### 15. `docker` and `aiohttp` version alignment
**Files:** `bot/requirements.txt`, `watchdog/requirements.txt`

Both containers pin `aiohttp==3.10.11`. The bot pins `playwright==1.49.1` and `aiogram==3.13.1`. The watchdog pins `docker==7.1.0` and `psutil==6.1.1`. These are reasonable choices, but there's no `requirements.txt` at the project root and no lock file. Consider using a tool like `pip-compile` or `pip freeze` for reproducible builds.

### 16. Watchdog reuses bot's notifications package
**File:** `Dockerfile.watchdog:9`

```dockerfile
COPY bot/notifications/ ./notifications/
```

The watchdog copies the `notifications/` package from the bot directory. This works but creates a tight coupling. If the notifications package is updated in the bot, the watchdog must be rebuilt to pick up changes. This is fine for the current architecture but should be documented.

### 17. Health server binds to 0.0.0.0
**File:** `bot/health.py:72`

```python
site = web.TCPSite(runner, "0.0.0.0", port)
```

The health endpoint listens on all interfaces. Since the `docker-compose.yml` uses `expose` (not `ports`), this is only accessible within the Docker network — which is correct. However, if someone runs the bot outside Docker, the health endpoint would be publicly accessible. The endpoint itself is read-only and non-sensitive, so this is low risk.

### 18. `is_allowed()` reparsing on every call
**File:** `bot/main.py:92-96`

```python
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS.strip():
        return True
    allowed = {int(x.strip()) for x in ALLOWED_USER_IDS.split(",") if x.strip()}
    return user_id in allowed
```

The `ALLOWED_USER_IDS` string is parsed into a set on every command invocation. This should be parsed once at module level. The performance impact is negligible, but it's unnecessary repeated work.

### 19. Hardcoded Chrome user agent
**Files:** `bot/higgsfield_service.py:133`, `bot/monitor_service.py:79`

```python
"user_agent": "Mozilla/5.0 ... Chrome/131.0.0.0 Safari/537.36"
```

Chrome 131 will become outdated. An outdated user agent is a common fingerprinting signal for bot detection. Consider making this configurable or auto-generating from the installed Chromium version.

---

## Minor Issues

- **`bot/main.py:265`**: Variable `l` in list comprehension shadows potential builtins and is non-descriptive. Use `link` instead.
- **`bot/main.py:508`**: `bus.recent_events[-5:]` accesses the property which already slices to 20 entries, then further slices to 5. This is fine but could be simplified.
- **`watchdog/docker_monitor.py:182`**: Local variable `signal` shadows the `signal` module import (not imported in this file, but naming convention issue).
- **Missing `__init__.py`** in `watchdog/` — not needed since watchdog runs directly, but could cause confusion if someone tries to import from it.
- **`bot/monitor_service.py:214`**: f-string in `logger.info` — use lazy logging `logger.info("Monitor — baseline: %s", self._last_hashes)` to avoid string formatting when logging is disabled.

---

## Positive Aspects

- **Clean overlay/underlay architecture** — The watchdog sidecar design is solid. A process can't monitor its own death, and the sidecar pattern correctly addresses this.
- **Pluggable notification system** — The `BaseNotifier` → `NotificationBus` pattern is well-designed, extensible, and supports multiple output channels.
- **Anti-loop protection in LoggingBridge** — The `_ignored_loggers` set prevents infinite notification loops when the notification system itself logs errors.
- **Rate limiting in TelegramNotifier** — Proper handling of Telegram's rate limits with `retry_after` support.
- **Hysteresis in host monitoring** — The 90% recovery threshold prevents alert flapping when metrics hover around the threshold.
- **Health check with recovery alerting** — The HealthChecker correctly tracks consecutive failures and sends a single alert per downtime episode, plus a recovery notification.
- **Proper signal handling** — The watchdog handles SIGTERM/SIGINT gracefully with `asyncio.Event`.
- **Docker Compose configuration** — Memory limits, read-only socket mount, health checks, and proper service dependencies are all configured correctly.
