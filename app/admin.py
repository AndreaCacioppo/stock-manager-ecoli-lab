"""Admin-only routes.

Add orders (many batches at once), remove a batch (-> archive, never deleted),
correct mistakes (intake correction + compensating reversal), view/export the
master log, set per-product low-stock thresholds, and manage user accounts
(create / change role / reset password / activate-deactivate, with the
last-admin and cross-admin guards living in app/db.py). The admin_required
decorator gates every route here. (Reconciliation has no page: it runs as a
nightly check via `manage.py reconcile`; see deploy/stock-reconcile.timer.)
"""

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, Response, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user

from . import get_db
from . import db
from .auth import admin_required
from .forms import (
    ConfirmForm, CorrectIntakeForm, CreateUserForm, OrderForm,
    ResetPasswordForm, ReverseForm, RoleForm,
)

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/admin/thresholds", methods=["GET", "POST"])
@admin_required
def thresholds():
    conn = get_db()
    if request.method == "POST":
        product = request.form.get("product_name", "").strip()
        raw = request.form.get("min_qty", "").strip()
        min_qty = None
        if raw != "":
            try:
                min_qty = int(raw)
            except ValueError:
                min_qty = -1
            if min_qty < 0:
                flash("Soglia non valida.", "error")
                return redirect(url_for("admin.thresholds"))
        if product:
            try:
                db.set_product_threshold(conn, product, min_qty)
                flash("Soglia aggiornata." if min_qty is not None else "Soglia rimossa.", "info")
            except sqlite3.OperationalError:
                flash("Database occupato, riprova.", "error")
        return redirect(url_for("admin.thresholds"))
    return render_template("thresholds.html",
                           rows=db.list_products_with_stock_and_thresholds(conn))


@admin_bp.route("/orders/new", methods=["GET", "POST"])
@admin_required
def orders_new():
    form = OrderForm()
    if form.validate_on_submit():
        batches = []
        for row in form.batches.entries:
            name = (row["product_name"].data or "").strip()
            quantity = row["quantity"].data
            if not name and not quantity:
                continue  # blank row: ignore it
            if not name or not quantity:
                flash("Riga incompleta: indica sia nome prodotto sia quantità.", "error")
                return render_template("orders_new.html", form=form)
            batches.append({
                "type": row["type"].data,
                "product_name": name,
                "manufacturer": (row["manufacturer"].data or "").strip(),
                "batch_code": (row["batch_code"].data or "").strip(),
                "expiring_date": (row["expiring_date"].data or "").strip(),
                "quality_flag": 1 if row["quality_flag"].data else 0,
                "quantity": quantity,
                "location": (row["location"].data or "inventory").strip() or "inventory",
            })
        if not batches:
            flash("Aggiungi almeno un lotto (nome prodotto + quantità).", "error")
            return render_template("orders_new.html", form=form)

        conn = get_db()
        try:
            order_number, supply_numbers = db.create_order(
                conn, current_user.id, form.order_date.data or None,
                form.notes.data or None, batches)
        except db.StockBusy:
            flash("Database occupato, riprova.", "error")
            return render_template("orders_new.html", form=form)
        except (db.StockError, sqlite3.IntegrityError):
            flash("Impossibile creare l'ordine: dati non validi.", "error")
            return render_template("orders_new.html", form=form)
        flash(f"Ordine n. {order_number} creato con i lotti "
              f"{', '.join(str(n) for n in supply_numbers)}.", "info")
        return redirect(url_for("main.inventory"))
    return render_template("orders_new.html", form=form)


@admin_bp.route("/batch/<int:supply_number>/remove", methods=["POST"])
@admin_required
def remove_batch(supply_number):
    form = ConfirmForm()
    if form.validate_on_submit():
        conn = get_db()
        try:
            total = db.remove_batch(conn, current_user.id, supply_number)
            flash(f"Lotto {supply_number} rimosso ({total} unità archiviate).", "info")
        except db.StockBusy:
            flash("Database occupato, riprova.", "error")
        except db.StockError as e:
            flash(f"Impossibile rimuovere il lotto: {e}", "error")
    else:
        flash("Richiesta non valida.", "error")
    return redirect(url_for("main.inventory"))


