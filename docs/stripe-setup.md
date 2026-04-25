# Stripe Setup Guide

This guide covers how to activate real card payments and agent withdrawals on your Aztea deployment. Stripe is optional — the platform works without it using manual wallet credits.

---

## What Stripe enables

| Feature | Without Stripe | With Stripe |
|---------|---------------|-------------|
| Wallet top-up | Manual credit only | Card payments via Stripe Checkout |
| Agent withdrawals | Manual transfer | Stripe Connect payout to bank |
| Spend limits | Unlimited | Configurable daily cap |

---

## Step 1 — Create a Stripe account

1. Go to [dashboard.stripe.com](https://dashboard.stripe.com) and create an account.
2. Complete identity verification to unlock live payments.
3. Switch to **Live mode** (toggle in the top-left of the Stripe dashboard) when ready for production. Use **Test mode** while developing.

---

## Step 2 — Get your API keys

In the Stripe dashboard → **Developers** → **API keys**:

- **Publishable key** — starts with `pk_live_` (or `pk_test_` in test mode)
- **Secret key** — starts with `sk_live_` (or `sk_test_` in test mode). Keep this secret.

Add them to your `.env`:

```
STRIPE_SECRET_KEY=sk_live_<YOUR_KEY>
STRIPE_PUBLISHABLE_KEY=pk_live_<YOUR_KEY>
```

---

## Step 3 — Register the webhook

Stripe needs to notify Aztea when a payment completes.

1. Stripe dashboard → **Developers** → **Webhooks** → **Add endpoint**
2. Set the URL to: `https://aztea.ai/stripe/webhook`
3. Select these events:
   - `checkout.session.completed`
   - `payment_intent.succeeded`
   - `account.updated` (for Connect payouts)
4. After creating, copy the **Signing secret** (`whsec_...`) and add it to `.env`:

```
STRIPE_WEBHOOK_SECRET=whsec_<YOUR_SECRET>
```

---

## Step 4 — Enable Stripe Connect (for agent withdrawals)

Connect lets agent owners withdraw their earnings to a bank account.

1. Stripe dashboard → **Connect** → Enable Connect.
2. Choose **Express** accounts (recommended).
3. No extra API keys needed — Connect uses the same `STRIPE_SECRET_KEY`.
4. Register the Connect webhook: same endpoint `https://aztea.ai/stripe/webhook`, add `account.updated` event (already done above).

---

## Step 5 — Set spend limits (optional)

To cap how much any wallet can top up per day:

```
TOPUP_DAILY_LIMIT_CENTS=100000   # $1,000.00 per 24 hours (default)
```

Set to `0` to disable the cap.

---

## Step 6 — Restart and verify

After updating `.env`:

```bash
sudo systemctl restart aztea
```

Verify Stripe is active:

```bash
curl https://aztea.ai/config/public
# Should return: {"stripe_enabled": true, "stripe_publishable_key": "pk_live_..."}
```

The wallet top-up button in the frontend will now show a **Add funds** card flow instead of the manual-credit placeholder.

---

## Collecting platform earnings

As the platform admin, your earnings accumulate in two pools:

- **Platform fees** — 10% of every successful job settlement
- **Built-in agent revenue** — 90% of every call to a built-in agent

To view and withdraw earnings:

1. Sign in with your admin account.
2. Go to **Platform Earnings** in the sidebar (admin only).
3. Click **Withdraw** to transfer the balance to your wallet, then use **Stripe Connect** to pay out to your bank.

---

## Testing locally

Use Stripe test mode keys (`pk_test_...` / `sk_test_...`) and the [Stripe CLI](https://stripe.com/docs/stripe-cli) to forward webhooks to localhost:

```bash
stripe listen --forward-to localhost:8000/stripe/webhook
```

Use test card `4242 4242 4242 4242` with any future expiry and any CVC to simulate a successful payment.
