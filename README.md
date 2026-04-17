# GoodMarket

A Web3 earning platform built on the GoodDollar ecosystem. Users earn G$ tokens on the Celo network through educational quizzes, social media tasks, minigames, and community engagement.

## Tech Stack

- **Backend:** Python 3.12, Flask, Gunicorn (gthread workers)
- **Frontend:** Server-side rendered Jinja2 templates with static assets
- **Database:** Supabase (PostgreSQL)
- **Blockchain:** Web3.py, Celo network, GoodDollar (G$) contracts
- **WalletConnect:** Node.js sidecar service (`wc_service.js`) using `@walletconnect/sign-client`
- **Package Manager (Python):** uv (with `uv.lock` and `pyproject.toml`)
- **Package Manager (Node):** npm (`package.json`)

## Project Layout

| Path | Description |
|------|-------------|
| `main.py` | Flask app entry point, initializes all services and blueprints |
| `routes.py` | Core API routes and auth decorators |
| `blockchain.py` | Blockchain logic (UBI claims, G$ balances) |
| `config.py` | Global configuration and reward settings |
| `supabase_client.py` | Database connection and utilities |
| `gunicorn.conf.py` | Gunicorn server configuration (port 5000, 0.0.0.0) |
| `wc_service.js` | Node.js WalletConnect service (runs on port 3001) |
| `learn_and_earn/` | Learn & Earn quiz module |
| `minigames/` | Minigames module |
| `twitter_task/` | Twitter social task module |
| `telegram_task/` | Telegram social task module |
| `discourse_task/` | Discourse forum task module |
| `savings/` | G$ Savings module (time-locked deposits, sponsor reward pool) |
| `contracts/GDSavings.sol` | Smart contract for savings — deployed to Celo Mainnet |
| `contracts/deploy_savings_contract.py` | Deployment script using SAVING_KEY |
| `community_stories/` | Community stories module |
| `jumble/` | Jumble word game module |
| `price_prediction/` | Price prediction module |
| `referral_program/` | Referral program module |
| `contracts/` | Solidity smart contracts and deployment scripts |
| `static/` | Static assets (JS bundles, icons, manifest) |
| `templates/` | Jinja2 HTML templates |

## Workflow

- **Start application:** `uv run gunicorn --config gunicorn.conf.py main:app`
- Runs on port **5000** (0.0.0.0)
- WalletConnect sidecar runs on port **3001** (started automatically by main.py if `WALLETCONNECT_PROJECT_ID` is set)

## Required Environment Variables / Secrets

The app gracefully degrades when these are missing, but full functionality requires:

- `SUPABASE_URL` — Supabase project URL
- `SUPABASE_ANON_KEY` — Supabase API key
- `SECRET_KEY` — Flask session secret key
- `WALLETCONNECT_PROJECT_ID` — WalletConnect project ID
- `CELO_RPC_URL` — Celo RPC endpoint (defaults to `https://forno.celo.org`)
- `GOODDOLLAR_CONTRACT` — GoodDollar token contract address
- `MERCHANT_ADDRESS` — Merchant wallet address for minigames
- `GAMES_KEY` — Private key for games blockchain transactions
- `COMMUNITY_KEY` — Private key for community stories rewards
- `PRODUCTION_DOMAIN` — Production domain (defaults to `https://goodmarket.live`)

## Admin Feature Visibility Controls

Admins can show or hide the `/swap` and `/wallet` pages from the admin dashboard under the **Feature Visibility** section. When hidden, users visiting those pages are shown a friendly "Feature Unavailable" page instead.

- Settings stored in the `maintenance_settings` Supabase table using `feature_name` values `swap_feature` and `wallet_feature`.
- Public API: `GET /api/feature-visibility` — returns `{ swap_visible, wallet_visible }`.
- Admin API: `GET/POST /api/admin/feature-visibility` — reads/updates settings (admin auth required).
- New template: `templates/feature_unavailable.html` — shown when a feature is hidden.

## Daily Voucher Feature

A daily payment link voucher that appears on all user dashboards every day at **2PM PHT** (UTC+8) and disappears the moment someone claims it.

### How it works
1. **Admin** goes to Admin Dashboard → **Daily Voucher** → pastes the payment link URL → clicks Save.
2. At 2PM PHT, a golden animated banner appears on every logged-in user's dashboard with a **"Claim GoodMarket Voucher"** button.
3. The **first user** to click the button claims it — the banner immediately disappears for everyone.
4. The admin can Reset the claim status to make it claimable again if needed.

### Database table required
Run `create_daily_voucher_table.sql` in your Supabase SQL Editor to create the `daily_voucher` table before using this feature.

### API endpoints
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/voucher/daily` | User | Returns active voucher if after 2PM PHT |
| POST | `/api/voucher/claim` | User | Claims the voucher (first-come-first-served) |
| GET | `/api/admin/voucher` | Admin | Gets today's voucher status |
| POST | `/api/admin/voucher` | Admin | Sets/updates the voucher link |
| POST | `/api/admin/voucher/reset` | Admin | Resets claim status |

## Deployment

### Replit Autoscale
Configured for **autoscale** deployment. Run command: `gunicorn --config gunicorn.conf.py main:app`

The WalletConnect sidecar (`wc_service.js`) is started automatically by the Flask app at runtime if `WALLETCONNECT_PROJECT_ID` is set — no separate process needed in deployment.

### Vercel
- `vercel.json` is configured to deploy the Flask app using `@vercel/python`
- `.vercelignore` excludes large/unnecessary files (node_modules, .pythonlibs, uv.lock, etc.)
- `requirements.txt` contains all Python dependencies for Vercel to install
- WalletConnect sidecar is gracefully skipped on Vercel (Node.js subprocess not available in serverless Python runtime; browser-side WalletConnect fallback is used)
- All environment variables must be set in the Vercel project dashboard

**Required Vercel Environment Variables:**
| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Flask session secret key |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anonymous/service key |
| `WALLETCONNECT_PROJECT_ID` | WalletConnect project ID |
| `CELO_RPC_URL` | Celo RPC endpoint (default: `https://forno.celo.org`) |
| `GOODDOLLAR_CONTRACT` | GoodDollar token contract address |
| `LEARN_WALLET_PRIVATE_KEY` | Private key for Learn & Earn reward disbursement |
| `LEARN_EARN_CONTRACT_ADDRESS` | Learn & Earn smart contract address |
| `DAILY_TASK_CONTRACT_ADDRESS` | Daily Task smart contract address |
| `TASK_KEY` | Private key for daily task rewards |
| `COMMUNITY_KEY` | Private key for community stories rewards |
| `GAMES_KEY` | Private key for minigame transactions |
| `REFERRAL_KEY` | Private key for referral rewards |
| `DISCOURSE_TASK_KEY` | Private key for Discourse task rewards |
| `IMGBB_API_KEY` | ImgBB API key for image uploads |
| `PRODUCTION_DOMAIN` | Production domain (e.g. `https://goodmarket.live`) |
| `PAYMENT_LINK_ENC_KEY` | Encryption key for payment links |
| `CELOSCAN_API_KEY` | Celoscan API key (optional) |

## Replit Setup Notes

- `pyproject.toml` was created during Replit import to enable `uv sync` for Python dependency management
- `package.json` was created during Replit import for Node.js WalletConnect dependencies
- Workflow: "Start application" runs on port 5000 (webview)