@admin_bp.route("/archive/<int:archive_id>/restore", methods=["GET", "POST"])
@admin_required
def archive_restore(archive_id):
    """Bring units archived by mistake back into inventory. GET asks how many;
    POST performs the (partial) restore. The bounds are enforced in app/db.py."""
    conn = get_db()
    entry = conn.execute(
        "SELECT a.id, a.supply_number, a.quantity, a.causale, a.timestamp, "
        "  b.product_name "
        "FROM archive a JOIN batches b ON b.supply_number = a.supply_number "
        "WHERE a.id = ?", (archive_id,),
    ).fetchone()
    if entry is None:
        flash("Voce di archivio inesistente.", "error")
        return redirect(url_for("main.archive"))
    remaining = entry["quantity"] - db.restored_so_far(conn, archive_id)

    if request.method == "POST":
        try:
            n = int(request.form.get("quantity", "0"))
        except (TypeError, ValueError):
            n = 0
        try:
            qty = db.restore_to_inventory(conn, current_user.id, archive_id, n)
            flash(f"Ripristinate {qty} unità in magazzino.", "info")
            return redirect(url_for("main.archive"))
        except db.StockError as e:
            flash(f"Impossibile ripristinare: {e}", "error")
            return redirect(url_for("admin.archive_restore", archive_id=archive_id))

    if entry["quantity"] <= 0 or remaining <= 0:
        flash("Questa voce non è ripristinabile.", "error")
        return redirect(url_for("main.archive"))
    return render_template("restore.html", entry=entry, remaining=remaining)


@admin_bp.route("/batch/<int:supply_number>/correct", methods=["GET"])
@admin_required
def correct(supply_number):
    conn = get_db()
    batch = conn.execute(
        "SELECT * FROM batches WHERE supply_number = ?", (supply_number,)
    ).fetchone()
    if batch is None:
        flash("Lotto inesistente.", "error")
        return redirect(url_for("main.dashboard"))
    log_rows = conn.execute(
        "SELECT * FROM master_log WHERE supply_number = ? ORDER BY id DESC",
        (supply_number,),
    ).fetchall()
    intake_form = CorrectIntakeForm()
    reverse_form = ReverseForm(supply_number=supply_number)
    ref = request.args.get("ref", type=int)
    if ref:
        reverse_form.ref_log_id.data = ref
    qty = request.args.get("qty", type=int)
    if qty:
        reverse_form.quantity.data = qty
    return render_template("correct.html", batch=batch, log_rows=log_rows,
                           intake_form=intake_form, reverse_form=reverse_form)


@admin_bp.route("/batch/<int:supply_number>/correct/intake", methods=["POST"])
@admin_required
def correct_intake(supply_number):
    form = CorrectIntakeForm()
    if form.validate_on_submit():
        conn = get_db()
        try:
            delta = db.correct_intake(conn, current_user.id, supply_number,
                                      form.new_total.data, form.reason.data)
            flash(f"Carico corretto (delta {delta:+d}).", "info")
        except db.StockError as e:
            flash(f"Correzione non riuscita: {e}", "error")
    else:
        flash("Modulo di correzione non valido.", "error")
    return redirect(url_for("admin.correct", supply_number=supply_number))


@admin_bp.route("/batch/<int:supply_number>/correct/reverse", methods=["POST"])
@admin_required
def correct_reverse(supply_number):
    form = ReverseForm()
    if form.validate_on_submit():
        conn = get_db()
        try:
            db.reverse_entry(conn, current_user.id, form.ref_log_id.data,
                             supply_number, form.quantity.data,
                             form.to_state.data, form.reason.data)
            flash(f"Restituite {form.quantity.data} unità alle scorte attive.", "info")
        except db.StockError as e:
            flash(f"Annullamento non riuscito: {e}", "error")
    else:
        flash("Modulo di annullamento non valido.", "error")
    return redirect(url_for("admin.correct", supply_number=supply_number))


