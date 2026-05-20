from flask import Flask, request, jsonify, render_template, session, redirect
from blockchain import has_recent_ubi_claim, is_identity_verified, check_ubi_entitlement
from analytics_service import analytics
from routes import routes
from learn_and_earn import init_learn_and_earn, init_learn_earn_stream_scheduler
from web3 import Web3
from datetime import datetime # Import datetime for session timestamp
import os
import logging
import subprocess
import sys
import json
import base64 as _b64

from reloadly import init_reloadly
from savings import init_savings


from functools import wraps
from time import time
from flask_compress import Compress

# Simple in-memory cache for frequently accessed data
_cache = {}
_cache_timestamps = {}
CACHE_DURATION = 60  # 60 seconds

def cached_response(duration=CACHE_DURATION):
    """Decorator to cache API responses"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Create cache key from function name and args
            cache_key = f"{f.__name__}:{str(args)}:{str(kwargs)}"

            # Check if cached and still valid
            if cache_key in _cache:
                if time() - _cache_timestamps.get(cache_key, 0) < duration:
                    return _cache[cache_key]

            # Execute function and cache result
            result = f(*args, **kwargs)
            _cache[cache_key] = result
            _cache_timestamps[cache_key] = time()

            # Limit cache size (keep only 100 most recent)
            if len(_cache) > 100:
                oldest_key = min(_cache_timestamps, key=_cache_timestamps.get)
                del _cache[oldest_key]
                del _cache_timestamps[oldest_key]

            return result
        return decorated_function
    return decorator



# Configure logging - reduced for production performance
logging.basicConfig(level=logging.WARNING)  # Changed from INFO to WARNING
logger = logging.getLogger(__name__)

# Reduce werkzeug logging for health checks
logging.getLogger('werkzeug').setLevel(logging.ERROR)  # Changed from WARNING to ERROR
logging.getLogger('httpx').setLevel(logging.ERROR)  # Reduce httpx logging

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.environ.get('SESSION_SECRET') or os.environ.get('SECRET_KEY', 'your-secret-key-here')

# Enable gzip compression
compress = Compress()
compress.init_app(app)

# Configure session for better persistence
from datetime import timedelta
app.permanent_session_lifetime = timedelta(hours=24)  # 24 hour session lifetime
app.config['SESSION_COOKIE_SECURE'] = True  # Use HTTPS for cookies
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_DOMAIN'] = None  # Allow cookies on all domains (including custom domains)

# Memory optimization for Reserved VM (1 vCPU / 2 GiB RAM)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB max file upload (reduced)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # Cache static files for 1 year

# Performance optimization for low-resource deployment
app.config['JSON_SORT_KEYS'] = False  # Reduce JSON processing overhead
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False  # Disable pretty printing for better performance
app.config['TEMPLATES_AUTO_RELOAD'] = False  # Disable template auto-reload in production

# Database connection pooling
app.config['SQLALCHEMY_POOL_SIZE'] = 5
app.config['SQLALCHEMY_POOL_TIMEOUT'] = 10
app.config['SQLALCHEMY_POOL_RECYCLE'] = 3600
app.config['SQLALCHEMY_MAX_OVERFLOW'] = 2


# ---------------------------------------------------------------------------
# Cache-busting / freshness
#
# Goal: users always see the latest HTML, and any updated JS/CSS/image assets
# are picked up immediately without hard-refresh. Static files are still
# cached long-term (SEND_FILE_MAX_AGE_DEFAULT), but every template reference
# is suffixed with ?v={{ ASSET_VERSION }}, so a new deployment produces new
# URLs and defeats stale cache entries.
# ---------------------------------------------------------------------------
def _compute_asset_version():
    env_version = (
        os.environ.get('ASSET_VERSION')
        or os.environ.get('VERCEL_GIT_COMMIT_SHA')
        or os.environ.get('GIT_COMMIT_SHA')
        or os.environ.get('RENDER_GIT_COMMIT')
        or os.environ.get('SOURCE_VERSION')
        or os.environ.get('HEROKU_SLUG_COMMIT')
    )
    if env_version:
        return env_version[:12]
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short=12', 'HEAD'],
            capture_output=True, text=True, timeout=2,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        sha = result.stdout.strip()
        if result.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return str(int(time()))


ASSET_VERSION = _compute_asset_version()
logger.info(f"Asset version (cache-buster): {ASSET_VERSION}")


@app.context_processor
def _inject_asset_version():
    """Expose ASSET_VERSION to all Jinja templates for cache-busting."""
    return {'ASSET_VERSION': ASSET_VERSION}


@app.after_request
def _add_cache_headers(response):
    """Ensure HTML pages are always revalidated so new deployments are
    visible immediately. Static assets keep their long-cache headers because
    their URLs are versioned via ?v=ASSET_VERSION."""
    try:
        path = request.path or ''
        content_type = response.headers.get('Content-Type', '')

        if path.startswith('/static/'):
            # Long-cache versioned assets; browser will refetch when the
            # ?v=... query changes.
            if 'Cache-Control' not in response.headers:
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            return response

        if content_type.startswith('text/html'):
            existing = response.headers.get('Cache-Control', '')
            # Don't downgrade stricter policies (e.g. login/admin/logout
            # already set private no-store); otherwise force no-cache so
            # rendered pages are always revalidated.
            if 'no-store' not in existing and 'no-cache' not in existing:
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
    except Exception as _cache_hdr_err:
        logger.debug(f"cache header hook skipped: {_cache_hdr_err}")
    return response


# ---------------------------------------------------------------------------
# Security headers
#
# Defense in depth for a non-custodial DeFi frontend. The biggest risk to
# users is NOT server-side custody theft (we don't hold keys) — it's that an
# attacker who compromises any layer of the page (CDN, hosting, dependency,
# inline script injection) can swap the smart-contract address or inject a
# `permit/approve` call that drains user wallets the moment they sign.
#
# These headers don't prevent that scenario on their own, but each one
# narrows the blast radius:
#   - CSP restricts WHERE scripts/styles/connections can come from.
#   - X-Frame-Options + frame-ancestors prevent click-jacking inside an iframe.
#   - X-Content-Type-Options stops MIME sniffing tricks.
#   - Referrer-Policy reduces leak of session URLs to third parties.
#   - Permissions-Policy turns off browser features we never use.
#   - Strict-Transport-Security forces HTTPS for return visits.
#
# CSP is intentionally permissive ('unsafe-inline' / 'unsafe-eval') because
# the existing templates use a lot of inline scripts and ethers.js historically
# uses Function() at runtime. Tightening should be incremental: convert inline
# handlers to addEventListener, then drop 'unsafe-inline'; verify ethers works
# without eval, then drop 'unsafe-eval'. Until then, the host whitelist is
# still meaningful — an attacker who injects `<script src=//evil.tld/x.js>`
# will be blocked by the browser even if our HTML is compromised.
# ---------------------------------------------------------------------------
_CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
    "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com "
    "https://esm.sh "
    "https://telegram.org https://*.telegram.org",
    "style-src 'self' 'unsafe-inline' "
    "https://fonts.googleapis.com https://cdnjs.cloudflare.com",
    "font-src 'self' data: "
    "https://fonts.gstatic.com https://cdnjs.cloudflare.com",
    "img-src 'self' data: blob: https:",
    "connect-src 'self' https: wss:",
    "frame-src 'self' "
    "https://telegram.org https://*.telegram.org "
    "https://www.youtube.com https://www.youtube-nocookie.com "
    "https://platform.twitter.com "
    "https://apiplus.squidrouter.com https://studio.squidrouter.com",
    "media-src 'self' data: blob:",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
    "upgrade-insecure-requests",
)
_CSP_HEADER_VALUE = "; ".join(_CSP_DIRECTIVES)


@app.after_request
def _add_security_headers(response):
    """Attach hardening headers to every HTML/JS response.

    Skips static assets (immutable, already cached on CDN/clients) so we
    don't bloat their headers — the assets themselves are versioned via
    ASSET_VERSION cache-busting.
    """
    try:
        # Don't add to opaque static binary responses; they don't render
        # JS/HTML and CSP wouldn't apply anyway.
        path = request.path or ""
        if not path.startswith("/static/"):
            response.headers.setdefault("Content-Security-Policy", _CSP_HEADER_VALUE)

        # Always-on lightweight headers.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=(), usb=(), "
            "magnetometer=(), gyroscope=(), accelerometer=()",
        )
        # 1 year HSTS, only meaningful when served over HTTPS in production.
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
        # Disable legacy XSS filter (modern browsers ignore this; old ones
        # have a buggy implementation that can introduce vulnerabilities).
        response.headers.setdefault("X-XSS-Protection", "0")
    except Exception as _sec_hdr_err:
        logger.debug(f"security header hook skipped: {_sec_hdr_err}")
    return response


# ---------------------------------------------------------------------------
# Scanner / vulnerability-probe hardening
#
# Public production sites are continuously hit by automated scanners that
# probe well-known leaked-secret paths (`.env`, `.git`, CI configs, AWS
# credentials, wp-admin, phpMyAdmin, etc.). Returning a fast 403 short-circuits
# the request before it touches any blueprint or the template engine, keeps
# the access logs cleaner, and signals to scanners that the path is denied.
#
# Note: this is defense-in-depth log hygiene, not a substitute for not
# committing secrets. The repo already does the right thing (only `.env.example`
# is checked in) — these probes have always returned 404 because the files
# genuinely don't exist.
# ---------------------------------------------------------------------------
import re as _re

_BLOCKED_PATH_PATTERNS = tuple(_re.compile(p, _re.IGNORECASE) for p in (
    r"^/\.env(\..*)?$",
    r"^/\.git(/|$)",
    r"^/\.aws(/|$)",
    r"^/\.ssh(/|$)",
    r"^/\.circleci(/|$)",
    r"^/\.github(/|$)",
    r"^/\.vscode(/|$)",
    r"^/\.idea(/|$)",
    r"^/\.docker(/|$)",
    r"^/\.npmrc$",
    r"^/\.htaccess$",
    r"^/\.htpasswd$",
    r"^/\.DS_Store$",
    r"^/config/(secrets|aws|credentials|prod|dev|production)(/|$)",
    r"^/secrets?(/|$)",
    r"^/credentials?(/|$)",
    r"^/aws[_-]?credentials",
    r"^/backup(\.|/|$)",
    r"^/dump(\.|/|$)",
    r"^/wp-(admin|login|content|includes)(/|$)",
    r"^/phpmyadmin(/|$)",
    r"^/phpinfo(\.php)?$",
    r"^/server-status$",
    r"^/server-info$",
    r"^/(actuator|jmx-console|manager)(/|$)",
    r"\.(sql|bak|swp|old|orig|save|tar|tar\.gz|tgz|zip|rar|7z)$",
))


@app.before_request
def _block_scanner_paths():
    """Reject requests that match well-known scanner / probe path patterns.

    Returns 403 instead of letting Flask fall through to a 404 from the
    template engine or the static handler. Logged at INFO so we have an
    audit trail of probes without spamming WARNING.
    """
    try:
        path = request.path or ""
        for pattern in _BLOCKED_PATH_PATTERNS:
            if pattern.search(path):
                logger.info(
                    "blocked scanner probe: %s %s from %s",
                    request.method, path, request.headers.get("X-Forwarded-For") or request.remote_addr,
                )
                return ("Forbidden", 403, {"Content-Type": "text/plain; charset=utf-8"})
    except Exception as _hard_err:
        logger.debug(f"scanner-block hook skipped: {_hard_err}")
    return None


# ---------------------------------------------------------------------------
# Root-level static fallbacks
#
# Browsers (and some Slack/Discord/Telegram link previewers) automatically
# request `/favicon.ico` and `/service-worker.js` at the site root, regardless
# of what the HTML <link> tags declare. We serve these explicitly so they
# don't 404 in production logs. The actual icon and service worker live under
# /static/, but root requests are common because:
#   - favicon.ico: built-in browser fallback when no <link rel=icon> matches.
#   - service-worker.js (root): older client browsers may still have a
#     stale registration pointing to the old root path; we serve a script
#     that unregisters itself so those clients self-clean.
# ---------------------------------------------------------------------------
from flask import send_from_directory, Response as _FlaskResponse


@app.route("/favicon.ico")
def _favicon():
    return send_from_directory(
        app.static_folder, "icons/favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


@app.route("/static/service-worker.js")
def _dynamic_service_worker():
    """Serve the service worker with the current build version baked into
    the response body.

    The browser decides whether to install a new service worker by doing a
    byte-for-byte comparison of the fetched SW file against the currently
    registered one. If we ship `static/service-worker.js` as a regular static
    asset, its bytes never change between deployments, so the browser keeps
    the old worker forever and the in-page "New Version Available" banner
    (see login.html / dashboard.html) never fires.

    By substituting `__BUILD_VERSION__` with `ASSET_VERSION` (which is derived
    from the git commit SHA, see `_compute_asset_version`), the SW file
    content changes on every deployment. That triggers the standard SW update
    lifecycle: install -> skipWaiting -> activate -> postMessage SW_UPDATED.

    Notes:
    - This route takes precedence over Flask's default `/static/<path>` rule
      because Werkzeug matches more-specific rules first.
    - Cache-Control is `no-cache` so browsers always revalidate the SW. The
      file is small, so the bandwidth cost is negligible.
    - `Service-Worker-Allowed` keeps the registration scope at `/static/`
      (the default for this URL); we don't widen it here.
    """
    sw_path = os.path.join(app.static_folder or "static", "service-worker.js")
    try:
        with open(sw_path, "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError as exc:
        logger.error(f"service worker read failed: {exc}")
        return _FlaskResponse("// service worker unavailable\n",
                              status=500, mimetype="application/javascript")

    body = body.replace("__BUILD_VERSION__", ASSET_VERSION)

    resp = _FlaskResponse(body, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Service-Worker-Allowed"] = "/static/"
    return resp


@app.route("/service-worker.js")
def _root_service_worker_unregister():
    """Self-unregistering shim for stale root-scoped service workers.

    Older deployments registered the service worker at `/service-worker.js`.
    Browsers cache that registration indefinitely, so even after we moved the
    SW to `/static/service-worker.js` those clients keep hitting this path.
    Returning a script that calls `registration.unregister()` makes those
    clients clean themselves up on the next page load.
    """
    body = (
        "// Stale root-scoped service worker shim — unregisters itself so the\n"
        "// browser falls back to /static/service-worker.js on next load.\n"
        "self.addEventListener('install', () => self.skipWaiting());\n"
        "self.addEventListener('activate', (event) => {\n"
        "  event.waitUntil((async () => {\n"
        "    try { await self.registration.unregister(); } catch (_) {}\n"
        "    const clients = await self.clients.matchAll();\n"
        "    for (const c of clients) { try { c.navigate(c.url); } catch (_) {} }\n"
        "  })());\n"
        "});\n"
    )
    resp = _FlaskResponse(body, mimetype="application/javascript")
    # Don't cache this aggressively — once stale clients clear, we want them
    # to stop getting it served.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


# Blockchain configuration for wallet balance checking
CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')  # Default to Celo's public RPC
# Use the main G$ token contract for balance checking
GOODDOLLAR_CONTRACT_ADDRESS = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')

# Global blockchain variables
w3 = None
gooddollar_contract = None

# Initialize blockchain connection
def initialize_blockchain():
    """Initialize blockchain connection for wallet balance checking"""
    global w3, gooddollar_contract
    try:
        from web3 import Web3

        # Initialize Web3 connection
        w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))

        if not w3.is_connected():
            logger.error("❌ Failed to connect to Celo network")
            return False

        logger.info("✅ Connected to Celo network")

        # GoodDollar ERC20 ABI for balance checking
        erc20_abi = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "decimals",
                "outputs": [{"name": "", "type": "uint8"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "symbol",
                "outputs": [{"name": "", "type": "string"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "name",
                "outputs": [{"name": "", "type": "string"}],
                "type": "function"
            }
        ]

        # Create GoodDollar contract instance
        gooddollar_contract = w3.eth.contract(
            address=Web3.to_checksum_address(GOODDOLLAR_CONTRACT_ADDRESS),
            abi=erc20_abi
        )

        logger.info(f"✅ GoodDollar contract loaded: {GOODDOLLAR_CONTRACT_ADDRESS}")
        return True

    except Exception as e:
        logger.error(f"❌ Blockchain initialization error: {e}")
        return False

# Initialize the blockchain connection when the app starts
if not initialize_blockchain():
    logger.warning("Blockchain initialization failed. Wallet balance features might not work.")

# Register the routes blueprint FIRST (contains all API routes including /api/recent-daily-tasks)
# This must be before any catch-all routes
app.register_blueprint(routes)
logger.info("✅ Routes blueprint registered with API endpoints")


# Context processor: inject feature visibility into all templates (server-side, no flicker)
_feature_visibility_cache = {"data": None, "expires_at": 0}

@app.context_processor
def inject_feature_visibility():
    import time
    global _feature_visibility_cache
    now = time.time()
    if _feature_visibility_cache["data"] and now < _feature_visibility_cache["expires_at"]:
        return _feature_visibility_cache["data"]
    try:
        from supabase_client import get_supabase_client, safe_supabase_operation
        supabase = get_supabase_client()
        swap_visible = True
        wallet_visible = True
        savings_visible = True
        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .select('feature_name,is_maintenance')
                    .in_('feature_name', ['swap_feature', 'wallet_feature', 'savings_feature', 'store_topup', 'store_giftcard', 'store_utility'])
                    .execute(),
                operation_name="context processor feature visibility"
            )
            topup_visible = True
            giftcard_visible = True
            utility_visible = True
            if result and result.data:
                for row in result.data:
                    fn = row['feature_name']
                    val = not row.get('is_maintenance', False)
                    if fn == 'swap_feature':
                        swap_visible = val
                    elif fn == 'wallet_feature':
                        wallet_visible = val
                    elif fn == 'savings_feature':
                        savings_visible = val
                    elif fn == 'store_topup':
                        topup_visible = val
                    elif fn == 'store_giftcard':
                        giftcard_visible = val
                    elif fn == 'store_utility':
                        utility_visible = val
        flags = {"swap_visible": swap_visible, "wallet_visible": wallet_visible,
                 "savings_visible": savings_visible,
                 "topup_visible": topup_visible, "giftcard_visible": giftcard_visible,
                 "utility_visible": utility_visible}
        _feature_visibility_cache["data"] = flags
        _feature_visibility_cache["expires_at"] = now + 15
        return flags
    except Exception:
        return {"swap_visible": True, "wallet_visible": True, "savings_visible": True,
                "topup_visible": True, "giftcard_visible": True, "utility_visible": True}

# Initialize Telegram Task
from telegram_task import init_telegram_task
if not init_telegram_task(app):
    logger.warning("⚠️ Telegram Task initialization failed")
else:
    logger.info("✅ Telegram Task initialized successfully")

# Initialize Twitter Task
from twitter_task import init_twitter_task
if not init_twitter_task(app):
    logger.warning("⚠️ Twitter Task initialization failed")
else:
    logger.info("✅ Twitter Task initialized successfully")

# Initialize Discourse Task
from discourse_task import init_discourse_task
if not init_discourse_task(app):
    logger.warning("⚠️ Discourse Task initialization failed")
else:
    logger.info("✅ Discourse Task initialized successfully")


# Initialize News Feed first
from news_feed import init_news_feed, news_feed_service
init_news_feed(app)

# Initialize Minigames System
logger.info("🎮 Initializing Minigames system...")
from minigames import init_minigames
if init_minigames(app):
    logger.info("✅ Minigames system initialized")
else:
    logger.error("❌ Minigames initialization failed")


# Register Telegram bot blueprint
from telegram_bot import telegram_bot
app.register_blueprint(telegram_bot)
logger.info("✅ Telegram bot blueprint registered")

# Initialize G$ Savings
logger.info("💰 Initializing G$ Savings system...")
if init_savings(app):
    logger.info("✅ G$ Savings initialized")
else:
    logger.error("❌ G$ Savings initialization failed")

# Initialize Reloadly Store
logger.info("🛒 Initializing Reloadly Store...")
if init_reloadly(app):
    logger.info("✅ Reloadly Store initialized")
else:
    logger.error("❌ Reloadly Store initialization failed")

# Initialize Jumble Words System
logger.info("🔤 Initializing Jumble Words system...")
from jumble import init_jumble
if init_jumble(app):
    logger.info("✅ Jumble Words system initialized")
else:
    logger.error("❌ Jumble Words initialization failed")

# Initialize Price Prediction System
logger.info("📈 Initializing Price Prediction system...")
try:
    from price_prediction import init_price_prediction
    init_price_prediction(app)
    logger.info("✅ Price Prediction system initialized")
except Exception as e:
    logger.error(f"❌ Price Prediction initialization failed: {e}")

# Initialize Community Stories System
logger.info("🌟 Initializing Community Stories system...")
from community_stories import init_community_stories
if init_community_stories(app):
    logger.info("✅ Community Stories system initialized")
else:
    logger.error("❌ Community Stories initialization failed")

# Initialize Reward Configuration Service
logger.info("💰 Initializing Reward Configuration Service...")
try:
    from reward_config_service import reward_config_service
    logger.info("✅ Reward Configuration Service initialized")
except Exception as e:
    logger.error(f"❌ Reward Configuration Service initialization failed: {e}")

# Initialize Referral Program system (at module level for gunicorn compatibility)
logger.info("🎁 Initializing Referral Program system...")
try:
    from referral_program import referral_bp
    app.register_blueprint(referral_bp)
    logger.info("✅ Referral Program system ready")
except Exception as e:
    logger.warning(f"⚠️ Referral Program initialization failed: {e}")

# Initialize Learn & Earn System at module level (required for gunicorn)
logger.info("🎓 Initializing Learn & Earn system...")
if init_learn_and_earn(app):
    logger.info("✅ Learn & Earn system initialized")
else:
    logger.error("❌ Learn & Earn initialization failed")

# Spawn the in-process Learn & Earn stream worker. Self-gated: starts only
# when LEARN_EARN_PAYOUT_MODE is a streaming alias (or LEARN_EARN_STREAM_SCHEDULER_ENABLED=1).
# Each Gunicorn worker spawns its own thread; per-row OCC claims keep concurrent runs safe.
try:
    if init_learn_earn_stream_scheduler(app):
        logger.info("✅ Learn & Earn stream scheduler started")
    else:
        logger.info("ℹ️ Learn & Earn stream scheduler not started (disabled or instant mode)")
except Exception as e:
    logger.error(f"❌ Learn & Earn stream scheduler initialization failed: {e}")

# Initialize trustless P2P Trading (GoodMarketP2PEscrow contract)
logger.info("🤝 Initializing P2P Trading system...")
try:
    from p2p_trading import init_p2p_trading
    init_p2p_trading(app)
    logger.info("✅ P2P Trading system initialized")
except Exception as e:
    logger.error(f"❌ P2P Trading initialization failed: {e}")

# Initialize GoodMarket claim reconciler (opt-in via
# GOODMARKET_CLAIM_RECONCILER_ENABLED). The reconciler is the server-side
# safety net for goodmarket_claim_facts: it polls rows stuck at
# status='submitted' and flips them to confirmed/failed/unknown based on
# real on-chain receipts, so users who successfully claimed but whose
# wallet UI never fired the receipt callback still roll into the
# goodmarket_unique_claimers KPI.
logger.info("🧮 Initializing GoodMarket claim reconciler...")
try:
    from goodmarket_claim_reconciler import init_goodmarket_claim_reconciler
    if init_goodmarket_claim_reconciler(app):
        logger.info("✅ GoodMarket claim reconciler started")
    else:
        logger.info("ℹ️ GoodMarket claim reconciler not started (disabled)")
except Exception as e:
    logger.error(f"❌ GoodMarket claim reconciler initialization failed: {e}")

# Initialize GoodMarket attribution backfill (auto-runs once on next boot,
# gated by a sentinel row so multi-worker deploys don't double-run). Catches
# every wallet that verified on GoodDollar AND has GoodMarket-claim activity
# but is still missing verified_after_goodmarket=TRUE in user_data.
logger.info("🏷️ Initializing GoodMarket attribution backfill...")
try:
    from goodmarket_attribution_backfill import init_attribution_backfill
    if init_attribution_backfill(app):
        logger.info("✅ GoodMarket attribution backfill scheduled")
    else:
        logger.info("ℹ️ GoodMarket attribution backfill not scheduled (disabled or already ran)")
except Exception as e:
    logger.error(f"❌ GoodMarket attribution backfill initialization failed: {e}")


@app.route("/health")
def health_check():
    """Lightweight health check for autoscale — no DB or blockchain calls"""
    return jsonify({"status": "ok"}), 200

@app.route("/")
def index():
    """Health check endpoint for deployment"""
    return jsonify({
        "status": "healthy",
        "service": "GoodDollar Analytics Platform",
        "version": "1.0.0"
    }), 200

@app.route("/login")
def home():
    return redirect("/", code=302)

@app.route("/api")
def api_status():
    return jsonify({
        "status": "online",
        "message": "GoodDollar Analytics Platform API",
        "version": "1.0.0",
        "endpoints": [
            "/api/analytics",
            "/api/gooddollar-balance",
            "/api/forum/posts",
            "/api/p2p/history",
            "/api/learn-earn/history"
        ]
    })



@app.route("/verify-ubi", methods=["POST"])
def verify_ubi():
    data = request.get_json()
    wallet = data.get("wallet")
    if not wallet:
        return jsonify({"status": "error", "message": "⚠️ Wallet address required"}), 400

    # Validate wallet format first
    if not (len(wallet) == 42 and wallet.startswith("0x")):
        result = {"status": "error", "message": "❌ Invalid wallet address format"}
        analytics.track_verification_attempt(wallet, False)
        return jsonify(result)

    # Use actual blockchain verification
    result = has_recent_ubi_claim(wallet)

    if result["status"] == "success":
        # Store wallet in session if verified
        session["wallet_address"] = wallet
        session["wallet"] = wallet # Keep this for backward compatibility if needed
        session["verified"] = True
        session["ubi_verified"] = True # Add this for clarity
        session["login_method"] = "walletconnect"
        session.permanent = True
        analytics.track_verification_attempt(wallet, True)
        analytics.track_user_session(wallet)

        return jsonify({
            "status": "success",
            "message": result["message"],
            "wallet": wallet,
            "block_number": result.get("summary", {}).get("latest_activity", {}).get("block"),
            "claim_amount": result.get("summary", {}).get("latest_activity", {}).get("amount", "N/A"),
            "redirect_to": "/overview"  # Skip terms page, go directly to overview
        })
    else:
        analytics.track_verification_attempt(wallet, False)
        return jsonify({
            "status": "error",
            "message": result["message"],
            "reason": "no_recent_claim"
        }), 400

@app.route("/api/faucet/gas-proxy", methods=["POST"])
def faucet_gas():
    """Proxy the GoodDollar topWallet faucet call server-side to avoid CORS
    and to properly inspect the response body for errors."""
    import requests as req_lib
    data = request.get_json(silent=True) or {}
    wallet = data.get("wallet", "").strip()
    if not wallet or not (len(wallet) == 42 and wallet.startswith("0x")):
        return jsonify({"ok": -1, "error": "Invalid wallet address"}), 400
    try:
        resp = req_lib.post(
            "https://goodserver.gooddollar.org/verify/topWallet",
            json={"chainId": 42220, "account": wallet},
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        body = resp.json()
        ok_val = body.get("ok", -1)
        if ok_val == -1:
            error_msg = body.get("error", "Faucet declined request")
            logging.warning(f"⚠️ GoodDollar faucet declined for {wallet}: {error_msg}")
            return jsonify({"ok": -1, "error": error_msg})
        logging.info(f"✅ GoodDollar faucet topped up gas for {wallet}")
        return jsonify({"ok": 1})
    except Exception as e:
        logging.error(f"❌ Faucet proxy error for {wallet}: {e}")
        return jsonify({"ok": -1, "error": str(e)})


@app.route("/api/faucet/onchain-proxy", methods=["POST"])
def faucet_onchain():
    """On-chain fallback: use GAMES_KEY to call topWallet(user) on the GoodDollar
    Faucet contract directly, bypassing the goodserver API rate-limits."""
    from web3 import Web3
    data = request.get_json(silent=True) or {}
    wallet = data.get("wallet", "").strip()
    if not wallet or not (len(wallet) == 42 and wallet.startswith("0x")):
        return jsonify({"ok": -1, "error": "Invalid wallet address"}), 400

    games_key = os.getenv("GAMES_KEY")
    if not games_key:
        logging.error("❌ GAMES_KEY not configured — on-chain faucet unavailable")
        return jsonify({"ok": -1, "error": "On-chain faucet not configured"})

    try:
        w3 = Web3(Web3.HTTPProvider("https://forno.celo.org"))
        if not w3.is_connected():
            return jsonify({"ok": -1, "error": "RPC connection failed"})

        games_key_clean = games_key.strip()
        if not games_key_clean.startswith("0x"):
            games_key_clean = "0x" + games_key_clean
        payer = w3.eth.account.from_key(games_key_clean)

        FAUCET = Web3.to_checksum_address("0x4F93Fa058b03953C851eFaA2e4FC5C34afDFAb84")
        user_addr = Web3.to_checksum_address(wallet)
        # topWallet(address) selector: 0x3771dcf8
        call_data = "0x3771dcf8" + "000000000000000000000000" + user_addr[2:].lower()

        gas_price = w3.eth.gas_price
        gas_est   = w3.eth.estimate_gas({"from": payer.address, "to": FAUCET, "data": call_data})
        nonce     = w3.eth.get_transaction_count(payer.address)

        tx = {
            "from":     payer.address,
            "to":       FAUCET,
            "data":     call_data,
            "gas":      int(gas_est * 1.2),
            "gasPrice": gas_price,
            "nonce":    nonce,
            "chainId":  42220,
        }
        signed = w3.eth.account.sign_transaction(tx, games_key_clean)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = tx_hash.hex()
        logging.info(f"✅ On-chain topWallet sent for {wallet}: {tx_hex}")
        return jsonify({"ok": 1, "txHash": tx_hex})
    except Exception as e:
        err_str = str(e)
        # "low toTop" means wallet already has enough CELO — treat as success
        if "low toTop" in err_str or "lowtotop" in err_str.lower():
            logging.info(f"ℹ️ On-chain topWallet skipped for {wallet}: wallet already above threshold")
            return jsonify({"ok": 1, "skipped": True, "reason": "already_funded"})
        logging.error(f"❌ On-chain faucet error for {wallet}: {e}")
        return jsonify({"ok": -1, "error": err_str})


@app.route("/api/ubi/check-entitlement", methods=["POST"])
def ubi_check_entitlement():
    data = request.get_json()
    wallet = data.get("wallet", "").strip()
    if not wallet or not (len(wallet) == 42 and wallet.startswith("0x")):
        return jsonify({"success": False, "error": "Invalid wallet address"}), 400
    result = check_ubi_entitlement(wallet)
    return jsonify(result)



@app.route("/dashboard")
def dashboard():
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect("/")

    # Track page view and get dashboard data
    analytics.track_page_view(wallet, "dashboard")
    dashboard_data = analytics.get_dashboard_stats(wallet)

    wc_project_id = os.environ.get("WALLETCONNECT_PROJECT_ID", "")
    has_explicit_sidecar = bool(os.getenv("WC_SERVICE_URL"))
    is_serverless_runtime = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
    walletconnect_sidecar_enabled = has_explicit_sidecar or not is_serverless_runtime
    return render_template(
        "dashboard.html",
        wallet=wallet,
        data=dashboard_data,
        wc_project_id=wc_project_id,
        walletconnect_project_id=wc_project_id,
        walletconnect_sidecar_enabled=walletconnect_sidecar_enabled,
        login_method=session.get("login_method", "walletconnect")
    )

@app.route("/overview")
def overview():
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect("/")

    # Track page view and get analytics data
    analytics.track_page_view(wallet, "overview")
    overview_data = analytics.get_dashboard_stats(wallet)

    return render_template("overview.html", wallet=wallet, data=overview_data)

@app.route("/profile")
def profile():
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect("/")
    analytics.track_page_view(wallet, "profile")
    return render_template("profile.html", wallet=wallet)

@app.route("/api/analytics")
@cached_response(duration=30)  # Cache for 30 seconds
def api_analytics():
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify(analytics.get_user_analytics(wallet))

@app.route("/api/gd-price", methods=["GET"])
def get_gd_price():
    """Fetch live GoodDollar price from CoinGecko in multiple currencies"""
    import time as _time
    cache_key = "gd_price_coingecko"
    cached = _cache.get(cache_key)
    if cached and _time.time() - _cache_timestamps.get(cache_key, 0) < 300:
        return cached

    try:
        import requests as _req
        vs_currencies = (
            "usd,eur,gbp,jpy,aud,cad,chf,nzd,sgd,hkd,"
            "php,ngn,idr,inr,thb,vnd,myr,pkr,bdt,mmk,"
            "kes,ghs,tzs,ugx,zar,xof,mad,etb,egp,"
            "brl,mxn,cop,ars,clp,pen,"
            "try,aed,sar,ils,"
            "cny,krw"
        )
        url = f"https://api.coingecko.com/api/v3/simple/price?ids=gooddollar&vs_currencies={vs_currencies}"
        resp = _req.get(url, timeout=10, headers={"Accept": "application/json"})
        data = resp.json()
        prices = data.get("gooddollar", {})

        result = jsonify({"success": True, "prices": prices})
        _cache[cache_key] = result
        import time as _time2
        _cache_timestamps[cache_key] = _time2.time()
        return result
    except Exception as e:
        logger.error(f"CoinGecko price fetch error: {e}")
        return jsonify({"success": False, "error": str(e), "prices": {}}), 500

@app.route("/api/gooddollar-balance", methods=["GET"])
def get_gooddollar_balance_api():
    """Get GoodDollar balance for current user"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        # Use blockchain.py get_gooddollar_balance function directly
        from blockchain import get_gooddollar_balance as get_balance
        result = get_balance(wallet)

        if not result:
            return jsonify({
                "success": False,
                "error": "Failed to fetch balance",
                "balance_formatted": "Error loading"
            }), 500

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Balance API error: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "balance_formatted": "Error loading"
        }), 500

