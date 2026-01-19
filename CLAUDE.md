# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram bot for selling and managing Remnawave VPN subscriptions. Built with Aiogram 3.x (async), SQLAlchemy 2.x (async ORM), and PostgreSQL. Supports multiple payment providers (YooKassa, CryptoPay, FreeKassa, Platega, SeverPay, Telegram Stars) and integrates with the Remnawave panel API.

## Common Development Commands

### Running the Bot

```bash
# Using Docker Compose (recommended)
docker compose up -d

# View logs
docker compose logs -f remnawave-tg-shop

# Stop
docker compose down

# Local development (requires PostgreSQL running)
python main.py
```

### Environment Setup

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your configuration
# Required: BOT_TOKEN, ADMIN_IDS, WEBHOOK_BASE_URL, POSTGRES_*, PANEL_API_URL, PANEL_API_KEY
```

### Database Operations

The bot auto-creates tables on startup via `init_db()` in `db/database_setup.py`. No manual migrations needed for initial setup.

## Architecture Overview

### Layered Architecture

```
Handlers (routing, input validation)
    ↓
Services (business logic, orchestration)
    ↓
DAL (data access layer - pure async functions)
    ↓
Models (SQLAlchemy ORM)
    ↓
PostgreSQL
```

### Core Components

**Entry Point**: `main.py` → `bot/main_bot.py::run_bot()`

**Initialization Flow**:
1. Load `.env` via `config/settings.py` (Pydantic)
2. Initialize database connection (`db/database_setup.py`)
3. Create dispatcher with middlewares (`bot/app/controllers/dispatcher_controller.py`)
4. Build all services via factory (`bot/app/factories/build_services.py`)
5. Register routers (`bot/routers.py`)
6. Start AIOHTTP web server for webhooks (`bot/app/web/web_server.py`)

### Middleware Pipeline (Execution Order)

All middlewares are outer middlewares applied at dispatcher level:

1. **DBSessionMiddleware** - Provides `session` to handlers, auto-commits/rollbacks
2. **I18nMiddleware** - Sets `current_language` and `i18n_instance` in handler data
3. **ProfileSyncMiddleware** - Syncs Telegram user profile to local DB
4. **BanCheckMiddleware** - Blocks banned users
5. **ChannelSubscriptionMiddleware** - Enforces required channel subscription (if configured)
6. **ActionLoggerMiddleware** - Logs all user actions to `message_logs` table

### Router Hierarchy

```
root_router (Private chat filter)
├── user_router_aggregate
│   ├── start_router (CommandStart)
│   ├── payment_router (payment flow callbacks)
│   ├── subscription_router (my_subscription callbacks)
│   ├── trial_router
│   ├── referral_router
│   ├── promo_user_router
│   └── payment method routers (yookassa, stars, crypto, etc.)
├── inline_mode.router
└── admin_main_filtered_router (AdminFilter)
    ├── admin_router_aggregate
    ├── admin_common_router
    ├── admin_payments_router
    ├── admin_promo_routers
    ├── admin_stats_router
    ├── admin_logs_router
    ├── admin_broadcast_router
    ├── admin_ads_router
    ├── admin_sync_router
    └── admin_user_management_router
