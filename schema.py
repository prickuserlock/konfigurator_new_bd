"""PostgreSQL schema init (clean install).

This file is intentionally "clean" (no SQLite PRAGMA/sqlite_master and no ALTER-try/except migrations).
Assumption: you are starting fresh on PostgreSQL.

If later you want real migrations, we can introduce Alembic.
"""


def init_db(conn, cur):
    # --- accounts (email auth) ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            is_verified INTEGER NOT NULL DEFAULT 0,
            verify_token TEXT,
            verify_expires_at BIGINT,
            reset_token TEXT,
            reset_expires_at BIGINT,
            created_at BIGINT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_verify_token ON accounts(verify_token)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_reset_token ON accounts(reset_token)")

    # --- bots ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bots (
            bot_id BIGINT PRIMARY KEY,
            token TEXT,
            username TEXT,
            owner TEXT UNIQUE,
            about TEXT DEFAULT 'Скоро всё будет',

            notify_chat_id TEXT,

            allow_in_hall INTEGER DEFAULT 1,
            allow_takeaway INTEGER DEFAULT 1,
            allow_delivery INTEGER DEFAULT 1,

            timezone TEXT DEFAULT 'Europe/Moscow',
            work_start TEXT,
            work_end TEXT,
            restrict_orders INTEGER DEFAULT 0,

            auto_cancel_minutes INTEGER DEFAULT 60,
            auto_cancel_enabled INTEGER DEFAULT 1,

            menu_photo_path TEXT,

            bonuses_enabled INTEGER DEFAULT 1,
            bonus_percent INTEGER DEFAULT 10,
            max_bonus_pay_percent INTEGER DEFAULT 30,
            min_order_for_bonus INTEGER DEFAULT 0,
            bonus_expire_days INTEGER DEFAULT 0,
            welcome_bonus INTEGER DEFAULT 0,

            payments_enabled INTEGER DEFAULT 0,
            payment_provider_token TEXT,
            min_order_total INTEGER DEFAULT 0
        )
        """
    )

    # --- clients ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            bot_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            code TEXT,
            points INTEGER DEFAULT 0,
            phone TEXT,
            address TEXT,
            PRIMARY KEY(bot_id, user_id),
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE
        )
        """
    )

    # --- categories ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id BIGSERIAL PRIMARY KEY,
            bot_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            photo_path TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE
        )
        """
    )

    # --- subcategories ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS subcategories (
            id BIGSERIAL PRIMARY KEY,
            bot_id BIGINT NOT NULL,
            cat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            photo_path TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            parent_subcat_id BIGINT,
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE,
            FOREIGN KEY (cat_id) REFERENCES categories (id) ON DELETE CASCADE,
            FOREIGN KEY (parent_subcat_id) REFERENCES subcategories (id) ON DELETE CASCADE
        )
        """
    )

    
    # ensure new columns (safe migration)
    cur.execute("ALTER TABLE subcategories ADD COLUMN IF NOT EXISTS photo_path TEXT")
    cur.execute("ALTER TABLE subcategories ADD COLUMN IF NOT EXISTS parent_subcat_id BIGINT")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_subcategories_parent ON subcategories(bot_id, cat_id, parent_subcat_id, enabled, sort_order, id)")
