import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId
import pandas as pd
from groq import Groq
import hashlib
import secrets
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ── Configuration ─────────────────────────────────────────────────────────────
GROQ_API_KEY  = os.environ.get("GROQ_API_KEY",  "YOUR_GROQ_API_KEY")
MONGODB_URI   = os.environ.get("MONGODB_URI",   "YOUR_MONGODB_URI")

client_groq   = Groq(api_key=GROQ_API_KEY)
client_mongo  = MongoClient(MONGODB_URI)
db            = client_mongo["financeos"]

# ── MongoDB Collections ───────────────────────────────────────────────────────
users_col    = db["users"]
txns_col     = db["transactions"]
budgets_col  = db["budgets"]

# ── Create Indexes ────────────────────────────────────────────────────────────
users_col.create_index("email", unique=True)
txns_col.create_index("user_id")

# ── Helpers ───────────────────────────────────────────────────────────────────
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def logged_in():
    return "user_id" in session

def get_uid():
    return session.get("user_id")


# ── Auth Routes ───────────────────────────────────────────────────────────────
@app.route("/")
def home():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    data  = request.json
    email = data.get("email", "").strip().lower()
    pwd   = hash_password(data.get("password", ""))
    user  = users_col.find_one({"email": email, "password": pwd})
    if user:
        session["user_id"]   = str(user["_id"])
        session["user_name"] = user["name"]
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Invalid email or password"}), 401


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("login.html")
    data  = request.json
    name  = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    pwd   = data.get("password", "")
    if not name or not email or not pwd:
        return jsonify({"status": "error", "message": "All fields are required"}), 400
    if len(pwd) < 6:
        return jsonify({"status": "error", "message": "Password must be at least 6 characters"}), 400
    try:
        result = users_col.insert_one({
            "name":       name,
            "email":      email,
            "password":   hash_password(pwd),
            "created_at": datetime.utcnow()
        })
        budgets_col.insert_one({
            "user_id": str(result.inserted_id),
            "amount":  50000
        })
        return jsonify({"status": "success"})
    except DuplicateKeyError:
        return jsonify({"status": "error", "message": "Email already registered"}), 409
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/me")
def me():
    if not logged_in():
        return jsonify({"status": "error"}), 401
    return jsonify({"name": session.get("user_name"), "id": get_uid()})


