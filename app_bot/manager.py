import asyncio
import os
import time
import re
import uuid

import qrcode
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
    BufferedInputFile,
    LabeledPrice,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from connection import conn, cur
from core.utils import normalize_notify_chat_id
from repo import (
    db_get_subcategories,
    db_count_enabled_subcategories,
    db_count_enabled_products_in_subcat,
    db_count_enabled_products_in_cat_no_subcat,
    title_for_category,
    title_for_subcategory,
    has_enabled_subcategories,
)

# === Команды бота (кнопка 'Меню' с /командами) ===
DEFAULT_BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="menu", description="Меню"),
    BotCommand(command="cart", description="Корзина"),
    BotCommand(command="status", description="Статус заказа"),
]

active_bots: dict[int, dict] = {}
user_states: dict[int, dict] = {}
async def launch_bot(bot_id: int, token: str, username: str):
    if bot_id in active_bots:
        try:
            await active_bots[bot_id]["bot"].session.close()
        except:
            pass
        del active_bots[bot_id]
        await asyncio.sleep(2)
    bot = Bot(token=token)
    dp = Dispatcher()
    # Устанавливаем команды, чтобы появилась синяя кнопка "Меню" и список /команд
    try:
        await bot.set_my_commands(DEFAULT_BOT_COMMANDS)
    except Exception as e:
        print("Не удалось установить команды бота:", e)
    if bot_id not in user_states:
        user_states[bot_id] = {}
    user_state = user_states[bot_id]

    async def notify_client_status(order_id: int, status_text: str):
        cur.execute("SELECT user_id FROM orders WHERE id=? AND bot_id=?", (order_id, bot_id))
        row = cur.fetchone()
        if not row:
            return
        client_id = row[0]
        try:
            await bot.send_message(int(client_id), f"Заказ №{order_id}\n{status_text}")
        except Exception as e:
            print("Не удалось уведомить клиента:", e)


    # === ОПЛАТА (Telegram Payments / ЮKassa) ===
    def _parse_invoice_payload(payload: str):
        # Ожидаем payload вида: order:<id>
        if not payload:
            return None
        m = re.match(r'^order:(\d+)$', payload.strip())
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    def _get_bot_payment_settings(_bot_id: int | None = None):
        bid = _bot_id if _bot_id is not None else bot_id
        try:
            cur.execute("SELECT payments_enabled, payment_provider_token FROM bots WHERE bot_id=?", (bid,))
            row = cur.fetchone()
        except Exception:
            row = None
        enabled = int(row[0] or 0) if row else 0
        token = (row[1] or '').strip() if row else ''
        if not token:
            token = None
        return {'enabled': enabled, 'provider_token': token}

    async def send_invoice_for_order(order_id: int, uid: int, temp_items: list | None = None) -> bool:
        settings = _get_bot_payment_settings()
        if settings['enabled'] != 1 or not settings['provider_token']:
            return False

        cur.execute("SELECT total, total_before_bonus, bonus_used FROM orders WHERE id=? AND bot_id=?", (order_id, bot_id))
        row = cur.fetchone()
        if not row:
            return False
        total_pay, total_before, bonus_used = (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))

        # Товары для краткого описания
        if temp_items is None:
            cur.execute("SELECT name, quantity, price FROM order_items WHERE order_id=?", (order_id,))
            items = cur.fetchall()
            short = ', '.join([f"{n}×{q}" for n,q,_ in items][:6])
        else:
            short = ', '.join([f"{name}×{qty}" for _, qty, name, _ in temp_items][:6])
        if short:
            short = f"Состав: {short}"
        desc = (short or 'Оплата заказа в Telegram')
        if bonus_used > 0:
            desc = (desc + f". Списано бонусов: {bonus_used}₽.")[:250]
        else:
            desc = desc[:250]

        prices = [LabeledPrice(label='К оплате', amount=total_pay * 100)]
        payload = f'order:{order_id}'
        start_param = f'order_{order_id}'

        try:
            sent = await bot.send_invoice(
                chat_id=uid,
                title=f'Заказ №{order_id}',
                description=desc,
                payload=payload,
                provider_token=settings['provider_token'],
                currency='RUB',
                prices=prices,
                start_parameter=start_param,
            )
            cur.execute("UPDATE orders SET invoice_message_id=?, invoice_sent_at=? WHERE id=? AND bot_id=?",
                        (sent.message_id, int(time.time()), order_id, bot_id))
            conn.commit()
            return True
        except Exception as e:
            print('send_invoice error:', e)
            return False

    async def send_order_to_cafe_by_id(order_id: int):
        cur.execute(
            "SELECT o.user_id, o.total, o.total_before_bonus, o.bonus_used, o.delivery_type, o.comment, o.phone, o.address, "
            "       o.is_paid, o.provider_payment_charge_id, b.notify_chat_id "
            "FROM orders o JOIN bots b ON o.bot_id=b.bot_id WHERE o.id=? AND o.bot_id=?",
            (order_id, bot_id)
        )
        row = cur.fetchone()
        if not row:
            return
        (uid, total_pay, total_before, bonus_used, delivery_type, comment, phone, address, is_paid, provider_charge_id, chat_id) = row
        chat_id = normalize_notify_chat_id(chat_id if chat_id is not None else None)
        if not chat_id:
            return
        cur.execute("SELECT name, quantity, price FROM order_items WHERE order_id=?", (order_id,))
        items = cur.fetchall()
        items_text = '\n'.join([f"• {n} ×{q} — {p*q} ₽" for n,q,p in items]) if items else 'Товары не найдены'
        bonus_line = ''
        if int(bonus_used or 0) > 0:
            bonus_line = f"\nСписано бонусов: {int(bonus_used)} ₽\nК оплате: {int(total_pay)} ₽"
        addr_line = f"\nАдрес: {address}" if delivery_type == 'Доставка' and address else ''
        pay_line = ''
        if int(is_paid or 0) == 1:
            pay_line = '\nОплата: онлайн ✅'
            if provider_charge_id:
                pay_line += f"\nТранзакция ЮKassa: {provider_charge_id}"
        full_text = (
            f"НОВЫЙ ЗАКАЗ №{order_id}\n"
            f"Тип: {delivery_type}\n"
            f"Сумма: {int(total_before)} ₽{bonus_line}{pay_line}\n"
            f"Комментарий клиента: {comment if comment else 'нет'}\n"
            f"Телефон: {phone if phone else 'нет'}{addr_line}\n"
            f"Товары:\n{items_text}\n"
            f"ID: {uid}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='Принять', callback_data=f'order_accept*{order_id}')],
            [InlineKeyboardButton(text='Отменить', callback_data=f'order_cancel*{order_id}')],
        ])
        try:
            sent = await bot.send_message(chat_id=int(chat_id), text=full_text, reply_markup=keyboard)
            cur.execute("UPDATE orders SET cafe_message_id=? WHERE id=? AND bot_id=?", (sent.message_id, order_id, bot_id))
            conn.commit()
        except Exception as e:
            print('Ошибка отправки в кафе:', e)

    @dp.pre_checkout_query()
    async def _pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
        order_id = _parse_invoice_payload(pre_checkout_query.invoice_payload)
        if order_id is None:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message='Некорректный счёт')
            return
        cur.execute("SELECT user_id, total, status, is_paid FROM orders WHERE id=? AND bot_id=?", (order_id, bot_id))
        row = cur.fetchone()
        if not row:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message='Заказ не найден')
            return
        uid, total, status, is_paid = row
        if int(uid) != int(pre_checkout_query.from_user.id):
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message='Этот счёт не для вас')
            return
        if int(is_paid or 0) == 1:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message='Заказ уже оплачен')
            return
        if status != 'awaiting_payment':
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message='Заказ не ожидает оплату')
            return
        expected = int(total or 0) * 100
        if int(pre_checkout_query.total_amount) != expected:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message='Сумма изменилась, создайте заказ заново')
            return
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

    @dp.message(lambda m: getattr(m, 'successful_payment', None) is not None)
    async def _successful_payment(message: types.Message):
        sp = message.successful_payment
        order_id = _parse_invoice_payload(sp.invoice_payload)
        if order_id is None:
            return
        cur.execute("SELECT status, is_paid FROM orders WHERE id=? AND bot_id=?", (order_id, bot_id))
        row = cur.fetchone()
        if not row:
            return
        status, is_paid = row
        if int(is_paid or 0) == 1:
            return
        now_ts = int(time.time())
        cur.execute(
            "UPDATE orders SET is_paid=1, payment_status='paid', paid_amount=?, currency=?, "
            "telegram_payment_charge_id=?, provider_payment_charge_id=?, paid_at=?, status='new' "
            "WHERE id=? AND bot_id=?",
            (int(sp.total_amount), sp.currency, sp.telegram_payment_charge_id, sp.provider_payment_charge_id, now_ts, order_id, bot_id)
        )
        conn.commit()
        # Уведомляем кафе только после успешной оплаты
        await send_order_to_cafe_by_id(order_id)
        await message.answer(f'✅ Оплата прошла! Заказ №{order_id} отправлен в кафе.')
        await show_main_menu(message)

    # /команды из синей кнопки "Меню"
    @dp.message(Command("menu"))
    async def cmd_menu(message: types.Message):
        await show_full_menu(message)

    @dp.message(Command("cart"))
    async def cmd_cart(message: types.Message):
        await show_cart(message)

    @dp.message(Command("status"))
    async def cmd_status(message: types.Message):
        await show_orders_list(message)
    # === БОНУСНАЯ СИСТЕМА: helpers ===
    def _get_bot_bonus_settings(_bot_id: int | None = None):
        """Возвращает настройки бонусов для бота.

        Даёт сразу два набора ключей:
        - канонические (как в базе): bonuses_enabled, bonus_percent, max_bonus_pay_percent, ...
        - legacy-алиасы (как раньше в коде): enabled, percent, max_pay_percent, ...
        """
        bid = _bot_id if _bot_id is not None else bot_id
        cur.execute(
            "SELECT bonuses_enabled, bonus_percent, max_bonus_pay_percent, min_order_for_bonus, bonus_expire_days "
            "FROM bots WHERE bot_id=?",
            (bid,)
        )
        row = cur.fetchone()
        enabled = int(row[0] or 0) if row else 0
        percent = int(row[1] or 0) if row else 0
        max_pay_percent = int(row[2] or 0) if row else 0
        min_order = int(row[3] or 0) if row else 0
        expire_days = int(row[4] or 0) if row else 0

        return {
            # canonical keys
            "bonuses_enabled": enabled,
            "bonus_percent": percent,
            "max_bonus_pay_percent": max_pay_percent,
            "min_order_for_bonus": min_order,
            "bonus_expire_days": expire_days,
            # legacy aliases
            "enabled": enabled,
            "percent": percent,
            "max_pay_percent": max_pay_percent,
            "min_order": min_order,
            "expire_days": expire_days,
        }

    def _ensure_bonus_ledger(uid: int):
        # Если раньше бонусы хранились только в clients.points, а таблица транзакций пустая — мигрируем остаток
        cur.execute("SELECT COUNT(1) FROM bonus_transactions WHERE bot_id=? AND user_id=?", (bot_id, uid))
        cnt = cur.fetchone()[0]
        if cnt == 0:
            cur.execute("SELECT points FROM clients WHERE bot_id=? AND user_id=?", (bot_id, uid))
            r = cur.fetchone()
            if r and (r[0] or 0) > 0:
                now_ts = int(time.time())
                cur.execute(
                    "INSERT INTO bonus_transactions (bot_id, user_id, points, created_at, expires_at, comment) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (bot_id, uid, int(r[0]), now_ts, None, "migrate_balance"),
                )
                conn.commit()

    def get_bonus_balance(uid: int) -> int:
        _ensure_bonus_ledger(uid)
        now_ts = int(time.time())
        cur.execute(
            "SELECT COALESCE(SUM(points), 0) FROM bonus_transactions "
            "WHERE bot_id=? AND user_id=? AND (expires_at IS NULL OR expires_at > ?)",
            (bot_id, uid, now_ts),
        )
        return int(cur.fetchone()[0] or 0)

    def add_bonus_tx(uid: int, points: int, expires_at: int | None, comment: str = ""):
        now_ts = int(time.time())
        cur.execute(
            "INSERT INTO bonus_transactions (bot_id, user_id, points, created_at, expires_at, comment) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, uid, int(points), now_ts, expires_at, comment),
        )
        conn.commit()
        # Держим clients.points как кэш (для быстрого показа/совместимости)
        new_balance = get_bonus_balance(uid)
        cur.execute(
            "UPDATE clients SET points=? WHERE bot_id=? AND user_id=?",
            (new_balance, bot_id, uid),
        )
        conn.commit()

    def refund_bonus_if_needed(order_id: int, reason: str = "refund"):
        cur.execute(
            "SELECT user_id, bonus_used, bonus_refunded FROM orders WHERE id=? AND bot_id=?",
            (order_id, bot_id),
        )
        row = cur.fetchone()
        if not row:
            return
        uid, bonus_used, bonus_refunded = row
        bonus_used = int(bonus_used or 0)
        if bonus_used <= 0:
            return
        if int(bonus_refunded or 0) == 1:
            return
        add_bonus_tx(int(uid), bonus_used, None, f"refund_order_{order_id}:{reason}")
        cur.execute(
            "UPDATE orders SET bonus_refunded=1 WHERE id=? AND bot_id=?",
            (order_id, bot_id),
        )
        conn.commit()

    def accrue_bonus_if_needed(order_id: int):
        settings = _get_bot_bonus_settings()
        if settings["enabled"] != 1 or settings["percent"] <= 0:
            return

        cur.execute(
            "SELECT user_id, total, total_before_bonus, bonus_earned, status "
            "FROM orders WHERE id=? AND bot_id=?",
            (order_id, bot_id),
        )
        row = cur.fetchone()
        if not row:
            return
        uid, total_pay, total_before, bonus_earned, status = row

        if status != "completed":
            return
        if int(bonus_earned or 0) > 0:
            return

        total_before = int(total_before if total_before is not None else (total_pay or 0))
        if total_before < int(settings["min_order"] or 0):
            return

        base = int(total_pay or 0)
        earned = int(base * int(settings["percent"]) / 100)
        if earned <= 0:
            return

        expires_at = None
        if int(settings["expire_days"] or 0) > 0:
            expires_at = int(time.time()) + int(settings["expire_days"]) * 86400

        add_bonus_tx(int(uid), earned, expires_at, f"earn_order_{order_id}")
        cur.execute(
            "UPDATE orders SET bonus_earned=? WHERE id=? AND bot_id=?",
            (earned, order_id, bot_id),
        )
        conn.commit()
    # === ГЛАВНОЕ МЕНЮ ===
    def is_cashier(user_id: int) -> bool:
        cur.execute("SELECT 1 FROM cashiers WHERE bot_id=? AND cashier_id=?", (bot_id, user_id))
        return cur.fetchone() is not None

    def _extract_start_payload(text: str) -> str:
        if not text:
            return ""
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def resolve_client_id_from_code(code: str) -> int | None:
        code = (code or "").strip()
        if not code:
            return None
        # Если прислали ссылку — вытаскиваем start=
        if "start=" in code:
            try:
                code = code.split("start=", 1)[1].split("&", 1)[0]
            except:
                pass
        cur.execute("SELECT user_id FROM clients WHERE bot_id=? AND code=?", (bot_id, code))
        row = cur.fetchone()
        if row:
            try:
                return int(row[0])
            except:
                return None
        if code.startswith("client_") and code[len("client_"):].isdigit():
            return int(code[len("client_"):])
        if code.isdigit():
            return int(code)
        return None

    async def start_cashier_accrual(cashier_uid: int, code: str):
        if not is_cashier(cashier_uid):
            return

        client_uid = resolve_client_id_from_code(code)
        if not client_uid:
            await bot.send_message(
                cashier_uid,
                "Не удалось распознать QR/код клиента. Попросите клиента открыть «Виртуальная карта» и показать новый QR."
            )
            return

        # гарантируем запись клиента
        cur.execute(
            "INSERT OR IGNORE INTO clients (bot_id, user_id, code, points) VALUES (?, ?, ?, 0)",
            (bot_id, client_uid, f"client_{client_uid}")
        )
        conn.commit()

        balance = get_bonus_balance(client_uid)

        user_state[cashier_uid] = {"type": "cashier_op_select", "client_uid": client_uid}

        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начислить бонусы")],
                [KeyboardButton(text="Списать бонусы")],
                [KeyboardButton(text="Отмена")],
            ],
            resize_keyboard=True
        )

        await bot.send_message(
            cashier_uid,
            f"Клиент ID: {client_uid}\nБаланс: {balance} бонусов\n\nВыберите действие:",
            reply_markup=kb
        )

    async def show_main_menu(message_or_callback: types.Message | types.CallbackQuery):
        # Получаем настройку бонусов
        cur.execute("SELECT bonuses_enabled FROM bots WHERE bot_id=?", (bot_id,))
        row = cur.fetchone()
        bonuses_enabled = row[0] if row else 1
        # Базовая клавиатура
        kb_buttons = [
            [KeyboardButton(text="Меню"), KeyboardButton(text="Корзина")],
            [KeyboardButton(text="Статус заказа")],
            [KeyboardButton(text="О нас")]
        ]
        if bonuses_enabled == 1:
            # С бонусами — три ряда
            kb_buttons[1].append(KeyboardButton(text="Виртуальная карта"))
            kb_buttons[2] = [KeyboardButton(text="Мой баланс"), KeyboardButton(text="О нас")]
        else:
            # Без бонусов — два ряда
            kb_buttons = [
                [KeyboardButton(text="Меню"), KeyboardButton(text="Корзина")],
                [KeyboardButton(text="Статус заказа"), KeyboardButton(text="О нас")]
            ]
        uid = message_or_callback.from_user.id
        if is_cashier(uid):
            kb_buttons.append([KeyboardButton(text="Кассир")])

        kb = ReplyKeyboardMarkup(keyboard=kb_buttons, resize_keyboard=True)
        if isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.message.answer("Вы в главном меню", reply_markup=kb)
            await message_or_callback.answer()
        else:
            await message_or_callback.answer("Вы в главном меню", reply_markup=kb)

    async def _start_comment_step(message: types.Message, delivery_type: str, temp_items: list, phone: str, address: str | None, previous_state: dict):
        uid = message.from_user.id

        user_state[uid] = {
            "type": "comment",
            "delivery_type": delivery_type,
            "temp_order_items": temp_items,
            "phone": phone,
            "address": address or "",
            "previous_state": previous_state,
            "awaiting_comment": True
        }

        kb = ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="Без комментария")],
            [KeyboardButton(text="Отмена")]
        ], resize_keyboard=True)

        await message.answer(
            "Добавьте комментарий к заказу (если есть):\nЕсли комментария нет — нажмите «Без комментария»",
            reply_markup=kb
        )


    async def go_next_after_phone(message: types.Message, delivery_type: str, temp_items: list, phone: str, previous_state: dict):
        uid = message.from_user.id

        # Если НЕ доставка — адрес не нужен
        if delivery_type != "Доставка":
            await _start_comment_step(message, delivery_type, temp_items, phone, None, previous_state)
            return

        # Доставка → проверяем сохранённый адрес
        cur.execute("SELECT address FROM clients WHERE bot_id=? AND user_id=?", (bot_id, uid))
        row = cur.fetchone()
        saved_address = row[0] if row and row[0] else None

        if saved_address:
            user_state[uid] = {
                "type": "address_confirm",
                "delivery_type": delivery_type,
                "temp_order_items": temp_items,
                "phone": phone,
                "saved_address": saved_address,
                "previous_state": previous_state,
                "awaiting_address_confirm": True
            }
            kb = ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Использовать сохранённый адрес")],
                [KeyboardButton(text="Указать другой адрес")],
                [KeyboardButton(text="Отмена")]
            ], resize_keyboard=True)

            await message.answer(
                f"Использовать сохранённый адрес доставки:\n{saved_address}",
                reply_markup=kb
            )
            return

        # Адреса нет — просим ввести
        user_state[uid] = {
            "type": "address_input",
            "delivery_type": delivery_type,
            "temp_order_items": temp_items,
            "phone": phone,
            "previous_state": previous_state,
            "awaiting_address_input": True
        }
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
        await message.answer("Укажите адрес доставки:", reply_markup=kb)

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("awaiting_address_confirm"))
    async def address_confirm_step(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        text = (message.text or "").strip()

        if text == "Отмена":
            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}
            if user_state.get(uid, {}).get("type") == "cart_view":
                await show_cart_full_list_and_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        if text == "Использовать сохранённый адрес":
            address = state.get("saved_address", "")
            delivery_type = state.get("delivery_type")
            temp_items = state.get("temp_order_items", [])
            phone = state.get("phone", "")
            prev = state.get("previous_state", {})

            # Переходим к комментарию
            await _start_comment_step(message, delivery_type, temp_items, phone, address, prev)
            return

        if text == "Указать другой адрес":
            # Переходим в ручной ввод
            state.pop("awaiting_address_confirm", None)
            state["type"] = "address_input"
            state["awaiting_address_input"] = True
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
            await message.answer("Укажите адрес доставки:", reply_markup=kb)
            return

        await message.answer("Пожалуйста, выберите вариант кнопками ниже.")

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("awaiting_address_input"))
    async def address_input_step(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        text = (message.text or "").strip()

        if text == "Отмена":
            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}
            if user_state.get(uid, {}).get("type") == "cart_view":
                await show_cart_full_list_and_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        # Простая валидация
        if len(text) < 5:
            await message.answer("Адрес слишком короткий. Введите адрес ещё раз или нажмите «Отмена».")
            return

        address = text
        delivery_type = state.get("delivery_type")
        temp_items = state.get("temp_order_items", [])
        phone = state.get("phone", "")
        prev = state.get("previous_state", {})

        # Сохраняем адрес в clients (создаём запись, если нет)
        cur.execute(
            "INSERT OR IGNORE INTO clients (bot_id, user_id, code, points) VALUES (?, ?, ?, 0)",
            (bot_id, uid, f"client_{uid}")
        )
        cur.execute(
            "UPDATE clients SET address=? WHERE bot_id=? AND user_id=?",
            (address, bot_id, uid)
        )
        conn.commit()

        await _start_comment_step(message, delivery_type, temp_items, phone, address, prev)


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "category_products" and m.text == "Корзина")
    async def go_to_cart_from_category(message: types.Message):
        uid = message.from_user.id
        # Сохраняем состояние категории перед уходом в корзину
        if uid in user_state:
            user_state[uid]["previous_state"] = {
                "type": "category_products",
                "cat_id": user_state[uid].get("cat_id"),
                "prods": user_state[uid].get("prods"),
                "page": user_state[uid].get("page", 0),
                "cat_name": user_state[uid].get("cat_name"),
                "cat_photo_path": user_state[uid].get("cat_photo_path"),
                "back_mode": user_state[uid].get("back_mode"),
                "back_cat_id": user_state[uid].get("back_cat_id"),
                "back_cat_name": user_state[uid].get("back_cat_name"),
                "categories_page": user_state[uid].get("categories_page"),
                "sub_page": user_state[uid].get("sub_page"),
                "parent_page": user_state[uid].get("parent_page"),
            }
        await show_cart(message)
    

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "category_products" and (m.text or "").strip() in [MENU_NAV_PREV, MENU_NAV_NEXT])
    async def category_pagination(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        page = int(state.get("page") or 0)
        pages = int(state.get("pages") or 1)
        t = (message.text or "").strip()
        if t == MENU_NAV_PREV:
            page -= 1
        else:
            page += 1
        page = _clamp_page(page, pages)
        state["page"] = page
        await show_category_products_keyboard(message, page)

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "category_products" and m.text == "Назад")
    async def back_to_categories_from_products(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})

        if st.get("back_mode") == "subsubcategories":
            # Назад к списку подподкатегорий
            await show_subsubcategories_only(
                message,
                int(st.get("back_cat_id") or st.get("cat_id") or 0),
                st.get("back_cat_name") or "Категория",
                st.get("back_cat_photo_path") or st.get("cat_photo_path"),
                parent_subcat_id=int(st.get("parent_subcat_id") or 0),
                parent_sub_name=st.get("parent_sub_name") or "Подкатегория",
                parent_sub_photo_path=st.get("parent_sub_photo_path"),
                page=int(st.get("subsub_page") or 0),
                parent_page=int(st.get("parent_page") or st.get("categories_page") or 0),
                sub_page=int(st.get("sub_page") or 0),
            )
            return

        if st.get("back_mode") == "subcategories":
            cat_id = int(st.get("back_cat_id") or st.get("cat_id") or 0)
            cat_name = st.get("back_cat_name") or "Категория"
            photo_path = st.get("back_cat_photo_path") or st.get("cat_photo_path")

            sub_page = int(st.get("sub_page") or 0)
            parent_page = int(st.get("parent_page") or st.get("categories_page") or 0)

            await show_subcategories_only(
                message,
                cat_id,
                cat_name,
                photo_path,
                page=sub_page,
                parent_page=parent_page,
            )
            return

        # По умолчанию — к списку категорий
        await show_categories_only(message, page=int(st.get("categories_page") or 0))


    #"НА ГЛАВНУЮ"
    @dp.message(lambda m: m.text == "На главную")
    async def go_main_menu(message: types.Message):
        uid = message.from_user.id
        if uid in user_state:
            user_state.pop(uid, None)
        await show_main_menu(message)