```

## Key Subsystems

### 1. Service Layer (`bot/services/`)

All services are created once at startup in `build_core_services()` and injected into dispatcher. Handlers access them via `data["service_name"]`.

**Core Services**:
- **PanelApiService** - REST client for Remnawave panel API (user CRUD, device management, stats)
- **SubscriptionService** - Subscription lifecycle management (trial, paid, renewals, traffic packages)
- **YooKassaService** - Primary payment provider with auto-renewal support
- **StarsService**, **CryptoPayService**, **FreeKassaService**, **PlategaService**, **SeverPayService** - Alternative payment providers
- **PanelWebhookService** - Handles panel subscription events (expiry notifications, auto-renew triggers)
- **ReferralService** - Referral tracking and bonus distribution
- **PromoCodeService** - Promo code validation and activation
- **LknpdService** - Tax receipt generation (nalog.ru integration)

**Service Wiring Pattern**:
```python
# Services are cross-injected after creation
subscription_service.yookassa_service = yookassa_service
panel_webhook_service.subscription_service = subscription_service
```

### 2. Data Access Layer (`db/dal/`)

Pure async functions (no classes). Each module provides CRUD operations for specific entities.

**Key DAL Modules**:
- `user_dal.py` - User CRUD, statistics
- `subscription_dal.py` - Subscription upsert, status tracking, expiry queries
- `payment_dal.py` - Payment records, financial statistics
- `promo_code_dal.py` - Promo code management, activation tracking
- `user_billing_dal.py` - Saved payment methods for auto-renewal

**Common Pattern**:
```python
async def get_user_by_id(session: AsyncSession, user_id: int) -> Optional[User]:
    stmt = select(User).where(User.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
```

### 3. Database Models (`db/models.py`)

**Key Entities**:
- **User** - Telegram user profile, panel linkage, referral tree
- **Subscription** - Panel-synced subscription state (active/inactive, expiry, traffic, auto-renew flag)
- **Payment** - Payment transaction records (status, provider, amount, promo code linkage)
- **PromoCode** / **PromoCodeActivation** - Promo codes with usage limits
- **UserBilling** / **UserPaymentMethod** - Saved payment methods for auto-renewal
- **MessageLog** - Audit trail of all user/admin actions
- **PanelSyncStatus** - Tracks automatic panel sync state
- **AdCampaign** / **AdAttribution** - Ad tracking

**Important Relationships**:
- User → Subscription (1:Many)
- User → Payment (1:Many)
- User → User (self-referencing for referral tree via `referred_by_id`)
- Subscription has `panel_user_uuid` and `panel_subscription_uuid` for sync

### 4. Webhook Handling (`bot/app/web/web_server.py`)

Single AIOHTTP application hosts multiple webhook routes:

| Path | Handler | Purpose |
|------|---------|---------|
| `/{BOT_TOKEN}` | SimpleRequestHandler | Telegram updates |
| `/webhook/yookassa` | yookassa_webhook_route | YooKassa payment status |
| `/webhook/cryptopay` | cryptopay_webhook_route | CryptoPay transactions |
| `/webhook/freekassa` | freekassa_webhook_route | FreeKassa payments |
| `/webhook/platega` | platega_webhook_route | Platega payments |
| `/webhook/severpay` | severpay_webhook_route | SeverPay payments |
| `/webhook/panel` | panel_webhook_route | Panel subscription events |

**Security**: All webhooks verify signatures (HMAC-SHA256 for panel, provider-specific for payment systems).

### 5. Panel Integration

**Remnawave API** (`bot/services/panel_api_service.py`):
- REST API client with Bearer token auth
- User CRUD operations
- Device management (HWID disconnect)
- Subscription link generation
- System/bandwidth stats

**User Linking Logic**:
When activating subscription, the bot ensures a panel user exists:
1. Check local DB for `panel_user_uuid`
2. If missing, search panel by `telegramId`
3. If not found, search by username pattern `tg_{telegram_id}`
4. If still missing, create new panel user with configured settings
5. Save returned UUID to local DB

**Panel Webhook Events** (`bot/services/panel_webhook_service.py`):
- `user.expires_in_72_hours` → Send notification
- `user.expires_in_48_hours` → Send notification (special if auto-renew enabled)
- `user.expires_in_24_hours` → **Trigger auto-renewal** if YooKassa + saved card
- `user.expired` → Send expiration notice
- `user.expired_24_hours_ago` → Follow-up reminder

### 6. Payment Flow

**Standard Payment Flow**:
1. User selects subscription duration
2. Handler creates `Payment` record (status: `pending_{provider}`)
3. Service creates payment with provider API
4. User redirected to payment page
5. Provider sends webhook on completion
6. Webhook handler verifies signature, updates Payment status
7. If succeeded: call `subscription_service.activate_subscription()`
   - Ensures panel user exists/linked
   - Creates/updates Subscription record
   - Updates panel user expiry and traffic
   - Enables auto-renew if payment method saved
8. Send success notification with config link

**Auto-Renewal Flow** (YooKassa only):
1. Panel webhook triggers 24h before expiry
2. `PanelWebhookService` checks if auto-renew enabled
3. Calls `subscription_service.charge_subscription_renewal()`
4. Uses saved `payment_method_id` for off-session charge
5. If successful: suppress 24h notification, create new Payment
6. If failed: send notification to user

## Important Architectural Patterns

### 1. Dependency Injection via Factory
All services created in `build_core_services()`, stored in dispatcher:
```python
dp["panel_service"] = panel_service
# Handlers access:
panel_service = data["panel_service"]
```

### 2. Upsert Pattern for Panel Sync
Uses `panel_subscription_uuid` as idempotency key to prevent duplicate subscriptions:
```python
async def upsert_subscription(session, payload):
    existing = await get_subscription_by_panel_subscription_uuid(...)
    if existing:
        # Update fields
    else:
        # Create new
```

### 3. FSM for Multi-Step Flows
State machine for complex interactions (defined in `bot/states/`):
```python
# Set state
await state.set_state(UserPromoStates.waiting_for_promo_code)

# Next handler checks state, processes, clears
if await state.get_state() == UserPromoStates.waiting_for_promo_code:
    # Process promo code
    await state.clear()
```

### 4. Notification Suppression
Track `last_notification_sent` per subscription to avoid spam:
```python
if last_notification_sent is None or date(last_notification_sent) < date(now):
    # Send notification
    await update_last_notification_sent(subscription)
```

## Configuration

All settings loaded via Pydantic from `.env` (see `.env.example` for full reference).

**Critical Settings**:
- `BOT_TOKEN` - Telegram bot token
- `ADMIN_IDS` - Comma-separated admin Telegram IDs
- `WEBHOOK_BASE_URL` - External URL for webhooks (HTTPS required)
- `PANEL_API_URL`, `PANEL_API_KEY` - Remnawave panel access
- `PANEL_WEBHOOK_SECRET` - HMAC signature verification
- Payment provider credentials (YooKassa, CryptoPay, etc.)
- Pricing: `RUB_PRICE_1_MONTH`, `STARS_PRICE_1_MONTH`, etc.

**Sales Modes**:
- **Time-based** (default): User buys N months subscription
- **Traffic-based** (`traffic_sale_mode`): User buys X GB with far-future expiry

## Development Guidelines

### Adding New Payment Provider

1. Create service in `bot/services/{provider}_service.py` implementing:
   - `create_payment()` - Generate payment URL/data
   - Webhook handler function
2. Add webhook route in `bot/app/web/web_server.py`
3. Add service to `build_core_services()` factory
4. Create handler router in `bot/handlers/user/subscription/payments_{provider}.py`
5. Register router in `bot/routers.py`
6. Add pricing settings to `config/settings.py`
7. Update payment method selection in `bot/handlers/user/subscription/payment_methods.py`

### Adding New Admin Feature

1. Create handler in `bot/handlers/admin/{feature}.py`
2. Add keyboard buttons in `bot/keyboards/inline/admin_keyboards.py`
3. Register router in `bot/routers.py` under `admin_main_filtered_router`
4. Use `AdminFilter()` to protect routes
5. Access services via `data["service_name"]`

### Database Changes

The project uses SQLAlchemy models without formal migrations. To add fields:
1. Update model in `db/models.py`
2. Add corresponding DAL functions in `db/dal/`
3. Drop and recreate tables in dev (bot auto-creates on startup)
4. For production, manually ALTER tables or use `db/migrator.py` as template

### Localization

Translations stored in `locales/{lang}/LC_MESSAGES/messages.json`. To add strings:
1. Add key-value to both `ru` and `en` files
2. Access in handlers via: `i18n.get("key_name")`
3. I18n instance available in handler data: `data["i18n_instance"]`

## Testing Checklist

When making changes, verify:
- [ ] Webhooks are reachable (use ngrok for local testing)
- [ ] Payment flow completes end-to-end
- [ ] Panel user creation/linking works
- [ ] Subscription activation updates both DB and panel
- [ ] Auto-renewal triggers correctly
- [ ] Notifications send at proper times
- [ ] Admin panel functions accessible only to admins
- [ ] Banned users cannot access bot
- [ ] Referral bonuses apply correctly
- [ ] Promo codes validate and activate

## Deployment

**Production Setup**:
1. Configure reverse proxy (Nginx/HAProxy) to route HTTPS webhooks to container
2. Set `WEBHOOK_BASE_URL` to external domain
3. Run `docker compose up -d`
4. Verify webhook registration in logs
5. Add bot as admin to required channel (if using `REQUIRED_CHANNEL_ID`)
6. Test payment flow with small amount

**Webhook Paths**:
- Telegram: `https://yourdomain.com/{BOT_TOKEN}`
- YooKassa: `https://yourdomain.com/webhook/yookassa`
- Panel: `https://yourdomain.com/webhook/panel`
- Other providers: `https://yourdomain.com/webhook/{provider}`

## Troubleshooting

**Bot not responding**:
- Check webhook is set correctly (logs show webhook URL on startup)
- Verify HTTPS is working for `WEBHOOK_BASE_URL`
- Check `docker compose logs` for errors

**Payment not completing**:
- Verify webhook route is accessible from provider
- Check signature verification is passing
- Look for errors in payment webhook handler logs
- Ensure `provider_payment_id` is unique (duplicate payments are rejected)

**Panel sync issues**:
- Check `PANEL_API_KEY` is valid
- Verify panel user exists with matching UUID
- Check panel webhook secret matches
- Review `panel_sync_status` table for last sync time

**Auto-renewal not working**:
- Only works with YooKassa
- User must have saved payment method (`user_billing` or `user_payment_methods` table)
- Subscription must have `auto_renew_enabled = true`
- Panel webhook must trigger 24h before expiry
