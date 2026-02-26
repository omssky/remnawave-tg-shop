import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiohttp import web
from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from bot.keyboards.inline.user_keyboards import (
    get_connect_and_main_keyboard,
    get_subscribe_only_markup,
)
from bot.middlewares.i18n import JsonI18n
from bot.services.notification_service import NotificationService
from bot.services.panel_api_service import PanelApiService
from bot.services.referral_service import ReferralService
from bot.services.subscription_service import SubscriptionService
from bot.utils.config_link import prepare_config_links
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display
from config.settings import Settings
from db.dal import payment_dal, subscription_dal, user_dal


def convert_period_to_months(period: Optional[str]) -> int:
    """Map Tribute period string to subscription months."""
    if not period:
        return 1

    mapping = {
        "monthly": 1,
        "quarterly": 3,
        "3-month": 3,
        "3months": 3,
        "3-months": 3,
        "q": 3,
        "halfyearly": 6,
        "yearly": 12,
        "annual": 12,
        "y": 12,
    }
    return mapping.get(period.lower(), 1)


class TributeService:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        panel_service: PanelApiService,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.panel_service = panel_service
        self.subscription_service = subscription_service
        self.referral_service = referral_service

    async def handle_webhook(
        self, raw_body: bytes, signature_header: Optional[str]
    ) -> web.Response:
        def ok(data: Optional[dict] = None) -> web.Response:
            payload = {"status": "ok"}
            if data:
                payload.update(data)
            return web.json_response(payload, status=200)

        def ignored(reason: str) -> web.Response:
            return web.json_response({"status": "ignored", "reason": reason}, status=200)

        if not self.settings.TRIBUTE_ENABLED:
            return ignored("tribute_disabled")

        if self.settings.TRIBUTE_API_KEY:
            if not signature_header:
                return web.json_response(
                    {"status": "error", "reason": "no_signature"}, status=403
                )
            expected_sig = hmac.new(
                self.settings.TRIBUTE_API_KEY.encode(),
                raw_body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected_sig, signature_header):
                return web.json_response(
                    {"status": "error", "reason": "invalid_signature"}, status=403
                )

        try:
            payload = json.loads(raw_body.decode())
        except Exception:
            return web.json_response(
                {"status": "error", "reason": "invalid_json"}, status=400
            )

        event_name = payload.get("name")
        data = payload.get("payload", {}) or {}
        user_id_raw = data.get("telegram_user_id")
        if not user_id_raw:
            return ignored("missing_telegram_user_id")

        try:
            user_id = int(user_id_raw)
        except (TypeError, ValueError):
            return ignored("invalid_telegram_user_id")

        async with self.async_session_factory() as session:
            try:
                if event_name == "new_subscription":
                    await self._handle_new_subscription(
                        session=session,
                        user_id=user_id,
                        data=data,
                        raw_body=raw_body,
                    )
                elif event_name == "cancelled_subscription":
                    await self._handle_cancellation(session=session, user_id=user_id)
                else:
                    return ignored("unsupported_event")
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logging.error("Tribute webhook processing failed: %s", exc, exc_info=True)
                return web.json_response(
                    {"status": "error", "reason": "processing_error"}, status=500
                )

        return ok({"event": event_name or "unknown"})

    async def _handle_new_subscription(
        self,
        session,
        user_id: int,
        data: dict,
        raw_body: bytes,
    ) -> None:
        months = convert_period_to_months(data.get("period"))
        amount_raw = data.get("amount") or data.get("price")
        currency = (
            data.get("currency")
            or self.settings.DEFAULT_CURRENCY_SYMBOL
            or "RUB"
        ).upper()

        if amount_raw is not None:
            try:
                amount_minor_units = float(amount_raw)
            except (TypeError, ValueError):
                amount_minor_units = 0.0
            amount = round(amount_minor_units / 100.0, 2)
        else:
            amount = 0.0

        event_id = str(
            data.get("event_id")
            or data.get("payment_id")
            or data.get("purchase_id")
            or data.get("invoice_id")
            or ""
        )
        if event_id:
            provider_payment_id = event_id
        else:
            sub_id_part = str(data.get("subscription_id") or "sub")
            payload_hash = hashlib.sha256(raw_body).hexdigest()[:16]
            provider_payment_id = f"{sub_id_part}:{payload_hash}"

        payment = await payment_dal.ensure_payment_with_provider_id(
            session,
            user_id=user_id,
            amount=amount,
            currency=currency,
            months=months,
            description="Tribute subscription",
            provider="tribute",
            provider_payment_id=provider_payment_id,
        )
        if payment.status == "succeeded":
            logging.info(
                "Duplicate Tribute event ignored for payment %s",
                provider_payment_id,
            )
            return

        activation = await self.subscription_service.activate_subscription(
            session,
            user_id,
            months,
            amount,
            payment.payment_id,
            provider="tribute",
        )
        if not activation or not activation.get("end_date"):
            raise RuntimeError("Subscription activation failed for Tribute payment.")

        await payment_dal.update_provider_payment_and_status(
            session=session,
            payment_db_id=payment.payment_id,
            provider_payment_id=provider_payment_id,
            new_status="succeeded",
        )

        referral_bonus = await self.referral_service.apply_referral_bonuses_for_payment(
            session,
            user_id,
            months,
            current_payment_db_id=payment.payment_id,
            skip_if_active_before_payment=False,
        )

        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = (
            db_user.language_code
            if db_user and db_user.language_code
            else self.settings.DEFAULT_LANGUAGE
        )
        _ = lambda key, **kwargs: self.i18n.gettext(lang, key, **kwargs)

        final_end = (
            referral_bonus.get("referee_new_end_date")
            if referral_bonus
            else None
        ) or activation.get("end_date")

        config_link_display, connect_button_url = await prepare_config_links(
            self.settings,
            activation.get("subscription_url"),
        )
        config_link_text = config_link_display or _("config_link_not_available")

        applied_ref_days = (
            referral_bonus.get("referee_bonus_applied_days")
            if referral_bonus
            else None
        )
        if applied_ref_days:
            inviter_name_display = _("friend_placeholder")
            if db_user and db_user.referred_by_id:
                inviter = await user_dal.get_user_by_id(session, db_user.referred_by_id)
                if inviter:
                    safe_name = (
                        sanitize_display_name(inviter.first_name)
                        if inviter.first_name
                        else None
                    )
                    if safe_name:
                        inviter_name_display = safe_name
                    elif inviter.username:
                        inviter_name_display = username_for_display(
                            inviter.username,
                            with_at=False,
                        )
            success_message = _(
                "payment_successful_with_referral_bonus_full",
                months=months,
                base_end_date=activation["end_date"].strftime("%Y-%m-%d"),
                bonus_days=applied_ref_days,
                final_end_date=final_end.strftime("%Y-%m-%d"),
                inviter_name=inviter_name_display,
                config_link=config_link_text,
            )
        else:
            success_message = _(
                "payment_successful_full",
                months=months,
                end_date=final_end.strftime("%Y-%m-%d"),
                config_link=config_link_text,
            )

        markup = get_connect_and_main_keyboard(
            lang,
            self.i18n,
            self.settings,
            config_link_display,
            connect_button_url=connect_button_url,
            preserve_message=True,
        )
        try:
            await self.bot.send_message(
                user_id,
                success_message,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.error(
                "Failed to send Tribute success message to %s: %s",
                user_id,
                exc,
            )

        try:
            notification_service = NotificationService(self.bot, self.settings, self.i18n)
            await notification_service.notify_payment_received(
                user_id=user_id,
                amount=amount,
                currency=currency,
                months=months,
                payment_provider="tribute",
                username=db_user.username if db_user else None,
            )
        except Exception as exc:
            logging.error("Failed to send Tribute admin notification: %s", exc)

    async def _handle_cancellation(self, session, user_id: int) -> None:
        grace_end = datetime.now(timezone.utc) + timedelta(days=1)
        active_subscriptions = await subscription_dal.get_active_subscriptions_for_user(
            session, user_id
        )

        panel_users_updated: set[str] = set()
        for sub in active_subscriptions:
            updated_sub = await subscription_dal.update_subscription(
                session,
                sub.subscription_id,
                {
                    "end_date": grace_end,
                    "status_from_panel": "CANCELLED",
                    "skip_notifications": True,
                },
            )
            panel_uuid = updated_sub.panel_user_uuid if updated_sub else None
            if panel_uuid and panel_uuid not in panel_users_updated:
                panel_users_updated.add(panel_uuid)
                try:
                    await self.panel_service.update_user_details_on_panel(
                        panel_uuid,
                        {
                            "expireAt": grace_end.isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z"),
                        },
                        log_response=False,
                    )
                except Exception as exc:
                    logging.error(
                        "Failed to update panel expiry for Tribute cancellation user %s (uuid=%s): %s",
                        user_id,
                        panel_uuid,
                        exc,
                    )

        if self.settings.TRIBUTE_SKIP_CANCELLATION_NOTIFICATIONS:
            return

        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = (
            db_user.language_code
            if db_user and db_user.language_code
            else self.settings.DEFAULT_LANGUAGE
        )
        markup = get_subscribe_only_markup(lang, self.i18n)
        _ = lambda key, **kwargs: self.i18n.gettext(lang, key, **kwargs)
        try:
            await self.bot.send_message(
                user_id,
                _("tribute_subscription_cancelled"),
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception as exc:
            logging.error(
                "Failed to send Tribute cancellation message to %s: %s",
                user_id,
                exc,
            )


async def tribute_webhook_route(request: web.Request) -> web.Response:
    tribute_service: Optional[TributeService] = request.app.get("tribute_service")
    if tribute_service is None:
        return web.Response(status=503, text="tribute_service_not_available")
    raw_body = await request.read()
    signature_header = request.headers.get("trbt-signature")
    return await tribute_service.handle_webhook(raw_body, signature_header)
