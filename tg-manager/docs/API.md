# API Documentation

## Internal Service APIs

### Database Layer (`database/db.py`)

#### Pool Management

```python
async def create_pool() -> asyncpg.Pool:
    """Create connection pool and apply schema migrations."""
```

#### Bot Queries

```python
async def fetchrow_bot(pool, query, *args) -> dict | None:
    """Fetch single bot row with auto-decrypted token."""

async def fetch_bots(pool, query, *args) -> list[dict]:
    """Fetch multiple bot rows with auto-decrypted tokens."""

async def add_bot(pool, token, bot_id, username, first_name, added_by, bot=None) -> bool | str:
    """Add bot. Returns True if inserted, False if duplicate, 'taken' if owned by another."""
```

### Flood Engine (`services/flood_engine.py`)

#### Account State

```python
def get_account_state(account_id: int) -> _AccountFloodState:
    """Get or create in-memory flood state for an account."""

def is_account_cooling(account_id: int) -> bool:
    """Check if account is in cooldown."""

def seconds_until_ready(account_id: int) -> float:
    """Get seconds until cooldown expires."""

def recommended_delay(account_id: int, action_type: str = "default") -> float:
    """Get recommended delay before next action."""
```

#### Flood Recording

```python
async def record_flood(pool, account_id, wait_seconds, action_type="default", operation_id=None) -> float:
    """Record FloodWait event. Returns actual cooldown applied."""
```

### Resource Selector (`services/resource_selector.py`)

```python
async def select_account(pool, owner_id, action_type="default", **kwargs) -> dict | None:
    """Select single best account for an operation."""

async def select_accounts(pool, owner_id, count, action_type="default", **kwargs) -> list[dict]:
    """Select N best accounts for wave operations."""

async def select_all_active(pool, owner_id, **kwargs) -> list[asyncpg.Record]:
    """Select all active accounts respecting cooldown and trust."""
```

### Token Vault (`services/token_vault.py`)

```python
def encrypt_token(token: str) -> str:
    """Encrypt token. Returns 'ENC:<base64>' string."""

def decrypt_token(enc: str) -> str:
    """Decrypt token. Returns plaintext."""

def session_fingerprint(session_str: str) -> str:
    """Deterministic SHA256 fingerprint for deduplication."""

def proxy_fingerprint(proxy_url: str) -> str:
    """Deterministic SHA256 fingerprint for proxy deduplication."""
```

### Cache (`services/cache.py`)

```python
class TTLCache:
    def get(self, key: str) -> Any | None:
        """Get value from cache. Returns None if expired."""

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Set value with optional TTL."""

    def delete(self, key: str) -> bool:
        """Delete key. Returns True if deleted."""

    def stats(self) -> dict:
        """Get cache statistics."""

def cached(ttl=300.0, cache_instance=None):
    """Decorator to cache function results."""

def invalidate_pattern(pattern: str) -> int:
    """Invalidate all keys matching pattern."""
```

### Rate Limiter (`services/security.py`)

Подключён `security_middleware()` ко всем `/api/` и `/webhook`; отдельного
модуля-лимитера нет.

```python
async def check_rate_limit(request, uid=None, max_requests=..., window=...) -> bool:
    """True — запрос разрешён. Ключ: u:<uid>, иначе ip:<адрес>."""

def rate_limit_response(request, uid=None) -> web.Response:
    """429 с заголовком Retry-After и JSON-телом."""
```

## Bot Handler APIs

### Callback Data Models (`bot/callbacks.py`)

All callback data classes inherit from `CallbackData`:

```python
class BotCb(CallbackData, prefix="bot"):
    action: str
    bot_id: int = 0
    page: int = 0
```

### Safe Operations (`bot/utils/op_helpers.py`)

```python
def backoff(attempt, base=2.0, cap=120.0, jitter=True) -> float:
    """Exponential backoff with optional jitter."""

def extract_flood_wait(exc, err_str) -> int:
    """Extract wait seconds from FloodWaitError."""

async def safe_edit(callback, text, reply_markup=None, parse_mode="HTML") -> None:
    """Edit message, handling common Telegram errors."""

async def safe_answer(callback, text="", show_alert=False) -> None:
    """Answer callback query, swallowing errors."""
```

### Subscription (`bot/utils/subscription.py`)

```python
def get_plan(pool, user_id) -> str:
    """Get user's subscription plan ('free' or 'paid')."""

def is_platform_admin(user_id) -> bool:
    """Check if user is a platform admin."""

def set_free_mode(enabled: bool) -> None:
    """Enable/disable global free mode."""
```
