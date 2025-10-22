from flask import Flask, request, jsonify, render_template, abort, redirect
import pymysql
pymysql.install_as_MySQLdb()

from flask_mysqldb import MySQL
from flask_login import (
    LoginManager, login_user, logout_user, login_required,
    current_user, UserMixin
)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # TODO: use an environment variable in production

# ========================
# MySQL CONFIG + INIT
# ========================
app.config['MYSQL_HOST'] = 'localhost'
app.config['MYSQL_USER'] = 'root'
app.config['MYSQL_PASSWORD'] = 'MYSQL54321!'   # TODO: env var in production
app.config['MYSQL_DB'] = 'user_management'
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'  # return dict rows
mysql = MySQL(app)

# ========================
# LOGIN MANAGER
# ========================
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ========================
# HELPERS
# ========================
def is_admin() -> bool:
    return getattr(current_user, 'role', None) == 'Admin'

def role_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated or getattr(current_user, 'role', None) not in roles:
                return abort(403)
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper

# ========================
# USER MODEL + LOADER
# ========================
class User(UserMixin):
    def __init__(self, id, name, email, password, role):
        self.id = id
        self.name = name
        self.email = email
        self.password = password
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    cur = mysql.connection.cursor()
    cur.execute(
        "SELECT id, name, email, password, role FROM users WHERE id = %s",
        (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    if row:
        return User(row['id'], row['name'], row['email'], row['password'], row['role'])
    return None

# ========================
# ROUTES
# ========================

# --- Login ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '')
        password = request.form.get('password', '')

        cur = mysql.connection.cursor()
        cur.execute(
            "SELECT id, name, email, password, role FROM users WHERE email = %s",
            (email,)
        )
        row = cur.fetchone()
        cur.close()

        if row and check_password_hash(row['password'], password):
            user = User(row['id'], row['name'], row['email'], row['password'], row['role'])
            login_user(user)
            return redirect('/')
        return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')

# --- Logout ---
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/login')

# --- Home Page ---
@app.route('/')
@login_required
def home():
    return render_template('home.html')

# --- Add User (Admin Only) ---
@app.route('/user', methods=['POST'])
@login_required
@role_required('Admin')
def add_user():
    if not request.is_json:
        return jsonify(error="Invalid submission: expected JSON"), 400

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip()
    password_raw = data.get('password') or ''  # require from client
    role = data.get('role') or 'Customer'      # default to Customer, not Admin

    if not name or not email or not password_raw:
        return jsonify(error="name, email, and password are required"), 400

    password = generate_password_hash(password_raw)

    cur = mysql.connection.cursor()
    cur.execute(
        "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, %s)",
        (name, email, password, role)
    )
    mysql.connection.commit()
    cur.close()
    return jsonify(message="User added successfully"), 201

# --- View Users (All Logged-In Users) ---
@app.route('/users', methods=['GET'])
@login_required
def get_users():
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, name, email, role FROM users")
    rows = cur.fetchall()
    cur.close()

    if is_admin():
        # Admins see everything
        return jsonify(rows)

    # Non-admins: hide email
    stripped = [{'id': r['id'], 'name': r['name'], 'role': r['role']} for r in rows]
    return jsonify(stripped)

# --- Delete User (Admin Only) ---
@app.route('/user/<int:id>', methods=['DELETE'])
@login_required
@role_required('Admin')
def delete_user(id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM users WHERE id = %s", (id,))
    mysql.connection.commit()
    cur.close()
    return jsonify(message="User deleted successfully")
    
# ========================
# MAIN
# ========================
if __name__ == '__main__':
    app.run(debug=True)