@app.route("/api/balance/<wallet_address>", methods=["GET"])
def get_balance_by_wallet(wallet_address):
    """Get GoodDollar balance for specific wallet (used by overview page)"""
    session_wallet = session.get("wallet")
    if not session_wallet or not session.get("verified"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    # Only allow getting balance for the authenticated user's wallet
    if wallet_address != session_wallet:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        # Use blockchain.py get_gooddollar_balance function directly
        from blockchain import get_gooddollar_balance as get_balance
        result = get_balance(wallet_address)

        if not result:
            return jsonify({
                'success': False,
                'error': 'Failed to fetch balance',
                'balance_formatted': 'Error loading'
            }), 500

        return jsonify(result)
    except Exception as e:
        logger.error(f"Balance API error: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'balance_formatted': 'Error loading'
        }), 500

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route('/api/twitter-task/transaction-history')
def get_twitter_task_transaction_history():
    """Get user's Twitter task transaction history for dashboard integration"""
    try:
        wallet = session.get('wallet') or session.get('wallet_address')
        verified = session.get('verified') or session.get('ubi_verified')

        if not wallet or not verified:
            return jsonify({
                "success": True,
                "transactions": [],
                "total": 0
            }), 200

        limit = int(request.args.get('limit', 50))

        logger.info(f"📋 Getting Twitter task history for {wallet[:8]}... (limit: {limit})")

        from twitter_task.twitter_task import twitter_task_service

        # Get transaction history
        history = twitter_task_service.get_transaction_history(wallet, limit)

        return jsonify(history)

    except Exception as e:
        logger.error(f"❌ Error getting Twitter task history: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "transactions": [],
            "total": 0
        }), 500