# Новая функция для генерации kb (вставь перед process_order_status)
    def generate_order_kb(current_status: str, is_delivery: bool, order_id: int):
        if is_delivery:
            allowed = {"new": ["accept"], "accepted": ["cooking"], "cooking": ["ontheway"], "ontheway": ["complete"]}
            button_texts = {"accept": "Принять", "cooking": "Готовится", "ontheway": "Курьер в пути", "complete": "Заказ выполнен"}
        else:
            allowed = {"new": ["accept"], "accepted": ["cooking"], "cooking": ["ready"], "ready": ["complete"]}
            button_texts = {"accept": "Принять", "cooking": "Готовится", "ready": "Готов к выдаче", "complete": "Заказ выполнен"}
        next_actions = allowed.get(current_status, [])
        rows = []
        for act in next_actions:
            rows.append([InlineKeyboardButton(text=button_texts[act], callback_data=f"order_{act}*{order_id}")])
        if current_status != "completed":
            rows.append([InlineKeyboardButton(text="Отменить", callback_data=f"order_cancel*{order_id}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
# ======== Карточка товара перед добавлением в корзину (выбор количества) ========

    async def show_product_pick_card(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        if state.get("type") != "product_pick":
            return

        prod_id = int(state.get("prod_id") or 0)
        qty = int(state.get("qty") or 1)
        qty = max(1, min(qty, 99))
        state["qty"] = qty

        cur.execute("SELECT name, price, description, photo_path FROM products WHERE id=? AND enabled=1", (prod_id,))
        row = cur.fetchone()
        if not row:
            # Товар удалён/выключен — возвращаемся назад
            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}
            await message.answer("Товар недоступен.")
            if user_state.get(uid, {}).get("type") == "category_products":
                await show_category_products_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        name, price, description, photo_path = row
        description = description or ""
        total = int(price) * qty

        text = f"<b>{name}</b>\n"
        if description:
            text += f"{description}\n\n"
        text += f"Цена: <b>{price} ₽</b>\nКоличество: <b>{qty} шт</b>\nСумма: <b>{total} ₽</b>"

        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="-1"), KeyboardButton(text=f"{qty} шт"), KeyboardButton(text="+1")],
                [KeyboardButton(text="Добавить")],
                [KeyboardButton(text="Назад")],
                [KeyboardButton(text="На главную")],
            ],
            resize_keyboard=True
        )

        if photo_path:
            try:
                await message.answer_photo(FSInputFile(photo_path), caption=text, parse_mode="HTML", reply_markup=kb)
                return
            except Exception as e:
                print("Не удалось отправить фото товара:", e)

        await message.answer(text, parse_mode="HTML", reply_markup=kb)

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "product_pick")
    async def product_pick_handler(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        text = (message.text or "").strip()

        if text == "+1":
            state["qty"] = min(99, int(state.get("qty", 1)) + 1)
            await show_product_pick_card(message)
            return

        if text == "-1":
            state["qty"] = max(1, int(state.get("qty", 1)) - 1)
            await show_product_pick_card(message)
            return

        if text == "Добавить":
            prod_id = int(state.get("prod_id") or 0)
            qty = max(1, int(state.get("qty") or 1))

            # имя — просто для красивого подтверждения
            cur.execute("SELECT name FROM products WHERE id=?", (prod_id,))
            r = cur.fetchone()
            prod_name = r[0] if r else "Товар"

            cur.execute(
                """INSERT INTO cart (bot_id, user_id, prod_id, quantity)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(bot_id, user_id, prod_id)
                   DO UPDATE SET quantity = cart.quantity + EXCLUDED.quantity""",
                (bot_id, uid, prod_id, qty)
            )
            conn.commit()

            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}

            await message.answer(f"✅ Добавлено в корзину: {prod_name} ×{qty}")

            if user_state.get(uid, {}).get("type") == "category_products":
                await show_category_products_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        if text == "Назад":
            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}
            if user_state.get(uid, {}).get("type") == "category_products":
                await show_category_products_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        if text == "На главную":
            user_state.pop(uid, None)
            await show_main_menu(message)
            return

        # Нажатие на "N шт" или любой другой текст — просто игнорируем
        return

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "category_products")
    async def add_product_from_keyboard(message: types.Message):
        uid = message.from_user.id
        state = user_state[uid]
        prods = state["prods"]
        prod_name = (message.text or "").strip()

        # системные кнопки не трогаем
        if prod_name in [MENU_NAV_PREV, MENU_NAV_NEXT, "Назад", "Корзина", "На главную"]:
            return

        # Находим prod_id по имени
        prod_id = next((p[0] for p in prods if p[1] == prod_name), None)
        if not prod_id:
            return

        # Сохраняем ТЕКУЩЕЕ состояние категории, чтобы вернуться на ту же страницу
        prev_state = state.copy()
        prev_state["type"] = "category_products"

        user_state[uid] = {
            "type": "product_pick",
            "prod_id": prod_id,
            "qty": 1,
            "previous_state": prev_state
        }

        await show_product_pick_card(message)

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        uid = message.from_user.id
        payload = _extract_start_payload(message.text or "")
        # Если кассир сканирует QR клиента — прилетает /start <code>
        if payload and is_cashier(uid):
            await start_cashier_accrual(uid, payload)
            return

    
        # Проверяем, есть ли клиент в базе
        cur.execute("SELECT points FROM clients WHERE bot_id=? AND user_id=?", (bot_id, uid))
        if not cur.fetchone():
            # Новый клиент — всегда создаём запись в clients
            cur.execute(
                "INSERT INTO clients (bot_id, user_id, points, code) VALUES (?, ?, ?, ?)",
                (bot_id, uid, 0, f"client_{uid}")
            )
            conn.commit()

            # Приветственный бонус — отдельная логика (только для нового клиента)
            cur.execute("SELECT welcome_bonus, bonuses_enabled FROM bots WHERE bot_id=?", (bot_id,))
            bot_settings = cur.fetchone()
            if bot_settings and bot_settings[1] == 1 and bot_settings[0] > 0:
                welcome = bot_settings[0]
                # начисляем бонус через транзакции (clients.points — это кэш)
                try:
                    add_bonus_tx(uid, welcome, None, comment="welcome")
                except Exception:
                    # на всякий случай, если транзакции отключены/сломаны
                    cur.execute(
                        "UPDATE clients SET points=? WHERE bot_id=? AND user_id=?",
                        (welcome, bot_id, uid)
                    )
                    conn.commit()
                await message.answer(f"🎁 Добро пожаловать! Вам начислено {welcome} приветственных бонусов!")

    
        await show_main_menu(message)

    # === Slash-команды (для синей кнопки «Меню») ===
    @dp.message(Command("menu"))
    async def cmd_menu(message: types.Message):
        # Аналог кнопки «Меню»
        await show_full_menu(message)

    @dp.message(Command("cart"))
    async def cmd_cart(message: types.Message):
        # Аналог кнопки «Корзина»
        await show_cart(message)

    @dp.message(Command("status"))
    async def cmd_status(message: types.Message):
        # Аналог кнопки «Статус заказа»
        await show_orders_list(message)

    # Доставка
    @dp.message(lambda m: m.text == "Кассир")
    async def cashier_menu(message: types.Message):
        uid = message.from_user.id
        if not is_cashier(uid):
            return
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="На главную")]], resize_keyboard=True)
        await message.answer(
            "🧾 Режим кассира\n\n"
            "Чтобы начислить бонусы офлайн:\n"
            "1) Клиент открывает «Виртуальная карта» и показывает QR.\n"
            "2) Вы сканируете QR камерой телефона — откроется этот бот.\n"
            "3) Нажимаете «Старт» — бот попросит сумму покупки.\n\n"
            "Если QR открывается ссылкой — просто откройте её в Telegram.",
            reply_markup=kb
        )

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cashier_accrual")
    async def cashier_accrual_amount(message: types.Message):
        cashier_uid = message.from_user.id
        if not is_cashier(cashier_uid):
            user_state.pop(cashier_uid, None)
            return
        text = (message.text or "").strip()
        if text == "Отмена":
            user_state.pop(cashier_uid, None)
            await show_main_menu(message)
            return
        cleaned = text.replace(" ", "").replace("₽", "").replace(",", "")
        try:
            amount = int(cleaned)
        except:
            await message.answer("Введите сумму числом, например: 1200")
            return
        if amount <= 0:
            await message.answer("Сумма должна быть больше 0.")
            return
        # берём настройки бонусов конкретного бота
        settings = _get_bot_bonus_settings()
        if settings.get("enabled", 1) != 1:
            user_state.pop(cashier_uid, None)
            await message.answer("Бонусная система выключена для этого бота.")
            await show_main_menu(message)
            return
        if amount < settings.get("min_order", 0):
            user_state.pop(cashier_uid, None)
            await message.answer(f"Сумма меньше минималки для начисления бонусов ({settings.get('min_order', 0)} ₽).")
            await show_main_menu(message)
            return
        percent = settings.get("percent", 10)
        points = int(amount * percent / 100)
        if points <= 0:
            user_state.pop(cashier_uid, None)
            await message.answer("По этим настройкам бонусы не начисляются для такой суммы.")
            await show_main_menu(message)
            return
        client_uid = user_state[cashier_uid]["client_uid"]
        expire_days = settings.get("expire_days", 0)
        expires_at = int(time.time()) + expire_days * 86400 if expire_days and expire_days > 0 else None
        # add_bonus_tx — синхронная функция (пишет в sqlite), await тут не нужен
        add_bonus_tx(client_uid, points, expires_at, f"offline:{amount}:cashier:{cashier_uid}")
        balance = get_bonus_balance(client_uid)
        try:
            await bot.send_message(int(client_uid), f"🎁 Начислено {points} бонусов за покупку {amount} ₽.\nБаланс: {balance}")
        except:
            pass
        user_state.pop(cashier_uid, None)
        await message.answer(f"✅ Начислено: {points} бонусов\nКлиент: {client_uid}\nНовый баланс: {balance}")
        await show_main_menu(message)

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cashier_op_select")
    async def cashier_choose_op(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        client_uid = state.get("client_uid")
        text = (message.text or "").strip()

        if text == "Отмена":
            user_state.pop(uid, None)
            await cashier_menu(message)
            return

        if text == "Начислить бонусы":
            user_state[uid] = {"type": "cashier_accrual", "client_uid": client_uid}
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
            await message.answer(f"Введите сумму покупки (₽). Клиент ID: {client_uid}", reply_markup=kb)
            return

        if text == "Списать бонусы":
            settings = _get_bot_bonus_settings()
            if settings.get("bonuses_enabled", 1) != 1:
                await message.answer("Бонусная система выключена в настройках.")
                user_state.pop(uid, None)
                await cashier_menu(message)
                return

            balance = get_bonus_balance(client_uid)
            if balance <= 0:
                await message.answer(f"У клиента нет бонусов для списания. Баланс: {balance}.")
                return

            user_state[uid] = {"type": "cashier_writeoff_purchase", "client_uid": client_uid}
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
            await message.answer(
                f"Введите сумму покупки (₽), чтобы рассчитать лимит списания.\nБаланс клиента: {balance} бонусов.",
                reply_markup=kb
            )
            return

        await message.answer("Выберите действие кнопками ниже.")

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cashier_writeoff_purchase")
    async def cashier_writeoff_purchase(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        client_uid = state.get("client_uid")
        text = (message.text or "").strip()

        if text == "Отмена":
            user_state.pop(uid, None)
            await cashier_menu(message)
            return

        try:
            purchase_amount = int(float(text.replace(",", ".")))
            if purchase_amount <= 0:
                raise ValueError
        except:
            await message.answer("Введите сумму покупки числом, например: 1200")
            return

        settings = _get_bot_bonus_settings()
        max_pct = int((settings.get("max_bonus_pay_percent") if settings.get("max_bonus_pay_percent") is not None else settings.get("max_pay_percent", 0)) or 0)

        balance = get_bonus_balance(client_uid)
        if max_pct <= 0:
            await message.answer("Списание бонусами запрещено (лимит оплаты бонусами = 0%).")
            user_state[uid] = {"type": "cashier_op_select", "client_uid": client_uid}
            return

        max_by_percent = int(purchase_amount * max_pct / 100)
        max_allowed = min(balance, max_by_percent)

        if max_allowed <= 0:
            await message.answer(
                f"Списать нельзя: баланс {balance}, лимит {max_pct}% от суммы покупки.\n"
                f"Максимум для этой покупки: {max_allowed}."
            )
            user_state[uid] = {"type": "cashier_op_select", "client_uid": client_uid}
            return

        user_state[uid] = {
            "type": "cashier_writeoff_amount",
            "client_uid": client_uid,
            "purchase_amount": purchase_amount,
            "max_bonus": max_allowed
        }

        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Максимум")],
                [KeyboardButton(text="Отмена")]
            ],
            resize_keyboard=True
        )

        await message.answer(
            f"Сколько бонусов списать?\n"
            f"Баланс: {balance}\n"
            f"Лимит: {max_pct}% → максимум {max_allowed} бонусов\n\n"
            f"Отправьте число или нажмите «Максимум».",
            reply_markup=kb
        )

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cashier_writeoff_amount")
    async def cashier_writeoff_amount(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        client_uid = state.get("client_uid")
        purchase_amount = int(state.get("purchase_amount", 0))
        max_bonus = int(state.get("max_bonus", 0))
        text = (message.text or "").strip()

        if text == "Отмена":
            user_state.pop(uid, None)
            await cashier_menu(message)
            return

        if text == "Максимум":
            spend = max_bonus
        else:
            try:
                spend = int(text)
            except:
                await message.answer("Введите число бонусов, например: 300 (или нажмите «Максимум»).")
                return

        if spend <= 0 or spend > max_bonus:
            await message.answer(f"Нужно число от 1 до {max_bonus}.")
            return

        # 1) списываем бонусы (минус)
        ref = f"offline_spend:{purchase_amount}:{uid}:{int(time.time())}"
        add_bonus_tx(client_uid, -spend, None, ref)

        to_pay = max(0, purchase_amount - spend)  # сколько оплатил деньгами

        # 2) начисляем бонусы от суммы, которую оплатил (to_pay)
        earned = 0
        settings = _get_bot_bonus_settings()
        if settings.get("enabled", 1) == 1 and int(settings.get("percent", 0) or 0) > 0:
            # минималка — как и онлайн: проверяем по сумме ДО списания
            if purchase_amount >= int(settings.get("min_order", 0) or 0):
                earned = int(to_pay * int(settings.get("percent", 0)) / 100)
                if earned > 0:
                    expires_at = None
                    if int(settings.get("expire_days", 0) or 0) > 0:
                        expires_at = int(time.time()) + int(settings.get("expire_days")) * 86400
                    add_bonus_tx(
                        client_uid,
                        earned,
                        expires_at,
                        f"offline_earn:purchase{purchase_amount}:spend{spend}:paid{to_pay}:cashier{uid}"
                    )

        final_balance = get_bonus_balance(client_uid)

        # уведомляем клиента
        try:
            msg = (
                f"💸 Списано {spend} бонусов за покупку {purchase_amount} ₽.\n"
                f"К оплате: {to_pay} ₽.\n"
            )
            if earned > 0:
                msg += f"🎁 Начислено {earned} бонусов от {to_pay} ₽.\n"
            msg += f"Баланс: {final_balance} бонусов"
            await bot.send_message(int(client_uid), msg)
        except Exception as e:
            print('Не удалось уведомить клиента о списании/начислении:', e)

        user_state.pop(uid, None)

        # ответ кассиру
        text_out = (
            f"✅ Списано {spend} бонусов\n"
            f"К оплате: {to_pay} ₽\n"
        )
        if earned > 0:
            text_out += f"🎁 Начислено клиенту: {earned} бонусов (от {to_pay} ₽)\n"
        text_out += f"Баланс клиента: {final_balance} бонусов"

        await message.answer(text_out)
        await cashier_menu(message)

    @dp.message(lambda m: m.text == "Статус заказа")
    async def show_orders_list(message: types.Message):
        uid = message.from_user.id
        cur.execute("""SELECT id, created_at, total, status, delivery_type
                    FROM orders
                    WHERE bot_id = ? AND user_id = ?
                    ORDER BY created_at DESC""", (bot_id, uid))
        orders = cur.fetchall()
        if not orders:
            await message.answer("У вас пока нет заказов.",
                            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="На главную")]], resize_keyboard=True))
            return
        user_state[uid] = {"type": "orders", "orders_list": orders, "index": 0}
        await show_order_detail(message, orders, 0)
    async def show_order_detail(message: types.Message, orders: list, index: int):
        uid = message.from_user.id
        order_id, created_at, total, status, delivery_type = orders[index]
        date = time.strftime("%d.%m.%Y %H:%M", time.localtime(created_at))
        cur.execute("""SELECT name, quantity, price FROM order_items WHERE order_id = ?""", (order_id,))
        items = cur.fetchall()
        status_emojis = {
            "new": "Новый",
            "accepted": "Принят",
            "cooking": "Готовится",
            "ready": "Готов к выдаче",
            "ontheway": "Курьер в пути",
            "completed": "Выполнен",
            "cancelled": "Отменён",
            "awaiting_payment": "Ожидает оплату"
        }
        status_text = status_emojis.get(status, "Неизвестно")
        items_text = "\n".join([f"• {name} ×{qty} — {price*qty} ₽" for name, qty, price in items]) if items else "Товары не найдены"
        text = f"""
<b>Заказ №{order_id}</b>
{date} | {delivery_type}
Сумма: <b>{total} ₽</b>
Статус: <b>{status_text}</b>
{items_text}
        """.strip()
        # ← ВОТ ГЛАВНОЕ ИСПРАВЛЕНИЕ: все кнопки через KeyboardButton!
        keyboard = []

        # Навигация: стрелки + счётчик (как на скрине: ◀ 1/5 ▶)
        total_orders = len(orders)
        keyboard.append([
            KeyboardButton(text="⬅️"),
            KeyboardButton(text=f"{index + 1}/{total_orders}"),
            KeyboardButton(text="➡️")
        ])

        # Оплата (если заказ ждёт оплату)
        if status == "awaiting_payment":
            keyboard.append([KeyboardButton(text="Оплатить")])

        # Всегда кнопка домой
        keyboard.append([KeyboardButton(text="На главную")])

        # Кнопка отмены для клиента — оставляем снизу
        if status in ["new", "awaiting_payment"]:
            keyboard.append([KeyboardButton(text="Отменить заказ")])

        kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    # === ЛИСТАНИЕ ЗАКАЗОВ + ОТМЕНА СО СТОРОНЫ КЛИЕНТА ===
    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "orders" and (
    (m.text or "").strip() in ["⬅️", "➡️", "Предыдущий", "Следующий", "На главную", "Отменить заказ", "Оплатить"]
    or re.fullmatch(r"\d+/\d+", (m.text or "").strip())
))
    async def navigate_orders(message: types.Message):
        uid = message.from_user.id
        state = user_state[uid]
        orders = state["orders_list"]
        index = state["index"]
        old_index = index
        t = (message.text or "").strip()
        # Нажатие на счётчик (например 2/5) — ничего не делаем
        if re.fullmatch(r"\d+/\d+", t):
            return
        if t in ("⬅️", "Предыдущий"):
            index = max(0, index - 1)
        elif t in ("➡️", "Следующий"):
            index = min(len(orders) - 1, index + 1)
        # Если нажали стрелку на границе списка — просто игнорируем
        if t in ("⬅️", "➡️", "Предыдущий", "Следующий") and index == old_index:
            return
        elif t == "На главную":
            user_state.pop(uid, None)
            await show_main_menu(message)
            return
        elif t == "Оплатить":
            order_id = orders[index][0]
            ok = await send_invoice_for_order(order_id, uid)
            if not ok:
                await message.answer("Оплата сейчас недоступна.")
            return
        elif t == "Отменить заказ":
            order_id = orders[index][0]

            # Проверяем актуальный статус (если сотрудники уже приняли — отмена недоступна)
            cur.execute("SELECT status FROM orders WHERE id = ? AND user_id = ? AND bot_id = ?", (order_id, uid, bot_id))
            row = cur.fetchone()
            current_status = row[0] if row else None
            if current_status and current_status not in ("new", "awaiting_payment"):
                # Обновим статус в локальном списке, чтобы UI показал правду
                try:
                    oid, created_at, total, _old_status, delivery_type = orders[index]
                    orders[index] = (oid, created_at, total, current_status, delivery_type)
                except Exception:
                    pass
                await message.answer("Заказ уже принят заведением — отмена недоступна. Если нужно, свяжитесь с заведением.")
                await show_order_detail(message, orders, index)
                return

            # Запоминаем, что ждём подтверждения отмены
            user_state[uid]["awaiting_cancel_confirm"] = order_id
            kb = ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Да, отменить заказ")],
                [KeyboardButton(text="Нет, оставить")],
                [KeyboardButton(text="На главную")]
            ], resize_keyboard=True)
            await message.answer("Вы уверены, что хотите отменить заказ?", reply_markup=kb)
            return
        state["index"] = index
        await show_order_detail(message, orders, index)
    # === ФИНАЛЬНАЯ ОТМЕНА ПОСЛЕ ВЫБОРА ПРИЧИНЫ (ПЕРВЫЙ ОБРАБОТЧИК!) ===
    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("awaiting_cancel_reason") is not None)
    async def client_cancel_with_reason(message: types.Message):
        uid = message.from_user.id
        order_id = user_state[uid]["awaiting_cancel_reason"]
        reason = message.text.strip()
        user_state.pop(uid, None) # Чистим состояние
        if reason == "Назад":
            user_state[uid] = {"awaiting_cancel_confirm": order_id}
            kb = ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Да, отменить заказ")],
                [KeyboardButton(text="Нет, оставить")],
                [KeyboardButton(text="На главную")]
            ], resize_keyboard=True)
            await message.answer("Вы уверены, что хотите отменить заказ?", reply_markup=kb)
            return
        # Отмена заказа
        cur.execute("UPDATE orders SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status IN ('new', 'awaiting_payment')", (order_id, uid))
        if cur.rowcount > 0:
            conn.commit()
            refund_bonus_if_needed(order_id, "client_cancel")
            # Уведомление сотрудникам с причиной
            cur.execute("""SELECT o.cafe_message_id, b.notify_chat_id, o.total, o.delivery_type
                        FROM orders o JOIN bots b ON o.bot_id = b.bot_id WHERE o.id = ?""", (order_id,))
            row = cur.fetchone()
            if row and row[0] and row[1]:
                notify_chat = normalize_notify_chat_id(str(row[1]))
                try:
                    items_text = ""
                    cur.execute("SELECT name, quantity, price FROM order_items WHERE order_id = ?", (order_id,))
                    for n, q, p in cur.fetchall():
                        items_text += f"• {n} ×{q} — {p*q} ₽\n"
                    await bot.edit_message_text(
                        chat_id=int(notify_chat),
                        message_id=row[0],
                        text=f"Заказ №{order_id} — ОТМЕНЁН КЛИЕНТОМ\nПричина: {reason}\nТип: {row[3]} | Сумма: {row[2]} ₽\n\n{items_text}Клиент отменил заказ❌",
                        reply_markup=None
                    )
                except: pass
                try:
                    await bot.send_message(int(notify_chat), f"ОТМЕНА №{order_id}\nПричина: {reason}❌")
                except: pass
            await message.answer(
                f"Заказ №{order_id} отменён❌\nПричина: {reason}\nСпасибо за обратную связь!",
                reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="На главную")]], resize_keyboard=True)
            )
        else:
            await message.answer("Заказ уже нельзя отменить.", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="На главную")]], resize_keyboard=True))
    # === ПОДТВЕРЖДЕНИЕ ОТМЕНЫ (ВТОРОЙ ОБРАБОТЧИК) ===
    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("awaiting_cancel_confirm") is not None)
    async def client_cancel_confirm(message: types.Message):
        uid = message.from_user.id
        order_id = user_state[uid]["awaiting_cancel_confirm"]
        if message.text == "Да, отменить заказ":
            # Если заказ уже принят заведением — отменять нельзя
            cur.execute("SELECT status FROM orders WHERE id = ? AND user_id = ? AND bot_id = ?", (order_id, uid, bot_id))
            row = cur.fetchone()
            current_status = row[0] if row else None
            if current_status and current_status not in ("new", "awaiting_payment"):
                # Сбрасываем только ожидание отмены, оставляя экран заказов
                try:
                    user_state[uid].pop("awaiting_cancel_confirm", None)
                except Exception:
                    pass
                # Попробуем показать обновлённую карточку заказа
                st = user_state.get(uid, {})
                if st.get("type") == "orders" and st.get("orders_list") is not None:
                    orders = st["orders_list"]
                    idx = st.get("index", 0)
                    try:
                        # обновим статус в списке
                        for i, o in enumerate(orders):
                            if o[0] == order_id:
                                oid, created_at, total, _old_status, delivery_type = o
                                orders[i] = (oid, created_at, total, current_status, delivery_type)
                                break
                    except Exception:
                        pass
                    await message.answer("Заказ уже принят заведением — отмена недоступна.")
                    await show_order_detail(message, orders, idx)
                else:
                    await message.answer("Заказ уже принят заведением — отмена недоступна.", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="На главную")] ], resize_keyboard=True))
                return
            user_state[uid]["awaiting_cancel_reason"] = order_id
            kb = ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Назад")],
                [KeyboardButton(text="Передумал")],
                [KeyboardButton(text="Ошибка в заказе")],
                [KeyboardButton(text="Другая причина")]
            ], resize_keyboard=True)
            await message.answer("Укажите причину отмены:", reply_markup=kb)
            return
        if message.text in ["Нет, оставить", "На главную"]:
            user_state.pop(uid, None)
            await show_main_menu(message)
            return
        # Просто игнорируем другие сообщения
        return
    # === КОРЗИНА (с пролистыванием, +1/-1, удалить) ===
    @dp.message(lambda m: m.text == "Корзина")
    async def show_cart(message: types.Message):
        uid = message.from_user.id
    
        # ИНИЦИАЛИЗИРУЕМ СЛОВАРЬ ДЛЯ ПОЛЬЗОВАТЕЛЯ, ЕСЛИ ЕГО НЕТ
        if uid not in user_state:
            user_state[uid] = {}
    
        # 1. Сохраняем текущее состояние как предыдущее
        current_state = user_state[uid].copy() # теперь безопасно, словарь существует
        if current_state:
            user_state[uid]["previous_state"] = current_state
        else:
            user_state[uid]["previous_state"] = {"from_main_menu": True}
    
        # 2. Загружаем товары из корзины
        cur.execute("""SELECT c.prod_id, c.quantity, p.name, p.price
                    FROM cart c JOIN products p ON c.prod_id = p.id
                    WHERE c.bot_id = ? AND c.user_id = ? ORDER BY c.prod_id""", (bot_id, uid))
        items = cur.fetchall()
    
        if not items:
            # фикс: задаём отдельный тип состояния, чтобы "Назад" отрабатывал
            user_state[uid] = {
                "type": "cart_empty",
                "previous_state": user_state[uid].get("previous_state")
            }

            await message.answer(
                "Ваша корзина пуста!",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="Назад")]],
                    resize_keyboard=True
                )
            )
            return
    
        # 3. УСТАНАВЛИВАЕМ СОСТОЯНИЕ КОРЗИНЫ
        user_state[uid] = {
            "type": "cart_view",
            "items": [(row[0], row[1], row[2], row[3]) for row in items],
            "page": 0,
            "previous_state": user_state[uid].get("previous_state")
        }
    
        # 4. Показываем корзину
        await show_cart_full_list_and_keyboard(message, 0)
    async def show_cart_full_list_and_keyboard(message: types.Message, page: int):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        if state.get("type") != "cart_view":
            return
    
        items = state["items"] # (prod_id, quantity, name, price)
        total_sum = sum(qty * price for _, qty, _, price in items)
    
        # Формируем полный список для сообщения
        list_text = ""
        for _, qty, name, price in items:
            list_text += f"• {name} × {qty} — {price * qty} ₽\n"
        full_text = f"<b>Ваша корзина:</b>\n\n{list_text}\n<b>Итого: {total_sum} ₽</b>"
    
        # Клавиатура с товарами (по 2 в ряд, до 6)
        per_page = 6
        start = page * per_page
        end = start + per_page
        current_items = items[start:end]
    
        keyboard = []
        for i in range(0, len(current_items), 2):
            row = [KeyboardButton(text=current_items[i][2])] # имя товара
            if i + 1 < len(current_items):
                row.append(KeyboardButton(text=current_items[i+1][2]))
            keyboard.append(row)
    
        # Нижняя строка: пагинация + "Назад" + "Заказать"
        nav_row = []
        if page > 0:
            nav_row.append(KeyboardButton(text="⬅️"))
        nav_row.append(KeyboardButton(text="Назад"))
        nav_row.append(KeyboardButton(text="Заказать"))
        if end < len(items):
            nav_row.append(KeyboardButton(text="➡️"))
        keyboard.append(nav_row)
    
        kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
        await message.answer(full_text, parse_mode="HTML", reply_markup=kb)
        # Состояние для карточки товара в корзине
    async def show_cart_product_card(message: types.Message, items: list, index: int):
        uid = message.from_user.id
        prod_id, qty, name, price = items[index]
    
        # Получаем полную инфу о товаре (фото, описание)
        cur.execute("""SELECT p.photo_path, p.description
                    FROM products p WHERE p.id = ?""", (prod_id,))
        row = cur.fetchone()
        photo_path = row[0] if row else None
        description = row[1] if row and row[1] else ""
    
        total_price = price * qty
        total_sum = sum(quantity * price for prod_id, quantity, name, price in items)
    
        text = f"<b>{name}</b>\n"
        if description:
            text += f"{description}\n\n"
        text += f"Цена: {price} ₽ × {qty} = <b>{total_price} ₽</b>\n\n"
        text += f"Товар {index + 1} из {len(items)}\nОбщая сумма: <b>{total_sum} ₽</b>"
    
        nav = []
        if index > 0:
            nav.append(KeyboardButton(text="Предыдущий"))
        if index < len(items) - 1:
            nav.append(KeyboardButton(text="Следующий"))
    
        kb = ReplyKeyboardMarkup(keyboard=[
            nav if nav else [],
            [KeyboardButton(text="-1"), KeyboardButton(text=f"{qty} шт"), KeyboardButton(text="+1")],
            [KeyboardButton(text="Удалить")],
            [KeyboardButton(text="Назад в корзину")]
        ], resize_keyboard=True)
    
        if photo_path:
            await message.answer_photo(FSInputFile(photo_path), caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
    
        # Сохраняем индекс для навигации
        user_state[uid]["cart_item_index"] = index
    # Навигация и действия в карточке товара
    SYSTEM_BTNS = {
    "⬅️", "➡️", "Назад", "Заказать",
    "Назад в корзину", "Предыдущий", "Следующий",
    "+1", "-1", "Удалить", "На главную"
    }
    @dp.message(lambda m: (
    user_state.get(m.from_user.id, {}).get("type") == "cart_view"
    and user_state.get(m.from_user.id, {}).get("cart_item_index") is None
    and (m.text or "").strip() not in SYSTEM_BTNS
    ))
    async def open_cart_item_from_list(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        items = state.get("items", [])
        text = (message.text or "").strip()

        if text in SYSTEM_BTNS:
            return

        index = next((i for i, (_, _, name, _) in enumerate(items) if name == text), None)
        if index is None:
            return

        state["cart_item_index"] = index
        await show_cart_product_card(message, items, index)
    @dp.message(lambda m: (
        user_state.get(m.from_user.id, {}).get("type") == "cart_view"
        and user_state.get(m.from_user.id, {}).get("cart_item_index") is not None
    ))
    async def cart_item_navigation(message: types.Message):
        uid = message.from_user.id
        state = user_state[uid]
        items = state["items"]
        index = state["cart_item_index"]

        text = (message.text or "").strip()

        # ✅ Назад из карточки в список корзины
        if text in ["Назад в корзину", "Назад"]:
            state.pop("cart_item_index", None)
            await show_cart_full_list_and_keyboard(message, state.get("page", 0))
            return

        # ✅ Листание товаров в карточке
        if text == "Предыдущий":
            index = max(0, index - 1)
            state["cart_item_index"] = index
            await show_cart_product_card(message, items, index)
            return

        if text == "Следующий":
            index = min(len(items) - 1, index + 1)
            state["cart_item_index"] = index
            await show_cart_product_card(message, items, index)
            return

        prod_id = items[index][0]

        # ✅ Изменение количества / удаление
        if text == "+1":
            items[index] = (prod_id, items[index][1] + 1, items[index][2], items[index][3])
            cur.execute("UPDATE cart SET quantity = quantity + 1 WHERE bot_id=? AND user_id=? AND prod_id=?",
                        (bot_id, uid, prod_id))
            conn.commit()
            await show_cart_product_card(message, items, index)
            return

        if text == "-1":
            new_qty = max(1, items[index][1] - 1)
            items[index] = (prod_id, new_qty, items[index][2], items[index][3])
            cur.execute("UPDATE cart SET quantity = ? WHERE bot_id=? AND user_id=? AND prod_id=?",
                        (new_qty, bot_id, uid, prod_id))
            conn.commit()
            await show_cart_product_card(message, items, index)
            return

        if text == "Удалить":
            cur.execute("DELETE FROM cart WHERE bot_id=? AND user_id=? AND prod_id=?",
                        (bot_id, uid, prod_id))
            conn.commit()
            del items[index]

            if not items:
                user_state.pop(uid, None)
                await message.answer(
                    "Корзина очищена!",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[[KeyboardButton(text="На главную")]],
                        resize_keyboard=True
                    )
                )
                return

            index = min(index, len(items) - 1)
            state["cart_item_index"] = index
            await show_cart_product_card(message, items, index)
            return


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cart_view" and m.text in ["⬅️", "➡️"])
    async def cart_pagination(message: types.Message):
        uid = message.from_user.id
        state = user_state[uid]
        page = state["page"]
        if message.text == "⬅️":
            page = max(0, page - 1)
        elif message.text == "➡️":
            page += 1
        state["page"] = page
        await show_cart_full_list_and_keyboard(message, page)
    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cart_view" and m.text == "Назад")
    async def back_from_cart(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})

        previous = state.get("previous_state")
        if previous:
            # Восстанавливаем предыдущее состояние
            ptype = previous.get("type")
            if ptype == "category_products":
                user_state[uid] = previous
                await show_category_products_keyboard(message, previous.get("page", 0))
                return

            if ptype == "subcategories":
                # Возврат к списку подкатегорий
                cat_id = int(previous.get("cat_id") or 0)
                cat_name = previous.get("cat_name") or "Категория"
                photo_path = previous.get("cat_photo_path")
                await show_subcategories_only(message, cat_id, cat_name, photo_path, page=int(previous.get("page", 0) or 0), parent_page=int(previous.get("parent_page", 0) or 0))
                return

            if ptype == "categories":
                # Возврат к списку категорий
                await show_categories_only(message, page=int(previous.get("page", 0) or 0))
                return

        # Если предыдущего состояния нет или оно главное меню — идём в главное
        user_state.pop(uid, None)
        await show_main_menu(message)

    async def ask_delivery_type(message: types.Message):
        uid = message.from_user.id
    
        # === ПРОВЕРКА ВРЕМЕНИ РАБОТЫ ===
        cur.execute("""SELECT restrict_orders, timezone, work_start, work_end
                    FROM bots WHERE bot_id = ?""", (bot_id,))
        bot_settings = cur.fetchone()
        if bot_settings and bot_settings[0] == 1: # если ограничение включено
            restrict, tz_name, start_str, end_str = bot_settings
            if start_str and end_str:
                blocked = False
                try:
                    from zoneinfo import ZoneInfo
                    import datetime
                
                    tz = ZoneInfo(tz_name)
                    now = datetime.datetime.now(tz)
                    current_time = now.time()
                
                    start_time = datetime.datetime.strptime(start_str, "%H:%M").time()
                    end_time = datetime.datetime.strptime(end_str, "%H:%M").time()
                
                    if not (start_time <= current_time <= end_time):
                        blocked = True
                except Exception as e:
                    print("Ошибка проверки времени (игнорируем):", e)
                    blocked = False
            
                if blocked:
                    tz_display = tz_name.split("/")[-1].replace("*", " ")
                    await message.answer(
                        f"Извините, мы сейчас не принимаем заказы 😔\n"
                        f"Работаем с {start_str} по {end_str} ({tz_display})\n"
                        f"Ждём вас в рабочее время!"
                    )
                    return
    
        # === ДОСТУПНЫЕ СПОСОБЫ ПОЛУЧЕНИЯ ===
        cur.execute("""SELECT allow_in_hall, allow_takeaway, allow_delivery, COALESCE(min_order_total, 0)
                    FROM bots WHERE bot_id = ?""", (bot_id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Ошибка настроек бота")
            return
        allow_hall, allow_takeaway, allow_delivery, min_order_total = row
        min_order_total = int(min_order_total or 0)
    
        # Берём товары из корзины
        cur.execute("""SELECT c.prod_id, c.quantity, p.name, p.price
                       FROM cart c JOIN products p ON c.prod_id = p.id
                       WHERE c.bot_id=? AND c.user_id=?""", (bot_id, uid))
        items = cur.fetchall()
        if not items:
            await message.answer("Корзина пуста!")
            user_state.pop(uid, None)
            await show_main_menu(message)
            return

        # Считаем сумму здесь
        total = sum(qty * price for _, qty, _, price in items)

        # === МИНИМАЛЬНАЯ СУММА ЗАКАЗА ===
        if min_order_total > 0 and total < min_order_total:
            diff = min_order_total - total
            await message.answer(
                f"Минимальная сумма заказа — {min_order_total} ₽.\n"
                f"Сейчас в корзине на {total} ₽.\n"
                f"Добавьте ещё на {diff} ₽ и попробуйте снова 🙂"
            )
            return

        # Сохраняем в состояние
        if uid not in user_state:
            user_state[uid] = {}
        # сохраняем товары и ПЕРЕКЛЮЧАЕМ режим, чтобы корзина не мешала
        prev = user_state.get(uid, {}).copy()
        user_state[uid] = {
            "type": "delivery_type",
            "temp_order_items": items,
            "previous_state": prev
        }

        # Клавиатура со способами
        buttons = []
        if allow_hall:
            buttons.append([KeyboardButton(text="В зале")])
        if allow_takeaway:
            buttons.append([KeyboardButton(text="Самовывоз")])
        if allow_delivery:
            buttons.append([KeyboardButton(text="Доставка курьером")])
        if not buttons:
            await message.answer("Извините, заказы временно недоступны.")
            return

        buttons.append([KeyboardButton(text="Отмена")])
        kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

        await message.answer(
            f"Общая сумма: {total} ₽\n\nВыберите способ получения:",
            reply_markup=kb
        )

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cart_empty" and (m.text or "").strip() == "Назад")
    async def back_from_empty_cart(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        previous = state.get("previous_state")

        if previous:
            ptype = previous.get("type")
            if ptype == "category_products":
                user_state[uid] = previous
                await show_category_products_keyboard(message, previous.get("page", 0))
                return

            if ptype == "subcategories":
                cat_id = int(previous.get("cat_id") or 0)
                cat_name = previous.get("cat_name") or "Категория"
                photo_path = previous.get("cat_photo_path")
                await show_subcategories_only(message, cat_id, cat_name, photo_path, page=int(previous.get("page", 0) or 0), parent_page=int(previous.get("parent_page", 0) or 0))
                return

            if ptype == "categories":
                await show_categories_only(message, page=int(previous.get("page", 0) or 0))
                return

        user_state.pop(uid, None)
        await show_main_menu(message)


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "cart_view" and m.text == "Заказать")
    async def order_from_cart(message: types.Message):
        await ask_delivery_type(message)

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "delivery_type")
    async def process_delivery_type(message: types.Message):
        uid = message.from_user.id
        choice = (message.text or "").strip()

        if choice == "Отмена":
            prev = user_state.get(uid, {}).get("previous_state", {})
            user_state[uid] = prev if prev else {}
            if user_state.get(uid, {}).get("type") == "cart_view":
                await show_cart_full_list_and_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        if choice == "Доставка курьером":
            choice = "Доставка"

        if choice not in ["В зале", "Самовывоз", "Доставка"]:
            await message.answer("Пожалуйста, выберите один из вариантов ниже.")
            return

        temp_items = user_state.get(uid, {}).get("temp_order_items", [])
        if not temp_items:
            await message.answer("Корзина пуста!")
            await show_main_menu(message)
            user_state.pop(uid, None)
            return

        # Проверяем, есть ли сохранённый телефон
        cur.execute("SELECT phone FROM clients WHERE bot_id=? AND user_id=?", (bot_id, uid))
        row = cur.fetchone()
        saved_phone = row[0] if row and row[0] else None

        if saved_phone:
            user_state[uid] = {
                "type": "phone_confirm",
                "delivery_type": choice,
                "temp_order_items": temp_items,
                "phone": saved_phone,
                "previous_state": user_state.get(uid, {}).get("previous_state", {})
            }
            kb = ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Использовать сохранённый")],
                [KeyboardButton(text="Указать другой")],
                [KeyboardButton(text="Отмена")]
            ], resize_keyboard=True)
            await message.answer(
                f"Использовать сохранённый номер для связи: {saved_phone}?",
                reply_markup=kb
            )
        else:
            user_state[uid] = {
                "type": "phone_request",
                "delivery_type": choice,
                "temp_order_items": temp_items,
                "previous_state": user_state.get(uid, {}).get("previous_state", {})
            }
            kb = ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Отправить контакт", request_contact=True)],
                [KeyboardButton(text="Ввести номер вручную")],
                [KeyboardButton(text="Отмена")]
            ], resize_keyboard=True)
            await message.answer(
                "Укажите номер телефона для связи (один раз — дальше будем подставлять автоматически):",
                reply_markup=kb
            )

    # --- Телефон: подтверждение сохранённого ---
    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "phone_confirm")
    async def phone_confirm_step(message: types.Message):
        uid = message.from_user.id
        text = (message.text or "").strip()
        state = user_state.get(uid, {})

        if text == "Отмена":
            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}
            if user_state.get(uid, {}).get("type") == "cart_view":
                await show_cart_full_list_and_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        if text == "Использовать сохранённый":
            phone = state.get("phone")
            delivery_type = state.get("delivery_type")
            temp_items = state.get("temp_order_items", [])
            prev = state.get("previous_state", {})

            await go_next_after_phone(message, delivery_type, temp_items, phone, prev)
            return

        if text == "Указать другой":
            user_state[uid] = {
                "type": "phone_request",
                "delivery_type": state.get("delivery_type"),
                "temp_order_items": state.get("temp_order_items", []),
                "previous_state": state.get("previous_state", {})
            }
            kb = ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Отправить контакт", request_contact=True)],
                [KeyboardButton(text="Ввести номер вручную")],
                [KeyboardButton(text="Отмена")]
            ], resize_keyboard=True)
            await message.answer(
                "Укажите способ передачи нового номера:",
                reply_markup=kb
            )
            return

        await message.answer("Пожалуйста, выберите вариант кнопками ниже.")

    # --- Телефон: запрос контакта / переход на ручной ввод ---
    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "phone_request")
    async def phone_request_step(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})

        if (message.text or "").strip() == "Отмена":
            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}
            if user_state.get(uid, {}).get("type") == "cart_view":
                await show_cart_full_list_and_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        if (message.text or "").strip() == "Ввести номер вручную":
            user_state[uid] = {
                "type": "phone_manual",
                "delivery_type": state.get("delivery_type"),
                "temp_order_items": state.get("temp_order_items", []),
                "previous_state": state.get("previous_state", {})
            }
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
            await message.answer("Введите номер телефона (например: +7XXXXXXXXXX):", reply_markup=kb)
            return

        if message.contact and message.contact.phone_number:
            phone = message.contact.phone_number.strip()

            # Сохраняем телефон в clients (на всякий случай создаём запись)
            cur.execute(
                "INSERT OR IGNORE INTO clients (bot_id, user_id, code, points) VALUES (?, ?, ?, 0)",
                (bot_id, uid, f"client_{uid}")
            )
            cur.execute(
                "UPDATE clients SET phone=? WHERE bot_id=? AND user_id=?",
                (phone, bot_id, uid)
            )
            conn.commit()

            delivery_type = state.get("delivery_type")
            temp_items = state.get("temp_order_items", [])
            prev = state.get("previous_state", {})

            await go_next_after_phone(message, delivery_type, temp_items, phone, prev)
            return


    # --- Телефон: ручной ввод ---
    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "phone_manual")
    async def phone_manual_step(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        text = (message.text or "").strip()

        if text == "Отмена":
            prev = state.get("previous_state", {})
            user_state[uid] = prev if prev else {}
            if user_state.get(uid, {}).get("type") == "cart_view":
                await show_cart_full_list_and_keyboard(message, user_state[uid].get("page", 0))
            else:
                await show_main_menu(message)
            return

        # простая валидация номера
        digits = ''.join(ch for ch in text if ch.isdigit())
        if len(digits) < 10 or len(digits) > 15:
            await message.answer("Похоже, это не номер телефона. Введите номер ещё раз (например: +7XXXXXXXXXX) или нажмите «Отмена».")
            return

        phone = text

        cur.execute(
            "INSERT OR IGNORE INTO clients (bot_id, user_id, code, points) VALUES (?, ?, ?, 0)",
            (bot_id, uid, f"client_{uid}")
        )
        cur.execute(
            "UPDATE clients SET phone=? WHERE bot_id=? AND user_id=?",
            (phone, bot_id, uid)
        )
        conn.commit()

        delivery_type = state.get("delivery_type")
        temp_items = state.get("temp_order_items", [])
        prev = state.get("previous_state", {})

        await go_next_after_phone(message, delivery_type, temp_items, phone, prev)
        return

    async def _create_order_and_notify(message: types.Message):
        uid = message.from_user.id
        state = user_state.get(uid, {})

        delivery_type = state.get("delivery_type")
        temp_items = state.get("temp_order_items", [])
        phone = state.get("phone")
        address = state.get("address", "")
        comment = state.get("comment", "") or ""
        bonus_used = int(state.get("bonus_used", 0) or 0)

        if not temp_items or not delivery_type:
            await message.answer("Корзина пуста или заказ не сформирован.")
            user_state.pop(uid, None)
            await show_main_menu(message)
            return

        total_before = sum(qty * price for _, qty, _, price in temp_items)
        bonus_used = max(0, min(bonus_used, total_before))
        total_pay = max(0, total_before - bonus_used)

        order_id = int(time.time())

        created_at = int(time.time())
        order_id = created_at  # если хочешь оставить как было

        cur.execute(
            """
            INSERT INTO orders (
                id, bot_id, user_id,
                total, total_before_bonus, bonus_used,
                created_at, delivery_type, comment, phone, address
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, bot_id, uid,
                total_pay, total_before, bonus_used,
                created_at, delivery_type, comment, phone, address
            )
        )
        conn.commit()

        # Если списали бонусы — фиксируем в истории (минус)
        if bonus_used > 0:
            try:
                add_bonus_tx(uid, -bonus_used, None, f"spend_order_{order_id}")
            except Exception as e:
                print("Ошибка списания бонусов:", e)

        # Сохраняем товары в order_items
        for prod_id, qty, name, price in temp_items:
            cur.execute(
                "INSERT INTO order_items (order_id, prod_id, name, price, quantity) VALUES (?, ?, ?, ?, ?)",
                (order_id, prod_id, name, price, qty)
            )
        conn.commit()

        # Очищаем корзину
        cur.execute("DELETE FROM cart WHERE bot_id=? AND user_id=?", (bot_id, uid))
        conn.commit()

        # === ОНЛАЙН-ОПЛАТА: если включено, отправляем счёт и ждём оплату ===
        pay_settings = _get_bot_payment_settings()
        if pay_settings.get('enabled') == 1 and pay_settings.get('provider_token'):
            try:
                cur.execute("UPDATE orders SET status='awaiting_payment', payment_status='pending', is_paid=0 WHERE id=? AND bot_id=?", (order_id, bot_id))
                conn.commit()
            except Exception as e:
                print('payment status update error:', e)
            ok = await send_invoice_for_order(order_id, uid, temp_items=temp_items)
            if ok:
                if bonus_used > 0:
                    await message.answer(
                        f"Заказ №{order_id} создан ✅\n"
                        f"Сумма: {total_before} ₽\nСписано бонусов: {bonus_used} ₽\nК оплате: {total_pay} ₽\n\n"
                        "Счёт на оплату отправлен в Telegram. После оплаты заказ уйдёт в кафе."
                    )
                else:
                    await message.answer(
                        f"Заказ №{order_id} создан ✅\nК оплате: {total_pay} ₽\n\n"
                        "Счёт на оплату отправлен в Telegram. После оплаты заказ уйдёт в кафе."
                    )
                await show_main_menu(message)
                user_state.pop(uid, None)
                return
            else:
                # Если счёт не отправился — делаем обычный заказ без онлайн-оплаты
                try:
                    cur.execute("UPDATE orders SET status='new', payment_status='none' WHERE id=? AND bot_id=?", (order_id, bot_id))
                    conn.commit()
                except Exception:
                    pass
                await message.answer('⚠️ Не удалось отправить счёт. Заказ оформлен без онлайн-оплаты.')

        # Формируем текст для сотрудников
        items_text = "\n".join([f"• {name} ×{qty} — {price*qty} ₽" for _, qty, name, price in temp_items])

        bonus_line = ""
        if bonus_used > 0:
            bonus_line = f"\nСписано бонусов: {bonus_used} ₽\nК оплате: {total_pay} ₽"

        addr_line = f"\nАдрес: {address}" if delivery_type == "Доставка" and address else ""

        full_text = (
            f"НОВЫЙ ЗАКАЗ №{order_id}\n"
            f"Тип: {delivery_type}\n"
            f"Сумма: {total_before} ₽{bonus_line}\n"
            f"Комментарий клиента: {comment if comment else 'нет'}\n"
            f"Телефон: {phone if phone else 'нет'}{addr_line}\n"
            f"Товары:\n"
            f"{items_text}\n"
            f"Клиент: {message.from_user.full_name}\n"
            f"@{message.from_user.username or 'нет'}\n"
            f"ID: {uid}"
        )

        # Отправляем в чат сотрудников
        cur.execute("SELECT notify_chat_id FROM bots WHERE bot_id=?", (bot_id,))
        row = cur.fetchone()
        chat_id = row[0] if row and row[0] else None
        chat_id = normalize_notify_chat_id(chat_id)

        if chat_id:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Принять", callback_data=f"order_accept*{order_id}")],
                [InlineKeyboardButton(text="Отменить", callback_data=f"order_cancel*{order_id}")]
            ])
            try:
                sent = await bot.send_message(chat_id=int(chat_id), text=full_text, reply_markup=keyboard)
                cur.execute("UPDATE orders SET cafe_message_id = ? WHERE id = ?", (sent.message_id, order_id))
                conn.commit()
            except Exception as e:
                print(f"Ошибка отправки в кафе: {e}")

        # Ответ клиенту
        if bonus_used > 0:
            await message.answer(
                f"Заказ №{order_id} успешно оформлен! ✅\n"
                f"Сумма: {total_before} ₽\n"
                f"Списано бонусов: {bonus_used} ₽\n"
                f"К оплате: {total_pay} ₽\n\n"
                "Ожидайте подтверждения от кафе."
            )
        else:
            await message.answer(f"Заказ №{order_id} успешно оформлен! ✅\nОжидайте подтверждения от кафе.")

        await show_main_menu(message)
        user_state.pop(uid, None)


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("awaiting_comment"))
    async def process_order_comment(message: types.Message):
        uid = message.from_user.id
        comment = (message.text or "").strip()

        if comment == "Отмена":
            user_state.pop(uid, None)
            await show_main_menu(message)
            return

        state = user_state[uid]
        temp_items = state.get("temp_order_items", [])

        # Если "Без комментария" — пустая строка
        if comment == "Без комментария":
            comment = ""

        # Сохраняем комментарий
        state["comment"] = comment
        state.pop("awaiting_comment", None)

        # ---- Бонусы: спрашиваем, если доступно ----
        settings = _get_bot_bonus_settings()
        if settings.get("enabled") != 1:
            state["bonus_used"] = 0
            await _create_order_and_notify(message)
            return

        total_before = sum(qty * price for _, qty, _, price in temp_items)
        balance = get_bonus_balance(uid)
        max_pay = int(total_before * int(settings.get("max_pay_percent", 0)) / 100)
        max_pay = min(balance, max_pay)

        if balance <= 0 or max_pay <= 0:
            state["bonus_used"] = 0
            await _create_order_and_notify(message)
            return

        state["bonus_max"] = max_pay
        state["awaiting_bonus_choice"] = True

        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Не использовать бонусы")],
                [KeyboardButton(text=f"Списать {max_pay}"), KeyboardButton(text="Ввести сумму")],
                [KeyboardButton(text="Отмена")]
            ],
            resize_keyboard=True
        )

        await message.answer(
            f"У вас {balance} бонусов. Можно списать до {max_pay} ₽ (не более {settings.get('max_pay_percent', 0)}% от суммы).\n"
            "Выберите вариант:",
            reply_markup=kb
        )


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("awaiting_bonus_choice"))
    async def process_bonus_choice(message: types.Message):
        uid = message.from_user.id
        text = (message.text or "").strip()

        state = user_state.get(uid, {})
        max_pay = int(state.get("bonus_max", 0) or 0)

        if text == "Отмена":
            user_state.pop(uid, None)
            await show_main_menu(message)
            return

        if text == "Не использовать бонусы":
            state["bonus_used"] = 0
            state.pop("awaiting_bonus_choice", None)
            await _create_order_and_notify(message)
            return

        if text.startswith("Списать"):
            # формат: "Списать 123"
            try:
                amount = int(text.split()[-1])
            except:
                amount = max_pay
            amount = max(0, min(amount, max_pay))

            state["bonus_used"] = amount
            state.pop("awaiting_bonus_choice", None)
            await _create_order_and_notify(message)
            return

        if text == "Ввести сумму":
            state.pop("awaiting_bonus_choice", None)
            state["awaiting_bonus_amount"] = True
            await message.answer(f"Введите число от 0 до {max_pay} (сколько бонусов списать):")
            return

        await message.answer("Пожалуйста, выберите один из вариантов кнопками ниже.")


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("awaiting_bonus_amount"))
    async def process_bonus_amount(message: types.Message):
        uid = message.from_user.id
        text = (message.text or "").strip()

        state = user_state.get(uid, {})
        max_pay = int(state.get("bonus_max", 0) or 0)

        if text == "Отмена":
            user_state.pop(uid, None)
            await show_main_menu(message)
            return

        try:
            amount = int(text)
        except:
            await message.answer("Введите число (например 100)")
            return

        amount = max(0, min(amount, max_pay))
        state["bonus_used"] = amount
        state.pop("awaiting_bonus_amount", None)

        await _create_order_and_notify(message)
    # === ВСЕ ОСТАЛЬНЫЕ КНОПКИ (ОБЯЗАТЕЛЬНО!) ===
    @dp.message(lambda m: m.text == "Виртуальная карта")
    async def virtual_card(message: types.Message):
        uid = message.from_user.id
        cur.execute("SELECT code FROM clients WHERE bot_id=? AND user_id=?", (bot_id, uid))
        row = cur.fetchone()
        default_code = f"client_{uid}"
        code = default_code
        if row and row[0] and "*" not in row[0] and " " not in row[0] and len(row[0]) <= 64:
            code = row[0]
        if not row:
            cur.execute("INSERT INTO clients (bot_id, user_id, code) VALUES (?, ?, ?)", (bot_id, uid, code))
            conn.commit()
        elif row[0] != code:
            cur.execute("UPDATE clients SET code=? WHERE bot_id=? AND user_id=?", (code, bot_id, uid))
            conn.commit()
        link = f"https://t.me/{username}?start={code}"
        qr_path = f"qr_{bot_id}_{uid}.png"
        qrcode.make(link).save(qr_path)
        await message.answer_photo(FSInputFile(qr_path), caption=f"Твоя карта\nКод: <code>{code}</code>", parse_mode="HTML")
        os.remove(qr_path)
    @dp.message(lambda m: m.text == "Мой баланс")
    @dp.message(lambda m: m.text == "Мой баланс")
    async def balance(message: types.Message):
        uid = message.from_user.id
        points = get_bonus_balance(uid)
        await message.answer(f"У тебя {points} бонусов")
    @dp.message(lambda m: m.text == "О нас")
    async def about(message: types.Message):
        cur.execute("SELECT about FROM bots WHERE bot_id=?", (bot_id,))
        row = cur.fetchone()
        text = row[0] if row and row[0] else "Скоро всё будет"
        await message.answer(text)
    # ===== Меню: категории / подкатегории (2 уровня) =====
    MENU_PAGE_SIZE = 8
    MENU_NAV_PREV = "◀️"
    MENU_NAV_NEXT = "▶️"
    MENU_PAGE_PREFIX = "Стр."

    def _clamp_page(page: int, total_pages: int) -> int:
        if total_pages <= 1:
            return 0
        return max(0, min(int(page or 0), total_pages - 1))

    def _page_slice(titles: list[str], page: int, per_page: int = MENU_PAGE_SIZE):
        total_pages = max(1, (len(titles) + per_page - 1) // per_page)
        page = _clamp_page(page, total_pages)
        start = page * per_page
        end = start + per_page
        return titles[start:end], page, total_pages

    async def _delete_prev_menu_message(message: types.Message, st: dict):
        # Раньше здесь удалялось предыдущее сообщение меню (давало неприятный эффект "удаления").
        # Теперь ничего не удаляем — как в пролистывании товаров.
        st.pop("menu_message_id", None)

    async def show_categories_only(message: types.Message, page: int | None = None):
        uid = message.from_user.id

        prev = user_state.get(uid, {})
        is_paging = prev.get("type") == "categories"
        if is_paging:
            await _delete_prev_menu_message(message, prev)
            if page is None:
                page = int(prev.get("page") or 0)
        else:
            if page is None:
                page = 0

        # Показываем только включённые категории
        cur.execute(
            "SELECT id, name, photo_path FROM categories WHERE bot_id=? AND enabled=1 ORDER BY sort_order, id",
            (bot_id,),
        )
        cats = cur.fetchall()
        if not cats:
            user_state.pop(uid, None)
            await message.answer("Категории ещё не добавлены.")
            return

        mapping: dict[str, dict] = {}
        ordered_titles: list[str] = []
        for cat_id, name, photo_path in cats:
            title = title_for_category(conn, bot_id, int(cat_id), name)
            # защита от дублей (на всякий)
            if title in mapping:
                title = f"{title} #{cat_id}"
            mapping[title] = {"id": int(cat_id), "name": name, "photo_path": photo_path}
            ordered_titles.append(title)

        visible, page, pages = _page_slice(ordered_titles, int(page or 0), MENU_PAGE_SIZE)

        keyboard_rows = []
        # компактно: 2 кнопки в ряд
        for i in range(0, len(visible), 2):
            row = [KeyboardButton(text=visible[i])]
            if i + 1 < len(visible):
                row.append(KeyboardButton(text=visible[i + 1]))
            keyboard_rows.append(row)

        if pages > 1:
            keyboard_rows.append([
                KeyboardButton(text=MENU_NAV_PREV),
                KeyboardButton(text=f"{MENU_PAGE_PREFIX} {page + 1}/{pages}"),
                KeyboardButton(text=MENU_NAV_NEXT),
            ])

        keyboard_rows.append([KeyboardButton(text="Назад")])
        kb = ReplyKeyboardMarkup(keyboard=keyboard_rows, resize_keyboard=True)

        user_state[uid] = {
            "type": "categories",
            "cats": mapping,
            "titles": ordered_titles,
            "page": page,
            "pages": pages,
        }
        

        caption = "Выберите категорию:"
        cover_path = None
        if is_paging:
            try:
                cur.execute(
                    "SELECT photo_path FROM menu_photos WHERE bot_id=? ORDER BY sort_order, id LIMIT 1",
                    (bot_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    cover_path = row[0]
            except Exception:
                cover_path = None

        if is_paging and cover_path and os.path.exists(cover_path):
            sent = await message.answer_photo(FSInputFile(cover_path), caption=caption, reply_markup=kb)
        else:
            sent = await message.answer(caption, reply_markup=kb)
        user_state[uid]["menu_message_id"] = sent.message_id

    async def show_subcategories_only(
        message: types.Message,
        cat_id: int,
        cat_name: str,
        cat_photo_path,
        page: int | None = None,
        parent_page: int | None = None,
    ):
        uid = message.from_user.id

        prev = user_state.get(uid, {})
        if prev.get("type") == "subcategories" and int(prev.get("cat_id") or 0) == int(cat_id):
            await _delete_prev_menu_message(message, prev)
            if page is None:
                page = int(prev.get("page") or 0)
            if parent_page is None:
                parent_page = int(prev.get("parent_page") or 0)
        else:
            if page is None:
                page = 0
            if parent_page is None:
                parent_page = 0

        subs = db_get_subcategories(conn, bot_id, cat_id, include_disabled=False)
        mapping: dict[str, dict] = {}
        keyboard = []

        titles: list[str] = []
        for sub_id, _b, _c, name, _en, _sort, sub_photo_path, _parent in subs:
            t = title_for_subcategory(conn, bot_id, int(sub_id), name)
            if t in mapping:
                t = f"{t} #{sub_id}"
            mapping[t] = {"kind": "subcat", "id": int(sub_id), "name": name, "photo_path": sub_photo_path}
            titles.append(t)

        visible, page, pages = _page_slice(titles, int(page or 0), MENU_PAGE_SIZE)

        for i in range(0, len(visible), 2):
            row = [KeyboardButton(text=visible[i])]
            if i + 1 < len(visible):
                row.append(KeyboardButton(text=visible[i + 1]))
            keyboard.append(row)

        if pages > 1:
            keyboard.append([
                KeyboardButton(text=MENU_NAV_PREV),
                KeyboardButton(text=f"{MENU_PAGE_PREFIX} {page + 1}/{pages}"),
                KeyboardButton(text=MENU_NAV_NEXT),
            ])

        keyboard.append([KeyboardButton(text="Назад"), KeyboardButton(text="Корзина")])
        keyboard.append([KeyboardButton(text="На главную")])

        kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        user_state[uid] = {
            "type": "subcategories",
            "cat_id": int(cat_id),
            "cat_name": cat_name,
            "cat_photo_path": cat_photo_path,
            "subs": mapping,
            "titles": titles,
            "page": page,
            "pages": pages,
            "parent_page": int(parent_page or 0),
        }

        caption = f"<b>{cat_name}</b>\nВыберите подкатегорию:"
        if cat_photo_path and os.path.exists(cat_photo_path):
            sent = await message.answer_photo(FSInputFile(cat_photo_path), caption=caption, parse_mode="HTML", reply_markup=kb)
        else:
            sent = await message.answer(caption, parse_mode="HTML", reply_markup=kb)

        user_state[uid]["menu_message_id"] = sent.message_id


    async def show_subsubcategories_only(
        message: types.Message,
        cat_id: int,
        cat_name: str,
        cat_photo_path,
        parent_subcat_id: int,
        parent_sub_name: str,
        parent_sub_photo_path,
        page: int | None = None,
        parent_page: int | None = None,
        sub_page: int | None = None,
    ):
        """Показывает подподкатегории (2-й уровень) для выбранной подкатегории."""
        uid = message.from_user.id

        prev = user_state.get(uid, {})
        if prev.get("type") == "subsubcategories" and int(prev.get("parent_subcat_id") or 0) == int(parent_subcat_id):
            await _delete_prev_menu_message(message, prev)
            if page is None:
                page = int(prev.get("page") or 0)
            if parent_page is None:
                parent_page = int(prev.get("parent_page") or 0)
            if sub_page is None:
                sub_page = int(prev.get("sub_page") or 0)
        else:
            if page is None:
                page = 0
            if parent_page is None:
                parent_page = int(parent_page or 0)
            if sub_page is None:
                sub_page = int(sub_page or 0)

        subs = db_get_subcategories(conn, bot_id, cat_id, parent_subcat_id=parent_subcat_id, include_disabled=False)

        mapping: dict[str, dict] = {}
        titles: list[str] = []
        for sub_id, _b, _c, name, _en, _sort, sub_photo_path, _parent in subs:
            t = title_for_subcategory(conn, bot_id, int(sub_id), name)
            if t in mapping:
                t = f"{t} #{sub_id}"
            mapping[t] = {"kind": "subsub", "id": int(sub_id), "name": name, "photo_path": sub_photo_path}
            titles.append(t)

        visible, page, pages = _page_slice(titles, int(page or 0), MENU_PAGE_SIZE)

        keyboard = []
        for i in range(0, len(visible), 2):
            row = [KeyboardButton(text=visible[i])]
            if i + 1 < len(visible):
                row.append(KeyboardButton(text=visible[i + 1]))
            keyboard.append(row)

        if pages > 1:
            keyboard.append([
                KeyboardButton(text=MENU_NAV_PREV),
                KeyboardButton(text=f"{MENU_PAGE_PREFIX} {page + 1}/{pages}"),
                KeyboardButton(text=MENU_NAV_NEXT),
            ])

        keyboard.append([KeyboardButton(text="Назад"), KeyboardButton(text="Корзина")])
        keyboard.append([KeyboardButton(text="На главную")])

        kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

        user_state[uid] = {
            "type": "subsubcategories",
            "cat_id": int(cat_id),
            "cat_name": cat_name,
            "cat_photo_path": cat_photo_path,
            "parent_subcat_id": int(parent_subcat_id),
            "parent_sub_name": parent_sub_name,
            "parent_sub_photo_path": parent_sub_photo_path,
            "subs": mapping,
            "titles": titles,
            "page": page,
            "pages": pages,
            "parent_page": int(parent_page or 0),
            "sub_page": int(sub_page or 0),
        }

        breadcrumb = f"{cat_name} → {parent_sub_name}"
        caption = f"<b>{breadcrumb}</b>\nВыберите подподкатегорию:"

        photo_path = parent_sub_photo_path or cat_photo_path
        if photo_path and os.path.exists(photo_path):
            sent = await message.answer_photo(
                FSInputFile(photo_path),
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            sent = await message.answer(caption, parse_mode="HTML", reply_markup=kb)

        user_state[uid]["menu_message_id"] = sent.message_id

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "categories" and (m.text or "").strip() in [MENU_NAV_PREV, MENU_NAV_NEXT])
    async def categories_pagination(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        page = int(st.get("page") or 0)
        pages = int(st.get("pages") or 1)
        if (message.text or "").strip() == MENU_NAV_PREV:
            page -= 1
        else:
            page += 1
        page = _clamp_page(page, pages)
        await show_categories_only(message, page=page)

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "subcategories" and (m.text or "").strip() in [MENU_NAV_PREV, MENU_NAV_NEXT])
    async def subcategories_pagination(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        page = int(st.get("page") or 0)
        pages = int(st.get("pages") or 1)
        if (message.text or "").strip() == MENU_NAV_PREV:
            page -= 1
        else:
            page += 1
        page = _clamp_page(page, pages)
        await show_subcategories_only(
            message,
            int(st.get("cat_id") or 0),
            st.get("cat_name") or "Категория",
            st.get("cat_photo_path"),
            page=page,
            parent_page=int(st.get("parent_page") or 0),
        )


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "subsubcategories" and (m.text or "").strip() in [MENU_NAV_PREV, MENU_NAV_NEXT])
    async def subsubcategories_pagination(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        page = int(st.get("page") or 0)
        pages = int(st.get("pages") or 1)
        if (message.text or "").strip() == MENU_NAV_PREV:
            page -= 1
        else:
            page += 1
        page = _clamp_page(page, pages)
        await show_subsubcategories_only(
            message,
            int(st.get("cat_id") or 0),
            st.get("cat_name") or "Категория",
            st.get("cat_photo_path"),
            int(st.get("parent_subcat_id") or 0),
            st.get("parent_sub_name") or "Подкатегория",
            st.get("parent_sub_photo_path"),
            page=page,
            parent_page=int(st.get("parent_page") or 0),
            sub_page=int(st.get("sub_page") or 0),
        )

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "subsubcategories" and (m.text or "").strip() == "Назад")
    async def back_to_subcategories_from_subsubcategories(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        await show_subcategories_only(
            message,
            int(st.get("cat_id") or 0),
            st.get("cat_name") or "Категория",
            st.get("cat_photo_path"),
            page=int(st.get("sub_page") or 0),
            parent_page=int(st.get("parent_page") or 0),
        )


    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "categories" and (m.text or "").strip() == "Назад")
    async def back_to_main_from_categories_state(message: types.Message):
        uid = message.from_user.id
        user_state.pop(uid, None)
        await show_main_menu(message)

    @dp.message(lambda m: user_state.get(m.from_user.id, {}).get("type") == "subcategories" and (m.text or "").strip() == "Назад")
    async def back_to_categories_from_subcategories(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        await show_categories_only(message, page=int(st.get("parent_page") or 0))

    def _is_category_choice(m: types.Message) -> bool:
        st = user_state.get(m.from_user.id, {})
        return st.get("type") == "categories" and (m.text or "") in st.get("cats", {})

    def _is_subcategory_choice(m: types.Message) -> bool:
        st = user_state.get(m.from_user.id, {})
        return st.get("type") == "subcategories" and (m.text or "") in st.get("subs", {})

    def _is_subsub_choice(m: types.Message) -> bool:
        st = user_state.get(m.from_user.id, {})
        return st.get("type") == "subsubcategories" and (m.text or "") in st.get("subs", {})


    @dp.message(lambda m: m.text == "Меню")
    async def show_full_menu(message: types.Message):
        cur.execute("SELECT photo_path FROM menu_photos WHERE bot_id=? ORDER BY sort_order, id", (bot_id,))
        photos = cur.fetchall()

        if photos:
            media = []
            for i, (photo_path,) in enumerate(photos[:10]):  # максимум 10 фото в альбоме
                caption = "Полное меню кафе" if i == 0 else None
                media.append(types.InputMediaPhoto(media=FSInputFile(photo_path), caption=caption))
            await message.answer_media_group(media=media)
        else:
            await message.answer("Меню ещё не загружено владельцем кафе 😔")

        # Сразу показываем категории (с количеством в скобках)
        await show_categories_only(message, page=0)

    @dp.message(_is_category_choice)
    async def category_selected(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        info = st.get("cats", {}).get((message.text or ""))
        if not info:
            return

        cat_id = int(info.get("id") or 0)
        cat_name = info.get("name") or "Категория"
        photo_path = info.get("photo_path")

        # Если в категории есть включённые подкатегории — показываем их
        if has_enabled_subcategories(conn, bot_id, cat_id):
            await show_subcategories_only(message, cat_id, cat_name, photo_path, page=0, parent_page=int(st.get("page") or 0))
            return

        # В этой категории нет включённых подкатегорий — показываем товары прямо в категории
        cur.execute(
            "SELECT id, name FROM products WHERE bot_id=? AND cat_id=? AND (subcat_id IS NULL OR subcat_id=0) AND enabled=1 ORDER BY sort_order, id",
            (bot_id, cat_id),
        )
        prods = cur.fetchall()

        user_state[uid] = {
            "type": "category_products",
            "cat_id": cat_id,
            "prods": [(p[0], p[1]) for p in prods],
            "page": 0,
            "cat_name": cat_name,
            "cat_photo_path": photo_path,
            "back_mode": "categories",
            "categories_page": int(st.get("page") or 0),
        }
        await show_category_products_keyboard(message, 0)
        return

    @dp.message(_is_subcategory_choice)
    async def subcategory_selected(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        choice = st.get("subs", {}).get((message.text or ""))
        if not choice:
            return

        cat_id = int(st.get("cat_id") or 0)
        base_cat_name = st.get("cat_name") or "Категория"
        cat_photo_path = st.get("cat_photo_path")

        subcat_id = int(choice.get("id") or 0)
        sub_name = choice.get("name") or "Подкатегория"
        sub_photo_path = choice.get("photo_path")

        # Есть ли подподкатегории?
        try:
            cur.execute(
                "SELECT COUNT(1) FROM subcategories WHERE bot_id=? AND parent_subcat_id=? AND enabled=1",
                (bot_id, subcat_id),
            )
            child_cnt = int(cur.fetchone()[0] or 0)
        except Exception:
            child_cnt = 0

        if child_cnt > 0:
            # Переходим на уровень подподкатегорий
            await show_subsubcategories_only(
                message,
                cat_id,
                base_cat_name,
                cat_photo_path,
                parent_subcat_id=subcat_id,
                parent_sub_name=sub_name,
                parent_sub_photo_path=(sub_photo_path or cat_photo_path),
                page=0,
                parent_page=int(st.get("parent_page") or 0),
                sub_page=int(st.get("page") or 0),
            )
            return

        # Лист — показываем товары
        photo_path = sub_photo_path or cat_photo_path

        cur.execute(
            "SELECT id, name FROM products WHERE bot_id=? AND subcat_id=? AND enabled=1 ORDER BY sort_order, id",
            (bot_id, subcat_id),
        )
        prods = cur.fetchall()
        breadcrumb = f"{base_cat_name} → {sub_name}"

        if not prods:
            caption = f"<b>{breadcrumb}</b>\nВ этой подкатегории пока нет товаров."
            if photo_path and os.path.exists(photo_path):
                await message.answer_photo(FSInputFile(photo_path), caption=caption, parse_mode="HTML")
            else:
                await message.answer(caption, parse_mode="HTML")
            return

        user_state[uid] = {
            "type": "category_products",
            "cat_id": cat_id,
            "prods": [(p[0], p[1]) for p in prods],
            "page": 0,
            "cat_name": breadcrumb,  # используется в заголовке (хлебные крошки)
            "cat_photo_path": photo_path,  # фото для товаров (приоритет: подкатегория)
            "back_mode": "subcategories",
            "back_cat_id": cat_id,
            "back_cat_name": base_cat_name,
            "back_cat_photo_path": cat_photo_path,  # фото для возврата к списку подкатегорий
            "categories_page": int(st.get("parent_page") or 0),
            "sub_page": int(st.get("page") or 0),
            "parent_page": int(st.get("parent_page") or 0),
        }
        await show_category_products_keyboard(message, 0)


    @dp.message(_is_subsub_choice)
    async def subsubcategory_selected(message: types.Message):
        uid = message.from_user.id
        st = user_state.get(uid, {})
        choice = st.get("subs", {}).get((message.text or ""))
        if not choice:
            return

        cat_id = int(st.get("cat_id") or 0)
        base_cat_name = st.get("cat_name") or "Категория"

        parent_subcat_id = int(st.get("parent_subcat_id") or 0)
        parent_sub_name = st.get("parent_sub_name") or "Подкатегория"
        parent_sub_photo_path = st.get("parent_sub_photo_path")
        cat_photo_path = st.get("cat_photo_path")

        subcat_id = int(choice.get("id") or 0)
        sub_name = choice.get("name") or "Подподкатегория"
        sub_photo_path = choice.get("photo_path")

        photo_path = sub_photo_path or parent_sub_photo_path or cat_photo_path

        cur.execute(
            "SELECT id, name FROM products WHERE bot_id=? AND subcat_id=? AND enabled=1 ORDER BY sort_order, id",
            (bot_id, subcat_id),
        )
        prods = cur.fetchall()

        breadcrumb = f"{base_cat_name} → {parent_sub_name} → {sub_name}"

        if not prods:
            caption = f"<b>{breadcrumb}</b>\nВ этой подподкатегории пока нет товаров."
            if photo_path and os.path.exists(photo_path):
                await message.answer_photo(FSInputFile(photo_path), caption=caption, parse_mode="HTML")
            else:
                await message.answer(caption, parse_mode="HTML")
            return

        user_state[uid] = {
            "type": "category_products",
            "cat_id": cat_id,
            "prods": [(p[0], p[1]) for p in prods],
            "page": 0,
            "cat_name": breadcrumb,  # хлебные крошки
            "cat_photo_path": photo_path,  # фото для товаров (приоритет: подподкатегория)
            "back_mode": "subsubcategories",
            "back_cat_id": cat_id,
            "back_cat_name": base_cat_name,
            "back_cat_photo_path": cat_photo_path,
            "parent_subcat_id": parent_subcat_id,
            "parent_sub_name": parent_sub_name,
            "parent_sub_photo_path": parent_sub_photo_path,
            "categories_page": int(st.get("parent_page") or 0),
            "parent_page": int(st.get("parent_page") or 0),
            "sub_page": int(st.get("sub_page") or 0),      # страница подкатегорий
            "subsub_page": int(st.get("page") or 0),       # страница подподкатегорий
        }
        await show_category_products_keyboard(message, 0)

    async def show_category_products_keyboard(message: types.Message, page: int):
        uid = message.from_user.id
        state = user_state.get(uid, {})
        if state.get("type") != "category_products":
            return

        prods = state.get("prods") or []
        per_page = 6
        total_pages = max(1, (len(prods) + per_page - 1) // per_page)
        page = _clamp_page(int(page or 0), total_pages)

        start = page * per_page
        end = start + per_page
        current_prods = prods[start:end]

        keyboard = []
        for i in range(0, len(current_prods), 2):
            row = [KeyboardButton(text=current_prods[i][1])]
            if i + 1 < len(current_prods):
                row.append(KeyboardButton(text=current_prods[i + 1][1]))
            keyboard.append(row)

        # Стрелки + "Стр. x/y" — в том же стиле, что категории/подкатегории
        if total_pages > 1:
            keyboard.append([
                KeyboardButton(text=MENU_NAV_PREV),
                KeyboardButton(text=f"{MENU_PAGE_PREFIX} {page + 1}/{total_pages}"),
                KeyboardButton(text=MENU_NAV_NEXT),
            ])

        keyboard.append([KeyboardButton(text="Назад"), KeyboardButton(text="Корзина")])
        keyboard.append([KeyboardButton(text="На главную")])

        kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

        # Если это первое открытие категории — отправляем фото + название
        if "category_photo_message_id" not in state:
            cat_name = state.get("cat_name", "Категория")
            caption = f"<b>{cat_name}</b>"
            photo_path = state.get("cat_photo_path")

            if photo_path and os.path.exists(photo_path):
                sent = await message.answer_photo(FSInputFile(photo_path), caption=caption, parse_mode="HTML", reply_markup=kb)
            else:
                sent = await message.answer(caption, parse_mode="HTML", reply_markup=kb)

            state["category_photo_message_id"] = sent.message_id
        else:
            # При листании — редактируем только клавиатуру (фото и текст остаются)
            try:
                await bot.edit_message_reply_markup(
                    chat_id=uid,
                    message_id=state["category_photo_message_id"],
                    reply_markup=kb
                )
            except Exception:
                # Если сообщение удалено — отправляем новое
                cat_name = state.get("cat_name", "Категория")
                caption = f"<b>{cat_name}</b>"
                photo_path = state.get("cat_photo_path")

                if photo_path and os.path.exists(photo_path):
                    sent = await message.answer_photo(FSInputFile(photo_path), caption=caption, parse_mode="HTML", reply_markup=kb)
                else:
                    sent = await message.answer(caption, parse_mode="HTML", reply_markup=kb)

                state["category_photo_message_id"] = sent.message_id

        state["page"] = page
        state["pages"] = total_pages

    @dp.message(lambda m: m.text == "Купить" and user_state.get(m.from_user.id, {}).get("type") == "product")
    async def buy_product(message: types.Message):
        uid = message.from_user.id
        state = user_state[uid]
        cat_id = state["cat_id"]
        index = state["index"]
        cur.execute("SELECT id FROM products WHERE cat_id=? ORDER BY id LIMIT 1 OFFSET ?", (cat_id, index))
        prod_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO cart (bot_id, user_id, prod_id, quantity)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(bot_id, user_id, prod_id) DO UPDATE SET quantity = cart.quantity + EXCLUDED.quantity""",
                    (bot_id, uid, prod_id))
        conn.commit()
        await message.answer("Товар добавлен в корзину!")
    @dp.message(lambda m: m.text in ["Предыдущий", "Следующий", "Назад", "На главную"]
                and user_state.get(m.from_user.id, {}).get("type") == "product")
    async def navigate_product(message: types.Message):
        uid = message.from_user.id
        state = user_state[uid]
        cat_id = state["cat_id"]
        index = state["index"]
        if message.text == "Предыдущий":
            index -= 1
        elif message.text == "Следующий":
            index += 1
        elif message.text == "Назад":
            # Возврат к списку категорий
            user_state.pop(uid, None)
            await show_categories_only(message)
            return
        user_state[uid]["index"] = index
        cur.execute("SELECT id, name, price, description, photo_path FROM products WHERE cat_id=? ORDER BY id", (cat_id,))
        prods = cur.fetchall()
        await show_product(message, prods, index)
    @dp.message(lambda m: m.text == "Назад" and user_state.get(m.from_user.id) is None)
    async def back_to_main_from_categories(message: types.Message):
        await show_main_menu(message)
# @dp.message(lambda m: m.text == "Назад")
# async def back_from_anywhere(message: types.Message):
# uid = message.from_user.id
# if uid in user_state:
# user_state.pop(uid, None)
# await show_main_menu(message)
    @dp.callback_query(lambda c: c.data and c.data.startswith("order_"))
    async def process_order_status(callback: types.CallbackQuery):
        if not callback.message:
            return

        data = callback.data

        try:
            # Убираем префикс
            payload = data[6:]  # order_

            # ---- ПРАВИЛЬНЫЙ РАЗБОР CALLBACK_DATA ----
            if "*" not in payload:
                await callback.answer("Неверный формат кнопки")
                return

            action, order_id_str = payload.split("*", 1)

            try:
                order_id = int(order_id_str)
            except ValueError:
                await callback.answer("Неверный ID заказа")
                return
            # ----------------------------------------

            # Загружаем данные заказа
            cur.execute(
                "SELECT delivery_type, status FROM orders WHERE id = ? AND bot_id = ?",
                (order_id, bot_id)
            )
            row = cur.fetchone()
            if not row:
                await callback.answer("Заказ не найден")
                return

            delivery_type, current_status = row
            is_delivery = delivery_type == "Доставка"

            # === 1. Кнопка «Отменить» ===
            if action == "cancel":
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Да, отменить",
                            callback_data=f"order_cancel_confirm*{order_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Нет, оставить",
                            callback_data=f"order_cancel_deny*{order_id}"
                        )
                    ]
                ])
                await callback.message.edit_reply_markup(reply_markup=kb)
                await callback.answer()
                return


            # === 2. Подтверждение отмены → причины ===
            if action == "cancel_confirm":
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Товар закончился",
                            callback_data=f"order_reason_0*{order_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Проблема с доставкой",
                            callback_data=f"order_reason_1*{order_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Заведение перегружено",
                            callback_data=f"order_reason_2*{order_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Другое",
                            callback_data=f"order_reason_3*{order_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="◀ Назад",
                            callback_data=f"order_back*{order_id}"
                        )
                    ]
                ])
                await callback.message.edit_reply_markup(reply_markup=kb)
                await callback.answer()
                return


            # === 3. Отмена отклонена ===
            if action == "cancel_deny":
                kb = generate_order_kb(current_status, is_delivery, order_id)
                await callback.message.edit_reply_markup(reply_markup=kb)
                await callback.answer()
                return

            # === 4. Причина отмены ===
            if action.startswith("reason_"):
                try:
                    reason_index = int(action.split("_")[1])
                except:
                    reason_index = 0

                reasons = [
                    "Товар закончился",
                    "Проблема с доставкой",
                    "Заведение перегружено",
                    "Другое"
                ]
                reason = reasons[reason_index % len(reasons)]

                cur.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE id = ? AND bot_id = ?",
                    (order_id, bot_id)
                )
                conn.commit()


                refund_bonus_if_needed(order_id, "staff_cancel")

                # Уведомление клиенту
                cur.execute("SELECT user_id FROM orders WHERE id = ?", (order_id,))
                row = cur.fetchone()
                if row:
                    try:
                        await bot.send_message(
                            row[0],
                            f"Извините, заказ №{order_id} отменён.\nПричина: {reason}"
                        )
                    except:
                        pass

                new_text = callback.message.text + f"\n\n❌ Заказ отменён\nПричина: {reason}"
                await callback.message.edit_text(new_text, reply_markup=None)
                await callback.answer("Заказ отменён")
                return

            # === 5. Назад ===
            if action == "back":
                kb = generate_order_kb(current_status, is_delivery, order_id)
                await callback.message.edit_reply_markup(reply_markup=kb)
                await callback.answer()
                return

            # === 6. Выполнение заказа ===
            if action == "complete":
                cur.execute(
                    "UPDATE orders SET status = 'completed' WHERE id = ? AND bot_id = ?",
                    (order_id, bot_id)
                )
                conn.commit()

                accrue_bonus_if_needed(order_id)


                new_text = callback.message.text + "\n\n✅ Заказ выполнен"
                await callback.message.edit_text(new_text, reply_markup=None)

                await notify_client_status(order_id, "Выполнен")  # <-- ДОБАВИТЬ

                await callback.answer()
                return


            # === 7. Стандартные статусы ===
            if is_delivery:
                allowed = {
                    "new": ["accept"],
                    "accepted": ["cooking"],
                    "cooking": ["ontheway"],
                    "ontheway": ["complete"]
                }
                status_map = {
                    "accept": ("accepted", "Принят"),
                    "cooking": ("cooking", "Готовится"),
                    "ontheway": ("ontheway", "Курьер в пути"),
                    "complete": ("completed", "Выполнен")
                }
            else:
                allowed = {
                    "new": ["accept"],
                    "accepted": ["cooking"],
                    "cooking": ["ready"],
                    "ready": ["complete"]
                }
                status_map = {
                    "accept": ("accepted", "Принят"),
                    "cooking": ("cooking", "Готовится"),
                    "ready": ("ready", "Готов к выдаче"),
                    "complete": ("completed", "Выполнен")
                }

            if action not in allowed.get(current_status, []):
                await callback.answer("Действие недоступно")
                return

            new_status, text = status_map[action]
            cur.execute(
                "UPDATE orders SET status = ? WHERE id = ? AND bot_id = ?",
                (new_status, order_id, bot_id)
            )
            conn.commit()

            await notify_client_status(order_id, text)  # <-- ДОБАВИТЬ

            if new_status == "completed":
                accrue_bonus_if_needed(order_id)


            new_text = callback.message.text.split("\n\nСтатус:")[0] + f"\n\nСтатус: {text}"
            kb = generate_order_kb(new_status, is_delivery, order_id)
            await callback.message.edit_text(new_text, reply_markup=kb)
            await callback.answer("Обновлено!")

        except Exception as e:
            print("Ошибка в process_order_status:", e)
            await callback.answer("Ошибка обработки", show_alert=True)

    # === ЗАПУСК ===
    active_bots[bot_id] = {"bot": bot, "dp": dp}
    asyncio.create_task(dp.start_polling(bot))
    print(f"Бот @{username} (ID: {bot_id}) — полностью готов!")