@admin_bp.route("/admin/log")
@admin_required
def log():
    conn = get_db()
    periodo = request.args.get("periodo", "tutto")
    sql = ("SELECT m.*, u.username FROM master_log m "
           "LEFT JOIN users u ON u.id = m.user_id")
    params = []
    days = {"mese": 30, "anno": 365}.get(periodo)
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        sql += " WHERE m.timestamp >= ?"
        params.append(cutoff)
    sql += " ORDER BY m.id DESC LIMIT 1000"
    rows = conn.execute(sql, params).fetchall()
    return render_template("log.html", rows=rows, periodo=periodo)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection.

    A cell whose text begins with = + - @ (or a tab/CR) is treated as a formula
    by Excel/LibreOffice. Prefix such a value with a single quote so it is read
    as plain text. Non-string values (ints, None) pass through unchanged."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@admin_bp.route("/admin/log/export")
@admin_required
def log_export():
    """Read-only export of the whole master log as CSV or JSONL."""
    fmt = request.args.get("fmt", "csv")
    conn = get_db()
    rows = conn.execute("SELECT * FROM master_log ORDER BY id").fetchall()
    columns = rows[0].keys() if rows else [
        "id", "timestamp", "user_id", "action", "supply_number", "from_state",
        "to_state", "quantity", "causale", "ref_log_id", "details"]

    if fmt == "jsonl":
        body = "\n".join(json.dumps(dict(r)) for r in rows)
        return Response(body, mimetype="application/x-ndjson",
                        headers={"Content-Disposition":
                                 "attachment; filename=master_log.jsonl"})

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([_csv_safe(r[c]) for c in columns])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=master_log.csv"})


@admin_bp.route("/admin/users")
@admin_required
def users():
    conn = get_db()
    return render_template(
        "users.html", users=db.list_users(conn),
        create_form=CreateUserForm(), role_form=RoleForm(),
        reset_form=ResetPasswordForm(), confirm_form=ConfirmForm())


@admin_bp.route("/admin/users/new", methods=["POST"])
@admin_required
def users_new():
    form = CreateUserForm()
    if form.validate_on_submit():
        conn = get_db()
        try:
            db.admin_create_user(conn, current_user.id,
                                 form.username.data.strip(),
                                 form.password.data, "user")
            flash(f"Utente '{form.username.data.strip()}' creato.", "info")
        except db.StockError as e:
            flash(f"Impossibile creare l'utente: {e}", "error")
    else:
        flash("Modulo utente non valido.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/<int:user_id>/deactivate", methods=["POST"])
@admin_required
def users_deactivate(user_id):
    if ConfirmForm().validate_on_submit():
        conn = get_db()
        try:
            db.set_user_active(conn, current_user.id, user_id, 0)
            flash("Utente disattivato.", "info")
        except db.StockError as e:
            flash(f"Impossibile disattivare: {e}", "error")
    else:
        flash("Richiesta non valida.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/<int:user_id>/reactivate", methods=["POST"])
@admin_required
def users_reactivate(user_id):
    if ConfirmForm().validate_on_submit():
        conn = get_db()
        try:
            db.set_user_active(conn, current_user.id, user_id, 1)
            flash("Utente riattivato.", "info")
        except db.StockError as e:
            flash(f"Impossibile riattivare: {e}", "error")
    else:
        flash("Richiesta non valida.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def users_role(user_id):
    form = RoleForm()
    if form.validate_on_submit():
        conn = get_db()
        try:
            db.set_user_role(conn, current_user.id, user_id, form.role.data)
            flash("Ruolo aggiornato.", "info")
        except db.StockError as e:
            flash(f"Impossibile cambiare ruolo: {e}", "error")
    else:
        flash("Modulo ruolo non valido.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def users_reset_password(user_id):
    form = ResetPasswordForm()
    if form.validate_on_submit():
        conn = get_db()
        try:
            db.reset_password(conn, current_user.id, user_id, form.new_password.data)
            flash("Password reimpostata.", "info")
        except db.StockError as e:
            flash(f"Impossibile reimpostare la password: {e}", "error")
    else:
        flash("Richiesta non valida.", "error")
    return redirect(url_for("admin.users"))
