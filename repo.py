from connection import CompatCursor


def _cursor(conn):
    """Returns a psycopg cursor that accepts SQLite-style '?' placeholders."""
    return CompatCursor(conn.cursor())


def db_get_subcategories(
    conn,
    bot_id: int,
    cat_id: int,
    parent_subcat_id: int | None = None,
    include_disabled: bool = True,
):
    """Fetch subcategories for a category and a specific parent (None = root level)."""
    cur = _cursor(conn)

    where_parent = "parent_subcat_id IS NULL" if parent_subcat_id is None else "parent_subcat_id=?"
    params = [bot_id, cat_id]
    if parent_subcat_id is not None:
        params.append(parent_subcat_id)

    if include_disabled:
        cur.execute(
            f"""
            SELECT id, bot_id, cat_id, name, enabled, sort_order, photo_path, parent_subcat_id
            FROM subcategories
            WHERE bot_id=? AND cat_id=? AND {where_parent}
            ORDER BY sort_order ASC, id ASC
            """,
            tuple(params),
        )
    else:
        cur.execute(
            f"""
            SELECT id, bot_id, cat_id, name, enabled, sort_order, photo_path, parent_subcat_id
            FROM subcategories
            WHERE bot_id=? AND cat_id=? AND {where_parent} AND enabled=1
            ORDER BY sort_order ASC, id ASC
            """,
            tuple(params),
        )
    return cur.fetchall()


def db_count_enabled_subcategories(conn, bot_id: int, cat_id: int, parent_subcat_id: int | None = None) -> int:
    cur = _cursor(conn)
    where_parent = "parent_subcat_id IS NULL" if parent_subcat_id is None else "parent_subcat_id=?"
    params = [bot_id, cat_id]
    if parent_subcat_id is not None:
        params.append(parent_subcat_id)

    cur.execute(
        f"""
        SELECT COUNT(*) FROM subcategories
        WHERE bot_id=? AND cat_id=? AND {where_parent} AND enabled=1
        """,
        tuple(params),
    )
    return int(cur.fetchone()[0] or 0)


def db_count_enabled_child_subcategories(conn, bot_id: int, parent_subcat_id: int) -> int:
    cur = _cursor(conn)
    cur.execute(
        """
        SELECT COUNT(*) FROM subcategories
        WHERE bot_id=? AND parent_subcat_id=? AND enabled=1
        """,
        (bot_id, parent_subcat_id),
    )
    return int(cur.fetchone()[0] or 0)


def db_count_enabled_products_in_subcat(conn, bot_id: int, subcat_id: int) -> int:
    cur = _cursor(conn)
    cur.execute(
        """
        SELECT COUNT(*) FROM products
        WHERE bot_id=? AND subcat_id=? AND enabled=1
        """,
        (bot_id, subcat_id),
    )
    return int(cur.fetchone()[0] or 0)


def db_count_enabled_products_in_cat_no_subcat(conn, bot_id: int, cat_id: int) -> int:
    """Count enabled products that are directly inside the category (no subcategory)."""
    cur = _cursor(conn)
    cur.execute(
        """
        SELECT COUNT(*) FROM products
        WHERE bot_id=? AND cat_id=? AND (subcat_id IS NULL OR subcat_id=0) AND enabled=1
        """,
        (bot_id, cat_id),
    )
    return int(cur.fetchone()[0] or 0)


def has_enabled_subcategories(conn, bot_id: int, cat_id: int) -> bool:
    """Root-level subcategories exist for this category."""
    return db_count_enabled_subcategories(conn, bot_id, cat_id, parent_subcat_id=None) > 0


def title_for_category(conn, bot_id: int, cat_id: int, cat_name: str) -> str:
    """Show count of items in the category.

    If the category has root subcategories -> show their count.
    Otherwise -> show count of products directly in the category.
    """
    subcnt = db_count_enabled_subcategories(conn, bot_id, cat_id, parent_subcat_id=None)
    if subcnt > 0:
        return f"{cat_name} ({subcnt})"
    prodcnt = db_count_enabled_products_in_cat_no_subcat(conn, bot_id, cat_id)
    return f"{cat_name} ({prodcnt})"


def title_for_subcategory(conn, bot_id: int, subcat_id: int, sub_name: str) -> str:
    childcnt = db_count_enabled_child_subcategories(conn, bot_id, subcat_id)
    if childcnt > 0:
        return f"{sub_name} ({childcnt})"
    prodcnt = db_count_enabled_products_in_subcat(conn, bot_id, subcat_id)
    return f"{sub_name} ({prodcnt})"