# ── Transaction Routes ────────────────────────────────────────────────────────
@app.route("/add", methods=["POST"])
def add_transaction():
    if not logged_in():
        return jsonify({"status": "error", "message": "Not logged in"}), 401
    data = request.json
    if not data.get("date") or not data.get("amount"):
        return jsonify({"status": "error", "message": "Date and amount required"}), 400
    try:
        txns_col.insert_one({
            "user_id":    get_uid(),
            "date":       data["date"],
            "category":   data["category"],
            "amount":     float(data["amount"]),
            "created_at": datetime.utcnow()
        })
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/delete/<txn_id>", methods=["DELETE"])
def delete_transaction(txn_id):
    if not logged_in():
        return jsonify({"status": "error"}), 401
    try:
        txns_col.delete_one({"_id": ObjectId(txn_id), "user_id": get_uid()})
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/upload", methods=["POST"])
def upload_csv():
    if not logged_in():
        return jsonify({"status": "error"}), 401
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    file = request.files["file"]
    try:
        df = pd.read_csv(file)
        # Smart column mapping
        mapping = {}
        for col in df.columns:
            c = col.lower().strip()
            if "date" in c:
                mapping[col] = "Date"
            elif "cat" in c or "desc" in c or "type" in c:
                mapping[col] = "Category"
            elif any(x in c for x in ["am","cost","price","pkr","total"]):
                mapping[col] = "Amount"
        df = df.rename(columns=mapping)
        required = ["Date", "Category", "Amount"]
        missing  = [r for r in required if r not in df.columns]
        if missing:
            return jsonify({"status": "error", "message": f"Missing: {', '.join(missing)}"}), 400
        df["Amount"]   = pd.to_numeric(df["Amount"], errors="coerce").fillna(0)
        df["Date"]     = df["Date"].astype(str)
        df["Category"] = df["Category"].astype(str)
        docs = []
        for _, row in df.iterrows():
            docs.append({
                "user_id":    get_uid(),
                "date":       row["Date"],
                "category":   row["Category"],
                "amount":     float(row["Amount"]),
                "created_at": datetime.utcnow()
            })
        if docs:
            txns_col.insert_many(docs)
        return jsonify({"status": "success", "imported": len(docs)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/set_budget", methods=["POST"])
def set_budget():
    if not logged_in():
        return jsonify({"status": "error"}), 401
    data = request.json
    try:
        budgets_col.update_one(
            {"user_id": get_uid()},
            {"$set": {"amount": float(data["amount"])}},
            upsert=True
        )
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/get_chart_data")
def get_chart_data():
    if not logged_in():
        return jsonify({"status": "error"}), 401
    try:
        uid    = get_uid()
        rows   = list(txns_col.find({"user_id": uid}))
        budget = budgets_col.find_one({"user_id": uid})
        budget_amount = float(budget["amount"]) if budget else 50000

        if not rows:
            return jsonify({"categories":[],"monthly":[],"total":0,"forecast":0,"budget":budget_amount,"transactions":[]})

        df = pd.DataFrame(rows)
        df["amount"] = df["amount"].astype(float)
        total_spent  = float(df["amount"].sum())

        # Category totals
        cat_df = df.groupby("category")["amount"].sum().reset_index()
        cat_df.columns = ["category", "total"]
        cat_df = cat_df.sort_values("total", ascending=False)

        # Monthly totals
        df["month"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%b %Y")
        monthly_df  = df.groupby("month")["amount"].sum().reset_index()
        monthly_df.columns = ["month", "total"]

        # Forecast
        monthly_vals = monthly_df["total"].values
        if len(monthly_vals) >= 2:
            trend    = float(monthly_vals[-1]) - float(monthly_vals[-2])
            forecast = round(max(float(monthly_vals[-1]) + trend * 0.5, 0), 2)
        else:
            forecast = round(total_spent * 1.05, 2)

        # Recent transactions
        recent = df.sort_values("date", ascending=False).head(10)
        txns   = []
        for _, row in recent.iterrows():
            txns.append({
                "id":       str(row["_id"]),
                "date":     str(row["date"]),
                "category": row["category"],
                "amount":   float(row["amount"])
            })

        return jsonify({
            "categories":   cat_df.to_dict(orient="records"),
            "monthly":      monthly_df.to_dict(orient="records"),
            "total":        total_spent,
            "forecast":     forecast,
            "budget":       budget_amount,
            "transactions": txns
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/ask_ai", methods=["POST"])
def ask_ai():
    if not logged_in():
        return jsonify({"reply": "Please log in first."}), 401
    try:
        user_q = request.json.get("query", "")
        uid    = get_uid()
        rows   = list(txns_col.find({"user_id": uid}))
        budget = budgets_col.find_one({"user_id": uid})

        budget_amount = float(budget["amount"]) if budget else 50000
        df            = pd.DataFrame(rows) if rows else pd.DataFrame()
        total_spent   = float(df["amount"].astype(float).sum()) if not df.empty else 0
        cat_summary   = df.groupby("category")["amount"].apply(lambda x: float(x.astype(float).sum())).to_dict() if not df.empty else {}
        savings       = budget_amount - total_spent

        system_prompt = f"""You are a smart personal finance advisor for a user in Pakistan.
Financial data:
- Monthly Budget: PKR {budget_amount:,.0f}
- Total Spending:  PKR {total_spent:,.0f}
- Savings:         PKR {savings:,.0f}
- By Category:     {cat_summary}
Rules: specific data-driven advice, friendly, concise, PKR currency, under 120 words, bullet points."""

        completion = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_q}
            ],
            max_tokens=300, temperature=0.7
        )
        return jsonify({"reply": completion.choices[0].message.content})
    except Exception as e:
        return jsonify({"reply": f"AI error: {str(e)}"})


@app.route("/clear", methods=["POST"])
def clear_data():
    if not logged_in():
        return jsonify({"status": "error"}), 401
    try:
        txns_col.delete_many({"user_id": get_uid()})
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=False)
