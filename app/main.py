"""Routes available to every logged-in user (admin and user).

Dashboard, the read-only browse views (inventory / lab / archive / expiring),
and the three shared mutations: move to lab, consume, mark ineligible/expired.
All mutations go through app.db helpers, so they inherit the BEGIN IMMEDIATE +
atomic-decrement + retry guarantees. Labels are generated AFTER the move
commits (never inside the transaction).
"""

import os
import sqlite3
from datetime import date, timedelta

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request,
    send_file, url_for,
)
from flask_login import current_user, login_required
from werkzeug.security import check_password_hash

from . import get_db
from . import db
from . import IT_LABELS
from .auth import admin_required
from .forms import (
    ArchiveActionForm, ChangePasswordForm, FeedbackForm, MoveForm,
)
from .labels import make_label

main_bp = Blueprint("main", __name__)


def _criteria_from_form(form):
    """Collect the non-empty identifier fields into a resolver criteria dict."""
    criteria = {}
    if form.supply_number.data:
        criteria["supply_number"] = form.supply_number.data
    if form.product_name.data and form.product_name.data.strip():
        criteria["product_name"] = form.product_name.data.strip()
    if form.manufacturer.data and form.manufacturer.data.strip():
        criteria["manufacturer"] = form.manufacturer.data.strip()
    if form.type.data:
        criteria["type"] = form.type.data
    return criteria


def _expiry_cutoff():
    """ISO date N days from today, where N is the configured window."""
    days = current_app.config["EXPIRY_WINDOW_DAYS"]
    return (date.today() + timedelta(days=days)).isoformat()


def _prefill_identity(form):
    """Pre-fill the batch-identifier fields from the query string, so a per-row
    action link opens the form pinned to the clicked product
    (supply number + product + manufacturer + type), not just the supply number."""
    sn = request.args.get("supply_number", type=int)
    if sn:
        form.supply_number.data = sn
    if request.args.get("product_name"):
        form.product_name.data = request.args.get("product_name")
    if request.args.get("manufacturer"):
        form.manufacturer.data = request.args.get("manufacturer")
    if request.args.get("type"):
        form.type.data = request.args.get("type")

@main_bp.route("/")
@login_required
def dashboard():
    return redirect(url_for("main.inventory"))


def _stock_view(state):
    conn = get_db()
    rows = conn.execute(
        "SELECT s.supply_number, s.location, s.quantity, b.type, b.product_name, "
        "  b.manufacturer, b.batch_code, b.expiring_date, b.quality_flag "
        "FROM stock s JOIN batches b ON b.supply_number = s.supply_number "
        "WHERE s.state = ? AND s.quantity > 0 "
        "ORDER BY b.expiring_date IS NULL, b.expiring_date, s.supply_number",
        (state,),
    ).fetchall()
    return rows


def _stock_render_ctx(state):
    """Browse rows plus what the template needs to tag each row's Stato
    (scaduto / in scadenza / in esaurimento / normale)."""
    conn = get_db()
    low_products = ([r["product_name"] for r in db.low_stock(conn)]
                    if current_user.is_admin else [])
    return {"rows": _stock_view(state), "today": date.today().isoformat(),
            "cutoff": _expiry_cutoff(), "low_products": low_products}


@main_bp.route("/inventory")
@login_required
def inventory():
    return render_template("stock_list.html", title="Magazzino",
                           state="inventory", **_stock_render_ctx("inventory"))


@main_bp.route("/lab")
@login_required
def lab():
    return render_template("stock_list.html", title="Laboratorio",
                           state="lab", **_stock_render_ctx("lab"))


@main_bp.route("/archive")
@admin_required
def archive():
    conn = get_db()
    rows = conn.execute(
        "SELECT a.id, a.supply_number, a.quantity, a.causale, a.timestamp, "
        "  b.product_name, u.username, "
        "  COALESCE((SELECT SUM(m.quantity) FROM master_log m "
        "            WHERE json_extract(m.details, '$.ref_archive_id') = a.id), 0) AS restored_qty "
        "FROM archive a JOIN batches b ON b.supply_number = a.supply_number "
        "LEFT JOIN users u ON u.id = a.user_id "
        "WHERE a.quantity > COALESCE((SELECT SUM(m2.quantity) FROM master_log m2 "
        "  WHERE json_extract(m2.details, '$.ref_archive_id') = a.id), 0) "
        "ORDER BY a.id DESC",
    ).fetchall()
    return render_template("archive_list.html", rows=rows)