# === АВТООТМЕНА ЗАКАЗОВ ===
    async def auto_cancel_task():
        while True:
            await asyncio.sleep(60) # проверяем каждую минуту
            try:
                current_unix = int(time.time())
                cur.execute("""SELECT o.id, o.user_id, o.cafe_message_id, b.notify_chat_id, b.auto_cancel_minutes, o.total, o.delivery_type
                            FROM orders o
                            JOIN bots b ON o.bot_id = b.bot_id
                            WHERE o.status = 'new'
                            AND b.auto_cancel_enabled = 1
                            AND o.created_at + (b.auto_cancel_minutes * 60) < ?""", (current_unix,))
                expired = cur.fetchall()
                for order_id, client_id, cafe_msg_id, notify_chat, minutes, total, delivery_type in expired:
                    notify_chat = normalize_notify_chat_id(str(notify_chat)) if notify_chat else None
                    cur.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
                    conn.commit()
                    refund_bonus_if_needed(order_id, "auto_cancel")
                
                    # Уведомление клиенту
                    try:
                        await bot.send_message(client_id, f"Заказ №{order_id} автоматически отменён 😔\nНе получили подтверждение от кафе в течение {minutes} минут.")
                    except: pass
                
                    # Если есть чат сотрудников — редактируем старое сообщение + новое
                    if cafe_msg_id and notify_chat:
                        try:
                            # Собираем список товаров
                            items_text = ""
                            cur.execute("SELECT name, quantity, price FROM order_items WHERE order_id = ?", (order_id,))
                            for n, q, p in cur.fetchall():
                                items_text += f"• {n} ×{q} — {p*q} ₽\n"
                        
                            # Редактируем старое сообщение
                            await bot.edit_message_text(
                                chat_id=int(notify_chat),
                                message_id=cafe_msg_id,
                                text=f"Заказ №{order_id} — АВТООТМЕНА\n"
                                    f"Тип: {delivery_type} | Сумма: {total} ₽\n\n"
                                    f"{items_text}"
                                    f"Автоматическая отмена (не подтверждён за {minutes} мин)",
                                reply_markup=None
                            )
                        
                            # Новое сообщение для уведомления
                            await bot.send_message(
                                int(notify_chat),
                                f"АВТООТМЕНА №{order_id}\n(не подтверждён за {minutes} мин)❌"
                            )
                        except Exception as e:
                            print("Ошибка редактирования при автоотмене:", e)
            except Exception as e:
                print("Ошибка автоотмены:", e)
    asyncio.create_task(auto_cancel_task())
# === Автозапуск всех ботов при старте ===


async def start_all_bots():
    """Autostart all bots from DB on FastAPI startup."""
    cur.execute("SELECT bot_id, token, username FROM bots")
    for bot_id, token, username in cur.fetchall():
        if bot_id not in active_bots:
            await launch_bot(bot_id, token, username)


async def stop_bot(bot_id: int):
    """Stop a running bot if it exists."""
    if bot_id in active_bots:
        try:
            await active_bots[bot_id]["bot"].session.close()
        except Exception:
            pass
        try:
            del active_bots[bot_id]
        except Exception:
            pass
