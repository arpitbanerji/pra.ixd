"""Telegram bot wiring: allowlist, commands, dispatch, health, lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import signal

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from ohbot.backends import Backend, build_backend
from ohbot.backends.base import BackendError
from ohbot.config import Settings
from ohbot.store import Store
from ohbot.surface import TelegramSurface
from ohbot.worker import ConversationRunner, SerialExecutor

log = logging.getLogger(__name__)

WELCOME = (
    "👋 *OpenHands from your phone.*\n\n"
    "Send me anything and I'll run it as a prompt on your OpenHands agent, "
    "then reply with the result — just like chatting in the web UI.\n\n"
    "One Telegram chat keeps one continuous OpenHands conversation, so context "
    "carries across messages. Use /new to start over."
)

HELP = (
    "*Commands*\n"
    "/new — start a fresh conversation\n"
    "/status — current conversation and its state\n"
    "/stop — interrupt the running agent\n"
    "/resume `<conversation_id>` — attach this chat to an existing conversation\n"
    "/id — show your Telegram user id\n"
    "/help — this message"
)


class Bridge:
    """Owns the handlers and the per-chat work queue."""

    def __init__(self, settings: Settings, backend: Backend, store: Store) -> None:
        self.settings = settings
        self.backend = backend
        self.store = store
        self.executor = SerialExecutor()
        self.surface: TelegramSurface | None = None
        self.runner: ConversationRunner | None = None

    def bind(self, application: Application) -> None:
        self.surface = TelegramSurface(application.bot)
        self.runner = ConversationRunner(
            backend=self.backend, store=self.store, surface=self.surface, settings=self.settings
        )

    # ---- authorisation ---------------------------------------------------
    async def _allowed(self, update: Update) -> bool:
        user = update.effective_user
        if user is not None and self.settings.is_authorized(user.id):
            return True
        user_id = user.id if user else "unknown"
        message = (
            "⛔️ *Not authorised.*\n\n"
            f"Your Telegram user id is `{user_id}`.\n"
            "Add it to `TELEGRAM_ALLOWED_USER_IDS` and restart the bot."
        )
        if update.callback_query is not None:
            await update.callback_query.answer("Not authorised", show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(message, parse_mode="Markdown")
        log.warning("unauthorised_access_attempt", extra={"user_id": user_id})
        return False

    # ---- handlers --------------------------------------------------------
    async def on_update(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Drop updates already handled before a restart."""
        if not await self.store.mark_update_seen(update.update_id):
            log.info("duplicate_update_ignored", extra={"update_id": update.update_id})
            raise ApplicationHandlerStop

    async def cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        await update.effective_message.reply_text(WELCOME, parse_mode="Markdown")

    async def cmd_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        await update.effective_message.reply_text(HELP, parse_mode="Markdown")

    async def cmd_id(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.effective_message.reply_text(
            f"Your Telegram user id is `{user.id if user else 'unknown'}`.", parse_mode="Markdown"
        )

    async def cmd_new(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        await self.store.clear_session(update.effective_chat.id)
        await update.effective_message.reply_text(
            "🆕 Fresh conversation. Your next message starts a new OpenHands session."
        )

    async def cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        chat_id = update.effective_chat.id
        session = await self.store.get_session(chat_id)
        if session is None:
            await update.effective_message.reply_text(
                "No conversation yet. Send a message to start one."
            )
            return
        try:
            status = await self.backend.get_status(session.conversation_id)
        except BackendError as exc:
            status = f"unreachable ({exc})"
        await update.effective_message.reply_text(
            f"*Conversation* `{session.conversation_id}`\n"
            f"*Backend* {session.backend}\n"
            f"*Status* {status}",
            parse_mode="Markdown",
        )

    async def cmd_stop(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        chat_id = update.effective_chat.id
        try:
            stopped = await self.runner.interrupt(chat_id)
        except BackendError as exc:
            await update.effective_message.reply_text(f"⚠️ Could not interrupt: `{exc}`")
            return
        await update.effective_message.reply_text(
            "🛑 Interrupt requested." if stopped else "There is no conversation to stop yet."
        )

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        if not context.args:
            await update.effective_message.reply_text(
                "Usage: `/resume <conversation_id>`", parse_mode="Markdown"
            )
            return
        conversation_id = context.args[0].strip()
        await self.store.set_session(update.effective_chat.id, conversation_id, self.backend.name)
        await update.effective_message.reply_text(
            f"🔗 This chat now follows conversation `{conversation_id}`.", parse_mode="Markdown"
        )

    async def on_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        chat_id = update.effective_chat.id
        self.executor.submit(chat_id, lambda: self.runner.handle_prompt(chat_id, text))

    async def on_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not await self._allowed(update):
            return
        await query.answer()
        if not (query.data or "").startswith("confirm:"):
            return
        chat_id = query.message.chat_id
        accept = query.data == "confirm:yes"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # cosmetic only
            log.debug("could_not_clear_buttons")
        self.executor.submit(chat_id, lambda: self._apply_confirmation(chat_id, accept))

    async def _apply_confirmation(self, chat_id: int, accept: bool) -> None:
        try:
            await self.runner.confirm(chat_id, accept)
        except NotImplementedError:
            await self.surface.send_markdown(
                chat_id, "This backend does not support approvals."
            )
            return
        await self.surface.send_markdown(chat_id, "✅ Approved." if accept else "❌ Rejected.")
        if accept:
            await self.runner.resume_turn(chat_id)


def build_application(settings: Settings, bridge: Bridge) -> Application:
    builder = ApplicationBuilder().token(settings.telegram_bot_token)
    if settings.telegram_proxy:
        # Needed where Telegram is blocked; the same proxy serves both the
        # polling connection and outgoing API calls.
        from telegram.request import HTTPXRequest

        request = HTTPXRequest(proxy=settings.telegram_proxy)
        builder = builder.request(request).get_updates_request(
            HTTPXRequest(proxy=settings.telegram_proxy)
        )
        log.info("telegram_proxy_configured")
    application = builder.build()
    application.add_handler(TypeHandler(Update, bridge.on_update), group=-1)
    application.add_handler(CommandHandler(["start"], bridge.cmd_start))
    application.add_handler(CommandHandler(["help"], bridge.cmd_help))
    application.add_handler(CommandHandler(["id"], bridge.cmd_id))
    application.add_handler(CommandHandler(["new", "clear"], bridge.cmd_new))
    application.add_handler(CommandHandler(["status"], bridge.cmd_status))
    application.add_handler(CommandHandler(["stop"], bridge.cmd_stop))
    application.add_handler(CommandHandler(["resume"], bridge.cmd_resume))
    application.add_handler(CallbackQueryHandler(bridge.on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.on_message))
    return application


async def serve_health(host: str, port: int) -> asyncio.AbstractServer:
    """Minimal HTTP health endpoint for container orchestration."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.read(65536), timeout=5)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        body = json.dumps({"status": "ok", "service": "ohbot"}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)


async def run(settings: Settings) -> None:
    backend = build_backend(settings)
    log.info(
        "backend_selected",
        extra={"mode": settings.openhands_mode.value, "base_url": settings.openhands_base_url},
    )

    async with Store(settings.db_path) as store:
        await store.prune_seen_updates()
        bridge = Bridge(settings, backend, store)
        application = build_application(settings, bridge)
        bridge.bind(application)
        health = await serve_health(settings.health_host, settings.health_port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - non-POSIX
                pass

        await application.initialize()
        await application.start()
        assert application.updater is not None
        await application.updater.start_polling(drop_pending_updates=False)
        log.info("bot_started", extra={"health_port": settings.health_port})

        try:
            await stop.wait()
        finally:
            log.info("shutting_down")
            await bridge.executor.drain()
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            health.close()
            await health.wait_closed()
            await backend.aclose()
            log.info("shutdown_complete")