@app.route('/learn-earn/quiz-history')
def get_learn_earn_quiz_history():
    """Get user's Learn & Earn quiz history for dashboard integration - ALL HISTORICAL DATA"""
    try:
        wallet = session.get('wallet') or session.get('wallet_address')
        verified = session.get('verified') or session.get('ubi_verified')

        if not wallet or not verified:
            logger.warning(f"⚠️ Unauthorized Learn & Earn history request - no wallet/verification")
            return jsonify({
                "success": True,
                "quiz_history": [],
                "total": 0,
                "message": "Not authenticated"
            }), 200

        limit = int(request.args.get('limit', 500))  # Default 500 records for comprehensive history

        logger.info(f"📋 Getting ALL Learn & Earn history for {wallet[:8]}... (limit: {limit})")

        from learn_and_earn.learn_and_earn import quiz_manager

        # Get quiz history - NO DATE FILTERING, ALL HISTORICAL LOGS
        history = quiz_manager.get_quiz_history(wallet, limit)

        # Ensure history is a list
        if not isinstance(history, list):
            logger.error(f"❌ Quiz history is not a list: {type(history)}")
            history = []

        # Format history for dashboard display
        formatted_history = []
        for record in history:
            try:
                formatted_record = {
                    'quiz_id': record.get('quiz_id'),
                    'score': record.get('score'),
                    'total_questions': record.get('total_questions'),
                    'amount_g$': record.get('amount_g$'),
                    'timestamp': record.get('timestamp'),
                    'transaction_hash': record.get('transaction_hash'),
                    'status': 'completed' if record.get('status') else 'failed',
                    'reward_status': record.get('reward_status', 'completed'),
                    'username': record.get('username', 'User')
                }
                formatted_history.append(formatted_record)
            except Exception as format_error:
                logger.error(f"❌ Error formatting quiz record: {format_error}")
                continue

        logger.info(f"✅ Found {len(formatted_history)} Learn & Earn records for {wallet[:8]}... (ALL TIME)")

        # Log date range if there are records
        if formatted_history:
            dates = [r['timestamp'] for r in formatted_history if r.get('timestamp')]
            if dates:
                oldest_date = min(dates)
                newest_date = max(dates)
                logger.info(f"📅 Complete history range: {oldest_date} (oldest) to {newest_date} (newest)")

        response_data = {
            "success": True,
            "quiz_history": formatted_history,
            "total": len(formatted_history)
        }

        if formatted_history:
            response_data["oldest_record"] = formatted_history[-1]['timestamp']
            response_data["newest_record"] = formatted_history[0]['timestamp']

        logger.info(f"✅ Returning Learn & Earn history response with {len(formatted_history)} records")
        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"❌ Error getting Learn & Earn history: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({
            "success": True,
            "quiz_history": [],
            "total": 0,
            "error": str(e)
        }), 200

