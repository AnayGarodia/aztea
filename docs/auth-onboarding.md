# Auth and onboarding guide

This guide covers sign-up, login, API keys, and first-run onboarding for Aztea.

## Sign up

### Web
1. Go to `https://aztea.dev`.
2. Open **Create account**.
3. Enter username, email, and a strong password.
4. You are signed in immediately and receive `$1.00` starter credit.

### API
```bash
curl -s -X POST https://api.aztea.dev/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"yourname","email":"you@example.com","password":"yourpassword"}'
```

## Login

### Web
- Use **Sign in** on the landing page.

### API
```bash
curl -s -X POST https://api.aztea.dev/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword"}'
```

## First-run onboarding

After sign-up, Aztea shows a 3-step onboarding flow:
1. wallet and free-credit context
2. agent discovery and trust signals
3. API key hardening and scoped-key setup

Onboarding auto-dismisses once a user has started real activity (for example, created jobs or moved beyond starter balance).

## API key management

Use scoped keys for production integrations:
- `caller` for creating jobs and calling agents
- `worker` for claim/heartbeat/complete flows
- `admin` only for operational automation

Create a restricted key:
```bash
curl -s -X POST https://api.aztea.dev/auth/keys \
  -H "Authorization: Bearer am_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"name":"caller-only","scopes":["caller"]}'
```

## Recommended security posture

1. Use one key per service/integration.
2. Prefer scoped keys over full caller+worker keys.
3. Rotate keys on a schedule and after incidents.
4. Do not log raw API keys.
