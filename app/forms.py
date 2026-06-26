"""Flask-WTF form definitions.

Every form here is a FlaskForm, which means Flask-WTF automatically requires a
valid CSRF token on submit. Templates render that token with
{{ form.csrf_token }} (or {{ csrf_token() }} for the bare confirm forms).

The order form uses FieldList(FormField(...)) so one POST can carry
several batch rows at once with per-field validation and a single CSRF token.
FieldList/FormField are the standard WTForms way to repeat a sub-form; the form
renders a few blank rows and ignores any row left without a product name.
"""

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField, FieldList, FormField, IntegerField, PasswordField,
    SelectField, StringField, TextAreaField,
)
from wtforms.validators import (
    DataRequired, InputRequired, Length, NumberRange, Optional,
)

TYPE_CHOICES = [("microbiology", "microbiologia"), ("primers", "primer"),
                ("other", "altro")]


class LoginForm(FlaskForm):
    username = StringField("Nome utente", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("Password", validators=[DataRequired()])


class BatchSubForm(FlaskForm):
    """One batch row inside an order. Meta.csrf=False: the parent OrderForm
    carries the single CSRF token for the whole POST."""

    class Meta:
        csrf = False

    type = SelectField("Tipo", choices=TYPE_CHOICES, validate_choice=False)
    product_name = StringField("Nome prodotto", validators=[Optional(), Length(max=200)])
    manufacturer = StringField("Produttore", validators=[Optional(), Length(max=200)])
    batch_code = StringField("Codice lotto (produttore)", validators=[Optional(), Length(max=200)])
    expiring_date = StringField("Scadenza (AAAA-MM-GG)", validators=[Optional()])
    quality_flag = BooleanField("Riservato CQ")
    quantity = IntegerField("Unità", validators=[Optional(), NumberRange(min=1)])
    location = StringField("Posizione", validators=[Optional(), Length(max=100)])


class OrderForm(FlaskForm):
    order_date = StringField("Data ricezione (AAAA-MM-GG)", validators=[Optional()])
    notes = TextAreaField("Note", validators=[Optional(), Length(max=1000)])
    batches = FieldList(FormField(BatchSubForm), min_entries=5, max_entries=50)


class MoveForm(FlaskForm):
    """Identify a batch by the smallest set of attributes and move
    `quantity` units inventory -> lab. Empty identifier fields are ignored; the
    resolver fills any ambiguity with FEFO + lowest supply_number."""

    supply_number = IntegerField("ID lotto", validators=[Optional()])
    product_name = StringField("Nome prodotto", validators=[Optional()])
    manufacturer = StringField("Produttore", validators=[Optional()])
    type = SelectField("Tipo", choices=[("", "qualsiasi")] + TYPE_CHOICES, validators=[Optional()])
    quantity = IntegerField("Unità", validators=[DataRequired(), NumberRange(min=1)])
    to_location = StringField("Posizione in laboratorio", validators=[Optional(), Length(max=100)])


class ArchiveActionForm(FlaskForm):
    """Shared form for consume / ineligible / expired. `from_state` chooses
    whether the units leave from the lab (default) or straight from inventory."""

    supply_number = IntegerField("ID lotto", validators=[Optional()])
    product_name = StringField("Nome prodotto", validators=[Optional()])
    manufacturer = StringField("Produttore", validators=[Optional()])
    type = SelectField("Tipo", choices=[("", "qualsiasi")] + TYPE_CHOICES, validators=[Optional()])
    quantity = IntegerField("Unità", validators=[DataRequired(), NumberRange(min=1)])
    from_state = SelectField("Da", choices=[("lab", "laboratorio"), ("inventory", "magazzino")])


class ConfirmForm(FlaskForm):
    """An empty form whose only job is to carry a CSRF token for a confirm
    button (e.g. remove batch)."""


class CorrectIntakeForm(FlaskForm):
    new_total = IntegerField("Totale ricevuto corretto",
                             validators=[InputRequired(), NumberRange(min=0, max=1_000_000)])
    reason = StringField("Motivo", validators=[DataRequired(), Length(max=500)])


class ReverseForm(FlaskForm):
    ref_log_id = IntegerField("ID voce registro originale", validators=[DataRequired()])
    supply_number = IntegerField("ID lotto", validators=[DataRequired()])
    quantity = IntegerField("Unità da restituire", validators=[DataRequired(), NumberRange(min=1)])
    to_state = SelectField("Restituisci a", choices=[("lab", "laboratorio"), ("inventory", "magazzino")])
    reason = StringField("Motivo", validators=[DataRequired(), Length(max=500)])


class CreateUserForm(FlaskForm):
    """Create a new account. The app creates only regular users; admin accounts
    are provisioned out-of-band (the CLI), so there is no role selector here.
    Passwords must be at least 12 characters."""

    username = StringField("Nome utente", validators=[DataRequired(), Length(max=64)])
    password = PasswordField(
        "Password iniziale",
        validators=[DataRequired(), Length(min=12, message="Almeno 12 caratteri.")])


class RoleForm(FlaskForm):
    """Change an existing account's role."""

    role = SelectField("Ruolo", choices=[("user", "utente"), ("admin", "amministratore")])


class ResetPasswordForm(FlaskForm):
    """Set a new password for an account."""

    new_password = PasswordField(
        "Nuova password",
        validators=[DataRequired(), Length(min=12, message="Almeno 12 caratteri.")])


class ChangePasswordForm(FlaskForm):
    """Self-service password change for the logged-in user's own password.

    Requires the current password so a left-open session cannot change
    it. The new password follows the same 12-char minimum as admin resets."""

    current_password = PasswordField("Password attuale", validators=[DataRequired()])
    new_password = PasswordField(
        "Nuova password",
        validators=[DataRequired(), Length(min=12, message="Almeno 12 caratteri.")])


class FeedbackForm(FlaskForm):
    """A short message any logged-in user can leave from the feedback widget."""

    message = TextAreaField("Il tuo feedback",
                            validators=[DataRequired(), Length(max=2000)])