# --- products ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id BIGSERIAL PRIMARY KEY,
            bot_id BIGINT NOT NULL,
            cat_id BIGINT NOT NULL,
            subcat_id BIGINT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            description TEXT,
            photo_path TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE,
            FOREIGN KEY (cat_id) REFERENCES categories (id) ON DELETE CASCADE,
            FOREIGN KEY (subcat_id) REFERENCES subcategories (id) ON DELETE SET NULL
        )
        """
    )

    # --- cart ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cart (
            bot_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            prod_id BIGINT NOT NULL,
            quantity INTEGER DEFAULT 1,
            PRIMARY KEY (bot_id, user_id, prod_id),
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE,
            FOREIGN KEY (prod_id) REFERENCES products (id) ON DELETE CASCADE
        )
        """
    )

    # --- orders ---
    # NOTE: in your code order_id is a timestamp (created_at), so id is NOT SERIAL.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id BIGINT PRIMARY KEY,
            bot_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            total INTEGER,
            total_before_bonus INTEGER,
            bonus_used INTEGER DEFAULT 0,
            bonus_earned INTEGER DEFAULT 0,
            bonus_refunded INTEGER DEFAULT 0,
            created_at BIGINT,
            status TEXT DEFAULT 'new',
            delivery_type TEXT,
            comment TEXT,
            phone TEXT,
            address TEXT,
            cafe_message_id BIGINT,

            is_paid INTEGER DEFAULT 0,
            payment_status TEXT DEFAULT 'none',
            paid_amount INTEGER,
            currency TEXT,
            telegram_payment_charge_id TEXT,
            provider_payment_charge_id TEXT,
            invoice_message_id BIGINT,
            invoice_sent_at BIGINT,
            paid_at BIGINT,

            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE
        )
        """
    )

    # --- order items ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            order_id BIGINT NOT NULL,
            prod_id BIGINT NOT NULL,
            name TEXT,
            price INTEGER,
            quantity INTEGER,
            PRIMARY KEY (order_id, prod_id),
            FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE
        )
        """
    )

    # --- menu photos ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_photos (
            id BIGSERIAL PRIMARY KEY,
            bot_id BIGINT NOT NULL,
            photo_path TEXT,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE
        )
        """
    )

    # --- bonus transactions ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bonus_transactions (
            id BIGSERIAL PRIMARY KEY,
            bot_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            points INTEGER NOT NULL,
            created_at BIGINT NOT NULL,
            expires_at BIGINT,
            comment TEXT,
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE
        )
        """
    )

    # --- cashiers ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cashiers (
            bot_id BIGINT NOT NULL,
            cashier_id BIGINT NOT NULL,
            PRIMARY KEY (bot_id, cashier_id),
            FOREIGN KEY (bot_id) REFERENCES bots (bot_id) ON DELETE CASCADE
        )
        """
    )

    # --- indices ---
    for _sql in [
        "CREATE INDEX IF NOT EXISTS idx_subcategories_bot_cat_sort ON subcategories(bot_id, cat_id, sort_order, id)",
        "CREATE INDEX IF NOT EXISTS idx_products_bot_cat_subcat_sort ON products(bot_id, cat_id, subcat_id, sort_order, id)",

        "CREATE INDEX IF NOT EXISTS idx_orders_bot_created_at ON orders (bot_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_bot_status ON orders (bot_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_bot_user_created_at ON orders (bot_id, user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status_created_at ON orders (status, created_at)",

        "CREATE INDEX IF NOT EXISTS idx_bonus_tx_bot_user_created_at ON bonus_transactions (bot_id, user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_bonus_tx_bot_user_expires_at ON bonus_transactions (bot_id, user_id, expires_at)",

        "CREATE INDEX IF NOT EXISTS idx_products_bot_cat_enabled ON products (bot_id, cat_id, enabled)",
        "CREATE INDEX IF NOT EXISTS idx_categories_bot_name ON categories (bot_id, name)",

        "CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id)",

        "CREATE INDEX IF NOT EXISTS idx_menu_photos_bot_sort ON menu_photos (bot_id, sort_order, id)",

        "CREATE INDEX IF NOT EXISTS idx_cashiers_cashier_bot ON cashiers (cashier_id, bot_id)",

        "CREATE INDEX IF NOT EXISTS idx_products_cat_enabled_id ON products (cat_id, enabled, id)",
        "CREATE INDEX IF NOT EXISTS idx_products_cat_id ON products (cat_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_cart_user_id ON cart (user_id)",
    ]:
        cur.execute(_sql)

    conn.commit()
