import argparse
import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from rules_ai import DEFAULT_MODEL, RulesAPIError, RulesAssistant

ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
API_ERROR_TEXT = "Сервис ответов временно недоступен. Попробуйте немного позже."
HISTORY_LENGTH = 3
DEFAULT_HR_URL = "https://connectedhome.kz/ru/new-contact"


def log_unanswered(path: Path, question: str) -> None:
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "question": question}
    try:
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        LOGGER.warning("Не удалось записать вопрос в журнал.")


def build_application(
    token: str, assistant: RulesAssistant, hr_url: str, log_path: Path
) -> Application:
    hr_button = (
        InlineKeyboardMarkup([[InlineKeyboardButton("Связаться с HR", url=hr_url)]])
        if hr_url else None
    )

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.chat_data.pop("history", None)
        await update.message.reply_text(
            "Привет! Это тестовый бот по правилам Connected Home. Задай свой вопрос.\n\n"
            "/reset — очистить историю\n"
            "/hr — контакты компании",
            reply_markup=hr_button,
        )

    async def hr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Контакты Connected Home — на странице компании:", reply_markup=hr_button
        )

    async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.chat_data.pop("history", None)
        await update.message.reply_text("Контекст очищен. Задайте новый вопрос.")

    async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        question = message.text.strip()
        if not question:
            await message.reply_text("Напишите вопрос по правилам компании.")
            return

        history = context.chat_data.setdefault("history", deque(maxlen=HISTORY_LENGTH))
        try:
            answer = await assistant.answer(question, tuple(history))
        except RulesAPIError as error:
            LOGGER.warning("%s", error)
            await message.reply_text(API_ERROR_TEXT)
            return

        await message.reply_text(answer.text, reply_markup=hr_button if answer.missing else None)
        history.append({"question": question, "answer": answer.text})
        if answer.missing:
            log_unanswered(log_path, question)

    async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.error("Ошибка обработки сообщения (%s).", type(context.error).__name__)

    app = Application.builder().token(token).build()
    private_message = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler("start", start, filters=private_message))
    app.add_handler(CommandHandler("reset", reset, filters=private_message))
    app.add_handler(CommandHandler("hr", hr, filters=private_message))
    app.add_handler(
        MessageHandler(private_message & filters.TEXT & ~filters.COMMAND, handle_question)
    )
    app.add_error_handler(handle_error)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот по правилам Connected Home")
    parser.add_argument("--ask", metavar="ВОПРОС", help="Проверить Gemini без Telegram")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", encoding="utf-8-sig")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
    if not key:
        parser.exit(2, "Укажите GEMINI_API_KEY в файле .env.\n")
    assistant = RulesAssistant(ROOT / "rules.txt", key, model)
    if args.ask is not None:
        try:
            print(asyncio.run(assistant.answer(args.ask)).text)
        except RulesAPIError as error:
            parser.exit(1, f"{error}\n")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        parser.exit(2, "Укажите TELEGRAM_BOT_TOKEN в файле .env.\n")
    hr_url = os.getenv("HR_URL", "").strip() or DEFAULT_HR_URL
    try:
        url = urlsplit(hr_url)
        if hr_url and (url.scheme != "https" or not url.hostname):
            raise ValueError
    except ValueError:
        parser.exit(2, "HR_URL должна быть корректной ссылкой https://.\n")

    try:
        app = build_application(token, assistant, hr_url, ROOT / "unanswered.jsonl")
        with asyncio.Runner() as runner:
            runner.get_loop()
            app.run_polling(allowed_updates=["message"], close_loop=False, bootstrap_retries=0)
    except TelegramError as error:
        parser.exit(1, f"Не удалось запустить Telegram-бота ({type(error).__name__}).\n")


if __name__ == "__main__":
    main()