@app.route('/api/unified-transaction-history')
def get_unified_transaction_history():
    """Get unified transaction history from all earning sources"""
    try:
        wallet = session.get('wallet') or session.get('wallet_address')
        verified = session.get('verified') or session.get('ubi_verified')

        if not wallet or not verified:
            return jsonify({"success": True, "transactions": [], "total": 0}), 200

        limit = int(request.args.get('limit', 100))
        all_transactions = []

        # 1. Daily Social Task (Twitter + Telegram)
        try:
            from twitter_task.twitter_task import twitter_task_service
            from telegram_task.telegram_task import telegram_task_service
            twitter_hist = twitter_task_service.get_transaction_history(wallet, 50)
            telegram_hist = telegram_task_service.get_transaction_history(wallet, 50)
            if twitter_hist.get('success') and twitter_hist.get('transactions'):
                for tx in twitter_hist['transactions']:
                    all_transactions.append({
                        'type': 'daily_task',
                        'source': 'Twitter',
                        'icon': '🐦',
                        'label': 'Daily Social Task (Twitter)',
                        'amount': float(tx.get('reward_amount', 0)),
                        'timestamp': tx.get('created_at') or tx.get('timestamp', ''),
                        'status': tx.get('status', 'unknown'),
                        'tx_hash': tx.get('transaction_hash') or tx.get('tx_hash'),
                        'rejection_reason': tx.get('rejection_reason')
                    })
            if telegram_hist.get('success') and telegram_hist.get('transactions'):
                for tx in telegram_hist['transactions']:
                    all_transactions.append({
                        'type': 'daily_task',
                        'source': 'Telegram',
                        'icon': '✈️',
                        'label': 'Daily Social Task (Telegram)',
                        'amount': float(tx.get('reward_amount', 0)),
                        'timestamp': tx.get('created_at') or tx.get('timestamp', ''),
                        'status': tx.get('status', 'unknown'),
                        'tx_hash': tx.get('transaction_hash') or tx.get('tx_hash'),
                        'rejection_reason': tx.get('rejection_reason')
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch daily task history: {e}")

        # 2. Learn & Earn
        try:
            from learn_and_earn.learn_and_earn import quiz_manager
            quiz_hist = quiz_manager.get_quiz_history(wallet, 100)
            if isinstance(quiz_hist, list):
                for rec in quiz_hist:
                    all_transactions.append({
                        'type': 'learn_earn',
                        'source': 'Learn & Earn',
                        'icon': '📚',
                        'label': f"Learn & Earn - {rec.get('quiz_id', 'Quiz')}",
                        'amount': float(rec.get('amount_g$', 0)),
                        'timestamp': rec.get('timestamp', ''),
                        'status': 'completed' if rec.get('status') else 'failed',
                        'tx_hash': rec.get('transaction_hash'),
                        'extra': f"Score: {rec.get('score', 0)}/{rec.get('total_questions', 0)}"
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch Learn & Earn history: {e}")

        # 3. Minigames (direct game rewards)
        try:
            from supabase_client import supabase as sb
            if sb:
                mg_result = sb.table('minigame_rewards_log')\
                    .select('*')\
                    .eq('wallet_address', wallet)\
                    .order('created_at', desc=True)\
                    .limit(50)\
                    .execute()
                for tx in (mg_result.data or []):
                    game = tx.get('game_type', 'minigame').replace('_', ' ').title()
                    all_transactions.append({
                        'type': 'minigame',
                        'source': 'Play & Earn',
                        'icon': '🎮',
                        'label': f"Play & Earn - {game}",
                        'amount': float(tx.get('reward_amount', 0)),
                        'timestamp': tx.get('created_at', ''),
                        'status': 'completed',
                        'tx_hash': tx.get('transaction_hash')
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch minigame history: {e}")

        # 3b. Minigames Play & Earn Withdrawals (Jumble Words, Crash Game, etc.)
        try:
            from supabase_client import supabase as sb
            if sb:
                wd_result = sb.table('minigame_withdrawals_log')\
                    .select('*')\
                    .eq('wallet_address', wallet)\
                    .order('withdrawal_date', desc=True)\
                    .limit(50)\
                    .execute()
                for tx in (wd_result.data or []):
                    all_transactions.append({
                        'type': 'minigame_withdrawal',
                        'source': 'Play & Earn',
                        'icon': '💸',
                        'label': 'Play & Earn Withdrawal',
                        'amount': float(tx.get('amount', 0)),
                        'timestamp': tx.get('withdrawal_date', '') or tx.get('created_at', ''),
                        'status': 'completed',
                        'tx_hash': tx.get('tx_hash')
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch minigame withdrawal history: {e}")

        # 4. Community Stories
        try:
            from community_stories.community_stories_service import community_stories_service
            cs_result = community_stories_service.get_user_submissions(wallet)
            for sub in (cs_result.get('submissions') or []):
                status = sub.get('status', 'pending')
                all_transactions.append({
                    'type': 'community_story',
                    'source': 'Community Stories',
                    'icon': '📖',
                    'label': 'Community Story Submission',
                    'amount': float(sub.get('reward_amount', 0)) if status == 'approved' else 0,
                    'timestamp': sub.get('submitted_at', ''),
                    'status': status,
                    'tx_hash': sub.get('tx_hash')
                })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch community stories history: {e}")

        # 5. Discourse Task
        try:
            from supabase_client import supabase as sb
            if sb:
                disc_result = sb.table('discourse_task_log')\
                    .select('*')\
                    .eq('wallet_address', wallet.lower())\
                    .order('submitted_at', desc=True)\
                    .limit(50)\
                    .execute()
                for row in (disc_result.data or []):
                    status = row.get('status', 'pending')
                    all_transactions.append({
                        'type': 'discourse_task',
                        'source': 'Discourse Task',
                        'icon': '💬',
                        'label': f"Discourse Task (@{row.get('discourse_username', 'unknown')})",
                        'amount': float(row.get('reward_amount', 0)) if status == 'approved' else 0,
                        'timestamp': row.get('submitted_at', ''),
                        'status': status,
                        'tx_hash': row.get('tx_hash')
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch discourse task history: {e}")

        # 6. Reloadly Orders (mobile top-up, gift cards, utility bills)
        try:
            from reloadly.service import get_user_orders
            reloadly_orders = get_user_orders(wallet, 50)
            type_labels = {
                'topup': ('📱', 'Mobile Top-Up (Reloadly)'),
                'giftcard': ('🎁', 'Gift Card (Reloadly)'),
                'utility': ('💡', 'Utility Bill (Reloadly)')
            }
            for order in reloadly_orders:
                order_type = order.get('order_type', 'order')
                icon, label = type_labels.get(order_type, ('🛒', f'Reloadly Order ({order_type})'))
                status = order.get('status', 'unknown')
                gd_amount = float(order.get('gd_amount', 0) or 0)
                usd_amount = order.get('usd_amount', 0)
                all_transactions.append({
                    'type': 'reloadly',
                    'source': 'GoodShop',
                    'icon': icon,
                    'label': label,
                    'amount': gd_amount,
                    'timestamp': order.get('created_at', ''),
                    'status': status,
                    'tx_hash': order.get('tx_hash'),
                    'extra': f"${usd_amount} USD"
                })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch Reloadly order history: {e}")

        # 7. Daily Voucher Claims (from voucher_claims_log — has tx_hash + amount)
        try:
            from supabase_client import supabase as sb
            if sb:
                vcl_result = sb.table('voucher_claims_log')\
                    .select('*')\
                    .eq('wallet_address', wallet)\
                    .order('claimed_at', desc=True)\
                    .limit(50)\
                    .execute()
                # Track dates already covered by voucher_claims_log
                vcl_dates = set()
                for row in (vcl_result.data or []):
                    vcl_dates.add(row.get('voucher_date', ''))
                    all_transactions.append({
                        'type': 'daily_voucher',
                        'source': 'GoodMarket Voucher',
                        'icon': '🎟️',
                        'label': f"GoodMarket Voucher Claimed ({row.get('voucher_date', '')})",
                        'amount': float(row.get('gd_amount') or 0),
                        'timestamp': row.get('claimed_at', ''),
                        'status': 'completed',
                        'tx_hash': row.get('tx_hash')
                    })
                # Also include daily_voucher claims not yet in voucher_claims_log (older or no tx saved)
                dv_result = sb.table('daily_voucher')\
                    .select('*')\
                    .eq('claimed_by', wallet)\
                    .eq('is_claimed', True)\
                    .order('claimed_at', desc=True)\
                    .limit(50)\
                    .execute()
                for row in (dv_result.data or []):
                    if row.get('voucher_date') not in vcl_dates:
                        all_transactions.append({
                            'type': 'daily_voucher',
                            'source': 'GoodMarket Voucher',
                            'icon': '🎟️',
                            'label': f"GoodMarket Voucher Claimed ({row.get('voucher_date', '')})",
                            'amount': 0,
                            'timestamp': row.get('claimed_at', '') or row.get('voucher_date', ''),
                            'status': 'completed',
                            'tx_hash': None
                        })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch daily voucher history: {e}")

        # 8. NFT Mints (Achievement NFTs)
        try:
            from supabase_client import supabase as sb
            if sb:
                nft_mint_result = sb.table('achievement_nft_mints')\
                    .select('*')\
                    .eq('owner_wallet', wallet)\
                    .order('minted_at', desc=True)\
                    .limit(50)\
                    .execute()
                for row in (nft_mint_result.data or []):
                    all_transactions.append({
                        'type': 'nft_mint',
                        'source': 'NFT Marketplace',
                        'icon': '🏅',
                        'label': f"NFT Minted - {row.get('quiz_name', row.get('quiz_id', 'Achievement'))}",
                        'amount': 0,
                        'timestamp': row.get('minted_at', '') or row.get('created_at', ''),
                        'status': 'completed',
                        'tx_hash': row.get('tx_hash'),
                        'extra': f"Token #{row.get('token_id', '')} | Score: {row.get('score', 0)}/{row.get('total', 0)}"
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch NFT mint history: {e}")

        # 9. NFT Burns
        try:
            from supabase_client import supabase as sb
            if sb:
                nft_burn_result = sb.table('nft_burn_history')\
                    .select('*')\
                    .eq('owner_wallet', wallet)\
                    .order('burned_at', desc=True)\
                    .limit(50)\
                    .execute()
                for row in (nft_burn_result.data or []):
                    all_transactions.append({
                        'type': 'nft_burn',
                        'source': 'NFT Marketplace',
                        'icon': '🔥',
                        'label': f"NFT Burned - {row.get('quiz_name', 'Achievement')}",
                        'amount': float(row.get('burn_amount_g', 0) or 0),
                        'timestamp': row.get('burned_at', '') or row.get('created_at', ''),
                        'status': 'completed',
                        'tx_hash': row.get('reward_tx_hash') or row.get('burn_tx_hash'),
                        'extra': f"Token #{row.get('token_id', '')}"
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch NFT burn history: {e}")

        # 10. NFT Sales (as seller)
        try:
            from supabase_client import supabase as sb
            if sb:
                nft_sale_result = sb.table('nft_sale_history')\
                    .select('*')\
                    .eq('seller_wallet', wallet)\
                    .order('sold_at', desc=True)\
                    .limit(50)\
                    .execute()
                for row in (nft_sale_result.data or []):
                    all_transactions.append({
                        'type': 'nft_sale',
                        'source': 'NFT Marketplace',
                        'icon': '💎',
                        'label': f"NFT Sold - {row.get('quiz_name', 'Achievement')}",
                        'amount': float(row.get('price_g', 0) or 0),
                        'timestamp': row.get('sold_at', '') or row.get('created_at', ''),
                        'status': 'completed',
                        'tx_hash': row.get('g_tx_hash') or row.get('nft_tx_hash'),
                        'extra': f"Token #{row.get('token_id', '')}"
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch NFT sale history: {e}")

        # 11. NFT Purchases (as buyer)
        try:
            from supabase_client import supabase as sb
            if sb:
                nft_buy_result = sb.table('nft_sale_history')\
                    .select('*')\
                    .eq('buyer_wallet', wallet)\
                    .order('sold_at', desc=True)\
                    .limit(50)\
                    .execute()
                for row in (nft_buy_result.data or []):
                    all_transactions.append({
                        'type': 'nft_purchase',
                        'source': 'NFT Marketplace',
                        'icon': '🛒',
                        'label': f"NFT Purchased - {row.get('quiz_name', 'Achievement')}",
                        'amount': float(row.get('price_g', 0) or 0),
                        'timestamp': row.get('sold_at', '') or row.get('created_at', ''),
                        'status': 'completed',
                        'tx_hash': row.get('g_tx_hash') or row.get('nft_tx_hash'),
                        'extra': f"Token #{row.get('token_id', '')}"
                    })
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch NFT purchase history: {e}")

        # Sort all by timestamp (newest first)
        def parse_ts(tx):
            ts = tx.get('timestamp', '') or ''
            return ts

        all_transactions.sort(key=parse_ts, reverse=True)
        all_transactions = all_transactions[:limit]

        SPENDING_TYPES = {'reloadly', 'nft_purchase'}
        total_earned = sum(
            t['amount'] for t in all_transactions
            if t.get('status') in ('approved', 'completed') and t.get('type') not in SPENDING_TYPES
        )

        return jsonify({
            "success": True,
            "transactions": all_transactions,
            "total": len(all_transactions),
            "total_earned": round(total_earned, 6)
        })

    except Exception as e:
        logger.error(f"❌ Error getting unified transaction history: {e}")
        return jsonify({"success": False, "error": str(e), "transactions": [], "total": 0}), 500


@app.route('/api/debug/session', methods=['GET'])
def debug_session():
    """Debug endpoint to check session status"""
    try:
        return jsonify({
            'success': True,
            'session_data': {
                'wallet': session.get('wallet'),
                'wallet_address': session.get('wallet_address'),
                'verified': session.get('verified'),
                'ubi_verified': session.get('ubi_verified'),
                'username': session.get('username'),
                'terms_accepted': session.get('terms_accepted'),
                'permanent': session.permanent
            }
        })
    except Exception as e:
        logger.error(f"❌ Session debug error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _process_referral_disbursement(referral_blockchain_service, referral_service,
                                   referrer_wallet, referee_wallet, referral_code):
    """Thin wrapper that delegates to ReferralService.process_referral_disbursement.

    The blockchain service argument is kept for backward-compatibility with
    existing call sites; the service method imports it on its own so the
    parameter is intentionally unused here.
    """
    del referral_blockchain_service  # imported inside the service method
    return referral_service.process_referral_disbursement(
        referrer_wallet=referrer_wallet,
        referee_wallet=referee_wallet,
        referral_code=referral_code,
    )


@app.route('/verify-identity', methods=['POST'])
def verify_identity():
    """Handle identity verification with UBI validation"""
    try:
        data = request.get_json()
        wallet_address = data.get('wallet_address', '').strip()
        referral_code = data.get('referral_code', '').strip()

        if not wallet_address:
            return jsonify({'error': 'Wallet address is required'}), 400

        # Validate wallet address format
        if not Web3.is_address(wallet_address):
            return jsonify({'error': 'Invalid wallet address format'}), 400

        # Normalize to EIP-55 checksum format so MetaMask, WalletConnect,
        # and manual paste all resolve to the SAME record in Supabase
        try:
            wallet_address = Web3.to_checksum_address(wallet_address)
        except Exception:
            return jsonify({'error': 'Could not normalize wallet address'}), 400

        logger.info(f"🔐 Identity verification attempt for {wallet_address}")

        # Check if user is already verified in the session
        if session.get('verified') and session.get('wallet') == wallet_address:
            logger.info(f"✅ User {wallet_address} already verified in session")
            return jsonify({
                'success': True,
                'message': 'Already verified!',
                'wallet': wallet_address,
                'already_verified': True
            })

        # Verify signature to prove wallet ownership
        signature = data.get('signature', '').strip()
        login_message = data.get('message', '').strip()
        if not signature or not login_message:
            logger.info(f"⚠️ Missing signature payload for {wallet_address} — rejecting login")
            return jsonify({'success': False, 'error': 'Signature and login message are required'}), 400

        try:
            from eth_account.messages import encode_defunct
            from eth_account import Account as EthAccount
            msg_obj = encode_defunct(text=login_message)
            recovered = EthAccount.recover_message(msg_obj, signature=signature)
            recovered_checksum = Web3.to_checksum_address(recovered)
            if recovered_checksum != wallet_address:
                logger.warning(f"❌ Signature mismatch for {wallet_address}: recovered {recovered_checksum}")
                return jsonify({'success': False, 'error': 'Signature verification failed — wrong wallet?'}), 400
            logger.info(f"✅ Signature verified for {wallet_address}")
        except Exception as sig_err:
            logger.warning(f"⚠️ Signature check error for {wallet_address}: {sig_err}")
            return jsonify({'success': False, 'error': 'Invalid signature'}), 400

        # ── NEW USER CHECK (before any DB write) ────────────────────────────────
        # We must determine whether this wallet is brand-new to the platform
        # BEFORE analytics.track_verification_attempt() creates the DB record,
        # because referrals are only valid for first-time users.
        is_new_user = False
        try:
            from supabase_client import get_supabase_client, safe_supabase_operation
            _sb = get_supabase_client()
            if _sb:
                _existing = safe_supabase_operation(
                    lambda: _sb.table('user_data')
                        .select('wallet_address')
                        .ilike('wallet_address', wallet_address)
                        .limit(1)
                        .execute(),
                    operation_name="check new user for referral"
                )
                is_new_user = not (_existing and _existing.data)
                logger.info(f"{'🆕 New' if is_new_user else '👤 Existing'} user detected: {wallet_address[:10]}...")
        except Exception as _nu_err:
            logger.warning(f"⚠️ Could not determine new-user status: {_nu_err}")

        # ── EXTERNAL FACE VERIFICATION CHECK ────────────────────────────────────
        # Check GoodDollar on-chain identity status BEFORE creating the DB record.
        # This is used both for tracking attribution AND for referral validation.
        fv_status = {}
        try:
            fv_status = is_identity_verified(wallet_address)
        except Exception as fv_err:
            logger.warning(f"⚠️ Could not check face verification: {fv_err}")

        is_face_verified = fv_status.get('verified', False)

        # Track verification attempt (GoodMarket access only, not face verification)
        analytics.track_verification_attempt(wallet_address, True)

        # Store in session with permanent flag
        session.permanent = True
        session['wallet'] = wallet_address
        session['wallet_address'] = wallet_address
        session['verified'] = True
        session['ubi_verified'] = True
        session['login_method'] = 'walletconnect'
        session['verification_time'] = datetime.now().isoformat()

        # Record unverified visit or log face-verified status for attribution
        try:
            from supabase_client import supabase_logger as sb_logger
            if not is_face_verified:
                if sb_logger:
                    sb_logger.record_unverified_visit(wallet_address)
                    logger.info(f"📝 Unverified visitor recorded: {wallet_address[:10]}...")
                session['ubi_verified'] = False
            else:
                logger.info(f"✅ User is already face-verified on GoodDollar: {wallet_address[:10]}...")
                # Backfill GoodMarket attribution for already-verified users who
                # log in (e.g. they verified on a previous device, or fell through
                # the /fv-callback net). The helper is idempotent and on-chain
                # gated, so it never produces false positives. Runs on a daemon
                # thread so we don't add latency to the verify-identity response.
                try:
                    from goodmarket_attribution_backfill import mark_verified_via_goodmarket
                    mark_verified_via_goodmarket(
                        wallet_address,
                        source="verify_identity",
                        require_on_chain_check=False,  # we already have fv_status above
                        background=True,
                    )
                except Exception as attr_bf_err:
                    logger.warning(f"⚠️ Attribution backfill skipped in verify-identity: {attr_bf_err}")
        except Exception as attr_err:
            logger.warning(f"⚠️ Could not record visit attribution: {attr_err}")

        # ── REFERRAL PROCESSING ──────────────────────────────────────────────────
        # Rules:
        #  1. Referral only valid for NEW users (not yet in user_data)
        #  2. Referral only valid if referee is NOT yet externally face-verified
        #  3. Reward only disbursed after referee completes face verification
        referral_warning = None
        if referral_code:
            try:
                from referral_program.referral_service import referral_service as ref_svc
                from referral_program.blockchain import referral_blockchain_service as ref_bc_svc

                if not is_new_user:
                    # Existing user — referral cannot apply
                    logger.info(f"ℹ️ Referral ignored: {wallet_address[:8]}... is an existing user")
                    referral_warning = "Referral rewards are only available for users joining the platform for the first time."
                elif is_face_verified:
                    # Already verified externally — referral cannot apply
                    logger.info(f"ℹ️ Referral ignored: {wallet_address[:8]}... is already face-verified on GoodDollar")
                    referral_warning = "Referral rewards are not applicable: your wallet is already verified on GoodDollar."
                else:
                    # Valid candidate — validate code and record referral
                    validation = ref_svc.validate_referral_code(referral_code.upper())
                    if validation.get('valid'):
                        referrer_wallet = validation.get('referrer_wallet')
                        record_result = ref_svc.record_referral(referral_code.upper(), wallet_address)
                        if record_result.get('success'):
                            logger.info(f"📋 Referral recorded: {referral_code} for {wallet_address[:8]}...")
                            # Save referrer and referral code in user_data for this new user
                            try:
                                from supabase_client import supabase_logger as _sb_log
                                if _sb_log:
                                    _sb_log.save_referrer_wallet(
                                        wallet_address, referrer_wallet, referral_code.upper()
                                    )
                            except Exception as _sr_err:
                                logger.warning(f"⚠️ Could not save referral data: {_sr_err}")
                            # Do NOT disburse yet — wait for face verification completion
                            logger.info(f"⏳ Referral pending face verification: {referral_code}")
                        elif record_result.get('already_exists'):
                            logger.info(f"ℹ️ Referral already recorded for {wallet_address[:8]}...")
                        else:
                            logger.warning(f"⚠️ Could not record referral: {record_result.get('error')}")
                    else:
                        code_len = len(referral_code)
                        if code_len < 8:
                            referral_warning = f"Referral code \"{referral_code.upper()}\" looks incomplete ({code_len}/8 characters). Please check the full code and try again."
                        else:
                            referral_warning = f"Referral code \"{referral_code.upper()}\" was not recognized. Please check the code and try again."
                        logger.warning(f"⚠️ Invalid referral code: {referral_code} — {validation.get('error')}")
            except Exception as ref_err:
                logger.warning(f"⚠️ Referral processing error in verify-identity: {ref_err}")
        elif is_face_verified:
            # No referral code submitted but user is face-verified — check for a
            # previously recorded pending referral and disburse reward now.
            # Use atomic claim to prevent double-disbursement with fv-callback.
            try:
                from referral_program.referral_service import referral_service as ref_svc
                from referral_program.blockchain import referral_blockchain_service as ref_bc_svc
                claimed = ref_svc.claim_pending_referral_for_disbursement(wallet_address)
                if claimed.get('claimed'):
                    referral_row = claimed.get('referral', {})
                    referrer_wallet = referral_row.get('referrer_wallet')
                    ref_code = referral_row.get('referral_code')
                    if referrer_wallet and ref_code:
                        logger.info(f"🔄 Pending referral claimed for {wallet_address[:8]}... — disbursing now")
                        _process_referral_disbursement(ref_bc_svc, ref_svc, referrer_wallet, wallet_address, ref_code)
                else:
                    logger.info(f"ℹ️ No pending referral to claim for {wallet_address[:8]}... in verify-identity.")
            except Exception as ref_err:
                logger.warning(f"⚠️ Pending referral check error in verify-identity: {ref_err}")

        logger.info(f"✅ Identity verification successful for {wallet_address}")

        # Pre-warm the dashboard stats cache in the background so /overview loads fast
        import threading as _threading
        from analytics_service import analytics as _analytics
        _threading.Thread(
            target=_analytics.get_dashboard_stats,
            args=(wallet_address,),
            daemon=True
        ).start()

        response_data = {
            'success': True,
            'message': 'Identity verification successful!',
            'wallet': wallet_address,
            'ubi_verified': True,
            'redirect_to': '/overview'
        }
        if referral_warning:
            response_data['referral_warning'] = referral_warning
        return jsonify(response_data)

    except Exception as e:
        logger.error(f"❌ Identity verification error: {e}")
        return jsonify({'error': 'Verification failed'}), 500


@app.route('/manual-wallet-login', methods=['POST'])
def manual_wallet_login():
    """
    Manual paste-an-address login is no longer supported.

    The endpoint used to start a session from any pasted EVM address with
    no signature, which let anyone log in as anyone. Now that
    WalletConnect QR-login is the supported sign-in path for users
    without an injected wallet, the route is hard-disabled to prevent
    direct API abuse — even though the UI affordance has been removed.
    """
    return jsonify({
        'success': False,
        'error': 'Manual wallet login is no longer supported. Please sign in with WalletConnect or an injected wallet.',
    }), 410


@app.route('/fv-callback')
def fv_callback():
    """Handle redirect back from GoodDollar face verification."""
    raw_verified = request.args.get('verified', '')
    if raw_verified:
        try:
            decoded = _b64.b64decode(raw_verified + '==').decode('utf-8').strip().lower()
            is_verified_param = decoded  # 'true' o 'false'
        except Exception:
            is_verified_param = raw_verified.lower()
    else:
        is_verified_param = request.args.get('isVerified', 'false').lower()
    wallet_address = request.args.get('wallet', '').strip()
    reason = request.args.get('reason', '')
    via_goodmarket = request.args.get('src', '') == 'goodmarket'

    if is_verified_param == 'true' and wallet_address:
        try:
            wallet_address = Web3.to_checksum_address(wallet_address)
        except Exception:
            pass

        # Trust GoodDollar's isVerified=true — set session immediately so the
        # user is not kicked back to the login page.  On-chain propagation can
        # lag behind by several seconds, so we do NOT block on the contract
        # check here.  The claim button re-checks on-chain before sending any
        # transaction, so it's safe to be optimistic here.
        session.permanent = True
        session['wallet'] = wallet_address
        session['wallet_address'] = wallet_address
        session['verified'] = True
        session['verification_time'] = datetime.now().isoformat()

        # GoodDollar already confirmed face verification via the callback URL —
        # record the attribution immediately, regardless of on-chain propagation lag.
        # This ensures ALL users who face-verify on GoodMarket are counted as
        # "verified via GoodMarket" in the DB (verified_after_goodmarket = True).
        logger.info(f"🔖 FV callback src=goodmarket: {via_goodmarket} for {wallet_address[:10]}...")
        analytics.track_verification_attempt(wallet_address, True, face_verified=True)
        analytics.track_user_session(wallet_address)

        # Disburse any pending referral reward now that this user is face-verified.
        # Use atomic claim to prevent double-disbursement if verify-identity fires concurrently.
        try:
            from referral_program.referral_service import referral_service as ref_svc
            from referral_program.blockchain import referral_blockchain_service as ref_bc_svc
            claimed = ref_svc.claim_pending_referral_for_disbursement(wallet_address)
            if claimed.get('claimed'):
                referral_row = claimed.get('referral', {})
                referrer_wallet = referral_row.get('referrer_wallet')
                ref_code = referral_row.get('referral_code')
                if referrer_wallet and ref_code:
                    logger.info(f"🎁 FV callback: disbursing pending referral {ref_code} for {wallet_address[:8]}...")
                    _process_referral_disbursement(ref_bc_svc, ref_svc, referrer_wallet, wallet_address, ref_code)
            else:
                logger.info(f"ℹ️ FV callback: no pending referral to claim for {wallet_address[:8]}... (already processed or none).")
        except Exception as ref_err:
            logger.warning(f"⚠️ Referral disbursement error in fv-callback: {ref_err}")

        # Do a quick on-chain check; if it already propagated, mark fully
        # verified; otherwise flag as pending (claim button will re-check).
        identity_result = is_identity_verified(wallet_address)
        if identity_result.get('verified'):
            session['ubi_verified'] = True
            logger.info(f"✅ FV callback: {wallet_address} verified and logged in")
            return redirect('/overview')
        else:
            session['ubi_verified'] = False
            logger.warning(f"⚠️ FV callback: {wallet_address} — GoodDollar says verified but contract not yet updated; redirecting to overview with pending notice")
            return redirect('/overview?fv_pending=1')

    # GoodDollar reported failure — preserve any existing login and redirect
    # back to overview if already logged in, otherwise go to homepage.
    logger.warning(f"⚠️ FV callback: not verified — reason: {reason}")
    existing_wallet = session.get('wallet')
    if existing_wallet:
        return redirect('/overview?fv_failed=1&reason=' + reason)
    return redirect('/?fv_failed=1&reason=' + reason)


@app.route('/api/debug/database-status')
def debug_database_status():
    """Debug endpoint to check database connection and data fetching"""
    try:
        from supabase_client import get_supabase_client, supabase_enabled

        status = {
            "supabase_enabled": supabase_enabled,
            "environment_vars": {
                "SUPABASE_URL_exists": bool(os.getenv("SUPABASE_URL")),
                "SUPABASE_ANON_KEY_exists": bool(os.getenv("SUPABASE_ANON_KEY"))
            },
            "connection_test": None,
            "analytics_test": None
        }

        # Test connection
        supabase = get_supabase_client()
        if supabase:
            try:
                # Try a simple query
                test_query = supabase.table("user_data").select("id").limit(1).execute()
                status["connection_test"] = {
                    "success": True,
                    "message": "Database connection successful"
                }
            except Exception as conn_error:
                status["connection_test"] = {
                    "success": False,
                    "error": str(conn_error)
                }
        else:
            status["connection_test"] = {
                "success": False,
                "error": "Supabase client not initialized"
            }

        # Test analytics data fetch
        try:
            disbursement_stats = analytics._get_total_disbursements_stats()
            status["analytics_test"] = {
                "success": True,
                "has_breakdown_formatted": "breakdown_formatted" in disbursement_stats,
                "breakdown_categories": len(disbursement_stats.get("breakdown_formatted", {})),
                "total_disbursed": disbursement_stats.get("total_g_disbursed", 0)
            }
        except Exception as analytics_error:
            status["analytics_test"] = {
                "success": False,
                "error": str(analytics_error)
            }

        return jsonify(status)

    except Exception as e:
        logger.error(f"❌ Database status check error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("🚀 Starting GoodDollar Analytics Platform...")

    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🌐 Starting Flask server on http://0.0.0.0:{port}")
    logger.info(f"✅ Server is ready to accept connections")
    logger.info(f"📡 Webview URL: https://{os.environ.get('REPL_SLUG', 'app')}.{os.environ.get('REPL_OWNER', 'replit')}.repl.co")

    # Start Flask with threaded mode for better concurrent request handling
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