@main_bp.route("/move", methods=["GET", "POST"])
@login_required
def move():
    form = MoveForm()

    def fail(msg):
        flash(msg, "error")
        return render_template("move.html", form=form)

    if form.validate_on_submit():
        criteria = _criteria_from_form(form)
        if not criteria:
            return fail("Indica almeno un attributo per identificare il lotto.")
        conn = get_db()
        to_location = (form.to_location.data or "lab").strip() or "lab"
        try:
            picks = db.move_to_lab(conn, current_user.id, criteria,
                                   form.quantity.data, to_location=to_location)
        except db.InsufficientStock as e:
            return fail(f"Scorta insufficiente: {e}")
        except db.StockBusy:
            return fail("Database occupato, riprova.")

        label_numbers = sorted({p[0] for p in picks})
        moved = sum(p[2] for p in picks)
        qmarks = ",".join("?" * len(label_numbers))
        products = [r["product_name"] for r in conn.execute(
            f"SELECT DISTINCT product_name FROM batches WHERE supply_number IN ({qmarks})",
            label_numbers).fetchall()]
        nums = ", ".join(str(n) for n in label_numbers)
        flash(f"Spostate {moved} unità di {', '.join(products)} in laboratorio "
              f"(lotto {nums}).", "info")
        return redirect(url_for("main.lab"))
    if request.method == "GET":
        _prefill_identity(form)
    return render_template("move.html", form=form)


def _archive_action(causale, title):
    """Shared handler for consume / ineligible / expired."""
    form = ArchiveActionForm()

    def fail(msg):
        flash(msg, "error")
        return render_template("archive_action.html", form=form,
                               title=title, causale=causale)

    if form.validate_on_submit():
        criteria = _criteria_from_form(form)
        if not criteria:
            return fail("Indica almeno un attributo per identificare il lotto.")
        conn = get_db()
        try:
            picks = db.archive_units(conn, current_user.id, criteria,
                                     form.quantity.data, causale,
                                     from_state=form.from_state.data)
        except db.InsufficientStock as e:
            return fail(f"Scorta insufficiente: {e}")
        except db.StockBusy:
            return fail("Database occupato, riprova.")
        total = sum(p[2] for p in picks)
        flash(f"Segnate {total} unità come {IT_LABELS.get(causale, causale)}.", "info")
        if current_user.is_admin:
            return redirect(url_for("main.archive"))
        dest = "main.inventory" if form.from_state.data == "inventory" else "main.lab"
        return redirect(url_for(dest))
    if request.method == "GET":
        _prefill_identity(form)
        fs = request.args.get("from_state")
        if fs in ("inventory", "lab"):
            form.from_state.data = fs
    return render_template("archive_action.html", form=form,
                           title=title, causale=causale)


@main_bp.route("/consume", methods=["GET", "POST"])
@login_required
def consume():
    return _archive_action("consumed", "Segna come consumato")


@main_bp.route("/ineligible", methods=["GET", "POST"])
@login_required
def ineligible():
    return _archive_action("ineligible", "Segna come non idoneo")


@main_bp.route("/expired", methods=["GET", "POST"])
@login_required
def expired():
    return _archive_action("expired", "Segna come scaduto")


def _safe_page():
    """The posted 'page', accepted only if it is a local path (open-redirect guard).

    Rejects "//..." and "/\\...": browsers normalise the backslash to "/",
    so "/\\evil.com" would otherwise act as a protocol-relative off-site URL."""
    page = request.form.get("page", "")
    if not page.startswith("/") or page.startswith("//") or "\\" in page:
        return url_for("main.dashboard")
    return page


@main_bp.route("/feedback", methods=["POST"])
@login_required
def feedback():
    """Store a short feedback message from the bottom-right widget."""
    form = FeedbackForm()
    if form.validate_on_submit():
        try:
            db.add_feedback(get_db(), current_user.id,
                            form.message.data.strip(), _safe_page())
            flash("Grazie, il tuo feedback è stato inviato.", "success")
        except sqlite3.OperationalError:
            flash("Database occupato, riprova.", "error")
    else:
        flash("Scrivi un breve messaggio prima di inviare.", "error")
    return redirect(_safe_page())


@main_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    """Change the logged-in user's own password (current + new)."""
    form = ChangePasswordForm()
    if form.validate_on_submit():
        conn = get_db()
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (current_user.id,)
        ).fetchone()
        if not (row and check_password_hash(row["password_hash"],
                                            form.current_password.data)):
            flash("La password attuale non è corretta.", "error")
            return render_template("change_password.html", form=form)
        try:
            db.reset_password(conn, current_user.id, current_user.id,
                              form.new_password.data)
        except db.StockBusy:
            flash("Database occupato, riprova.", "error")
            return render_template("change_password.html", form=form)
        flash("Password aggiornata.", "success")
        return redirect(url_for("main.inventory"))
    return render_template("change_password.html", form=form)


@main_bp.route("/label/<int:supply_number>")
@login_required
def label(supply_number):
    """Generate the batch's label PDF fresh and serve it.

    Always regenerated (it is cheap) so the label reflects the current format and
    the current batch data, never a stale cached copy from an earlier version."""
    conn = get_db()
    batch = conn.execute(
        "SELECT * FROM batches WHERE supply_number = ?", (supply_number,)
    ).fetchone()
    if batch is None:
        flash("Lotto inesistente.", "error")
        return redirect(url_for("main.dashboard"))
    try:
        path = make_label(batch, current_app.config["LABELS_DIR"])
    except OSError:
        flash("Impossibile generare l'etichetta, riprova.", "error")
        return redirect(url_for("main.lab"))
    return send_file(path, mimetype="application/pdf")
