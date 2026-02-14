import os
import time
import re
import uuid
import secrets
import smtplib
import ssl
import asyncio
from email.message import EmailMessage
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

from typing import List

from fastapi import Form, Request, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from connection import conn, cur
from core.utils import safe_filename, safe_return_to, set_qp, normalize_notify_chat_id
from core.security import hash_password, verify_password
from aiogram import Bot
from app_bot.manager import active_bots, launch_bot, stop_bot, DEFAULT_BOT_COMMANDS


def register_routes(app):
    """Attach all web routes to the given FastAPI app."""
    templates = Jinja2Templates(directory="templates")

    # === Аутентификация ===
    def get_current_user(request: Request):
        user = (request.cookies.get('user') or '').strip().lower()
        if not user:
            raise HTTPException(status_code=303, headers={'Location': '/login'})
        cur.execute('SELECT 1 FROM accounts WHERE email=? AND is_verified=1', (user,))
        if not cur.fetchone():
            raise HTTPException(status_code=303, headers={'Location': '/login'})
        return user


    # === Email (SMTP) ===
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
    SMTP_USER = os.getenv("SMTP_USER", "")  # например: mybox@yandex.ru
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")  # пароль приложения
    SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "1") == "1"

    _email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def is_valid_email(s: str) -> bool:
        return bool(_email_re.match((s or "").strip().lower()))

    def build_abs_url(path: str, **params) -> str:
        base = APP_BASE_URL
        if not path.startswith("/"):
            path = "/" + path
        url = base + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        return url

    def send_email(to_email: str, subject: str, text_body: str, html_body: str | None = None):
        if not EMAIL_ENABLED:
            return
        if not SMTP_USER or not SMTP_PASSWORD or not SMTP_FROM:
            raise RuntimeError("SMTP не настроен: укажи SMTP_USER / SMTP_PASSWORD / SMTP_FROM (env).")

        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)

    async def send_email_async(to_email: str, subject: str, text_body: str, html_body: str | None = None):
        # чтобы не блокировать event loop
        return await asyncio.to_thread(send_email, to_email, subject, text_body, html_body)

    # === Маршруты ===
    @app.get("/")
    async def home(request: Request):
        if request.cookies.get("user"):
            return RedirectResponse("/dashboard", status_code=303)
        return templates.TemplateResponse("home.html", {"request": request, "is_logged_in": False})

    # --- SUBCATEGORIES (3 уровня: Категория → Подкатегория → Подподкатегория → Товары) ---

    def _subcat_owner_ok(bot_id: int, user: str) -> bool:
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        return cur.fetchone() is not None

    def _subcat_renumber(bot_id: int, cat_id: int, parent_subcat_id: int | None):
        """Renumber sort_order for subcategories within the same (bot, cat, parent)."""
        if parent_subcat_id in ("", "0"):
            parent_subcat_id = None

        if parent_subcat_id is None:
            cur.execute(
                "SELECT id FROM subcategories WHERE bot_id=? AND cat_id=? AND parent_subcat_id IS NULL ORDER BY sort_order ASC, id ASC",
                (bot_id, cat_id),
            )
        else:
            cur.execute(
                "SELECT id FROM subcategories WHERE bot_id=? AND cat_id=? AND parent_subcat_id=? ORDER BY sort_order ASC, id ASC",
                (bot_id, cat_id, int(parent_subcat_id)),
            )

        ids = [r[0] for r in cur.fetchall()]
        for i, sid in enumerate(ids, start=1):
            cur.execute(
                "UPDATE subcategories SET sort_order=? WHERE bot_id=? AND id=?",
                (i, bot_id, sid),
            )

    def _subcat_has_children(bot_id: int, subcat_id: int) -> bool:
        cur.execute("SELECT 1 FROM subcategories WHERE bot_id=? AND parent_subcat_id=? LIMIT 1", (bot_id, subcat_id))
        return cur.fetchone() is not None

    def _subcat_has_products(bot_id: int, subcat_id: int) -> bool:
        cur.execute("SELECT 1 FROM products WHERE bot_id=? AND subcat_id=? LIMIT 1", (bot_id, subcat_id))
        return cur.fetchone() is not None

    def _subcat_is_leaf(bot_id: int, subcat_id: int) -> bool:
        return not _subcat_has_children(bot_id, subcat_id)

    @app.get("/dashboard")
    async def dashboard(request: Request, user: str = Depends(get_current_user)):
        cur.execute(
            """SELECT bot_id, username, about,
                        notify_chat_id,
                        allow_in_hall, allow_takeaway, allow_delivery,
                        timezone, work_start, work_end, restrict_orders,
                        auto_cancel_minutes, auto_cancel_enabled,
                        bonuses_enabled,
                        bonus_percent,
                        max_bonus_pay_percent,
                        min_order_for_bonus,
                        bonus_expire_days,
                        welcome_bonus,
                        payments_enabled,
                        payment_provider_token,
                        min_order_total
                FROM bots WHERE owner=?""",
            (user,),
        )
        bots = cur.fetchall()

        categories = {}
        subcategories_root = {}     # cat_id -> list(root subcats)
        subcategories_children = {} # parent_subcat_id -> list(child subcats)
        products_by_subcat = {}     # subcat_id -> list(products (8 полей))
        products_by_cat = {}         # cat_id -> list(products for category root)
        cashiers = {}

        def get_menu_photos(bot_id: int):
            cur.execute("SELECT id, photo_path FROM menu_photos WHERE bot_id=? ORDER BY sort_order, id", (bot_id,))
            return [{"id": r[0], "photo_path": r[1]} for r in cur.fetchall()]

        for bot in bots:
            bot_id = bot[0]

            cur.execute("SELECT cashier_id FROM cashiers WHERE bot_id=? ORDER BY cashier_id", (bot_id,))
            cashiers[bot_id] = [r[0] for r in cur.fetchall()]

            cur.execute(
                "SELECT id, bot_id, name, photo_path, enabled, sort_order FROM categories WHERE bot_id=? ORDER BY sort_order, id",
                (bot_id,),
            )
            cats = cur.fetchall()
            categories[bot_id] = cats

            for cat in cats:
                cat_id = cat[0]
                cur.execute(
                    """
                    SELECT id, bot_id, cat_id, name, enabled, sort_order, photo_path, parent_subcat_id
                    FROM subcategories
                    WHERE bot_id=? AND cat_id=?
                    ORDER BY parent_subcat_id, sort_order, id
                    """,
                    (bot_id, cat_id),
                )
                rows = cur.fetchall()

                roots = []
                for sub in rows:
                    pid = sub[7]
                    if pid is None:
                        roots.append(sub)
                    else:
                        subcategories_children.setdefault(pid, []).append(sub)

                subcategories_root[cat_id] = roots

            cur.execute(
                """
                SELECT id, bot_id, cat_id, name, price, description, photo_path, enabled, subcat_id
                FROM products
                WHERE bot_id=?
                ORDER BY cat_id, subcat_id, sort_order, id
                """,
                (bot_id,),
            )
            for row in cur.fetchall():
                cat_id = row[2]
                subcat_id = row[8]
                p = row[:8]
                if subcat_id in (None, 0):
                    products_by_cat.setdefault(cat_id, []).append(p)
                else:
                    products_by_subcat.setdefault(subcat_id, []).append(p)

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "bots": bots,
                "categories": categories,
                "subcategories_root": subcategories_root,
                "subcategories_children": subcategories_children,
                "products_by_subcat": products_by_subcat,
                "products_by_cat": products_by_cat,
                "get_menu_photos": get_menu_photos,
                "cashiers": cashiers,
            },
        )

    @app.post("/add_subcategory")

    async def add_subcategory(

        bot_id: int = Form(...),

        cat_id: int = Form(...),

        name: str = Form(...),

        photo: UploadFile = File(None),

        parent_subcat_id: str | None = Form(None),

        return_to: str = Form("/dashboard"),

        user: str = Depends(get_current_user),

    ):

        if not _subcat_owner_ok(bot_id, user):

            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)


        nm = (name or "").strip()

        if not nm:

            return RedirectResponse(set_qp(safe_return_to(return_to, f"/dashboard#cat-{cat_id}"), "err", "Введите название"), status_code=303)


        cur.execute("SELECT 1 FROM categories WHERE bot_id=? AND id=?", (bot_id, cat_id))

        if not cur.fetchone():

            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Категория не найдена"), status_code=303)


        # Нельзя создавать подкатегории, если в категории уже есть товары без подкатегории

        cur.execute(

            "SELECT 1 FROM products WHERE bot_id=? AND cat_id=? AND (subcat_id IS NULL OR subcat_id=0) LIMIT 1",

            (bot_id, cat_id),

        )

        if cur.fetchone():

            return RedirectResponse(

                set_qp(

                    safe_return_to(return_to, f"/dashboard#cat-{cat_id}"),

                    "err",

                    "Нельзя добавить подкатегорию: в этой категории уже есть товары. Удалите товары или перенесите их в подкатегорию.",

                ),

                status_code=303,

            )


        # parent_subcat_id: None = уровень 1; иначе уровень 2 (подподкатегория)

        parent_id: int | None = None

        if parent_subcat_id not in (None, "", "0"):

            try:

                parent_id = int(parent_subcat_id)

            except Exception:

                parent_id = None


        if parent_id is not None:

            # parent должен быть root-подкатегорией (глубина максимум 2)

            cur.execute(

                "SELECT parent_subcat_id FROM subcategories WHERE id=? AND bot_id=? AND cat_id=?",

                (parent_id, bot_id, cat_id),

            )

            pr = cur.fetchone()

            if not pr:

                return RedirectResponse(set_qp(safe_return_to(return_to, f"/dashboard#cat-{cat_id}"), "err", "Родительская подкатегория не найдена"), status_code=303)

            if pr[0] is not None:

                return RedirectResponse(set_qp(safe_return_to(return_to, f"/dashboard#cat-{cat_id}"), "err", "Нельзя создавать 4-й уровень вложенности"), status_code=303)


            # нельзя сделать родителя не-листом, если в нём уже есть товары (на всякий случай)

            if _subcat_has_products(bot_id, parent_id):

                return RedirectResponse(set_qp(safe_return_to(return_to, f"/dashboard#subcat-{parent_id}"), "err", "Нельзя добавить подподкатегорию: в родительской подкатегории уже есть товары"), status_code=303)


            cur.execute(

                "SELECT COALESCE(MAX(sort_order), 0) FROM subcategories WHERE bot_id=? AND cat_id=? AND parent_subcat_id=?",

                (bot_id, cat_id, parent_id),

            )

            mx = int(cur.fetchone()[0] or 0)

        else:

            cur.execute(

                "SELECT COALESCE(MAX(sort_order), 0) FROM subcategories WHERE bot_id=? AND cat_id=? AND parent_subcat_id IS NULL",

                (bot_id, cat_id),

            )

            mx = int(cur.fetchone()[0] or 0)


        # фото (опционально)

        photo_path = None

        if photo and getattr(photo, "filename", None):

            photo_bytes = await photo.read()

            if photo_bytes:

                ext = os.path.splitext(photo.filename)[1].lower()

                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):

                    ext = ".jpg"

                os.makedirs("static/subcategories", exist_ok=True)

                photo_path = f"static/subcategories/{bot_id}_{int(cat_id)}_{int(time.time())}_{uuid.uuid4().hex}{ext}"

                with open(photo_path, "wb") as f:

                    f.write(photo_bytes)


        cur.execute(

            "INSERT INTO subcategories(bot_id, cat_id, name, enabled, sort_order, photo_path, parent_subcat_id) VALUES(?, ?, ?, 1, ?, ?, ?)",

            (bot_id, cat_id, nm, mx + 1, photo_path, parent_id),

        )


        conn.commit()

        return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{cat_id}"), status_code=303)

    @app.get("/edit_subcategory")
    async def edit_subcategory(
        bot_id: int,
        subcat_id: int,
        request: Request,
        user: str = Depends(get_current_user),
    ):
        if not _subcat_owner_ok(bot_id, user):
            return RedirectResponse("/dashboard?err=Нет доступа", status_code=303)

        cur.execute(
            """
            SELECT id, bot_id, cat_id, name, enabled, sort_order, photo_path, parent_subcat_id
            FROM subcategories
            WHERE id=? AND bot_id=?
            """,
            (subcat_id, bot_id),
        )
        sub = cur.fetchone()
        if not sub:
            return RedirectResponse("/dashboard?err=Подкатегория не найдена", status_code=303)

        # хлебные крошки (показываем где находится, но редактировать "родителя" больше не даём)
        cur.execute("SELECT name FROM categories WHERE id=? AND bot_id=?", (int(sub[2]), bot_id))
        cat_name = (cur.fetchone() or ["Категория"])[0]
        crumbs = f"{cat_name} → {sub[3]}"
        if sub[7] is not None:
            cur.execute("SELECT name FROM subcategories WHERE id=? AND bot_id=?", (int(sub[7]), bot_id))
            parent_name = (cur.fetchone() or ["Подкатегория"])[0]
            crumbs = f"{cat_name} → {parent_name} → {sub[3]}"

        return_to = safe_return_to(request.query_params.get("return_to"), f"/dashboard#cat-{sub[2]}")

        return templates.TemplateResponse(
            "edit_subcategory.html",
            {
                "request": request,
                "sub": sub,
                "crumbs": crumbs,
                "return_to": return_to,
                "bot_id": bot_id,
            },
        )

    @app.post("/update_subcategory")
    async def update_subcategory(
        bot_id: int = Form(...),
        subcat_id: int = Form(...),
        name: str = Form(...),
        enabled: int = Form(0),
        photo: UploadFile = File(None),
        delete_photo: str = Form(None),
        return_to: str = Form("/dashboard"),
        user: str = Depends(get_current_user),
    ):
        if not _subcat_owner_ok(bot_id, user):
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)

        cur.execute(
            "SELECT cat_id, photo_path FROM subcategories WHERE id=? AND bot_id=?",
            (subcat_id, bot_id),
        )
        row = cur.fetchone()
        if not row:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Подкатегория не найдена"), status_code=303)

        real_cat_id = int(row[0])
        old_photo = row[1]

        nm = (name or "").strip()
        if not nm:
            return RedirectResponse(set_qp(safe_return_to(return_to, f"/dashboard#cat-{real_cat_id}"), "err", "Введите название"), status_code=303)

        # photo handling
        photo_path = old_photo
        if photo and getattr(photo, "filename", None) and photo.filename:
            photo_bytes = await photo.read()
            if photo_bytes:
                os.makedirs("static/subcategories", exist_ok=True)

                safe_name = safe_filename(photo.filename, default="subcat.jpg")
                _, ext = os.path.splitext(safe_name)
                ext = (ext or ".jpg").lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                    ext = ".jpg"

                photo_path = f"static/subcategories/{bot_id}_{real_cat_id}_{int(time.time())}_{uuid.uuid4().hex}{ext}"
                with open(photo_path, "wb") as f:
                    f.write(photo_bytes)

                if old_photo and os.path.exists(old_photo):
                    try:
                        os.remove(old_photo)
                    except Exception:
                        pass
        elif delete_photo == "on" and old_photo:
            try:
                if os.path.exists(old_photo):
                    os.remove(old_photo)
            except Exception:
                pass
            photo_path = None

        en = 1 if int(enabled) == 1 else 0
        cur.execute(
            "UPDATE subcategories SET name=?, enabled=?, photo_path=? WHERE id=? AND bot_id=?",
            (nm, en, photo_path, subcat_id, bot_id),
        )
        conn.commit()

        target = set_qp(safe_return_to(return_to, f"/dashboard#cat-{real_cat_id}"), "msg", "Подкатегория обновлена")
        return RedirectResponse(target, status_code=303)

    @app.post("/delete_subcategory")
    async def delete_subcategory(
        bot_id: int = Form(...),
        cat_id: int = Form(...),
        subcat_id: int = Form(...),
        return_to: str = Form("/dashboard"),
        user: str = Depends(get_current_user),
    ):
        if not _subcat_owner_ok(bot_id, user):
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)

        cur.execute(
            "SELECT cat_id, parent_subcat_id, photo_path FROM subcategories WHERE id=? AND bot_id=?",
            (subcat_id, bot_id),
        )
        row = cur.fetchone()
        if not row:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Подкатегория не найдена"), status_code=303)

        real_cat_id = int(row[0])
        parent_id = row[1]
        photo_path = row[2]

        if _subcat_has_children(bot_id, subcat_id):
            return RedirectResponse(set_qp(safe_return_to(return_to, f"/dashboard#subcat-{subcat_id}"), "err", "Сначала удалите вложенные подкатегории"), status_code=303)

        if _subcat_has_products(bot_id, subcat_id):
            return RedirectResponse(set_qp(safe_return_to(return_to, f"/dashboard#subcat-{subcat_id}"), "err", "Сначала удалите товары из этой подкатегории"), status_code=303)

        cur.execute("DELETE FROM subcategories WHERE id=? AND bot_id=?", (subcat_id, bot_id))
        conn.commit()

        if photo_path and os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except:
                pass

        _subcat_renumber(bot_id, real_cat_id, parent_id)
        return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{real_cat_id}"), status_code=303)

    @app.post("/move_subcategory")
    async def move_subcategory(
        bot_id: int = Form(...),
        cat_id: int = Form(...),
        subcat_id: int = Form(...),
        direction: str = Form(...),
        return_to: str = Form("/dashboard"),
        user: str = Depends(get_current_user),
    ):
        if not _subcat_owner_ok(bot_id, user):
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)

        cur.execute(
            "SELECT cat_id, parent_subcat_id FROM subcategories WHERE id=? AND bot_id=?",
            (subcat_id, bot_id),
        )
        row = cur.fetchone()
        if not row:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Подкатегория не найдена"), status_code=303)

        real_cat_id = int(row[0])
        parent_id = row[1]

        if parent_id is None:
            cur.execute(
                "SELECT id FROM subcategories WHERE bot_id=? AND cat_id=? AND parent_subcat_id IS NULL ORDER BY sort_order, id",
                (bot_id, real_cat_id),
            )
        else:
            cur.execute(
                "SELECT id FROM subcategories WHERE bot_id=? AND cat_id=? AND parent_subcat_id=? ORDER BY sort_order, id",
                (bot_id, real_cat_id, int(parent_id)),
            )

        ids = [r[0] for r in cur.fetchall()]
        if subcat_id not in ids:
            return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{real_cat_id}"), status_code=303)

        i = ids.index(subcat_id)
        if direction == "up" and i > 0:
            ids[i - 1], ids[i] = ids[i], ids[i - 1]
        elif direction == "down" and i < len(ids) - 1:
            ids[i + 1], ids[i] = ids[i], ids[i + 1]
        else:
            return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{real_cat_id}"), status_code=303)

        for idx, sid in enumerate(ids, start=1):
            cur.execute("UPDATE subcategories SET sort_order=? WHERE bot_id=? AND id=?", (idx, bot_id, sid))

        conn.commit()
        return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{real_cat_id}"), status_code=303)

    @app.get("/register")
    async def register_get(request: Request):
        return templates.TemplateResponse("register.html", {"request": request})
    @app.post("/save_bonus_settings")
    async def save_bonus_settings(
        bot_id: int = Form(),
        bonuses_enabled: str = Form("off"),
        bonus_percent: int = Form(10),
        max_bonus_pay_percent: int = Form(30),
        min_order_for_bonus: int = Form(0),
        bonus_expire_days: int = Form(0),
        welcome_bonus: int = Form(0),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone():
            enabled = 1 if bonuses_enabled == "on" else 0
            cur.execute("""UPDATE bots SET
                bonuses_enabled = ?,
                bonus_percent = ?,
                max_bonus_pay_percent = ?,
                min_order_for_bonus = ?,
                bonus_expire_days = ?,
                welcome_bonus = ?
                WHERE bot_id = ?""",
                (enabled, bonus_percent, max_bonus_pay_percent, min_order_for_bonus, bonus_expire_days, welcome_bonus, bot_id))
            conn.commit()
        return RedirectResponse("/dashboard?msg=Настройки бонусной системы сохранены!", status_code=303)

    @app.post("/save_min_order")
    async def save_min_order(
        bot_id: int = Form(),
        min_order_total: int = Form(0),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone():
            try:
                val = int(min_order_total or 0)
            except Exception:
                val = 0
            if val < 0:
                val = 0
            # разумный верхний предел, чтобы не вбить случайно миллион миллионов
            if val > 1_000_000:
                val = 1_000_000
            cur.execute("UPDATE bots SET min_order_total=? WHERE bot_id=?", (val, bot_id))
            conn.commit()
        return RedirectResponse("/dashboard?msg=Минимальная сумма заказа сохранена!", status_code=303)



    @app.post("/save_payment_settings")
    async def save_payment_settings(
        bot_id: int = Form(),
        payments_enabled: str = Form("off"),
        payment_provider_token: str = Form(""),
        user: str = Depends(get_current_user),
    ): 
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse("/dashboard", status_code=303)

        enabled = 1 if payments_enabled == "on" else 0
        token = (payment_provider_token or "").strip()

        if enabled == 1 and not token:
            return RedirectResponse("/dashboard?msg=Укажите provider token (TEST/LIVE) для оплаты", status_code=303)

        cur.execute("UPDATE bots SET payments_enabled=?, payment_provider_token=? WHERE bot_id=?", (enabled, token, bot_id))
        conn.commit()
        return RedirectResponse("/dashboard?msg=Настройки оплаты сохранены!", status_code=303)
    # === КАССИРЫ (админка) ===
    @app.post("/add_cashier")
    async def add_cashier(
        bot_id: int = Form(),
        cashier_id: str = Form(),
        user: str = Depends(get_current_user)
    ):
        # Проверяем владельца
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse("/dashboard", status_code=303)

        try:
            cid = int(cashier_id.strip())
        except:
            return RedirectResponse(f"/dashboard?msg=Неверный ID кассира&bot={bot_id}", status_code=303)

        cur.execute("INSERT INTO cashiers (bot_id, cashier_id) VALUES (?, ?) ON CONFLICT (bot_id, cashier_id) DO NOTHING", (bot_id, cid))
        conn.commit()
        return RedirectResponse(f"/dashboard?msg=Кассир добавлен&bot={bot_id}", status_code=303)


    @app.post("/delete_cashier")
    async def delete_cashier(
        bot_id: int = Form(),
        cashier_id: int = Form(),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse("/dashboard", status_code=303)

        cur.execute("DELETE FROM cashiers WHERE bot_id=? AND cashier_id=?", (bot_id, cashier_id))
        conn.commit()
        return RedirectResponse(f"/dashboard?msg=Кассир удалён&bot={bot_id}", status_code=303)

    @app.post("/upload_category_photo")
    async def upload_category_photo(
        bot_id: int = Form(),
        cat_id: int = Form(),
        photo: UploadFile = File(None),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user)
    ):
        # Проверяем права
        cur.execute("SELECT 1 FROM categories c JOIN bots b ON c.bot_id = b.bot_id WHERE c.id=? AND b.owner=?", (cat_id, user))
        if not cur.fetchone():
            return RedirectResponse("/dashboard", status_code=303)

        photo_path = None
        if photo and photo.filename:
            photo_bytes = await photo.read()
            os.makedirs("static/categories", exist_ok=True)

            # расширение берём из исходного файла (или .jpg если пусто)
            _, ext = os.path.splitext(photo.filename)
            ext = ext.lower() if ext else ".jpg"
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"

            photo_path = f"static/categories/cat_{cat_id}_{int(time.time())}_{uuid.uuid4().hex}{ext}"

            with open(photo_path, "wb") as f:
                f.write(photo_bytes)

        # Удаляем старое фото, если было
        cur.execute("SELECT photo_path FROM categories WHERE id=?", (cat_id,))
        old = cur.fetchone()
        if old and old[0] and os.path.exists(old[0]):
            try: os.remove(old[0])
            except: pass

        cur.execute("UPDATE categories SET photo_path = ? WHERE id = ?", (photo_path, cat_id))
        conn.commit()

        return RedirectResponse("/dashboard?msg=Фото категории загружено!", status_code=303)
    @app.post("/move_category")
    async def move_category(
        bot_id: int = Form(),
        cat_id: int = Form(),
        direction: str = Form(),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user)
    ):
        # Проверяем владельца
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "msg", "Нет доступа"), status_code=303)

        # Берём текущий порядок
        cur.execute("SELECT id FROM categories WHERE bot_id=? ORDER BY sort_order, id", (bot_id,))
        ids = [r[0] for r in cur.fetchall()]
        if cat_id not in ids:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "msg", "Категория не найдена"), status_code=303)

        i = ids.index(cat_id)

        if direction == "up" and i > 0:
            ids[i - 1], ids[i] = ids[i], ids[i - 1]
        elif direction == "down" and i < len(ids) - 1:
            ids[i + 1], ids[i] = ids[i], ids[i + 1]
        else:
            return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)



        # Перезаписываем sort_order подряд 1..N (стабильно, без “дыр”)
        for pos, cid in enumerate(ids, start=1):
            cur.execute(
                "UPDATE categories SET sort_order=? WHERE bot_id=? AND id=?",
                (pos, bot_id, cid)
            )
        conn.commit()

        return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)

    @app.post("/move_product")
    async def move_product(
        bot_id: int = Form(),
        cat_id: int = Form(),
        subcat_id: str | None = Form(None),
        prod_id: int = Form(),
        direction: str = Form(),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user)
    ):
        # Проверяем владельца
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "msg", "Нет доступа"), status_code=303)

        # Берём текущий порядок товаров внутри категории/подкатегории

        sc = None
        if subcat_id not in (None, "", "0"):
            try:
                sc = int(subcat_id)
            except:
                sc = None

        if sc is None:
            cur.execute(
                "SELECT id FROM products WHERE bot_id=? AND cat_id=? AND (subcat_id IS NULL OR subcat_id=0) ORDER BY sort_order, id",
                (bot_id, cat_id)
            )
        else:
            cur.execute(
                "SELECT id FROM products WHERE bot_id=? AND cat_id=? AND subcat_id=? ORDER BY sort_order, id",
                (bot_id, cat_id, sc)
            )

        ids = [r[0] for r in cur.fetchall()]
        if prod_id not in ids:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "msg", "Товар не найден"), status_code=303)

        i = ids.index(prod_id)

        if direction == "up" and i > 0:
            ids[i - 1], ids[i] = ids[i], ids[i - 1]
        elif direction == "down" and i < len(ids) - 1:
            ids[i + 1], ids[i] = ids[i], ids[i + 1]
        else:
            return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)

        # Перенумеровываем sort_order подряд (с 1)
        for idx, pid in enumerate(ids, start=1):
            cur.execute(
                "UPDATE products SET sort_order=? WHERE id=? AND bot_id=?",
                (idx, pid, bot_id)
            )
        conn.commit()

        return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)

    @app.post("/delete_category")
    async def delete_category(cat_id: int = Form(), bot_id: int = Form(), return_to: str | None = Form(None), user: str = Depends(get_current_user)):
        # 1) проверяем права + что категория относится к этому боту
        cur.execute("""
            SELECT 1
            FROM categories c
            JOIN bots b ON b.bot_id = c.bot_id
            WHERE c.id = ? AND c.bot_id = ? AND b.owner = ?
        """, (cat_id, bot_id, user))
        if not cur.fetchone():
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "msg", "Категория не найдена или нет доступа"), status_code=303)

        # 2) проверяем, есть ли товары в категории
        cur.execute("SELECT COUNT(1) FROM products WHERE bot_id = ? AND cat_id = ?", (bot_id, cat_id))
        cnt = int(cur.fetchone()[0] or 0)

        if cnt > 0:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нельзя удалить категорию: сначала удалите товары из неё"), status_code=303)

        # 3) можно удалять
        cur.execute("DELETE FROM categories WHERE id = ? AND bot_id = ?", (cat_id, bot_id))
        conn.commit()
        return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "msg", "Категория удалена"), status_code=303)
    @app.post("/toggle_category")
    async def toggle_category(
        bot_id: int = Form(...),
        cat_id: int = Form(...),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user),
    ):
        if not _subcat_owner_ok(bot_id, user):
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)

        cur.execute("SELECT enabled FROM categories WHERE bot_id=? AND id=?", (bot_id, cat_id))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Категория не найдена"), status_code=303)

        enabled = int(row[0]) if row[0] is not None else 1
        new_val = 0 if enabled == 1 else 1
        cur.execute("UPDATE categories SET enabled=? WHERE bot_id=? AND id=?", (new_val, bot_id, cat_id))
        conn.commit()
        return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{cat_id}"), status_code=303)


    @app.get("/edit_category")
    async def edit_category(
        request: Request,
        bot_id: int,
        cat_id: int,
        return_to: str = "/dashboard",
        user: str = Depends(get_current_user),
    ):
        # Проверяем владельца и берём username бота (для заголовка в шаблоне)
        cur.execute("SELECT username FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        brow = cur.fetchone()
        if not brow:
            return RedirectResponse(set_qp("/dashboard", "err", "Нет доступа"), status_code=303)
        bot_username = brow[0]

        cur.execute(
            "SELECT id, bot_id, name, photo_path FROM categories WHERE id=? AND bot_id=?",
            (cat_id, bot_id),
        )
        row = cur.fetchone()
        if not row:
            return RedirectResponse(set_qp("/dashboard", "err", "Категория не найдена"), status_code=303)

        # edit_category.html ожидает cat как: (id, bot_id, name, photo_path, bot_username)
        cat_tpl = (row[0], row[1], row[2], row[3], bot_username)

        return templates.TemplateResponse(
            "edit_category.html",
            {
                "request": request,
                "cat": cat_tpl,
                "return_to": safe_return_to(return_to, f"/dashboard#cat-{cat_id}"),
            },
        )

    @app.post("/update_category")
    async def update_category(
        cat_id: int = Form(...),
        name: str = Form(...),
        photo: UploadFile = File(None),
        delete_photo: str | None = Form(None),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user),
    ):
        nm = (name or "").strip()
        if not nm:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Введите название"), status_code=303)

        # Достаём bot_id и старое фото + проверяем владельца
        cur.execute(
            """
            SELECT c.bot_id, c.photo_path
            FROM categories c
            JOIN bots b ON b.bot_id = c.bot_id
            WHERE c.id=? AND b.owner=?
            """,
            (cat_id, user),
        )
        row = cur.fetchone()
        if not row:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)

        bot_id, old_photo = int(row[0]), row[1]

        photo_path = old_photo

        # Новое фото
        if photo and getattr(photo, "filename", None):
            photo_bytes = await photo.read()
            if photo_bytes:
                os.makedirs("static/categories", exist_ok=True)

                safe_name = safe_filename(photo.filename, default="category.jpg")
                _, ext = os.path.splitext(safe_name)
                ext = (ext or ".jpg").lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                    ext = ".jpg"

                photo_path = f"static/categories/cat_{bot_id}_{cat_id}_{int(time.time())}_{uuid.uuid4().hex}{ext}"
                with open(photo_path, "wb") as f:
                    f.write(photo_bytes)

                # удаляем старое фото (если было)
                if old_photo:
                    try:
                        if os.path.exists(old_photo):
                            os.remove(old_photo)
                    except Exception:
                        pass

        # Удалить текущее фото
        elif delete_photo == "on" and old_photo:
            try:
                if os.path.exists(old_photo):
                    os.remove(old_photo)
            except Exception:
                pass
            photo_path = None

        cur.execute(
            "UPDATE categories SET name=?, photo_path=? WHERE id=? AND bot_id=?",
            (nm, photo_path, cat_id, bot_id),
        )

        conn.commit()
        return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{cat_id}"), status_code=303)


    @app.post("/toggle_subcategory")
    async def toggle_subcategory(
        bot_id: int = Form(...),
        cat_id: int = Form(...),
        subcat_id: int = Form(...),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user),
    ):
        if not _subcat_owner_ok(bot_id, user):
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)

        cur.execute("SELECT enabled FROM subcategories WHERE bot_id=? AND cat_id=? AND id=?", (bot_id, cat_id, subcat_id))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Подкатегория не найдена"), status_code=303)

        enabled = int(row[0]) if row[0] is not None else 1
        new_val = 0 if enabled == 1 else 1
        cur.execute("UPDATE subcategories SET enabled=? WHERE bot_id=? AND id=?", (new_val, bot_id, subcat_id))
        conn.commit()
        return RedirectResponse(safe_return_to(return_to, f"/dashboard#cat-{cat_id}"), status_code=303)

    @app.post("/upload_menu_photo")
    async def upload_menu_photo(
        bot_id: int = Form(),
        photo: UploadFile = File(None),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT bot_id, menu_photo_path FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        row = cur.fetchone()
        if row:
            old_path = row[1]
            photo_path = None
            if photo and photo.filename:
                photo_bytes = await photo.read()
                os.makedirs("static/menu", exist_ok=True)
                photo_path = f"static/menu/{bot_id}*{int(time.time())}.jpg"
                with open(photo_path, "wb") as f:
                    f.write(photo_bytes)
    
            cur.execute("UPDATE bots SET menu_photo_path = ? WHERE bot_id = ?", (photo_path, bot_id))
            conn.commit()
    
            if old_path and os.path.exists(old_path):
                try: os.remove(old_path)
                except: pass
        return RedirectResponse("/dashboard?msg=Фото меню загружено!", status_code=303)
    @app.post("/save_auto_cancel")
    async def save_auto_cancel(
        bot_id: int = Form(),
        minutes: int = Form(60),
        auto_cancel_enabled: str = Form("off"),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone():
            enabled = 1 if auto_cancel_enabled == "on" else 0
            if 10 <= minutes <= 120:
                cur.execute("""UPDATE bots SET
                    auto_cancel_minutes = ?,
                    auto_cancel_enabled = ?
                    WHERE bot_id = ?""", (minutes, enabled, bot_id))
                conn.commit()
        return RedirectResponse("/dashboard?msg=Автоотмена сохранена!", status_code=303)
    @app.post("/save_work_time")
    async def save_work_time(
        bot_id: int = Form(),
        timezone: str = Form("Europe/Moscow"),
        work_start: str = Form(None),
        work_end: str = Form(None),
        restrict_orders: str = Form("off"),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone():
            cur.execute("""UPDATE bots SET
                timezone = ?,
                work_start = ?,
                work_end = ?,
                restrict_orders = ?
                WHERE bot_id = ?""",
                (timezone, work_start or None, work_end or None, 1 if restrict_orders == "on" else 0, bot_id))
            conn.commit()
        return RedirectResponse("/dashboard?msg=Время работы сохранено!", status_code=303)
    @app.post("/toggle_product")
    async def toggle_product(
        prod_id: int = Form(),
        enabled: str = Form("off"),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM products p JOIN bots b ON p.bot_id = b.bot_id WHERE p.id = ? AND b.owner = ?", (prod_id, user))
        if cur.fetchone():
            cur.execute("UPDATE products SET enabled = ? WHERE id = ?", (1 if enabled == "on" else 0, prod_id))
            conn.commit()
        return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "msg", "Товар обновлён!"), status_code=303)
    @app.post("/toggle_order_type")
    async def toggle_order_type(
        bot_id: int = Form(),
        in_hall: str = Form("off"),
        takeaway: str = Form("off"),
        delivery: str = Form("off"),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse("/dashboard", status_code=303)
        cur.execute("""UPDATE bots SET
            allow_in_hall = ?,
            allow_takeaway = ?,
            allow_delivery = ?
            WHERE bot_id = ?""",
            (1 if in_hall == "on" else 0,
            1 if takeaway == "on" else 0,
            1 if delivery == "on" else 0,
            bot_id))
        conn.commit()
        return RedirectResponse("/dashboard?msg=Настройки сохранены!", status_code=303)
    @app.post("/register")
    async def register_post(
        email: str = Form(),
        password: str = Form(),
        password2: str = Form(""),
    ):
        email_norm = (email or "").strip().lower()
        if not is_valid_email(email_norm):
            return RedirectResponse(f"/register?err={quote('Введите корректный email')}", status_code=303)

        if len(password or "") < 8:
            return RedirectResponse(f"/register?err={quote('Пароль должен быть минимум 8 символов')}", status_code=303)

        if password2 and password != password2:
            return RedirectResponse(f"/register?err={quote('Пароли не совпадают')}", status_code=303)

        now_ts = int(time.time())
        verify_token = secrets.token_urlsafe(32)
        verify_expires = now_ts + 24 * 3600

        cur.execute("SELECT is_verified FROM accounts WHERE email=?", (email_norm,))
        row = cur.fetchone()

        # Если уже есть подтверждённый аккаунт — сообщаем
        if row and int(row[0] or 0) == 1:
            return RedirectResponse(f"/login?err={quote('Аккаунт с таким email уже существует. Войдите или восстановите пароль.')}", status_code=303)

        if row:
            # аккаунт есть, но не подтверждён — обновим пароль/токен и отправим письмо ещё раз
            cur.execute(
                "UPDATE accounts SET password_hash=?, verify_token=?, verify_expires_at=? WHERE email=?",
                (hash_password(password), verify_token, verify_expires, email_norm),
            )
            conn.commit()
        else:
            cur.execute(
                "INSERT INTO accounts (email, password_hash, is_verified, verify_token, verify_expires_at, created_at) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (email_norm, hash_password(password), verify_token, verify_expires, now_ts),
            )
            conn.commit()

        # отправка письма
        verify_url = build_abs_url("/verify", token=verify_token)
        subj = "Подтверждение почты — BonusDostavkaBot"
        text_body = (
            "Подтвердите почту, чтобы завершить регистрацию:\n"
            f"{verify_url}\n\n"
            "Ссылка действует 24 часа."
        )
        html_body = (
            "<h2>Подтвердите почту</h2>"
            "<p>Чтобы завершить регистрацию в BonusDostavkaBot, нажмите кнопку ниже:</p>"
            f"<p><a href='{verify_url}' style='display:inline-block;padding:12px 18px;background:#2563eb;color:#fff;border-radius:12px;text-decoration:none;font-weight:700'>Подтвердить email</a></p>"
            f"<p style='color:#64748b'>Если кнопка не работает, откройте ссылку: <br><a href='{verify_url}'>{verify_url}</a></p>"
            "<p style='color:#64748b'>Ссылка действует 24 часа.</p>"
        )
        try:
            await send_email_async(email_norm, subj, text_body, html_body)
        except Exception as e:
            # если письмо не ушло — лучше не оставлять "висящий" аккаунт при первой регистрации
            # (если аккаунт уже был — не удаляем)
            cur.execute("SELECT is_verified FROM accounts WHERE email=?", (email_norm,))
            r2 = cur.fetchone()
            if r2 and int(r2[0] or 0) == 0 and not row:
                cur.execute("DELETE FROM accounts WHERE email=? AND is_verified=0", (email_norm,))
                conn.commit()
            return RedirectResponse(
                f"/register?err={quote('Не удалось отправить письмо. Проверь SMTP настройки (SMTP_USER/SMTP_PASSWORD).')}",
                status_code=303,
            )

        return RedirectResponse(f"/login?msg={quote('Мы отправили письмо с подтверждением. Проверьте почту!')}&email={quote(email_norm)}", status_code=303)


    @app.get("/login")
    async def login_get(request: Request):
        return templates.TemplateResponse("login.html", {"request": request})


    @app.post("/login")
    async def login_post(email: str = Form(), password: str = Form()):
        email_norm = (email or "").strip().lower()
        cur.execute("SELECT password_hash, is_verified FROM accounts WHERE email=?", (email_norm,))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(f"/login?err={quote('Неверный email или пароль')}&email={quote(email_norm)}", status_code=303)

        stored_hash, is_verified = row
        if int(is_verified or 0) != 1:
            # Авто-переотправка письма подтверждения
            verify_token = secrets.token_urlsafe(32)
            verify_expires = int(time.time()) + 24 * 3600
            cur.execute("UPDATE accounts SET verify_token=?, verify_expires_at=? WHERE email=?", (verify_token, verify_expires, email_norm))
            conn.commit()
            verify_url = build_abs_url("/verify", token=verify_token)
            try:
                await send_email_async(
                    email_norm,
                    "Подтверждение почты — BonusDostavkaBot",
                    "Подтвердите почту:\n" + verify_url,
                )
            except Exception:
                pass
            return RedirectResponse(
                f"/login?err={quote('Email не подтверждён. Мы отправили письмо ещё раз. Проверьте почту.')}&email={quote(email_norm)}",
                status_code=303,
            )

        if verify_password(password, stored_hash):
            resp = RedirectResponse("/dashboard", status_code=303)
            secure_cookie = APP_BASE_URL.startswith("https://")
            resp.set_cookie("user", email_norm, httponly=True, max_age=604800, samesite="lax", secure=secure_cookie)
            return resp

        return RedirectResponse(f"/login?err={quote('Неверный email или пароль')}&email={quote(email_norm)}", status_code=303)


    @app.get("/verify")
    async def verify_email(token: str, request: Request):
        token = (token or "").strip()
        now_ts = int(time.time())
        cur.execute("SELECT email, verify_expires_at, is_verified FROM accounts WHERE verify_token=?", (token,))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(f"/login?err={quote('Ссылка недействительна или уже использована')}", status_code=303)

        email, expires_at, is_verified = row
        if int(is_verified or 0) == 1:
            return RedirectResponse(f"/login?msg={quote('Почта уже подтверждена. Можно входить.')}&email={quote(email)}", status_code=303)

        if expires_at is not None and int(expires_at) < now_ts:
            return RedirectResponse(f"/login?err={quote('Ссылка подтверждения истекла. Попробуйте войти — мы отправим новую.')}&email={quote(email)}", status_code=303)

        cur.execute(
            "UPDATE accounts SET is_verified=1, verify_token=NULL, verify_expires_at=NULL WHERE email=?",
            (email,),
        )
        conn.commit()
        return RedirectResponse(f"/login?msg={quote('Почта подтверждена! Теперь можно войти.')}&email={quote(email)}", status_code=303)


    @app.get("/resend_verification")
    async def resend_verification(email: str = ""):
        email_norm = (email or "").strip().lower()
        if not is_valid_email(email_norm):
            return RedirectResponse(f"/login?err={quote('Введите корректный email')}", status_code=303)

        cur.execute("SELECT is_verified FROM accounts WHERE email=?", (email_norm,))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(f"/login?msg={quote('Если такой email существует — письмо будет отправлено.')}", status_code=303)

        if int(row[0] or 0) == 1:
            return RedirectResponse(f"/login?msg={quote('Почта уже подтверждена. Можно входить.')}&email={quote(email_norm)}", status_code=303)

        verify_token = secrets.token_urlsafe(32)
        verify_expires = int(time.time()) + 24 * 3600
        cur.execute("UPDATE accounts SET verify_token=?, verify_expires_at=? WHERE email=?", (verify_token, verify_expires, email_norm))
        conn.commit()

        verify_url = build_abs_url("/verify", token=verify_token)
        try:
            await send_email_async(
                email_norm,
                "Подтверждение почты — BonusDostavkaBot",
                "Подтвердите почту:\n" + verify_url,
            )
        except Exception:
            return RedirectResponse(f"/login?err={quote('Не удалось отправить письмо. Проверь SMTP настройки.')}&email={quote(email_norm)}", status_code=303)

        return RedirectResponse(f"/login?msg={quote('Письмо с подтверждением отправлено ещё раз.')}&email={quote(email_norm)}", status_code=303)


    @app.get("/forgot")
    async def forgot_get(request: Request):
        return templates.TemplateResponse("forgot_password.html", {"request": request})


    @app.post("/forgot")
    async def forgot_post(email: str = Form()):
        email_norm = (email or "").strip().lower()
        # Всегда отвечаем одинаково, чтобы не палить, есть ли такой email
        ok_redirect = RedirectResponse(
            f"/login?msg={quote('Если такой email существует — мы отправили ссылку для восстановления. Проверьте почту!')}&email={quote(email_norm)}",
            status_code=303,
        )

        if not is_valid_email(email_norm):
            return ok_redirect

        cur.execute("SELECT is_verified FROM accounts WHERE email=?", (email_norm,))
        row = cur.fetchone()
        if not row or int(row[0] or 0) != 1:
            return ok_redirect

        reset_token = secrets.token_urlsafe(32)
        reset_expires = int(time.time()) + 60 * 60  # 1 час
        cur.execute(
            "UPDATE accounts SET reset_token=?, reset_expires_at=? WHERE email=?",
            (reset_token, reset_expires, email_norm),
        )
        conn.commit()

        reset_url = build_abs_url("/reset", token=reset_token)
        subj = "Восстановление пароля — BonusDostavkaBot"
        text_body = "Ссылка для восстановления пароля (действует 1 час):\n" + reset_url
        html_body = (
            "<h2>Восстановление пароля</h2>"
            "<p>Нажмите кнопку, чтобы задать новый пароль:</p>"
            f"<p><a href='{reset_url}' style='display:inline-block;padding:12px 18px;background:#16a34a;color:#fff;border-radius:12px;text-decoration:none;font-weight:700'>Сбросить пароль</a></p>"
            f"<p style='color:#64748b'>Если кнопка не работает, откройте ссылку: <br><a href='{reset_url}'>{reset_url}</a></p>"
            "<p style='color:#64748b'>Ссылка действует 1 час.</p>"
        )

        try:
            await send_email_async(email_norm, subj, text_body, html_body)
        except Exception:
            pass

        return ok_redirect


    @app.get("/reset")
    async def reset_get(request: Request, token: str):
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})


    @app.post("/reset")
    async def reset_post(token: str = Form(), password: str = Form(), password2: str = Form("")):
        token = (token or "").strip()
        if len(password or "") < 8:
            return RedirectResponse(f"/reset?token={quote(token)}&err={quote('Пароль должен быть минимум 8 символов')}", status_code=303)
        if password2 and password != password2:
            return RedirectResponse(f"/reset?token={quote(token)}&err={quote('Пароли не совпадают')}", status_code=303)

        now_ts = int(time.time())
        cur.execute("SELECT email, reset_expires_at FROM accounts WHERE reset_token=?", (token,))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(f"/login?err={quote('Ссылка восстановления недействительна или уже использована')}", status_code=303)

        email, expires_at = row
        if expires_at is not None and int(expires_at) < now_ts:
            return RedirectResponse(f"/login?err={quote('Ссылка восстановления истекла. Запросите новую.')}&email={quote(email)}", status_code=303)

        cur.execute(
            "UPDATE accounts SET password_hash=?, reset_token=NULL, reset_expires_at=NULL WHERE email=?",
            (hash_password(password), email),
        )
        conn.commit()

        return RedirectResponse(f"/login?msg={quote('Пароль обновлён. Теперь можно войти.')}&email={quote(email)}", status_code=303)
    # Добавить категорию
    # Добавить категорию
    @app.post("/add_category")
    async def add_category(
        bot_id: int = Form(),
        name: str = Form(),
        photo: UploadFile = File(None),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user),
    ):
        cur.execute("SELECT bot_id FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Нет доступа"), status_code=303)

        nm = (name or "").strip()
        if not nm:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Введите название"), status_code=303)

        # фото (опционально)
        photo_path = None
        if photo and getattr(photo, "filename", None):
            photo_bytes = await photo.read()
            if photo_bytes:
                ext = os.path.splitext(photo.filename)[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    ext = ".jpg"
                os.makedirs("static/categories", exist_ok=True)
                photo_path = f"static/categories/cat_{bot_id}_{int(time.time())}_{uuid.uuid4().hex}{ext}"
                with open(photo_path, "wb") as f:
                    f.write(photo_bytes)

        cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM categories WHERE bot_id=?", (bot_id,))
        next_sort = int(cur.fetchone()[0] or 0) + 1

        cur.execute(
            "INSERT INTO categories (bot_id, name, photo_path, sort_order) VALUES (?, ?, ?, ?)",
            (bot_id, nm, photo_path, next_sort),
        )
        conn.commit()
        return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)
    @app.get("/create")
    async def create_get(request: Request, user: str = Depends(get_current_user)):
        cur.execute("SELECT 1 FROM bots WHERE owner=? LIMIT 1", (user,))
        if cur.fetchone():
            return RedirectResponse("/dashboard?msg=У вас уже создан бот (1 бот на аккаунт).", status_code=303)
        return templates.TemplateResponse("create.html", {"request": request})

    @app.post("/create")
    async def create_post(token: str = Form(), user: str = Depends(get_current_user)):
        # запрет на 2й бот
        cur.execute("SELECT 1 FROM bots WHERE owner=? LIMIT 1", (user,))
        if cur.fetchone():
            return RedirectResponse("/dashboard?msg=У вас уже есть бот. Удалите его, чтобы создать новый.", status_code=303)

        try:
            bot = Bot(token=token)
            me = await bot.get_me()

            # Устанавливаем команды сразу при создании (на всякий случай)
            try:
                await bot.set_my_commands(DEFAULT_BOT_COMMANDS)
            except Exception as e:
                print("Не удалось установить команды при создании бота:", e)

            cur.execute(
                "INSERT INTO bots (bot_id, token, username, owner, about) VALUES (?,?,?,?,?)",
                (me.id, token, me.username, user, "Скоро всё будет")
            )
            conn.commit()

            await bot.session.close()
            await launch_bot(me.id, token, me.username)
            return RedirectResponse("/dashboard", status_code=303)

        except Exception as e:
            return HTMLResponse(f"Ошибка: {e}")
    #добавить товары в категорию
    # Создаём папку для фото
    os.makedirs("static/products", exist_ok=True)
    @app.post("/add_product")
    async def add_product(
        bot_id: int = Form(),
        cat_id: int = Form(),
        subcat_id: str | None = Form(None),
        name: str = Form(),
        price: int = Form(),
        description: str = Form(None),
        photo: UploadFile = File(None),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT bot_id FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if not cur.fetchone():
            return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)


        # subcat_id может быть пустым: тогда товар добавляем прямо в категорию (без подкатегории)
        subcat_int: int | None = None
        if subcat_id not in (None, "", "0"):
            try:
                subcat_int = int(subcat_id)
            except Exception:
                subcat_int = None

        # Категория должна существовать
        cur.execute("SELECT 1 FROM categories WHERE bot_id=? AND id=?", (bot_id, cat_id))
        if not cur.fetchone():
            return RedirectResponse(
                set_qp(safe_return_to(return_to, "/dashboard"), "err", "Категория не найдена"),
                status_code=303,
            )

        if subcat_int is None:
            # Добавляем товар прямо в категорию.
            # Запрещаем, если уже есть подкатегории (иначе товары окажутся недоступны в меню).
            cur.execute("SELECT COUNT(1) FROM subcategories WHERE bot_id=? AND cat_id=?", (bot_id, cat_id))
            sc_cnt = int(cur.fetchone()[0] or 0)
            if sc_cnt > 0:
                return RedirectResponse(
                    set_qp(
                        safe_return_to(return_to, f"/dashboard#cat-{cat_id}"),
                        "err",
                        "Нельзя добавить товар в категорию: в ней уже есть подкатегории. Добавляйте товар в подкатегорию (лист).",
                    ),
                    status_code=303,
                )

        else:
            # Добавляем товар в подкатегорию (лист). Запрещаем, если в категории уже есть товары без подкатегории.
            cur.execute(
                "SELECT 1 FROM products WHERE bot_id=? AND cat_id=? AND (subcat_id IS NULL OR subcat_id=0) LIMIT 1",
                (bot_id, cat_id),
            )
            if cur.fetchone():
                return RedirectResponse(
                    set_qp(
                        safe_return_to(return_to, f"/dashboard#cat-{cat_id}"),
                        "err",
                        "Нельзя добавлять товары в подкатегории: в категории уже есть товары без подкатегории. Удалите их или используйте другой раздел.",
                    ),
                    status_code=303,
                )

            # Проверяем, что подкатегория существует и принадлежит этому боту/категории,
            # и что это лист (без детей).
            cur.execute(
                "SELECT cat_id FROM subcategories WHERE id=? AND bot_id=?",
                (subcat_int, bot_id),
            )
            srow = cur.fetchone()
            if (not srow) or (int(srow[0]) != int(cat_id)):
                return RedirectResponse(
                    set_qp(
                        safe_return_to(return_to, "/dashboard"),
                        "err",
                        "Подкатегория не найдена или не принадлежит этой категории.",
                    ),
                    status_code=303,
                )

            if not _subcat_is_leaf(bot_id, int(subcat_int)):
                return RedirectResponse(
                    set_qp(
                        safe_return_to(return_to, "/dashboard"),
                        "err",
                        "Нельзя добавлять товары сюда: выберите подкатегорию (лист) без вложенных подкатегорий.",
                    ),
                    status_code=303,
                )


        photo_path = None
        if photo and photo.filename:
            photo_bytes = await photo.read()
            os.makedirs("static/products", exist_ok=True)

            safe_name = safe_filename(photo.filename, default="product.jpg")
            _, ext = os.path.splitext(safe_name)
            ext = (ext or ".jpg").lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"

            photo_path = f"static/products/{bot_id}_{cat_id}_{int(time.time())}_{uuid.uuid4().hex}{ext}"

            with open(photo_path, "wb") as f:
                f.write(photo_bytes)

        # sort_order: добавляем товар в конец списка внутри группы (категория + подкатегория/без неё)
        if subcat_int is None:
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM products WHERE bot_id=? AND cat_id=? AND (subcat_id IS NULL OR subcat_id=0)",
                (bot_id, cat_id),
            )
        else:
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM products WHERE bot_id=? AND cat_id=? AND subcat_id=?",
                (bot_id, cat_id, subcat_int),
            )
        next_sort = int(cur.fetchone()[0] or 0) + 1

        cur.execute(
            """
            INSERT INTO products (bot_id, cat_id, subcat_id, name, price, description, photo_path, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (bot_id, cat_id, subcat_int, name.strip(), int(price), (description or "").strip(), photo_path, next_sort)
        )
        conn.commit()

        return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)

    @app.post("/delete_product")
    async def delete_product(
        prod_id: int = Form(),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user)
    ):
        cur.execute("""
            SELECT products.photo_path, bots.bot_id
            FROM products
            JOIN bots ON products.bot_id = bots.bot_id
            WHERE products.id = ? AND bots.owner = ?
        """, (prod_id, user))
        row = cur.fetchone()
        if row:
            photo_path, bot_id_from_db = row
            if photo_path:
                try:
                    os.remove(photo_path)
                except:
                    pass
            cur.execute("DELETE FROM products WHERE id = ?", (prod_id,))
            conn.commit()
        return RedirectResponse(safe_return_to(return_to, "/dashboard"), status_code=303)

    @app.post("/toggle_bonuses")
    async def toggle_bonuses(
        bot_id: int = Form(),
        bonuses_enabled: str = Form("off"),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone():
            enabled = 1 if bonuses_enabled == "on" else 0
            cur.execute("UPDATE bots SET bonuses_enabled = ? WHERE bot_id = ?", (enabled, bot_id))
            conn.commit()
        return RedirectResponse("/dashboard?msg=Бонусная система обновлена!", status_code=303)
    @app.post("/upload_menu_photos")
    async def upload_menu_photos(
        bot_id: int = Form(),
        photos: List[UploadFile] = File([]),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone() and photos:
            os.makedirs("static/menu", exist_ok=True)
            for photo in photos:
                if photo.filename:
                    photo_bytes = await photo.read()
                    # Добавляем уникальное имя, чтобы не перезаписывать
                    safe_name = safe_filename(photo.filename, default="menu.jpg")
                    photo_path = f"static/menu/{bot_id}_{int(time.time())}_{uuid.uuid4().hex}_{safe_name}"
                    with open(photo_path, "wb") as f:
                        f.write(photo_bytes)
                    cur.execute("INSERT INTO menu_photos (bot_id, photo_path) VALUES (?, ?)", (bot_id, photo_path))
            conn.commit()
        return RedirectResponse("/dashboard?msg=Фото меню загружены!", status_code=303)
    @app.post("/delete_menu_photo")
    async def delete_menu_photo(
        bot_id: int = Form(),
        photo_id: int = Form(),
        user: str = Depends(get_current_user)
    ):
        cur.execute("SELECT photo_path FROM menu_photos WHERE id=? AND bot_id IN (SELECT bot_id FROM bots WHERE owner=?)", (photo_id, user))
        row = cur.fetchone()
        if row:
            if os.path.exists(row[0]):
                try: os.remove(row[0])
                except: pass
            cur.execute("DELETE FROM menu_photos WHERE id=?", (photo_id,))
            conn.commit()
        return RedirectResponse("/dashboard", status_code=303)
    @app.post("/update_about")
    async def update_about(bot_id: int = Form(), about: str = Form(), user: str = Depends(get_current_user)):
        cur.execute("UPDATE bots SET about=? WHERE bot_id=? AND owner=?", (about, bot_id, user))
        conn.commit()
        return RedirectResponse("/dashboard", status_code=303)
    from aiogram.types import InputFile
    from io import BytesIO
    from aiogram.types import BufferedInputFile # ← ЭТО ГЛАВНОЕ!
    @app.post("/send_broadcast")
    async def send_broadcast(
        bot_id: int = Form(),
        message: str = Form(""),
        photo: UploadFile | None = File(None),
        user: str = Depends(get_current_user)
    ):
        # Проверяем владельца
        cur.execute("SELECT token, username FROM bots WHERE bot_id = ? AND owner = ?", (bot_id, user))
        row = cur.fetchone()
        if not row:
            return HTMLResponse("Доступ запрещён", status_code=403)
        token, username = row
        # Запускаем бот если нужно
        if bot_id not in active_bots:
            await launch_bot(bot_id, token, username)
            await asyncio.sleep(2)
        bot = active_bots[bot_id]["bot"]
        # Клиенты
        cur.execute("SELECT user_id FROM clients WHERE bot_id = ?", (bot_id,))
        user_ids = [r[0] for r in cur.fetchall()]
        if not user_ids:
            return RedirectResponse(f"/dashboard?msg=Нет клиентов для рассылки&bot={bot_id}", status_code=303)
        sent = 0
        photo_file = None
        # Если загружено фото — готовим его правильно
        if photo and photo.filename:
            photo_bytes = await photo.read()
            photo_file = BufferedInputFile(photo_bytes, filename=photo.filename)
        # Отправляем всем
        for uid in user_ids:
            try:
                if photo_file:
                    await bot.send_photo(
                        chat_id=uid,
                        photo=photo_file,
                        caption=message if message.strip() else " "
                    )
                elif message.strip():
                    await bot.send_message(chat_id=uid, text=message)
                sent += 1
                await asyncio.sleep(0.04)
            except Exception as e:
                pass # пропускаем заблокировавших бота
        result = f"Рассылка завершена! Отправлено: {sent} из {len(user_ids)}"
        if photo_file:
            result += " (с фото)"
        return RedirectResponse(f"/dashboard?msg={result}&bot={bot_id}", status_code=303)
    # Первый клик — "Удалить" → перенаправляем с подтверждением
    @app.post("/delete_bot")
    async def delete_bot_request(bot_id: int = Form(), user: str = Depends(get_current_user)):
        # Проверяем, что бот принадлежит пользователю
        cur.execute("SELECT bot_id FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone():
            return RedirectResponse(f"/dashboard?confirm_delete=1&bot={bot_id}", status_code=303)
        return RedirectResponse("/dashboard", status_code=303)
    # Подтверждение — "ДА, УДАЛИТЬ"
    @app.post("/confirm_delete_bot")
    async def confirm_delete_bot(bot_id: int = Form(), user: str = Depends(get_current_user)):
        # Проверяем, что бот принадлежит пользователю
        cur.execute("SELECT username FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        row = cur.fetchone()
        if not row:
            return RedirectResponse(f"/dashboard?err={quote('Бот не найден')}", status_code=303)
        username = row[0]

        def _safe_unlink(p: str | None):
            """Удаляем только файлы из static/, чтобы случайно не снести что-то лишнее."""
            if not p:
                return
            try:
                if isinstance(p, str) and p.startswith("static/") and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        try:
            # Собираем пути файлов, чтобы после удаления почистить диск
            cur.execute("SELECT photo_path FROM categories WHERE bot_id=?", (bot_id,))
            cat_photos = [r[0] for r in cur.fetchall() if r and r[0]]
            cur.execute("SELECT photo_path FROM products WHERE bot_id=?", (bot_id,))
            prod_photos = [r[0] for r in cur.fetchall() if r and r[0]]
            cur.execute("SELECT photo_path FROM menu_photos WHERE bot_id=?", (bot_id,))
            menu_photos = [r[0] for r in cur.fetchall() if r and r[0]]

            # ВАЖНО: порядок удаления из-за FOREIGN KEY
            cur.execute(
                "DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE bot_id=?)",
                (bot_id,),
            )
            cur.execute("DELETE FROM orders WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM cart WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM bonus_transactions WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM cashiers WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM menu_photos WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM products WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM categories WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM clients WHERE bot_id=?", (bot_id,))
            cur.execute("DELETE FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
            conn.commit()

            # Останавливаем бота в памяти
            if bot_id in active_bots:
                try:
                    await active_bots[bot_id]["bot"].session.close()
                except Exception:
                    pass
                del active_bots[bot_id]

            # Чистим файлы с диска
            for p in (cat_photos + prod_photos + menu_photos):
                _safe_unlink(p)

        except sqlite3.IntegrityError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return RedirectResponse(
                f"/dashboard?err={quote('Не удалось удалить бота из-за связанных данных')}",
                status_code=303,
            )
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return RedirectResponse(
                f"/dashboard?err={quote('Ошибка при удалении бота')}",
                status_code=303,
            )

        return RedirectResponse(f"/dashboard?msg={quote(f'Бот @{username} удалён')}", status_code=303)

    # === РЕДАКТИРОВАНИЕ ТОВАРА ===
    @app.get("/edit_product/{prod_id}")
    async def edit_product_get(prod_id: int, request: Request, user: str = Depends(get_current_user)):
        cur.execute(
            """
            SELECT p.id, p.name, p.price, p.description, p.photo_path, p.cat_id, p.subcat_id, b.bot_id
            FROM products p
            JOIN bots b ON p.bot_id = b.bot_id
            WHERE p.id = ? AND b.owner = ?
            """,
            (prod_id, user),
        )
        prod = cur.fetchone()
        if not prod:
            return RedirectResponse("/dashboard", status_code=303)

        return templates.TemplateResponse(
            "edit_product.html",
            {
                "request": request,
                "prod": prod,
                "return_to": safe_return_to(request.query_params.get("return_to"), "/dashboard"),
            },
        )

    @app.post("/update_product")
    async def update_product(
        prod_id: int = Form(...),
        name: str = Form(...),
        price: int = Form(...),
        description: str = Form(None),
        photo: UploadFile = File(None),
        delete_photo: str = Form(None),
        return_to: str | None = Form(None),
        user: str = Depends(get_current_user),
    ):
        cur.execute(
            """
            SELECT p.photo_path, p.bot_id, p.cat_id, p.subcat_id, b.owner
            FROM products p
            JOIN bots b ON p.bot_id = b.bot_id
            WHERE p.id = ?
            """,
            (prod_id,),
        )
        row = cur.fetchone()
        if not row or row[4] != user:
            return RedirectResponse("/dashboard", status_code=303)

        old_photo_path, bot_id, cat_id = row[0], int(row[1]), int(row[2])

        clean_name = (name or "").strip()
        if not clean_name:
            return RedirectResponse(set_qp(safe_return_to(return_to, "/dashboard"), "err", "Введите название"), status_code=303)

        # фото
        photo_path = old_photo_path
        if photo and getattr(photo, "filename", None) and photo.filename:
            photo_bytes = await photo.read()
            if photo_bytes:
                os.makedirs("static/products", exist_ok=True)

                safe_name = safe_filename(photo.filename, default="product.jpg")
                _, ext = os.path.splitext(safe_name)
                ext = (ext or ".jpg").lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                    ext = ".jpg"

                photo_path = f"static/products/{bot_id}_{cat_id}_{int(time.time())}_{uuid.uuid4().hex}{ext}"
                with open(photo_path, "wb") as f:
                    f.write(photo_bytes)

                if old_photo_path and os.path.exists(old_photo_path):
                    try:
                        os.remove(old_photo_path)
                    except Exception:
                        pass
        elif delete_photo == "on" and old_photo_path:
            try:
                if os.path.exists(old_photo_path):
                    os.remove(old_photo_path)
            except Exception:
                pass
            photo_path = None

        cur.execute(
            "UPDATE products SET name=?, price=?, description=?, photo_path=? WHERE id=? AND bot_id=?",
            (clean_name, int(price), (description or "").strip(), photo_path, prod_id, bot_id),
        )
        conn.commit()
        target = safe_return_to(return_to, "/dashboard")
        target = set_qp(target, "msg", "Товар успешно обновлён!")
        return RedirectResponse(target, status_code=303)


    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/")
        resp.delete_cookie("user")
        return resp
    @app.post("/save_notify_chat")
    async def save_notify_chat(
        bot_id: int = Form(),
        notify_chat_id: str = Form(""),
        user: str = Depends(get_current_user)
    ):
        # Проверка, что бот принадлежит пользователю
        cur.execute("SELECT 1 FROM bots WHERE bot_id=? AND owner=?", (bot_id, user))
        if cur.fetchone():
            normalized = normalize_notify_chat_id(notify_chat_id)
            cur.execute("UPDATE bots SET notify_chat_id=? WHERE bot_id=?", (normalized, bot_id))
            conn.commit()

        return RedirectResponse("/dashboard?msg=Чат для заказов сохранён!", status_code=303)
