"""
Gift Offer — единый модуль.
Все компоненты (конфиг, домен, UI, представления, хранилище, транспорт, сервис) объединены.
Старая админ-панель удалена. Добавлена система подписки и команда /add_worker.
"""
from __future__ import annotations

import asyncio
import copy
import hmac
import json
import logging
import os
import re
import secrets
import socket
import ssl
import sqlite3
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import certifi


log = logging.getLogger("gift_offer")


# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

def load_dotenv(path: Path) -> None:
    """Простой построчный читатель .env. Уже заданные переменные окружения имеют приоритет."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("Неверная строка в .env: используйте KEY=value.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def integer_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: допустимо от {minimum} до {maximum}.")
    return value


@dataclass(frozen=True)
class Config:
    token: str
    admin_id: int
    name: str = "Gift Offer"
    default_language: str = "ru"
    offer_ttl: int = 21600
    cooldown: int = 3
    db_path: str = "data/offers.sqlite3"
    subscribe_url: str = "https://t.me/your_payment_bot"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(Path(".env"))
        token = next((os.environ.get(key, "").strip() for key in
                      ("BOT_TOKEN", "API_TOKEN", "TELEGRAM_BOT_TOKEN")
                      if os.environ.get(key, "").strip()), "")
        if not re.fullmatch(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажите настоящий BOT_TOKEN из @BotFather в .env или переменных сервера.")
        try:
            admin_id = int(os.environ.get("ADMIN_ID", "0").strip() or "0")
        except ValueError as exc:
            raise ValueError("ADMIN_ID должен быть числом (ваш Telegram user id).") from exc
        if admin_id <= 0:
            raise ValueError("Укажите ADMIN_ID в .env — числовой Telegram ID администратора.")
        language = os.environ.get("DEFAULT_LANGUAGE", "ru").strip().lower()
        if language not in {"ru", "en", "zh", "ar"}:
            raise ValueError("DEFAULT_LANGUAGE: ru, en, zh или ar.")
        name = os.environ.get("BOT_NAME", "Gift Offer").strip()
        if not 1 <= len(name) <= 64:
            raise ValueError("BOT_NAME: от 1 до 64 символов.")
        data_dir = os.environ.get("DATA_DIR", "data").strip() or "data"
        db_path = os.environ.get("DB_PATH", "").strip() or str(Path(data_dir) / "offers.sqlite3")
        subscribe_url = os.environ.get("SUBSCRIBE_URL", "https://t.me/your_payment_bot").strip()
        if not re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{4,}", subscribe_url):
            raise ValueError("SUBSCRIBE_URL должен быть ссылкой вида https://t.me/BotUsername.")
        return cls(
            token=token, admin_id=admin_id, name=name, default_language=language,
            offer_ttl=integer_env("OFFER_TTL_SECONDS", 21600, 60, 86400),
            cooldown=integer_env("CREATE_COOLDOWN_SECONDS", 3, 0, 60),
            db_path=db_path, subscribe_url=subscribe_url,
        )


# =============================================================================
# ДОМЕН
# =============================================================================

LANGUAGES = ("ru", "en", "zh", "ar")


class OfferError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OfferSpec:
    gift_url: str
    gift_slug: str
    gift_name: str
    amount: str
    currency: str
    language: str


def parse_buy(text: str, default_language: str = "ru") -> OfferSpec:
    parts = text.strip().split()
    if len(parts) not in (4, 5) or parts[0].lower() != ".buy":
        raise OfferError("syntax")
    try:
        url = urlsplit(parts[1])
    except ValueError as exc:
        raise OfferError("gift_url") from exc
    if url.scheme != "https" or url.netloc.lower() != "t.me" or url.query or url.fragment:
        raise OfferError("gift_url")
    match = re.fullmatch(r"/nft/([A-Za-z][A-Za-z0-9]{0,99})-([1-9][0-9]{0,11})/?", url.path)
    if not match:
        raise OfferError("gift_url")
    collection, number = match.groups()
    currency = parts[3].lower()
    if currency not in {"stars", "gram"}:
        raise OfferError("currency")
    amount_text = parts[2]
    if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,9})?", amount_text):
        raise OfferError("amount")
    amount = Decimal(amount_text)
    maximum = Decimal(100_000_000 if currency == "stars" else 1_000_000)
    if amount <= 0 or amount > maximum:
        raise OfferError("amount")
    if currency == "stars" and amount != amount.to_integral_value():
        raise OfferError("stars_integer")
    if currency == "stars":
        amount_text = str(int(amount))
    else:
        amount_text = format(amount, "f")
        if "." in amount_text:
            amount_text = amount_text.rstrip("0").rstrip(".")
    language = parts[4].lower() if len(parts) == 5 else default_language
    if language not in LANGUAGES:
        raise OfferError("language")
    slug = f"{collection}-{number}"
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", collection)
    return OfferSpec(f"https://t.me/nft/{slug}", slug, f"{name} #{number}", amount_text, currency, language)


TRANSITIONS = {
    "accept": ("seller", {"pending"}, "accepted"),
    "reject": ("seller", {"pending"}, "rejected"),
    "confirm_transfer": ("seller", {"accepted", "payment_reported", "gift_reported", "gift_received"}, "transfer_confirmed"),
    "cancel": ("buyer", {"pending", "accepted"}, "cancelled"),
    "sent": ("seller", {"accepted", "payment_reported"}, "gift_reported"),
    "received": ("buyer", {"gift_reported"}, "gift_received"),
    "settled": ("seller", {"accepted", "gift_reported", "gift_received"}, "payment_reported"),
}


def next_status(offer: dict, action: str, actor_id: int, now: int) -> str:
    if action not in TRANSITIONS:
        raise OfferError("unknown_action")
    role, allowed_states, target = TRANSITIONS[action]
    if actor_id != offer[f"{role}_id"]:
        raise OfferError("wrong_role")
    if action == "settled" and offer.get("payment_claimed"):
        raise OfferError("already_done")
    if offer["status"] == "expired" or (offer["status"] == "pending" and now >= offer["expires_at"]):
        raise OfferError("expired")
    if offer["status"] == target:
        raise OfferError("already_done")
    if offer["status"] not in allowed_states:
        raise OfferError("wrong_state")
    if action == "settled":
        if offer["status"] == "gift_received":
            return "completed"
        if offer["status"] == "gift_reported":
            return "gift_reported"
    if action == "received" and offer.get("payment_claimed"):
        return "completed"
    return target


# =============================================================================
# UI
# =============================================================================

STARS_EMOJI_ID = "6028338546736107668"
GRAM_EMOJI_ID = "6007983438294949171"
NEGATIVE_EMOJI_ID = "5774077015388852135"
POSITIVE_EMOJI_ID = "5774022692642492953"
CURRENCY_EMOJI = {
    "stars": (STARS_EMOJI_ID, "⭐", "Stars"),
    "gram": (GRAM_EMOJI_ID, "💎", "Gram"),
}


def price_text(amount: str, currency: str, *, label: str | None = None) -> str:
    emoji_id, fallback, default_label = CURRENCY_EMOJI[currency]
    return f'<b>{escape(amount)}</b> <tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji> {escape(label or default_label)}'


def button(text: str, *, callback_data: str | None = None,
           url: str | None = None, negative: bool = False) -> dict:
    if (callback_data is None) == (url is None):
        raise ValueError("Инлайн-кнопке нужно ровно одно действие.")
    result = {"text": text, "icon_custom_emoji_id": NEGATIVE_EMOJI_ID if negative else POSITIVE_EMOJI_ID}
    result["callback_data" if callback_data is not None else "url"] = callback_data if callback_data is not None else url
    return result


def has_custom_emoji(params: dict) -> bool:
    if "<tg-emoji " in params.get("text", ""):
        return True
    return any("icon_custom_emoji_id" in item
               for row in params.get("reply_markup", {}).get("inline_keyboard", []) for item in row)


def without_custom_emoji(params: dict) -> dict:
    """Сохраняет все действия и подписи, если Telegram отклонил кастомные эмодзи."""
    result = copy.deepcopy(params)
    if "text" in result:
        result["text"] = re.sub(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", r"\1", result["text"], flags=re.S)
    for row in result.get("reply_markup", {}).get("inline_keyboard", []):
        for item in row:
            emoji_id = item.pop("icon_custom_emoji_id", None)
            if emoji_id in {NEGATIVE_EMOJI_ID, POSITIVE_EMOJI_ID}:
                icon = "❌" if emoji_id == NEGATIVE_EMOJI_ID else "✅"
                item["text"] = f'{icon} {item["text"]}'
    return result


# =============================================================================
# ПРЕДСТАВЛЕНИЯ (блок TEXT для ru/en/zh/ar сохранён как был)
# =============================================================================

TEXT = {
    "ru": {
        "offer": "✦ <b>ПРЕДЛОЖЕНИЕ</b> ✦", "proposes": "✦ <b>НОВОЕ ПРЕДЛОЖЕНИЕ</b> ✦\n\nПользователь предлагает Вам\n\n{price} за подарок {gift}.",
        "stars": "Звёзд", "gram": "GRAM", "expires": "⌛ <i>Действует ещё {hours} ч. {minutes} мин.</i>",
        "disclosure": "──────────────\n<i>Расчёт и передача — напрямую между участниками. Действия с офферами записываются в журнал.</i>",
        "accept": "Принять", "reject": "Отклонить", "gift": "Показать подарок",
        "transfer": "Передать подарок", "confirm_transfer": "Подтвердить передачу",
        "transfer_confirmed": "Продавец подтвердил передачу подарка. Подтверждение сохранено.",
        "buyer": "Профиль покупателя", "cancel": "Отменить оффер",
        "sent": "Я передал подарок", "received": "Я получил подарок", "settled": "Я получил оплату",
        "pending": "Ожидает решения продавца",
        "accepted": "Предложение принято. Передайте подарок покупателю {buyer} в Telegram, затем подтвердите передачу.",
        "payment_reported": "Продавец сообщил о получении оплаты. Ожидается передача подарка.",
        "payment_note": "Продавец также сообщил о получении оплаты.",
        "gift_reported": "Продавец сообщил о передаче подарка. Покупатель должен проверить его получение и подтвердить.",
        "gift_received": "Покупатель сообщил о получении подарка. Продавец может подтвердить получение оплаты после проверки собственного счёта.",
        "completed": "Участники сообщили о получении подарка и оплаты. Оффер закрыт по их заявлениям.",
        "rejected": "Продавец отклонил предложение.", "cancelled": "Оффер отменён.",
        "expired": "Срок предложения истёк. Создайте новый оффер, если хотите продолжить.",
        "delivery_failed": "Не удалось отправить оффер. Команда остаётся в чате.",
        "delivery_unknown": "Статус отправки неизвестен. Если карточка появилась, нажмите её кнопку; не создавайте дубликат до проверки чата.",
        "help": "<b>Как создать оффер</b>\n\nПодключи бота через «Автоматизацию чатов» в Telegram и отправь команду в личный чат с владельцем подарка:\n\n<code>.buy ССЫЛКА_НА_НФТ ЦЕНА ВАЛЮТА ЯЗЫК</code>\n\n<b>Примеры:</b>\n<code>.buy https://t.me/nft/DurovsCap-1234 500 stars</code>\n<code>.buy https://t.me/nft/DurovsCap-1234 10 gram en</code>\n\nВалюты: <code>stars / gram</code>\nЯзыки: <code>ru / en / zh / ar</code> — можно не указывать.\n\nПосле отправки оффера команда удаляется из чата, если разрешено удаление сообщений.\n\n{disclosure}",
        "terms": "<b>{name} · Условия</b>\n\nБот хранит офферы и заявления участников. Stars и Gram — обозначения цены предложения. Касса, резервирование, выплаты, гарантия, автоматическая передача NFT и возвраты не подключены.\n\nПринятие означает согласие с ценой предложения. Кнопки передачи, получения и оплаты фиксируют только заявления пользователей. Проверяйте подарок в аккаунте, а оплату на собственном счёте.",
        "syntax": "Формат: .buy https://t.me/nft/Название-1234 500 stars ru",
        "gift_url": "Нужна ссылка https://t.me/nft/Название-1234 без параметров и сторонних доменов.",
        "currency": "Валюта: stars или gram.", "amount": "Цена должна быть положительной: до 100 000 000 Stars или 1 000 000 Gram.",
        "stars_integer": "Цена в Stars должна быть целым числом.", "language": "Язык: ru, en, zh или ar.",
        "wrong_role": "Эта кнопка предназначена другому участнику сделки.",
        "wrong_state": "На этом этапе действие недоступно. Карточка будет обновлена.",
        "already_done": "Это действие уже выполнено.", "not_found": "Оффер не найден.",
        "unknown_action": "Неизвестная кнопка.", "wrong_card": "Эта кнопка не относится к исходной карточке оффера.",
        "connection_disabled": "Подключение бота отключено или у него нет права отвечать в этом чате.",
        "cooldown": "Подожди несколько секунд перед созданием следующего оффера.",
        "status_syntax": "Используй .status или .cancel без дополнительных аргументов.",
        "done": "Сохранено.", "check_chat": "Карточка могла быть отправлена. Проверь чат перед повтором команды.",
        "delivery_cause": "Ответ Telegram ({code}): <code>{cause}</code>",
        "delete_permission": "Оффер создан. Включи удаление сообщений для бота в «Автоматизации чатов», чтобы команда .buy удалялась автоматически.",
        "card_updates_paused": "Обновление карточки приостановлено после отказа Telegram. Действие не выполнено.",
        "card_wait": "Telegram попросил подождать ещё {seconds} сек. Действие не выполнено.",
        "card_refresh_failed": "Telegram не подтвердил обновление существующей карточки. Пауза сохранена; новое действие не записано.",
        "card_refresh_unknown": "Не удалось подтвердить обновление карточки. Пауза сохранена; новое действие не записано.",
    },
    "en": {
        "offer": "✦ <b>OFFER</b> ✦", "proposes": "✦ <b>NEW OFFER</b> ✦\n\nA user offers you\n\n{price} for {gift}.",
        "stars": "Stars", "gram": "Gram", "expires": "⌛ <i>Valid for {hours} h {minutes} min.</i>",
        "disclosure": "──────────────\n<i>Payment and gift transfer are arranged directly between participants.</i>",
        "accept": "Accept", "reject": "Decline", "gift": "Show gift", "buyer": "Buyer profile",
        "transfer": "Transfer gift", "confirm_transfer": "Confirm transfer",
        "transfer_confirmed": "The seller confirmed the gift transfer. Confirmation saved.",
        "cancel": "Cancel offer", "sent": "I sent the gift", "received": "I received the gift", "settled": "I received payment",
        "pending": "Waiting for the seller",
        "accepted": "Offer accepted. Transfer the gift to the buyer {buyer} in Telegram, then confirm the transfer.",
        "payment_reported": "The seller reported receiving payment. Waiting for the gift transfer.",
        "payment_note": "The seller also reported receiving payment.",
        "gift_reported": "The seller reported sending the gift. The buyer should check their account before confirming receipt.",
        "gift_received": "The buyer reported receiving the gift. The seller may confirm payment after checking their own account.",
        "completed": "Participants reported receiving the gift and payment. The offer is closed based on their statements.",
        "rejected": "The seller declined the offer.", "cancelled": "Offer cancelled.",
        "expired": "This offer has expired. Create a new offer to continue.",
        "delivery_failed": "Could not send the offer. The command remains in the chat.",
        "delivery_unknown": "Message delivery is uncertain. If the card appeared, use its button.",
        "help": "<b>Create an offer</b>\n\nConnect the bot through Telegram Chat Automation, then send this command in a private chat with the gift owner:\n\n<code>.buy NFT_LINK PRICE CURRENCY LANGUAGE</code>\n\n<code>.buy https://t.me/nft/DurovsCap-1234 500 stars</code>\n\nCurrencies: <code>stars / gram</code>\nLanguages: <code>ru / en / zh / ar</code> (optional)\n\n{disclosure}",
        "terms": "<b>{name} · Terms</b>\n\nThe bot records offers and participant statements. Stars and Gram are price labels. Payment collection, escrow, payouts, guarantees, automatic NFT transfers and refunds are not connected.",
        "syntax": "Format: .buy https://t.me/nft/Name-1234 500 stars en",
        "gift_url": "Use https://t.me/nft/Name-1234 without query parameters or other domains.",
        "currency": "Currency must be stars or gram.", "amount": "Use a positive price up to 100,000,000 Stars or 1,000,000 Gram.",
        "stars_integer": "Stars must be a whole number.", "language": "Language must be ru, en, zh or ar.",
        "wrong_role": "This action belongs to the other participant.", "wrong_state": "This action is unavailable at this stage.",
        "already_done": "This action is already recorded.", "not_found": "Offer not found.", "unknown_action": "Unknown button.",
        "wrong_card": "This is not the original offer card.",
        "connection_disabled": "The connection is disabled or the bot cannot reply in this chat.",
        "cooldown": "Wait a few seconds before creating another offer.",
        "status_syntax": "Use .status or .cancel without extra arguments.",
        "done": "Saved.", "check_chat": "The card may have been delivered. Check the chat before retrying.",
        "delivery_cause": "Telegram response ({code}): <code>{cause}</code>",
        "delete_permission": "Offer created. Enable message deletion for the bot in Chat Automation.",
        "card_updates_paused": "Card updates are paused after a Telegram refusal.",
        "card_wait": "Telegram requires waiting another {seconds} seconds.",
        "card_refresh_failed": "Telegram did not confirm the existing card update.",
        "card_refresh_unknown": "The card update could not be confirmed.",
    },
    "zh": {
        "offer": "报价", "proposes": "用户愿以\n{price} 购买您的礼物 {gift}。",
        "stars": "Stars", "gram": "Gram", "expires": "报价剩余有效时间：{hours} 小时 {minutes} 分钟。",
        "disclosure": "付款和礼物转移由双方直接协商。",
        "accept": "接受", "reject": "拒绝", "gift": "查看礼物", "buyer": "买家资料",
        "transfer": "转让礼物", "confirm_transfer": "确认转让",
        "transfer_confirmed": "卖家已确认礼物转让，确认记录已保存。",
        "cancel": "取消报价", "sent": "我已转移礼物", "received": "我已收到礼物", "settled": "我已收到付款",
        "pending": "等待卖家决定",
        "accepted": "报价已接受。请在 Telegram 中将礼物转让给买家 {buyer}，然后确认转让。",
        "payment_reported": "卖家表示已收到付款。等待礼物转移。",
        "payment_note": "卖家也表示已收到付款。",
        "gift_reported": "卖家表示已转移礼物。买家应先在自己的账户中核实。",
        "gift_received": "买家表示已收到礼物。卖家核查自己的账户后，可确认收到付款。",
        "completed": "双方表示已收到礼物和付款。",
        "rejected": "卖家拒绝了报价。", "cancelled": "报价已取消。", "expired": "报价已过期。",
        "delivery_failed": "无法发送报价。命令仍保留在聊天中。",
        "delivery_unknown": "发送状态未知。",
        "help": "<b>创建报价</b>\n\n<code>.buy 礼物链接 价格 币种 语言</code>",
        "terms": "<b>{name} · 使用说明</b>", "syntax": "格式：.buy https://t.me/nft/Name-1234 500 stars zh",
        "gift_url": "请使用 https://t.me/nft/Name-1234。", "currency": "币种必须为 stars 或 gram。",
        "amount": "价格必须为正数。", "stars_integer": "Stars 必须为整数。",
        "language": "语言必须为 ru、en、zh 或 ar。", "wrong_role": "此操作应由另一位参与者执行。",
        "wrong_state": "当前阶段无法执行此操作。", "already_done": "此操作已记录。",
        "not_found": "未找到报价。", "unknown_action": "未知按钮。", "wrong_card": "这不是原始报价卡片。",
        "connection_disabled": "连接已关闭。", "cooldown": "请等待几秒。",
        "status_syntax": "使用 .status 或 .cancel。", "done": "已保存。", "check_chat": "请检查聊天。",
        "delivery_cause": "Telegram 返回（{code}）：<code>{cause}</code>",
        "delete_permission": "报价已创建。", "card_updates_paused": "卡片更新已暂停。",
        "card_wait": "请再等待 {seconds} 秒。",
        "card_refresh_failed": "Telegram 未确认卡片更新。", "card_refresh_unknown": "无法确认卡片更新。",
    },
    "ar": {
        "offer": "عرض", "proposes": "✦ <b>عرض جديد</b> ✦\n\nيعرض عليك مستخدم\n\n{price} مقابل الهدية {gift}.",
        "stars": "Stars", "gram": "Gram", "expires": "⌛ <i>صالح لمدة {hours} ساعة و{minutes} دقيقة أخرى</i>",
        "disclosure": "يتفق الطرفان مباشرة على الدفع ونقل الهدية.",
        "accept": "قبول", "reject": "رفض", "gift": "عرض الهدية", "buyer": "ملف المشتري",
        "transfer": "نقل الهدية", "confirm_transfer": "تأكيد النقل",
        "transfer_confirmed": "أكد البائع نقل الهدية. تم حفظ التأكيد.",
        "cancel": "إلغاء العرض", "sent": "أرسلت الهدية", "received": "استلمت الهدية", "settled": "استلمت الدفع",
        "pending": "بانتظار قرار البائع",
        "accepted": "تم قبول العرض. انقل الهدية إلى المشتري {buyer} في Telegram، ثم أكد النقل.",
        "payment_reported": "أبلغ البائع عن استلام الدفع.", "payment_note": "أبلغ البائع أيضًا عن استلام الدفع.",
        "gift_reported": "أبلغ البائع عن إرسال الهدية.", "gift_received": "أبلغ المشتري عن استلام الهدية.",
        "completed": "أبلغ الطرفان عن استلام الهدية والدفع.", "rejected": "رفض البائع العرض.",
        "cancelled": "تم إلغاء العرض.", "expired": "انتهت صلاحية العرض.",
        "delivery_failed": "تعذّر إرسال العرض.", "delivery_unknown": "حالة الإرسال غير معروفة.",
        "help": "<b>إنشاء عرض</b>\n\n<code>.buy NFT_LINK PRICE CURRENCY LANGUAGE</code>",
        "terms": "<b>{name} · الشروط</b>", "syntax": "الصيغة: .buy https://t.me/nft/Name-1234 500 stars ar",
        "gift_url": "استخدم https://t.me/nft/Name-1234.", "currency": "العملة يجب أن تكون stars أو gram.",
        "amount": "السعر موجب.", "stars_integer": "يجب أن يكون سعر Stars عدداً صحيحاً.",
        "language": "اللغة: ru أو en أو zh أو ar.", "wrong_role": "هذا الإجراء مخصص للمشارك الآخر.",
        "wrong_state": "الإجراء غير متاح في هذه المرحلة.", "already_done": "تم تسجيل هذا الإجراء بالفعل.",
        "not_found": "لم يُعثر على العرض.", "unknown_action": "زر غير معروف.", "wrong_card": "هذه ليست بطاقة العرض الأصلية.",
        "connection_disabled": "الاتصال معطل.", "cooldown": "انتظر بضع ثوانٍ.",
        "status_syntax": "استخدم .status أو .cancel.", "done": "تم الحفظ.", "check_chat": "تحقق من المحادثة.",
        "delivery_cause": "رد Telegram ({code}): <code>{cause}</code>",
        "delete_permission": "تم إنشاء العرض.", "card_updates_paused": "تحديث البطاقة متوقف.",
        "card_wait": "انتظر {seconds} ثانية إضافية.",
        "card_refresh_failed": "لم يؤكد Telegram تحديث البطاقة.", "card_refresh_unknown": "تعذر تأكيد تحديث البطاقة.",
    },
}

# Тексты системы подписки (только эти новые ключи; старые не тронуты).
SUBSCRIBE_TEXT = {
    "ru": {
        "welcome_locked": "Добрый день! У вас нет доступа к функциям нашего бота.\nДля полного функционала вам нужно купить разовую подписку.",
        "subscribe_button": "Купить подписку",
        "unlocked": "Поздравляем! Вам разблокированы функции бота.\nОтправьте /start для запуска.",
        "already_unlocked": "У вас уже есть доступ к функциям бота. Отправьте /start для запуска.",
        "not_for_you": "Эта команда доступна только администратору.",
        "add_worker_usage": "Использование: /add_worker <id>",
        "add_worker_bad_id": "ID должен быть положительным числом.",
        "add_worker_done": "Пользователь {id} добавлен в воркеры. Ему отправлено уведомление.",
        "add_worker_already": "Пользователь {id} уже в списке воркеров.",
        "admin_panel": "<b>Админ-панель</b>\n\nВсего воркеров: <b>{workers}</b>\nВсего пользователей: <b>{users}</b>\nВсего офферов: <b>{offers}</b>\n\n/add_worker &lt;id&gt; — выдать доступ\n/remove_worker &lt;id&gt; — забрать доступ",
        "workers_list": "<b>Воркеры</b>\n\n{rows}",
        "workers_empty": "Список воркеров пуст.",
        "remove_worker_done": "Пользователь {id} удалён из воркеров.",
        "remove_worker_missing": "Пользователя {id} нет в списке воркеров.",
        "admin_buttons": "Список воркеров",
        "refresh": "Обновить",
    },
    "en": {
        "welcome_locked": "Hello! You don't have access to this bot's features.\nTo unlock the full functionality, you need to buy a one-time subscription.",
        "subscribe_button": "Buy subscription",
        "unlocked": "Congratulations! You now have access to the bot's features.\nSend /start to begin.",
        "already_unlocked": "You already have access. Send /start to begin.",
        "not_for_you": "This command is available only to the administrator.",
        "add_worker_usage": "Usage: /add_worker <id>",
        "add_worker_bad_id": "ID must be a positive number.",
        "add_worker_done": "User {id} added to workers. Notification sent.",
        "add_worker_already": "User {id} is already a worker.",
        "admin_panel": "<b>Admin panel</b>\n\nWorkers: <b>{workers}</b>\nUsers: <b>{users}</b>\nOffers: <b>{offers}</b>\n\n/add_worker &lt;id&gt; — grant access\n/remove_worker &lt;id&gt; — revoke access",
        "workers_list": "<b>Workers</b>\n\n{rows}",
        "workers_empty": "Worker list is empty.",
        "remove_worker_done": "User {id} removed from workers.",
        "remove_worker_missing": "User {id} is not a worker.",
        "admin_buttons": "Workers list",
        "refresh": "Refresh",
    },
    "zh": {
        "welcome_locked": "您好！您还没有使用本机器人功能的权限。\n如需完整功能，请购买一次性订阅。",
        "subscribe_button": "购买订阅",
        "unlocked": "恭喜！机器人功能已为您解锁。\n请发送 /start 开始使用。",
        "already_unlocked": "您已经拥有访问权限。请发送 /start。",
        "not_for_you": "此命令仅管理员可用。",
        "add_worker_usage": "用法：/add_worker <id>",
        "add_worker_bad_id": "ID 必须是正整数。",
        "add_worker_done": "用户 {id} 已加入 workers，并已发送通知。",
        "add_worker_already": "用户 {id} 已在 workers 中。",
        "admin_panel": "<b>管理面板</b>\n\nWorkers: <b>{workers}</b>\nUsers: <b>{users}</b>\nOffers: <b>{offers}</b>",
        "workers_list": "<b>Workers</b>\n\n{rows}",
        "workers_empty": "列表为空。",
        "remove_worker_done": "用户 {id} 已从 workers 移除。",
        "remove_worker_missing": "用户 {id} 不在 workers 中。",
        "admin_buttons": "Workers 列表",
        "refresh": "刷新",
    },
    "ar": {
        "welcome_locked": "مرحباً! ليس لديك صلاحية استخدام ميزات هذا البوت.\nللحصول على الوظائف الكاملة، يلزم شراء اشتراك لمرة واحدة.",
        "subscribe_button": "شراء الاشتراك",
        "unlocked": "تهانينا! تم فتح ميزات البوت لك.\nأرسل /start للبدء.",
        "already_unlocked": "لديك صلاحية بالفعل. أرسل /start للبدء.",
        "not_for_you": "هذا الأمر متاح للمشرف فقط.",
        "add_worker_usage": "الاستخدام: /add_worker <id>",
        "add_worker_bad_id": "المعرّف يجب أن يكون رقماً موجباً.",
        "add_worker_done": "تمت إضافة المستخدم {id} إلى workers وإرسال إشعار.",
        "add_worker_already": "المستخدم {id} موجود بالفعل في workers.",
        "admin_panel": "<b>لوحة المشرف</b>\n\nWorkers: <b>{workers}</b>\nUsers: <b>{users}</b>\nOffers: <b>{offers}</b>",
        "workers_list": "<b>Workers</b>\n\n{rows}",
        "workers_empty": "القائمة فارغة.",
        "remove_worker_done": "تمت إزالة المستخدم {id} من workers.",
        "remove_worker_missing": "المستخدم {id} غير موجود في workers.",
        "admin_buttons": "قائمة Workers",
        "refresh": "تحديث",
    },
}


def language_for(user: dict, default: str = "ru") -> str:
    language = str(user.get("language_code", "")).lower().split("-")[0]
    return language if language in TEXT else default


def tr(language: str, key: str, **kwargs) -> str:
    table = TEXT.get(language, TEXT["ru"])
    template = table.get(key) or TEXT["ru"].get(key, key)
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


def subtr(language: str, key: str, **kwargs) -> str:
    table = SUBSCRIBE_TEXT.get(language, SUBSCRIBE_TEXT["ru"])
    template = table.get(key) or SUBSCRIBE_TEXT["ru"].get(key, key)
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


def help_text(name: str, language: str) -> str:
    return tr(language, "help", name=escape(name), disclosure=tr(language, "disclosure")).rstrip()


def remaining_minutes(offer: dict, now: int) -> int:
    return (max(0, offer["expires_at"] - now) + 59) // 60


def transfer_url(offer: dict) -> str:
    username = str(offer.get("buyer_username", ""))
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", username):
        recipient = username
    else:
        buyer_id = offer["buyer_id"]
        if type(buyer_id) is not int or buyer_id <= 0:
            raise ValueError("Получатель подарка должен быть покупателем оффера")
        recipient = str(buyer_id)
    return f"tg://send_gift?to={recipient}"


def render_text(offer: dict, name: str, *, now: int | None = None) -> str:
    language = offer["language"]
    gift = f'<a href="{escape(offer["gift_url"], quote=True)}">{escape(offer["gift_name"])}</a>'
    buyer = f'<a href="tg://user?id={offer["buyer_id"]}">{escape(offer["buyer_name"])}</a>'
    price = price_text(offer["amount"], offer["currency"], label=tr(language, offer["currency"]))
    lines = [tr(language, "proposes", buyer=buyer, price=price, gift=gift)]
    if offer["status"] == "pending":
        hours, minutes = divmod(remaining_minutes(offer, int(time.time()) if now is None else now), 60)
        lines.extend(["", tr(language, "expires", hours=hours, minutes=minutes)])
    else:
        key = offer["status"] if offer["status"] in TEXT.get(language, {}) else "pending"
        lines.extend(["", tr(language, key, buyer=buyer)])
    if offer["status"] == "gift_reported" and offer.get("payment_claimed"):
        lines.extend(["", tr(language, "payment_note")])
    return "\n".join(lines)


def keyboard(offer: dict) -> dict:
    language = offer["language"]

    def action(key: str) -> dict:
        return button(tr(language, key), callback_data=f'g:{offer["id"]}:{key}',
                      negative=key in {"reject", "cancel"})

    rows: list[list[dict]] = []
    state = offer["status"]
    if state == "pending":
        rows.append([action("reject"), action("accept")])
    elif state in {"accepted", "payment_reported", "gift_reported", "gift_received"}:
        rows.append([button(tr(language, "transfer"), url=transfer_url(offer))])
        rows.append([action("confirm_transfer")])
    return {"inline_keyboard": rows}


def card_payload(offer: dict, name: str, *, now: int | None = None) -> dict:
    return {
        "business_connection_id": offer["connection_id"], "chat_id": offer["chat_id"],
        "text": render_text(offer, name, now=now), "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False, "url": offer["gift_url"],
                                 "prefer_large_media": True, "show_above_text": True},
        "reply_markup": keyboard(offer),
    }


# =============================================================================
# ТРАНСПОРТ TELEGRAM
# =============================================================================

class TelegramError(RuntimeError):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        self.code = code
        self.retry_after = retry_after
        self.description = description
        super().__init__(f"Ошибка Telegram API {code}: {description}")


class TransportError(RuntimeError):
    """Сетевой запрос мог не дойти до Telegram, но мог и дойти."""


class Telegram:
    METHODS = frozenset({
        "getMe", "getWebhookInfo", "deleteWebhook", "getUpdates", "getBusinessConnection",
        "sendMessage", "editMessageText", "answerCallbackQuery", "setMyCommands",
        "deleteBusinessMessages",
    })
    RETRY_SAFE = frozenset(METHODS - {"sendMessage"})

    def __init__(self, token: str):
        self._token = token
        self._plain_emoji_scopes: set[str] = set()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "[BOT_TOKEN]")[:500]

    def _decode_response(self, data: bytes, http_code: int = 0):
        try:
            result = json.loads(data)
        except ValueError:
            raise TransportError("Telegram вернул некорректный JSON") from None
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise TransportError("Telegram вернул некорректный ответ API")
        if result["ok"]:
            if "result" not in result or http_code >= 400:
                raise TransportError("Telegram вернул неполный ответ API")
            return result["result"]
        code = result.get("error_code", http_code or 500)
        description = result.get("description", "Неизвестная ошибка API")
        if type(code) is not int or not isinstance(description, str):
            raise TransportError("Telegram вернул некорректный ответ с ошибкой")
        parameters = result.get("parameters")
        retry_after = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
        if type(retry_after) is not int or retry_after < 0:
            retry_after = 0
        raise TelegramError(code, self._redact(description), retry_after)

    def _call(self, method: str, params: dict, timeout: int):
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            context = ssl.create_default_context(cafile=certifi.where())
            with urlopen(request, timeout=timeout, context=context) as response:
                data = response.read()
        except HTTPError as exc:
            try:
                data = exc.read()
            except OSError:
                raise TransportError("Ответ Telegram с ошибкой прервался") from None
            return self._decode_response(data, exc.code)
        except (URLError, OSError, socket.timeout):
            raise TransportError("Сетевой запрос к Telegram не удался") from None
        return self._decode_response(data)

    async def call(self, method: str, **params):
        if method not in self.METHODS:
            raise ValueError("Этот метод API не поддерживается Gift Offer.")
        message_method = method in {"sendMessage", "editMessageText"}
        emoji_scope = params.get("business_connection_id") or "bot"
        if message_method and emoji_scope in self._plain_emoji_scopes:
            params = without_custom_emoji(params)
        timeout = int(params.get("timeout", 0)) + 15 if method == "getUpdates" else 20
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._call, method, params, timeout)
            except TelegramError as exc:
                description = exc.description.lower().replace("_", " ")
                if (message_method and exc.code in {400, 403} and has_custom_emoji(params)
                        and any(marker in description for marker in
                                ("custom emoji", "tg-emoji", "button icon", "emoji id", "document invalid"))):
                    result = await self.call(method, **without_custom_emoji(params))
                    self._plain_emoji_scopes.add(emoji_scope)
                    log.info("Кастомные эмодзи недоступны; используем стандартные иконки.")
                    return result
                if exc.code == 429 and attempt < 2:
                    await asyncio.sleep(max(exc.retry_after, 1))
                    continue
                if method in self.RETRY_SAFE and exc.code >= 500 and attempt < 2:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise
            except TransportError:
                if method not in self.RETRY_SAFE or attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)
        raise AssertionError("Недостижимая ветка")


# =============================================================================
# ХРАНИЛИЩЕ
# =============================================================================

class Store:
    """Синхронные короткие транзакции SQLite в одном цикле событий asyncio."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS business_chat_state(
                connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
                last_incoming_at INTEGER NOT NULL DEFAULT 0,
                edits_paused INTEGER NOT NULL DEFAULT 0,
                edit_retry_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(connection_id, chat_id)
            );
            CREATE TABLE IF NOT EXISTS offers(
                id TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL, card_message_id INTEGER,
                buyer_id INTEGER NOT NULL, seller_id INTEGER NOT NULL,
                buyer_name TEXT NOT NULL, buyer_username TEXT NOT NULL,
                gift_url TEXT NOT NULL, gift_slug TEXT NOT NULL, gift_name TEXT NOT NULL,
                amount TEXT NOT NULL, currency TEXT NOT NULL, language TEXT NOT NULL,
                status TEXT NOT NULL, created_at INTEGER NOT NULL,
                payment_claimed INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                dirty INTEGER NOT NULL DEFAULT 0,
                display_enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(connection_id, chat_id, source_message_id)
            );
            CREATE INDEX IF NOT EXISTS offers_expiry ON offers(status, expires_at);
            CREATE TABLE IF NOT EXISTS command_cleanup(
                offer_id TEXT PRIMARY KEY REFERENCES offers(id),
                sent_at INTEGER NOT NULL, retry_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS command_cleanup_due ON command_cleanup(retry_at);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id TEXT NOT NULL REFERENCES offers(id),
                actor_id INTEGER, action TEXT NOT NULL, at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, first_name TEXT NOT NULL,
                last_name TEXT NOT NULL, username TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers(
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL
            );
        """)

    def close(self) -> None:
        self.db.close()

    def get_setting(self, key: str, default: str = "0") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))

    def save_connection(self, connection: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO connections VALUES(?,?)", (
                connection["id"], json.dumps(connection, ensure_ascii=False),
            ))

    def note_incoming(self, connection_id: str, chat_id: int, at: int) -> None:
        with self.db:
            self.db.execute("INSERT INTO business_chat_state(connection_id,chat_id,last_incoming_at) "
                            "VALUES(?,?,?) ON CONFLICT(connection_id,chat_id) DO UPDATE SET "
                            "last_incoming_at=MAX(last_incoming_at,excluded.last_incoming_at)",
                            (connection_id, chat_id, at))

    def last_incoming(self, connection_id: str, chat_id: int) -> int:
        row = self.db.execute("SELECT last_incoming_at FROM business_chat_state "
                              "WHERE connection_id=? AND chat_id=?", (connection_id, chat_id)).fetchone()
        return row[0] if row else 0

    def pause_card_updates(self, connection_id: str, chat_id: int) -> None:
        with self.db:
            self.db.execute("INSERT INTO business_chat_state(connection_id,chat_id,edits_paused) "
                            "VALUES(?,?,1) ON CONFLICT(connection_id,chat_id) DO UPDATE SET edits_paused=1",
                            (connection_id, chat_id))

    def resume_card_updates(self, connection_id: str, chat_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE business_chat_state SET edits_paused=0 "
                            "WHERE connection_id=? AND chat_id=?", (connection_id, chat_id))

    def defer_card_updates(self, connection_id: str, chat_id: int, retry_at: int) -> None:
        with self.db:
            self.db.execute("INSERT INTO business_chat_state(connection_id,chat_id,edit_retry_at) "
                            "VALUES(?,?,?) ON CONFLICT(connection_id,chat_id) DO UPDATE SET "
                            "edit_retry_at=MAX(edit_retry_at,excluded.edit_retry_at)",
                            (connection_id, chat_id, retry_at))

    def can_update_card(self, connection_id: str, chat_id: int, now: int) -> bool:
        row = self.db.execute("SELECT edits_paused,edit_retry_at FROM business_chat_state "
                              "WHERE connection_id=? AND chat_id=?", (connection_id, chat_id)).fetchone()
        return row is None or (not row[0] and row[1] <= now)

    def card_update_wait(self, connection_id: str, chat_id: int, now: int) -> int:
        row = self.db.execute("SELECT edit_retry_at FROM business_chat_state "
                              "WHERE connection_id=? AND chat_id=?", (connection_id, chat_id)).fetchone()
        return max(row[0] - now, 0) if row else 0

    def touch_user(self, user: dict, now: int) -> None:
        user_id = user.get("id")
        if not isinstance(user_id, int) or user_id <= 0 or user.get("is_bot"):
            return
        previous = self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        profile = {key: str(user[key] if key in user else previous[key] if previous else "")
                   for key in ("first_name", "last_name", "username")}
        with self.db:
            self.db.execute("INSERT INTO users VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                            "first_name=excluded.first_name,last_name=excluded.last_name,"
                            "username=excluded.username,updated_at=excluded.updated_at", (
                user_id, profile["first_name"][:128], profile["last_name"][:128],
                profile["username"][:32], now,
            ))

    # ---------------- Воркеры / доступ ----------------

    def is_worker(self, user_id: int) -> bool:
        if not isinstance(user_id, int) or user_id <= 0:
            return False
        row = self.db.execute("SELECT 1 FROM workers WHERE user_id=?", (user_id,)).fetchone()
        return row is not None

    def add_worker(self, user_id: int, added_by: int, now: int) -> bool:
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO workers(user_id,added_by,added_at) VALUES(?,?,?)",
                                  (user_id, added_by, now))
            return cur.rowcount > 0

    def remove_worker(self, user_id: int) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM workers WHERE user_id=?", (user_id,))
            return cur.rowcount > 0

    def workers_list(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT w.user_id, w.added_by, w.added_at, u.first_name, u.last_name, u.username "
            "FROM workers w LEFT JOIN users u ON u.user_id = w.user_id "
            "ORDER BY w.added_at DESC LIMIT 50"
        )]

    def workers_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM workers").fetchone()[0]

    def users_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def offers_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM offers").fetchone()[0]

    def known_connections(self, owner_id: int) -> list[dict]:
        return [connection for row in self.db.execute("SELECT payload FROM connections")
                if (connection := json.loads(row[0])).get("user", {}).get("id") == owner_id]

    def connection(self, connection_id: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM connections WHERE id=?", (connection_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---------------- Офферы ----------------

    def offer(self, offer_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        return dict(row) if row else None

    def from_source(self, connection_id: str, chat_id: int, message_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM offers WHERE connection_id=? AND chat_id=? AND source_message_id=?",
                              (connection_id, chat_id, message_id)).fetchone()
        return dict(row) if row else None

    def latest_offer(self, connection_id: str, chat_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM offers WHERE connection_id=? AND chat_id=? "
                              "ORDER BY created_at DESC, rowid DESC LIMIT 1", (connection_id, chat_id)).fetchone()
        return dict(row) if row else None

    def latest_creation(self, buyer_id: int) -> int:
        row = self.db.execute("SELECT MAX(created_at) FROM offers WHERE buyer_id=?", (buyer_id,)).fetchone()
        return int(row[0] or 0)

    def create(self, spec: OfferSpec, connection: dict, message: dict, now: int, ttl: int) -> dict:
        user = connection["user"]
        row = {
            "id": secrets.token_hex(8), "connection_id": connection["id"],
            "chat_id": message["chat"]["id"], "source_message_id": message["message_id"],
            "buyer_id": user["id"], "seller_id": message["chat"]["id"],
            "buyer_name": " ".join(filter(None, [user.get("first_name", ""), user.get("last_name", "")])) or "Покупатель",
            "buyer_username": user.get("username", ""),
            **asdict(spec), "status": "pending", "created_at": now,
            "expires_at": now + ttl, "updated_at": now,
        }
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        with self.db:
            self.db.execute(f"INSERT INTO offers({columns}) VALUES({placeholders})", tuple(row.values()))
            sent_at = message.get("date")
            if type(sent_at) is not int or sent_at <= 0:
                sent_at = now
            self.db.execute("INSERT INTO command_cleanup(offer_id,sent_at) VALUES(?,?)", (row["id"], sent_at))
            self.db.execute("INSERT INTO events(offer_id,actor_id,action,at) VALUES(?,?,?,?)",
                            (row["id"], user["id"], "create", now))
        return self.offer(row["id"])

    def attach_card(self, offer_id: str, message_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE offers SET card_message_id=?, dirty=0 WHERE id=?", (message_id, offer_id))

    def delivery_problem(self, offer_id: str, ambiguous: bool, now: int) -> None:
        status = "delivery_unknown" if ambiguous else "delivery_failed"
        with self.db:
            self.db.execute("UPDATE offers SET status=?, updated_at=? WHERE id=?", (status, now, offer_id))
            self.db.execute("INSERT INTO events(offer_id,action,at) VALUES(?,?,?)", (offer_id, status, now))
            if not ambiguous:
                self.db.execute("DELETE FROM command_cleanup WHERE offer_id=?", (offer_id,))

    def command_cleanups(self, now: int) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT o.*,c.sent_at FROM command_cleanup c JOIN offers o ON o.id=c.offer_id "
            "WHERE c.retry_at<=? AND o.card_message_id>0 AND o.display_enabled=1 "
            "ORDER BY c.retry_at,o.created_at LIMIT 5", (now,))]

    def finish_command_cleanup(self, offer_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM command_cleanup WHERE offer_id=?", (offer_id,))

    def defer_command_cleanup(self, offer_id: str, retry_at: int) -> None:
        with self.db:
            self.db.execute("UPDATE command_cleanup SET retry_at=? WHERE offer_id=?", (retry_at, offer_id))

    def recover_interrupted_sends(self, now: int) -> None:
        with self.db:
            ids = self.db.execute("SELECT id FROM offers WHERE card_message_id IS NULL AND status='pending'").fetchall()
            for row in ids:
                self.db.execute("UPDATE offers SET status='delivery_unknown',updated_at=? WHERE id=?", (now, row[0]))
                self.db.execute("INSERT INTO events(offer_id,action,at) VALUES(?,?,?)",
                                (row[0], "delivery_unknown", now))

    def recover_card(self, offer_id: str, message_id: int, now: int) -> dict:
        with self.db:
            self.db.execute("UPDATE offers SET card_message_id=?,status='pending',dirty=1,updated_at=? "
                            "WHERE id=? AND card_message_id IS NULL AND status='delivery_unknown'",
                            (message_id, now, offer_id))
        return self.offer(offer_id)

    def transition(self, offer_id: str, action: str, actor_id: int, now: int) -> dict:
        with self.db:
            row = self.offer(offer_id)
            if row is None:
                raise OfferError("not_found")
            target = next_status(row, action, actor_id, now)
            self.db.execute("UPDATE offers SET status=?,updated_at=?,dirty=1, "
                            "payment_claimed=CASE WHEN ?='settled' THEN 1 ELSE payment_claimed END WHERE id=?",
                            (target, now, action, offer_id))
            self.db.execute("INSERT INTO events(offer_id,actor_id,action,at) VALUES(?,?,?,?)",
                            (offer_id, actor_id, action, now))
        return self.offer(offer_id)

    def expire(self, now: int) -> None:
        with self.db:
            rows = self.db.execute("SELECT id FROM offers WHERE status='pending' AND expires_at<=?", (now,)).fetchall()
            for row in rows:
                self.db.execute("UPDATE offers SET status='expired',updated_at=?,dirty=1 WHERE id=?", (now, row[0]))
                self.db.execute("INSERT INTO events(offer_id,action,at) VALUES(?,?,?)", (row[0], "expire", now))

    def refresh_pending_countdowns(self, now: int) -> None:
        minute = str(now // 60)
        if self.get_setting("countdown_minute", "") == minute:
            return
        with self.db:
            self.db.execute("UPDATE offers SET dirty=1 WHERE status='pending' AND expires_at>? "
                            "AND card_message_id IS NOT NULL AND display_enabled=1", (now,))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('countdown_minute',?)", (minute,))

    def dirty_offers(self, now: int = 0) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT o.* FROM offers o LEFT JOIN business_chat_state s "
            "ON s.connection_id=o.connection_id AND s.chat_id=o.chat_id "
            "WHERE o.dirty=1 AND o.card_message_id IS NOT NULL AND o.display_enabled=1 "
            "AND COALESCE(s.edits_paused,0)=0 AND COALESCE(s.edit_retry_at,0)<=? "
            "ORDER BY o.updated_at,o.rowid LIMIT 30", (now,)
        )]

    def mark_clean(self, offer_id: str, displayed_status: str) -> None:
        with self.db:
            self.db.execute("UPDATE offers SET dirty=0 WHERE id=? AND status=?", (offer_id, displayed_status))

    def hide_card(self, connection_id: str, chat_id: int, message_id: int, now: int) -> None:
        with self.db:
            self.db.execute("UPDATE offers SET display_enabled=0,dirty=0,updated_at=?, "
                            "status=CASE WHEN status IN ('pending','accepted') THEN 'cancelled' ELSE status END "
                            "WHERE connection_id=? AND chat_id=? AND card_message_id=?",
                            (now, connection_id, chat_id, message_id))
            self.db.execute("DELETE FROM command_cleanup WHERE offer_id IN ("
                            "SELECT id FROM offers WHERE connection_id=? AND chat_id=? "
                            "AND (source_message_id=? OR card_message_id=?))",
                            (connection_id, chat_id, message_id, message_id))


# =============================================================================
# СЕРВИС
# =============================================================================

CALLBACK = re.compile(r"g:([a-f0-9]{16}):(accept|reject|confirm_transfer|cancel|sent|received|settled)")
ADMIN_CALLBACK = re.compile(r"adm:(home|workers)")


def can_reply(connection: dict) -> bool:
    return bool(connection.get("is_enabled") and connection.get("rights", {}).get("can_reply"))


class Service:
    def __init__(self, config: Config, store: Store, api: Telegram, bot_id: int, clock=time.time):
        self.config, self.store, self.api, self.bot_id = config, store, api, bot_id
        self.clock = clock
        self.lock = asyncio.Lock()
        self.connections: dict[str, dict] = {}
        self.connection_checked_at: dict[str, int] = {}
        self.retry_at: dict[str, int] = {}
        self.delete_notices: set[str] = set()

    # ---------- доступ ----------

    def has_access(self, user_id: int) -> bool:
        return user_id == self.config.admin_id or self.store.is_worker(user_id)

    async def send_locked_message(self, chat_id: int, language: str) -> None:
        await self.api.call(
            "sendMessage", chat_id=chat_id,
            text=subtr(language, "welcome_locked"),
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup={"inline_keyboard": [[
                button(subtr(language, "subscribe_button"), url=self.config.subscribe_url)
            ]]},
        )

    # ---------- базовые хелперы ----------

    async def connection(self, connection_id: str) -> dict:
        now = int(self.clock())
        if connection_id not in self.connections or now - self.connection_checked_at.get(connection_id, 0) >= 60:
            connection = await self.api.call("getBusinessConnection", business_connection_id=connection_id)
            user = connection.get("user") if isinstance(connection, dict) else None
            if (not isinstance(user, dict) or connection.get("id") != connection_id
                    or type(user.get("id")) is not int or user["id"] <= 0
                    or type(connection.get("user_chat_id")) is not int or connection["user_chat_id"] <= 0):
                raise TransportError("Telegram вернул неполное бизнес-подключение")
            self.connections[connection_id] = connection
            self.connection_checked_at[connection_id] = now
            self.store.save_connection(connection)
            self.store.touch_user(connection["user"], now)
        return self.connections[connection_id]

    async def answer(self, query_id: str, text: str, alert: bool = False) -> None:
        try:
            await self.api.call("answerCallbackQuery", callback_query_id=query_id, text=text[:200], show_alert=alert)
        except (TelegramError, TransportError) as exc:
            log.warning("Не удалось ответить на callback: %s", exc)

    async def direct(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        extras = {"reply_markup": reply_markup} if reply_markup is not None else {}
        await self.api.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                            link_preview_options={"is_disabled": True}, **extras)

    async def notify_owner(self, connection: dict, text: str) -> None:
        try:
            await self.direct(connection["user_chat_id"], text)
        except (TelegramError, TransportError) as exc:
            log.warning("Не удалось уведомить владельца: %s", exc)

    # ---------- маршрутизация обновлений ----------

    async def handle(self, update: dict) -> None:
        async with self.lock:
            now = int(self.clock())
            self.store.expire(now)
            if "business_connection" in update:
                connection = update["business_connection"]
                self.connections[connection["id"]] = connection
                self.connection_checked_at[connection["id"]] = now
                self.delete_notices.discard(connection["id"])
                self.store.save_connection(connection)
                self.store.touch_user(connection["user"], now)
            elif "callback_query" in update:
                await self.callback(update["callback_query"], now)
            elif "business_message" in update:
                await self.business_message(update["business_message"], now)
            elif "deleted_business_messages" in update:
                deleted = update["deleted_business_messages"]
                for message_id in deleted.get("message_ids", []):
                    self.store.hide_card(deleted["business_connection_id"], deleted["chat"]["id"], message_id, now)
            elif "message" in update:
                await self.private_message(update["message"], now)
            await self._flush(now)
            await self._cleanup_commands(now)

    async def private_message(self, message: dict, now: int) -> None:
        if message.get("chat", {}).get("type") != "private" or message.get("from", {}).get("is_bot"):
            return
        user = message.get("from", {})
        user_id = user.get("id")
        chat_id = message["chat"]["id"]
        if not isinstance(user_id, int) or chat_id != user_id:
            return
        text = message.get("text", "").strip()
        if not text:
            return
        command = text.split()[0].split("@")[0].lower()
        language = language_for(user, self.config.default_language)
        self.store.touch_user(user, now)

        # --- команды админа ---
        if command == "/add_worker":
            await self.cmd_add_worker(user_id, chat_id, text, language)
            return
        if command == "/remove_worker":
            await self.cmd_remove_worker(user_id, chat_id, text, language)
            return
        if command == "/admin":
            if user_id != self.config.admin_id:
                await self.direct(chat_id, subtr(language, "not_for_you"))
                return
            await self.show_admin_panel(chat_id, language)
            return

        # --- /start: доступ или paywall ---
        if command == "/start":
            if not self.has_access(user_id):
                await self.send_locked_message(chat_id, language)
                return
            await self.direct(chat_id, help_text(self.config.name, language))
            return

        # --- остальное ---
        if command in {"/help", ".buy"}:
            if not self.has_access(user_id):
                await self.send_locked_message(chat_id, language)
                return
            response = help_text(self.config.name, language)
        elif command in {"/terms", "/paysupport"}:
            if not self.has_access(user_id):
                await self.send_locked_message(chat_id, language)
                return
            response = tr(language, "terms", name=escape(self.config.name))
        else:
            return
        try:
            await self.direct(chat_id, response)
        except (TelegramError, TransportError) as exc:
            log.warning("Не удалось ответить в личный чат: %s", exc)

    # ---------- админ-команды ----------

    async def cmd_add_worker(self, user_id: int, chat_id: int, text: str, language: str) -> None:
        if user_id != self.config.admin_id:
            await self.direct(chat_id, subtr(language, "not_for_you"))
            return
        parts = text.split()
        if len(parts) != 2 or not re.fullmatch(r"[0-9]{1,19}", parts[1]):
            await self.direct(chat_id, subtr(language, "add_worker_usage"))
            return
        try:
            target = int(parts[1])
        except ValueError:
            await self.direct(chat_id, subtr(language, "add_worker_bad_id"))
            return
        if target <= 0:
            await self.direct(chat_id, subtr(language, "add_worker_bad_id"))
            return
        if not self.store.add_worker(target, user_id, int(self.clock())):
            await self.direct(chat_id, subtr(language, "add_worker_already", id=target))
            return
        await self.direct(chat_id, subtr(language, "add_worker_done", id=target))
        try:
            await self.direct(target, subtr(language, "unlocked"))
        except (TelegramError, TransportError) as exc:
            log.warning("Не удалось отправить уведомление воркеру %s: %s", target, exc)

    async def cmd_remove_worker(self, user_id: int, chat_id: int, text: str, language: str) -> None:
        if user_id != self.config.admin_id:
            await self.direct(chat_id, subtr(language, "not_for_you"))
            return
        parts = text.split()
        if len(parts) != 2 or not re.fullmatch(r"[0-9]{1,19}", parts[1]):
            await self.direct(chat_id, "/remove_worker <id>")
            return
        target = int(parts[1])
        if not self.store.remove_worker(target):
            await self.direct(chat_id, subtr(language, "remove_worker_missing", id=target))
            return
        await self.direct(chat_id, subtr(language, "remove_worker_done", id=target))

    async def show_admin_panel(self, chat_id: int, language: str, message_id: int | None = None) -> None:
        text = subtr(
            language, "admin_panel",
            workers=self.store.workers_count(),
            users=self.store.users_count(),
            offers=self.store.offers_count(),
        )
        markup = {"inline_keyboard": [[
            button(subtr(language, "admin_buttons"), callback_data="adm:workers"),
            button(subtr(language, "refresh"), callback_data="adm:home"),
        ]]}
        if message_id is None:
            await self.direct(chat_id, text, markup)
        else:
            try:
                await self.api.call("editMessageText", chat_id=chat_id, message_id=message_id,
                                    text=text, parse_mode="HTML", reply_markup=markup,
                                    link_preview_options={"is_disabled": True})
            except TelegramError as exc:
                if "message is not modified" not in exc.description.lower():
                    log.warning("Не удалось обновить админ-панель: %s", exc)
            except TransportError as exc:
                log.warning("Не удалось обновить админ-панель: %s", exc)

    async def show_workers(self, chat_id: int, language: str, message_id: int | None = None) -> None:
        rows = self.store.workers_list()
        if rows:
            lines = []
            for row in rows:
                name = " ".join(filter(None, [row.get("first_name") or "", row.get("last_name") or ""])) or "—"
                username = row.get("username") or ""
                link = f'<a href="tg://user?id={row["user_id"]}">{escape(name)}</a>'
                if username:
                    link += f" @{escape(username)}"
                lines.append(f"• {link} · <code>{row['user_id']}</code>")
            text = subtr(language, "workers_list", rows="\n".join(lines))
        else:
            text = subtr(language, "workers_empty")
        markup = {"inline_keyboard": [[button(subtr(language, "refresh"), callback_data="adm:workers")]]}
        if message_id is None:
            await self.direct(chat_id, text, markup)
        else:
            try:
                await self.api.call("editMessageText", chat_id=chat_id, message_id=message_id,
                                    text=text, parse_mode="HTML", reply_markup=markup,
                                    link_preview_options={"is_disabled": True})
            except TelegramError as exc:
                if "message is not modified" not in exc.description.lower():
                    log.warning("Не удалось обновить список воркеров: %s", exc)
            except TransportError as exc:
                log.warning("Не удалось обновить список воркеров: %s", exc)

    async def admin_callback(self, query: dict) -> None:
        query_id = query.get("id")
        if not query_id:
            return
        user, message = query.get("from", {}), query.get("message", {})
        user_id = user.get("id")
        if (user_id != self.config.admin_id or user.get("is_bot")
                or message.get("chat", {}).get("type") != "private"
                or message.get("chat", {}).get("id") != user_id
                or message.get("from", {}).get("id") != self.bot_id
                or message.get("business_connection_id") or message.get("forward_origin")
                or not message.get("message_id") or message.get("date") == 0):
            await self.answer(query_id, "Недоступно.", True)
            return
        match = ADMIN_CALLBACK.fullmatch(query.get("data", ""))
        if not match:
            await self.answer(query_id, "Неизвестная кнопка.", True)
            return
        await self.answer(query_id, "")
        language = self.config.default_language
        if match.group(1) == "home":
            await self.show_admin_panel(user_id, language, message["message_id"])
        else:
            await self.show_workers(user_id, language, message["message_id"])

    # ---------- бизнес-сообщения ----------

    async def business_message(self, message: dict, now: int) -> None:
        sender, chat = message.get("from", {}), message.get("chat", {})
        connection_id, at = message.get("business_connection_id"), message.get("date")
        if (isinstance(connection_id, str) and connection_id and chat.get("type") == "private"
                and type(chat.get("id")) is int and chat["id"] > 0
                and sender.get("id") == chat["id"] and not sender.get("is_bot")
                and not message.get("sender_business_bot") and not message.get("is_from_offline")
                and type(at) is int and 0 < at <= now):
            self.store.note_incoming(connection_id, chat["id"], at)
        text = message.get("text", "").strip()
        is_gift = message.get("unique_gift", {}).get("origin") == "transfer"
        if not is_gift and (not text or text.split()[0].lower() not in {".buy", ".help", ".status", ".cancel"}):
            return
        sender = message.get("from", {})
        if not isinstance(sender.get("id"), int) or sender.get("id", 0) <= 0 or sender.get("is_bot"):
            return
        if message.get("forward_origin") or (message.get("sender_business_bot") and not is_gift):
            return
        chat = message.get("chat", {})
        if chat.get("type") != "private" or not isinstance(chat.get("id"), int) or chat["id"] <= 0:
            return
        if chat["id"] == sender["id"] and not is_gift:
            return
        connection_id = message.get("business_connection_id")
        if not connection_id:
            return
        try:
            connection = await self.connection(connection_id)
        except (TelegramError, TransportError) as exc:
            log.warning("Не удалось получить подключение: %s", exc)
            if isinstance(exc, TransportError) or exc.code == 429 or exc.code >= 500:
                raise
            return
        owner_id = connection["user"]["id"]
        self.store.touch_user(connection["user"], now)
        self.store.touch_user(sender, now)
        self.store.touch_user(chat, now)
        if is_gift:
            if connection.get("is_enabled"):
                self.telegram_gift(message, connection, now)
            return
        if owner_id != sender["id"]:
            return

        # проверка доступа воркера для бизнес-команд
        if not self.has_access(sender["id"]):
            return

        language = self.config.default_language
        if not can_reply(connection):
            await self.notify_owner(connection, tr(language, "connection_disabled"))
            return
        command = text.split()[0].lower()
        if command == ".help":
            await self.notify_owner(connection, help_text(self.config.name, language))
            return
        if command in {".status", ".cancel"}:
            await self.owner_command(connection, message, command, now)
            return
        if self.store.from_source(connection_id, chat["id"], message["message_id"]):
            return
        try:
            spec = parse_buy(text, language)
        except OfferError as exc:
            await self.notify_owner(connection, tr(language, exc.code))
            return
        if now - self.store.latest_creation(sender["id"]) < self.config.cooldown:
            await self.notify_owner(connection, tr(language, "cooldown"))
            return
        offer = self.store.create(spec, connection, message, now, self.config.offer_ttl)
        try:
            result = await self.api.call("sendMessage", **card_payload(offer, self.config.name, now=now))
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if type(message_id) is not int or message_id <= 0:
                raise TransportError("Telegram не вернул идентификатор доставленного сообщения")
        except (TelegramError, TransportError) as exc:
            ambiguous = isinstance(exc, TransportError) or exc.code >= 500
            self.store.delivery_problem(offer["id"], ambiguous, now)
            log.warning("Оффер %s не доставлен: %s", offer["id"], exc)
            if ambiguous:
                response = tr(spec.language, "check_chat")
            else:
                cause = escape(exc.description.replace(self.config.token, "[BOT_TOKEN]")[:500])
                response = tr(spec.language, "delivery_failed") + "\n\n" + tr(
                    spec.language, "delivery_cause", code=exc.code, cause=cause)
            await self.notify_owner(connection, response)
            return
        self.store.attach_card(offer["id"], message_id)
        self.store.resume_card_updates(connection_id, chat["id"])

    async def _cleanup_commands(self, now: int) -> None:
        for offer in self.store.command_cleanups(now):
            if now - offer["sent_at"] >= 48 * 3600:
                self.store.finish_command_cleanup(offer["id"])
                continue
            try:
                connection = await self.connection(offer["connection_id"])
                if not connection.get("is_enabled") or connection["user"]["id"] != offer["buyer_id"]:
                    self.store.defer_command_cleanup(offer["id"], now + 60)
                    continue
                if not connection.get("rights", {}).get("can_delete_all_messages"):
                    self.store.defer_command_cleanup(offer["id"], now + 60)
                    if connection["id"] not in self.delete_notices:
                        self.delete_notices.add(connection["id"])
                        await self.notify_owner(connection, tr(offer["language"], "delete_permission"))
                    continue
                deleted = await self.api.call("deleteBusinessMessages", business_connection_id=connection["id"],
                                              message_ids=[offer["source_message_id"]])
                if deleted is not True:
                    raise TransportError("Telegram не подтвердил удаление команды")
            except TelegramError as exc:
                description = exc.description.lower()
                if "message to delete not found" in description:
                    self.store.finish_command_cleanup(offer["id"])
                elif exc.code == 429 or exc.code >= 500:
                    self.store.defer_command_cleanup(offer["id"], now + max(exc.retry_after, 30))
                elif exc.code == 403:
                    self.connections.pop(offer["connection_id"], None)
                    self.store.defer_command_cleanup(offer["id"], now + 60)
                else:
                    self.store.finish_command_cleanup(offer["id"])
                    log.warning("Не удалось удалить команду создания: %s", exc)
                continue
            except TransportError:
                self.store.defer_command_cleanup(offer["id"], now + 30)
                continue
            self.store.finish_command_cleanup(offer["id"])

    async def owner_command(self, connection: dict, message: dict, command: str, now: int) -> None:
        parts = message["text"].strip().split()
        language = self.config.default_language
        if len(parts) != 1:
            await self.notify_owner(connection, tr(language, "status_syntax"))
            return
        offer = self.store.latest_offer(connection["id"], message["chat"]["id"])
        if not offer:
            await self.notify_owner(connection, tr(language, "not_found"))
            return
        if command == ".cancel":
            try:
                offer = self.store.transition(offer["id"], "cancel", connection["user"]["id"], now)
            except OfferError as exc:
                await self.notify_owner(connection, tr(offer["language"], exc.code))
                return
        await self.notify_owner(connection, render_text(offer, self.config.name, now=now))

    # ---------- callback кнопок оффера / админки ----------

    async def callback(self, query: dict, now: int) -> None:
        user = query.get("from", {})
        language = language_for(user, self.config.default_language)
        query_id = query.get("id")
        if not query_id:
            return
        data = query.get("data", "")
        if data.startswith("adm:"):
            await self.admin_callback(query)
            return
        match = CALLBACK.fullmatch(data)
        if not match:
            await self.answer(query_id, tr(language, "unknown_action"), True)
            return
        offer_id, action = match.groups()
        offer = self.store.offer(offer_id)
        if not offer:
            await self.answer(query_id, tr(language, "not_found"), True)
            return
        language = offer["language"]
        if user.get("id") not in {offer["buyer_id"], offer["seller_id"]} or user.get("is_bot"):
            await self.answer(query_id, tr(language, "wrong_role"), True)
            return
        if not self.has_access(user["id"]):
            await self.answer(query_id, subtr(language, "welcome_locked"), True)
            return
        message = query.get("message", {})
        if (message.get("business_connection_id") != offer["connection_id"]
                or message.get("chat", {}).get("id") != offer["chat_id"]
                or not offer["display_enabled"] or not message.get("message_id")
                or message.get("date") == 0):
            await self.answer(query_id, tr(language, "wrong_card"), True)
            return
        try:
            connection = await self.connection(offer["connection_id"])
        except (TelegramError, TransportError) as exc:
            if isinstance(exc, TransportError) or exc.code == 429 or exc.code >= 500:
                raise
            await self.answer(query_id, tr(language, "connection_disabled"), True)
            return
        if not can_reply(connection) or connection["user"]["id"] != offer["buyer_id"]:
            await self.answer(query_id, tr(language, "connection_disabled"), True)
            return
        if offer["card_message_id"] is None and offer["status"] == "delivery_unknown":
            if message.get("sender_business_bot", {}).get("id") != self.bot_id:
                await self.answer(query_id, tr(language, "wrong_card"), True)
                return
            offer = self.store.recover_card(offer_id, message["message_id"], now)
        if message["message_id"] != offer["card_message_id"]:
            await self.answer(query_id, tr(language, "wrong_card"), True)
            return
        paused = not self.store.can_update_card(offer["connection_id"], offer["chat_id"], now)
        repeated = False
        try:
            next_status(offer, action, user["id"], now)
        except OfferError as exc:
            if not paused or exc.code != "already_done":
                await self.answer(query_id, tr(language, exc.code), True)
                return
            repeated = True
        if paused and not await self.refresh_paused_card(offer, connection, query_id, now):
            return
        if repeated:
            await self.answer(query_id, tr(language, "already_done"), True)
            return
        self.store.touch_user(user, now)
        try:
            self.store.transition(offer_id, action, user["id"], now)
        except OfferError as exc:
            if exc.code == "expired":
                self.store.expire(now)
            await self.answer(query_id, tr(language, exc.code), True)
            return
        await self.answer(query_id, tr(language, "done"))

    async def refresh_paused_card(self, offer: dict, connection: dict, query_id: str, now: int) -> bool:
        wait = self.store.card_update_wait(offer["connection_id"], offer["chat_id"], now)
        if wait:
            await self.answer(query_id, tr(offer["language"], "card_wait", seconds=wait), True)
            return False
        try:
            result = await self.api.call("editMessageText", message_id=offer["card_message_id"],
                                         **card_payload(offer, self.config.name, now=now))
            if (not isinstance(result, dict) or type(result.get("message_id")) is not int
                    or result["message_id"] != offer["card_message_id"]):
                raise TransportError("Telegram не подтвердил обновление карточки")
        except TelegramError as exc:
            if not (exc.code == 400 and "message is not modified" in exc.description.lower()):
                if exc.code == 429:
                    self.store.defer_card_updates(offer["connection_id"], offer["chat_id"],
                                                  int(self.clock()) + max(exc.retry_after, 60))
                self.store.pause_card_updates(offer["connection_id"], offer["chat_id"])
                log.warning("Запрошенное обновление карточки %s отклонено: %s", offer["id"], exc)
                await self.answer(query_id, tr(offer["language"], "card_updates_paused"), True)
                cause = escape(exc.description.replace(self.config.token, "[BOT_TOKEN]")[:500])
                response = tr(offer["language"], "card_refresh_failed") + "\n\n" + tr(
                    offer["language"], "delivery_cause", code=exc.code, cause=cause)
                await self.notify_owner(connection, response)
                return False
        except TransportError:
            await self.answer(query_id, tr(offer["language"], "card_refresh_unknown"), True)
            return False
        self.store.mark_clean(offer["id"], offer["status"])
        self.store.resume_card_updates(offer["connection_id"], offer["chat_id"])
        self.retry_at.pop(offer["id"], None)
        return True

    async def tick(self) -> None:
        async with self.lock:
            now = int(self.clock())
            self.store.expire(now)
            self.store.refresh_pending_countdowns(now)
            await self._flush(now)
            await self._cleanup_commands(now)

    async def _flush(self, now: int) -> None:
        for offer in self.store.dirty_offers(now):
            if self.retry_at.get(offer["id"], 0) > now:
                continue
            if not self.store.can_update_card(offer["connection_id"], offer["chat_id"], now):
                continue
            try:
                connection = await self.connection(offer["connection_id"])
                if not can_reply(connection):
                    continue
                await self.api.call("editMessageText", message_id=offer["card_message_id"],
                                    **card_payload(offer, self.config.name, now=now))
            except TelegramError as exc:
                if exc.code == 429:
                    self.store.defer_card_updates(offer["connection_id"], offer["chat_id"],
                                                  int(self.clock()) + max(exc.retry_after, 60))
                elif "message is not modified" in exc.description.lower():
                    self.store.mark_clean(offer["id"], offer["status"])
                elif "message to edit not found" in exc.description.lower():
                    self.store.hide_card(offer["connection_id"], offer["chat_id"], offer["card_message_id"], now)
                else:
                    self.connections.pop(offer["connection_id"], None)
                    self.retry_at[offer["id"]] = now + 60
                continue
            except TransportError:
                self.retry_at[offer["id"]] = now + 30
                continue
            self.store.mark_clean(offer["id"], offer["status"])
            self.retry_at.pop(offer["id"], None)

    def telegram_gift(self, message: dict, connection: dict, now: int) -> None:
        info = message["unique_gift"]
        gift = info.get("gift", {})
        slug = gift.get("name", "")
        if not isinstance(slug, str):
            return
        try:
            spec = parse_buy(f".buy https://t.me/nft/{slug} 1 stars")
        except OfferError:
            return
        owner_id, sender_id, peer_id = connection["user"]["id"], message["from"]["id"], message["chat"]["id"]
        if sender_id == owner_id and not info.get("owned_gift_id"):
            recipient_id = peer_id
        elif sender_id == peer_id:
            recipient_id = owner_id
        else:
            return
        # Просто фиксируем факт в events без старой audit_log
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO events(offer_id,actor_id,action,at) VALUES(?,?,?,?)",
                ("telegram", sender_id, "telegram_transfer", now),
            )


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    store = Store(config.db_path)
    api = Telegram(config.token)
    me = await api.call("getMe")
    bot_id = me["id"]
    store.recover_interrupted_sends(int(time.time()))
    service = Service(config, store, api, bot_id)
    await api.call("setMyCommands", commands=[
        {"command": "start", "description": "Начать"},
        {"command": "help", "description": "Как создать оффер"},
        {"command": "terms", "description": "Условия"},
    ])
    offset = 0
    log.info("Gift Offer запущен как @%s", me.get("username"))
    while True:
        try:
            updates = await api.call("getUpdates", offset=offset, timeout=25,
                                     allowed_updates=["message", "callback_query",
                                                      "business_connection", "business_message",
                                                      "deleted_business_messages"])
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                try:
                    await service.handle(update)
                except (TelegramError, TransportError) as exc:
                    log.warning("Не удалось обработать обновление: %s", exc)
            await service.tick()
        except (TelegramError, TransportError) as exc:
            log.warning("Опрос Telegram завершился ошибкой: %s", exc)
            await asyncio.sleep(3)
        except KeyboardInterrupt:
            break
    store.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
