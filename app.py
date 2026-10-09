import os
from datetime import date, datetime, timedelta
from decimal import Decimal

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

load_dotenv()

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
if not app.config["SECRET_KEY"] or len(app.config["SECRET_KEY"]) < 32:
    raise RuntimeError("Set a strong SECRET_KEY in your .env file.")

database_url = os.environ.get(
    "DATABASE_URL", "sqlite:///expenseflow.db"
)

if database_url.startswith("postgres://"):
    database_url = "postgresql://" + database_url[len("postgres://"):]

if database_url.startswith("postgresql://"):
    database_url = database_url.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

if os.environ.get("COOKIE_SECURE", "false").lower() == "true":
    app.config["SESSION_COOKIE_SECURE"] = True

db = SQLAlchemy(app)
csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."


# ---------------- DATABASE MODELS ----------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    email = db.Column(
        db.String(255), unique=True, nullable=False, index=True
    )
    password_hash = db.Column(db.String(255), nullable=False)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    kind = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200), default="")
    date = db.Column(db.Date, nullable=False, default=date.today)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def money(value):
    return float(value or 0)


def user_transactions():
    # Always restrict financial records to the logged-in user.
    return Transaction.query.filter_by(user_id=current_user.id)


def summary_for(query):
    income = query.filter_by(kind="income").with_entities(
        func.coalesce(func.sum(Transaction.amount), 0)
    ).scalar()

    expenses = query.filter_by(kind="expense").with_entities(
        func.coalesce(func.sum(Transaction.amount), 0)
    ).scalar()

    income = Decimal(str(income or 0))
    expenses = Decimal(str(expenses or 0))

    return {
        "income": money(income),
        "expenses": money(expenses),
        "balance": money(income - expenses),
    }


# ---------------- LOGIN AND SIGNUP ----------------

@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or len(username) > 50:
            flash("Enter a valid username.", "error")
        elif not email or len(email) > 255 or "@" not in email:
            flash("Enter a valid email address.", "error")
        elif len(password) < 12 or len(password) > 128:
            flash("Password must contain 12–128 characters.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "error")
        else:
            user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
            )
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user, fresh=True)
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have logged out.", "success")
    return redirect(url_for("login"))


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():
    transactions = (
        user_transactions()
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(10)
        .all()
    )

    totals = summary_for(user_transactions())

    return render_template(
        "dashboard.html",
        transactions=transactions,
        income=totals["income"],
        expenses=totals["expenses"],
        balance=totals["balance"],
    )


# ---------------- ADD TRANSACTION ----------------

@app.route("/transactions/add", methods=["POST"])
@login_required
def add_transaction():
    kind = request.form.get("kind", "")
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()

    try:
        amount = Decimal(request.form.get("amount", "0"))
        transaction_date = datetime.strptime(
            request.form.get("date", ""), "%Y-%m-%d"
        ).date()

        if (
            not amount.is_finite()
            or amount <= 0
            or amount > Decimal("9999999999.99")
        ):
            raise ValueError

        if kind not in ("income", "expense"):
            raise ValueError

        if not category or len(category) > 50 or len(description) > 200:
            raise ValueError

    except (ValueError, TypeError):
        flash("Please check the transaction details.", "error")
        return redirect(url_for("dashboard"))

    transaction = Transaction(
        user_id=current_user.id,
        kind=kind,
        amount=amount,
        category=category,
        description=description,
        date=transaction_date,
    )

    db.session.add(transaction)
    db.session.commit()

    flash("Transaction added successfully!", "success")
    return redirect(url_for("dashboard"))


# ---------------- SUMMARY API ----------------

@app.route("/api/summary")
@login_required
def api_summary():
    totals = summary_for(user_transactions())

    today = date.today()
    month_start = today.replace(day=1)

    month_totals = summary_for(
        user_transactions().filter(Transaction.date >= month_start)
    )

    return jsonify({
        "success": True,
        "overall": totals,
        "this_month": month_totals,
        "currency": "INR",
    })


# ---------------- ANALYTICS API ----------------

@app.route("/api/analytics")
@login_required
def api_analytics():
    today = date.today()

    # Last six calendar months, including the current month.
    first_month = (today.replace(day=1) - timedelta(days=150))
    first_month = first_month.replace(day=1)

    records = (
        user_transactions()
        .filter(Transaction.date >= first_month)
        .order_by(Transaction.date.asc())
        .all()
    )

    monthly = {}
    categories = {}

    for offset in range(5, -1, -1):
        month_date = (today.replace(day=1) - timedelta(days=offset * 30))
        key = month_date.strftime("%Y-%m")
        monthly[key] = {
            "month": key,
            "income": 0.0,
            "expenses": 0.0,
        }

    for item in records:
        key = item.date.strftime("%Y-%m")

        if key not in monthly:
            continue

        amount = money(item.amount)

        if item.kind == "income":
            monthly[key]["income"] += amount
        elif item.kind == "expense":
            monthly[key]["expenses"] += amount
            categories[item.category] = (
                categories.get(item.category, 0.0) + amount
            )

    # Return category spending for the current month only.
    current_month_categories = {}
    for item in records:
        if (
            item.kind == "expense"
            and item.date.year == today.year
            and item.date.month == today.month
        ):
            current_month_categories[item.category] = (
                current_month_categories.get(item.category, 0.0)
                + money(item.amount)
            )

    return jsonify({
        "success": True,
        "monthly": list(monthly.values()),
        "categories": [
            {"category": name, "amount": round(amount, 2)}
            for name, amount in sorted(
                current_month_categories.items(),
                key=lambda pair: pair[1],
                reverse=True,
            )
        ],
    })


# ---------------- TRANSACTION API ----------------

@app.route("/api/transactions")
@login_required
def api_transactions():
    query = user_transactions()

    kind = request.args.get("kind", "")
    if kind in ("income", "expense"):
        query = query.filter_by(kind=kind)

    transactions = (
        query.order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(100)
        .all()
    )

    return jsonify({
        "success": True,
        "transactions": [
            {
                "id": item.id,
                "kind": item.kind,
                "amount": money(item.amount),
                "category": item.category,
                "description": item.description or "",
                "date": item.date.isoformat(),
            }
            for item in transactions
        ],
    })


# ---------------- DATABASE INITIALIZATION ----------------

@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("Database tables created.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    app.run(
        debug=os.environ.get("FLASK_DEBUG") == "1",
        host="127.0.0.1",
        port=5000,
    )