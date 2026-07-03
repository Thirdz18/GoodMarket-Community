# Privy Setup Guide for GoodMarket

This guide explains how to configure Privy for your GoodMarket application.

## Prerequisites

1. A Privy account at [privy.io](https://privy.io)
2. A Privy application created in the Privy Dashboard

## Getting Your Privy Credentials

### 1. Create a Privy App

1. Go to [dashboard.privy.io](https://dashboard.privy.io)
2. Click "Create New App"
3. Enter app name: "GoodMarket"
4. Select your workspace
5. Copy the **App ID** from the dashboard

### 2. Get App Secret

1. In your app dashboard, go to **Settings** → **API Keys**
2. Copy the **App Secret** (keep this secure!)

### 3. Configure Supported Chains

In Privy Dashboard → Your App → **Embedded Wallets** → **Chains**:

Add the following chains:
- **Celo Mainnet**: Chain ID `42220`
- **Ethereum** (optional): Chain ID `1`

### 4. Configure Login Methods

In Privy Dashboard → Your App → **Login Methods**:

Enable the following:
- ✅ **Wallet** (for MetaMask, WalletConnect, etc.)
- ✅ **Email** (magic link)
- ✅ **Google OAuth** (requires Google Cloud setup)

## Environment Variables

Add these to your `.env` file:

```bash
# Privy Configuration (REQUIRED)
PRIVY_APP_ID=cmcxxxxxxxxxxxxxxxxxxxxxxxxxxxx
PRIVY_APP_SECRET=privy_app_secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Optional Configuration

```bash
# WalletConnect (handled by Privy - leave empty/null)
WALLETCONNECT_PROJECT_ID=

# Session configuration
SESSION_SECRET=your_session_secret_here
```

## Testing Locally

1. Set your environment variables:
   ```bash
   export PRIVY_APP_ID="your_app_id"
   export PRIVY_APP_SECRET="your_app_secret"
   ```

2. Run the app:
   ```bash
   python main.py
   ```

3. Visit `/login-privy` to test the new login flow

## Deployment

### Heroku

```bash
heroku config:set PRIVY_APP_ID=your_app_id
heroku config:set PRIVY_APP_SECRET=your_app_secret
```

### Vercel

Add to `.env.local` or Vercel Dashboard → Environment Variables:
- `PRIVY_APP_ID`
- `PRIVY_APP_SECRET`

### Docker

Add to your `docker-compose.yml`:
```yaml
environment:
  - PRIVY_APP_ID=your_app_id
  - PRIVY_APP_SECRET=your_app_secret
```

## Privy Dashboard Settings

### Recommended Settings

#### Embedded Wallets
- ✅ Enable embedded wallets
- ✅ "Show wallet UI" option
- ✅ "Require user login" to create wallet

#### Login Methods
- **Wallet**: Required
- **Email**: Optional (recommended)
- **Google**: Optional (recommended for better UX)

#### Appearance
Set your brand colors and logo in the Dashboard for consistent UI.

## Troubleshooting

### "Privy service not configured"
- Make sure `PRIVY_APP_ID` and `PRIVY_APP_SECRET` are set
- Restart the server after setting environment variables

### "Token verification failed"
- Check if your Privy App ID is correct
- Make sure the App Secret matches

### Wallet not connecting
- Check browser console for errors
- Ensure Celo network is supported in your Privy app settings

### Embedded wallet not appearing
- Make sure "Embedded Wallets" is enabled in Privy Dashboard
- Check that you've enabled the wallet login method

## Security Notes

- Never commit `PRIVY_APP_SECRET` to version control
- Use environment variables or a secrets manager
- The App Secret should be kept server-side only (never exposed to frontend)

## Support

- Privy Docs: https://docs.privy.io/
- Privy Dashboard: https://dashboard.privy.io/
- GoodMarket Support: Create an issue on GitHub

---

*Last updated: 2026-07-03*
