 # ============================================================================
# SECTION 1: IMPORTS
# ============================================================================

from flask import Flask, request, jsonify, render_template, redirect, abort, send_file
from flask_mysqldb import MySQL
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user, UserMixin
)
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
import os
import sqlite3, csv
from werkzeug.utils import secure_filename
import re
from openai import OpenAI
from groq import Groq
import google.generativeai as genai

#from google import genai
import openpyxl
import PyPDF2
import base64
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

# ============================================================================
# SECTION 2: FLASK APP CONFIGURATION
# ============================================================================

app = Flask(__name__)
app.secret_key = "f6c9a97f5519dca4cb070015296c7b87475a5377e4247b584fc200758d065992"

FTS_DB_PATH = os.path.join(os.path.dirname(__file__), "datasets_fts.db")

DEFAULT_OPENAI_KEY = os.getenv('OPENAI_API_KEY', '')
DEFAULT_GROQ_KEY = os.getenv('GROQ_API_KEY', '')
DEFAULT_GEMINI_KEY = os.getenv('GEMINI_API_KEY', '')

# ============================================================================
# SECTION 3: FULL-TEXT SEARCH (FTS5)
# ============================================================================

def _fts_conn():
    conn = sqlite3.connect(FTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def fts_init():
    conn = _fts_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS docs
        USING fts5(content, file, rownum UNINDEXED, tokenize='porter');
    """)
    conn.commit()
    conn.close()

def fts_clear_file(filename: str):
    conn = _fts_conn()
    conn.execute("DELETE FROM docs WHERE file=?", (filename,))
    conn.commit()
    conn.close()

def fts_add_rows(filename: str, rows: list[tuple[int, str]]):
    conn = _fts_conn()
    conn.executemany(
        "INSERT INTO docs (content, file, rownum) VALUES (?, ?, ?)",
        [(text or "", filename, int(rownum)) for rownum, text in rows if (text or "").strip()],
    )
    conn.commit()
    conn.close()

def fts_search(query: str, limit: int = 5):
    conn = _fts_conn()

    def _run(q: str):
        try:
            q = (q or "").strip().replace('"', '""')
            sql = """
              SELECT file, rownum, snippet(docs, 0, '', '', ' … ', 64) as snip
              FROM docs
              WHERE docs MATCH ?
              ORDER BY rank
              LIMIT ?;
            """
            return conn.execute(sql, (q, limit)).fetchall()
        except Exception:
            return []

    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", query or "")]
    STOP = {
        "a","an","the","is","are","was","were","be","been","being",
        "to","of","in","on","by","for","with","and","or","as","at","from",
        "that","this","it","its","into","over","about","after","before",
        "who","what","when","where","why","how","which","whats","whens","did","do","does"
    }
    content = [t for t in tokens if t not in STOP]

    def qterm(t: str) -> str:
        return f'"{t}"'

    attempts = []

    if len(content) >= 2:
        attempts.append(f'{qterm(content[0])} NEAR/6 {qterm(content[1])}')
        attempts.append(' AND '.join(qterm(t) for t in content))

    if content:
        attempts.append(' OR '.join(qterm(t) for t in content))
        attempts.append(qterm(content[0]))

    if not attempts and tokens:
        attempts.append(' OR '.join(qterm(t) for t in tokens))

    for q in attempts:
        rows = _run(q)
        if rows:
            out = [{"file": r["file"], "row": r["rownum"], "snippet": r["snip"]} for r in rows]
            conn.close()
            return out

    conn.close()
    return []

# ============================================================================
# SECTION 4: FILE PROCESSING
# ============================================================================

def extract_rows_simple(path: str):
    p = path.lower()
    out = []
    
    if p.endswith(".csv"):
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            r = csv.reader(f)
            rows = list(r)
        if not rows:
            return out
        header = rows[0]
        for i, row in enumerate(rows[1:], start=2):
            try:
                text = " | ".join(f"{h}: {v}" for h, v in zip(header, row))
            except Exception:
                text = " | ".join(row)
            out.append((i, text))
        return out
    
    elif p.endswith(".xlsx"):
        try:
            wb = openpyxl.load_workbook(path, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = list(ws.values)
                if not rows:
                    continue
                header = rows[0]
                for i, row in enumerate(rows[1:], start=2):
                    try:
                        text = f"[Sheet: {sheet_name}] " + " | ".join(
                            f"{h}: {v}" for h, v in zip(header, row) if v is not None
                        )
                    except Exception:
                        text = f"[Sheet: {sheet_name}] " + " | ".join(str(v) for v in row if v is not None)
                    if text.strip():
                        out.append((i, text))
            return out
        except Exception as e:
            return [(1, f"Error reading Excel file: {str(e)}")]
    
    elif p.endswith(".pdf"):
        try:
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page_num, page in enumerate(reader.pages, start=1):
                    text = page.extract_text()
                    if text.strip():
                        chunks = [c.strip() for c in text.split('\n\n') if c.strip()]
                        for chunk_idx, chunk in enumerate(chunks, start=1):
                            out.append((page_num * 1000 + chunk_idx, f"[Page {page_num}] {chunk[:2000]}"))
            return out
        except Exception as e:
            return [(1, f"Error reading PDF: {str(e)}")]
    
    else:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for idx, chunk in enumerate([c.strip() for c in content.split("\n\n") if c.strip()], start=1):
            out.append((idx, chunk[:2000]))
        return out
    
# ============================================================================
# SECTION 5: AI PROVIDER FUNCTION
# ============================================================================

def ask_ai_with_context(user_question: str, search_results: list, ai_provider: str = "local", api_key: str = "", model: str = "") -> dict:
    
    if search_results:
        context = "Here is relevant information from the uploaded datasets:\n\n"
        for i, hit in enumerate(search_results, 1):
            context += f"{i}. From file '{hit['file']}' (row {hit['row']}):\n"
            context += f"   {hit['snippet']}\n\n"
    else:
        context = "No relevant information found in the uploaded datasets.\n\n"
    
    # LOCAL MODE
    if ai_provider == "local":
        if not search_results:
            return {
                "reply": "I couldn't find anything relevant in the datasets. Please upload some data files first or try a different question.",
                "error": None
            }
        
        reply = "Here's what I found:\n\n" + "\n".join(
            f"📄 **{hit['file']}** (row {hit['row']})\n   {hit['snippet']}\n" 
            for hit in search_results
        )
        return {"reply": reply, "error": None}
    
    if not api_key or api_key.strip() == "":
        return {
            "reply": None,
            "error": f"Please enter your {ai_provider.upper()} API key to use AI-powered responses."
        }
    
    system_message = """You are a helpful assistant that answers questions based on uploaded dataset files. 
    When datasets are available, prioritize information from those datasets in your answer.
    If the datasets don't contain relevant information, you can use your general knowledge but mention that clearly.
    Be concise, accurate, and cite which file/row the information came from when using dataset information.
    Format your response in a clear, readable way."""
    
    user_message = f"{context}User question: {user_question}\n\nPlease provide a clear, helpful answer."
    
    # OPENAI MODE
    if ai_provider == "openai":
        try:
            openai_model = model if model else "gpt-4o-mini"
            
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=openai_model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message}
                ],
                max_tokens=500,
                temperature=0.7
            )
            
            return {
                "reply": response.choices[0].message.content.strip(),
                "error": None
            }
        except Exception as e:
            return {
                "reply": None,
                "error": f"OpenAI Error: {str(e)}"
            }
    
    # GROQ MODE
    elif ai_provider == "groq":
        try:
            client = Groq(api_key=api_key)
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message}
                ],
                max_tokens=500,
                temperature=0.7
            )
            
            return {
                "reply": response.choices[0].message.content.strip(),
                "error": None
            }
        except Exception as e:
            return {
                "reply": None,
                "error": f"Groq Error: {str(e)}"
            }
    
    # GEMINI MODE
    elif ai_provider == "gemini":
        try:
            client = genai.Client(api_key=api_key)
            full_prompt = f"{system_message}\n\n{user_message}"
            
            gemini_model = model if model else 'gemini-2.5-flash'
            
            models_to_try = [gemini_model]
            if gemini_model not in ['gemini-2.5-flash', 'gemini-2.0-flash-exp', 'gemini-1.5-flash', 'gemini-1.5-pro']:
                models_to_try.extend([
                    'gemini-2.5-flash',
                    'gemini-2.0-flash-exp',
                    'gemini-1.5-flash',
                    'gemini-1.5-pro',
                ])
            
            response = None
            last_error = None
            
            for model_name in models_to_try:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=full_prompt
                    )
                    break
                except Exception as e:
                    last_error = str(e)
                    continue
            
            if not response:
                return {
                    "reply": None,
                    "error": f"Gemini Error: Model '{gemini_model}' failed. Last error: {last_error}"
                }
            
            if hasattr(response, 'text'):
                reply_text = response.text.strip()
            elif hasattr(response, 'candidates') and response.candidates:
                reply_text = response.candidates[0].content.parts[0].text.strip()
            else:
                return {
                    "reply": None,
                    "error": "Gemini Error: Unable to extract text from response"
                }
            
            return {
                "reply": reply_text,
                "error": None
            }
            
        except Exception as e:
            error_msg = str(e)
            
            if "API_KEY_INVALID" in error_msg or "API key" in error_msg:
                return {
                    "reply": None,
                    "error": "Gemini Error: Invalid API key. Get one from https://aistudio.google.com/apikey"
                }
            elif "quota" in error_msg.lower() or "RESOURCE_EXHAUSTED" in error_msg:
                return {
                    "reply": None,
                    "error": "Gemini Error: Rate limit exceeded (15/min). Wait a minute and try again."
                }
            elif "SAFETY" in error_msg:
                return {
                    "reply": None,
                    "error": "Gemini Error: Response blocked by safety filters. Try rephrasing your question."
                }
            else:
                return {
                    "reply": None,
                    "error": f"Gemini Error: {error_msg}"
                }
    
    else:
        return {
            "reply": None,
            "error": f"Unknown AI provider: {ai_provider}"
        }
        
        
 # ============================================================================
# SECTION 6: MYSQL & AUTHENTICATION
# ============================================================================

app.config['MYSQL_HOST'] = os.getenv('MYSQL_HOST', 'localhost')
app.config['MYSQL_USER'] = os.getenv('MYSQL_USER', 'root')
app.config['MYSQL_PASSWORD'] = os.getenv('MYSQL_PASSWORD', 'MYSQL54321!')  # ⚠️ CHANGE THIS!
app.config['MYSQL_DB'] = os.getenv('MYSQL_DB', 'user_management')
mysql = MySQL(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

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
    cur.execute("SELECT id, name, email, password, role FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    return User(*row) if row else None

def role_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect('/login')
            if current_user.role not in roles:
                return abort(403)
            return fn(*args, **kwargs)
        return decorated
    return wrapper   

# ============================================================================
# SECTION 7: LOGIN/LOGOUT ROUTES
# ============================================================================

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        password = request.form.get('password') or ''
        
        cur = mysql.connection.cursor()
        cur.execute("SELECT id, name, email, password, role FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        cur.close()
        
        if row and check_password_hash(row[3], password):
            login_user(User(*row))
            return redirect('/')
        
        return render_template('login.html', error="Invalid credentials")
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/login')

@app.route('/')
@login_required
def home():
    return render_template('home.html', user_role=getattr(current_user, "role", "User"))

# ============================================================================
# SECTION 8: USER MANAGEMENT (CRUD)
# ============================================================================

@app.route('/user', methods=['POST'])
@login_required
@role_required('Admin')
def add_user():
    if not request.is_json:
        return jsonify(error="Expected JSON body"), 400

    data = request.get_json()
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip()
    password = (data.get('password') or '').strip()
    role = (data.get('role') or '').strip()

    if not all([name, email, password, role]):
        return jsonify(error="Name, email, password, and role are required"), 400

    hashed_pw = generate_password_hash(password)

    cur = mysql.connection.cursor()
    try:
        cur.execute(
            "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, %s)",
            (name, email, hashed_pw, role)
        )
        mysql.connection.commit()
        return jsonify(message="User added successfully"), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify(error=str(e)), 400
    finally:
        cur.close()

@app.route('/users', methods=['GET'])
@login_required
def get_users():
    cur = mysql.connection.cursor()
    cur.execute("SELECT id, name, email, role FROM users")
    rows = cur.fetchall()
    cur.close()

    if getattr(current_user, 'role', None) != 'Admin':
        return jsonify([{'id': r[0], 'name': r[1], 'role': r[3]} for r in rows])

    return jsonify([{'id': r[0], 'name': r[1], 'email': r[2], 'role': r[3]} for r in rows])

@app.route('/user/<int:user_id>', methods=['DELETE'])
@login_required
@role_required('Admin')
def delete_user(user_id):
    cur = mysql.connection.cursor()
    try:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        mysql.connection.commit()
        return jsonify(message="User deleted successfully")
    except Exception as e:
        mysql.connection.rollback()
        return jsonify(error=str(e)), 400
    finally:
        cur.close()
        
# ============================================================================
# SECTION 9: ADMIN FILE UPLOAD
# ============================================================================

try:
    fts_init()
except Exception:
    pass

@app.route("/upload", methods=["POST"])
@login_required
@role_required("Admin")
def upload_files():
    if "files" not in request.files:
        return jsonify(error="No files part in the request"), 400

    saved, skipped = [], []
    datasets_dir = os.path.join(os.path.dirname(__file__), "datasets")
    os.makedirs(datasets_dir, exist_ok=True)

    for f in request.files.getlist("files"):
        if not f:
            skipped.append("unknown")
            continue

        filename = secure_filename(f.filename)
        allowed_extensions = ('.csv', '.txt', '.xlsx', '.pdf')
        if not filename.lower().endswith(allowed_extensions):
            skipped.append(filename)
            continue

        dest = os.path.join(datasets_dir, filename)
        f.save(dest)
        saved.append(filename)

        try:
            fts_clear_file(filename)
            rows = extract_rows_simple(dest)
            fts_add_rows(filename, rows)
        except Exception as e:
            skipped.append(f"{filename} (index error)")

    return jsonify(saved=saved, skipped=skipped), 200

 # ============================================================================
# SECTION 10: USER FILE UPLOAD
# ============================================================================

@app.route("/chat/upload", methods=["POST"])
@login_required
def chat_upload_files():
    if "files" not in request.files:
        return jsonify(error="No files part in the request"), 400

    saved, skipped = [], []
    user_folder = f"user_{current_user.id}_files"
    user_datasets_dir = os.path.join(os.path.dirname(__file__), "datasets", user_folder)
    os.makedirs(user_datasets_dir, exist_ok=True)

    for f in request.files.getlist("files"):
        if not f:
            skipped.append("unknown")
            continue

        filename = secure_filename(f.filename)
        allowed_extensions = ('.csv', '.txt', '.xlsx', '.pdf')
        if not filename.lower().endswith(allowed_extensions):
            skipped.append(f"{filename} (unsupported format)")
            continue

        user_filename = f"{user_folder}/{filename}"
        dest = os.path.join(user_datasets_dir, filename)
        
        f.save(dest)
        saved.append(filename)

        try:
            fts_clear_file(user_filename)
            rows = extract_rows_simple(dest)
            fts_add_rows(user_filename, rows)
        except Exception as e:
            skipped.append(f"{filename} (index error)")

    return jsonify(saved=saved, skipped=skipped), 200

@app.route("/chat/files", methods=["GET"])
@login_required
def chat_get_files():
    import pathlib
    user_folder = f"user_{current_user.id}_files"
    user_dir = pathlib.Path(os.path.dirname(__file__)) / "datasets" / user_folder
    
    if not user_dir.exists():
        return jsonify(files=[])
    
    files = [f.name for f in user_dir.iterdir() if f.is_file()]
    return jsonify(files=sorted(files))

@app.route("/chat/files/<path:filename>", methods=["DELETE"])
@login_required
def chat_delete_file(filename):
    import pathlib
    user_folder = f"user_{current_user.id}_files"
    user_dir = pathlib.Path(os.path.dirname(__file__)) / "datasets" / user_folder
    file_path = user_dir / filename
    
    if not file_path.exists() or not file_path.is_file():
        return jsonify(error="File not found"), 404
    
    # Remove from FTS index FIRST
    user_filename = f"{user_folder}/{filename}"
    try:
        fts_clear_file(user_filename)
        print(f"✅ Cleared from FTS index: {user_filename}")
    except Exception as e:
        print(f"⚠️ FTS clear error for {user_filename}: {str(e)}")
    
    # Delete the actual file
    try:
        file_path.unlink()
        print(f"✅ Deleted file: {file_path}")
        return jsonify(status="deleted", file=filename, message=f"File '{filename}' deleted successfully")
    except Exception as e:
        print(f"❌ File delete error: {str(e)}")
        return jsonify(error=str(e)), 500

@app.route("/chat/download/<path:filename>", methods=["GET"])
@login_required
def chat_download_file(filename):
    import pathlib
    user_folder = f"user_{current_user.id}_files"
    user_dir = pathlib.Path(os.path.dirname(__file__)) / "datasets" / user_folder
    file_path = user_dir / filename
    
    if not file_path.exists():
        return jsonify(error="File not found"), 404
    
    return send_file(file_path, as_attachment=True, download_name=filename)


# ============================================================================
# SECTION 11: CHAT ROUTE
# ============================================================================

@app.route("/chat/send", methods=["POST"])
@login_required
def chat_send():
    data = request.get_json(silent=True) or {}
    q = (data.get("message") or "").strip()
    ai_provider = data.get("ai_provider", "local")
    api_key = data.get("api_key", "")
    model = data.get("model", "")
    
    if not q:
        return jsonify(error="Empty message."), 400

    try:
        hits = fts_search(q, limit=5)
    except Exception:
        return jsonify(error="Search is unavailable right now."), 500

    result = ask_ai_with_context(q, hits, ai_provider, api_key, model)
    
    if result["error"]:
        return jsonify(error=result["error"]), 400
    
    return jsonify(reply=result["reply"], sources=hits, provider=ai_provider), 200


# ============================================================================
# SECTION 11B: FTS REFRESH ROUTES
# ============================================================================

@app.route("/admin/fts/rebuild", methods=["POST"])
@login_required
@role_required("Admin")
def rebuild_fts():
    """Rebuild entire FTS index from scratch"""
    import pathlib
    
    try:
        # Clear entire FTS database
        conn = _fts_conn()
        conn.execute("DELETE FROM docs")
        conn.commit()
        conn.close()
        
        indexed_files = []
        failed_files = []
        
        # Reindex all files
        datasets_dir = pathlib.Path(os.path.dirname(__file__)) / "datasets"
        
        # Index admin files
        for f in datasets_dir.iterdir():
            if f.is_file():
                try:
                    rows = extract_rows_simple(str(f))
                    fts_add_rows(f.name, rows)
                    indexed_files.append(f.name)
                except Exception as e:
                    failed_files.append(f"{f.name}: {str(e)}")
        
        # Index user files
        for user_folder in datasets_dir.iterdir():
            if user_folder.is_dir() and user_folder.name.startswith("user_") and user_folder.name.endswith("_files"):
                for f in user_folder.iterdir():
                    if f.is_file():
                        try:
                            rows = extract_rows_simple(str(f))
                            user_filename = f"{user_folder.name}/{f.name}"
                            fts_add_rows(user_filename, rows)
                            indexed_files.append(user_filename)
                        except Exception as e:
                            failed_files.append(f"{user_folder.name}/{f.name}: {str(e)}")
        
        return jsonify(
            status="success",
            indexed=indexed_files,
            failed=failed_files,
            total=len(indexed_files)
        ), 200
        
    except Exception as e:
        return jsonify(status="error", error=str(e)), 500


@app.route("/chat/fts/refresh", methods=["POST"])
@login_required
def refresh_user_fts():
    """Refresh FTS index for current user's files"""
    import pathlib
    
    try:
        user_folder = f"user_{current_user.id}_files"
        user_dir = pathlib.Path(os.path.dirname(__file__)) / "datasets" / user_folder
        
        if not user_dir.exists():
            return jsonify(status="success", message="No files to index", indexed=[], failed=[]), 200
        
        indexed_files = []
        failed_files = []
        
        # Clear user's existing entries
        conn = _fts_conn()
        conn.execute("DELETE FROM docs WHERE file LIKE ?", (f"{user_folder}/%",))
        conn.commit()
        conn.close()
        
        # Reindex user's files
        for f in user_dir.iterdir():
            if f.is_file():
                try:
                    rows = extract_rows_simple(str(f))
                    user_filename = f"{user_folder}/{f.name}"
                    fts_add_rows(user_filename, rows)
                    indexed_files.append(f.name)
                except Exception as e:
                    failed_files.append(f"{f.name}: {str(e)}")
        
        return jsonify(
            status="success",
            indexed=indexed_files,
            failed=failed_files,
            total=len(indexed_files)
        ), 200
        
    except Exception as e:
        return jsonify(status="error", error=str(e)), 500
    
    
# ============================================================================
# SECTION 12: IMAGE GENERATION (DALL-E)
# ============================================================================

@app.route("/generate-image", methods=["POST"])
@login_required
def generate_image():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    api_key = data.get("api_key", "")
    
    if not prompt:
        return jsonify(error="Please provide a prompt"), 400
    
    if not api_key:
        return jsonify(error="Please enter your OpenAI API key"), 400
    
    try:
        client = OpenAI(api_key=api_key)
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        
        image_url = response.data[0].url
        return jsonify(image_url=image_url), 200
        
    except Exception as e:
        return jsonify(error=f"Image generation error: {str(e)}"), 400
    
 # ============================================================================
# SECTION 13: DATA VISUALIZATION
# ============================================================================

@app.route("/visualize", methods=["POST"])
@login_required
def visualize_data():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    chart_type = data.get("chart_type", "bar")
    
    if not filename:
        return jsonify(error="No filename provided"), 400
    
    datasets_dir = os.path.join(os.path.dirname(__file__), "datasets")
    filepath = os.path.join(datasets_dir, filename)
    
    if not os.path.exists(filepath):
        return jsonify(error="File not found"), 404
    
    try:
        if filename.lower().endswith('.csv'):
            df = pd.read_csv(filepath)
        elif filename.lower().endswith('.xlsx'):
            df = pd.read_excel(filepath)
        else:
            return jsonify(error="Only CSV and XLSX files can be visualized"), 400
        
        plt.figure(figsize=(10, 6))
        
        if chart_type == "bar" and len(df.columns) >= 2:
            df.plot(kind='bar', x=df.columns[0], y=df.columns[1], ax=plt.gca())
        elif chart_type == "line" and len(df.columns) >= 2:
            df.plot(kind='line', x=df.columns[0], y=df.columns[1], ax=plt.gca())
        elif chart_type == "pie" and len(df.columns) >= 2:
            df.set_index(df.columns[0])[df.columns[1]].plot(kind='pie', autopct='%1.1f%%', ax=plt.gca())
        elif chart_type == "scatter" and len(df.columns) >= 2:
            df.plot(kind='scatter', x=df.columns[0], y=df.columns[1], ax=plt.gca())
        else:
            numeric_cols = df.select_dtypes(include=['number']).columns
            if len(numeric_cols) > 0:
                df[numeric_cols[0]].plot(kind='bar', ax=plt.gca())
            else:
                return jsonify(error="No numeric data to visualize"), 400
        
        plt.title(f"{chart_type.capitalize()} Chart - {filename.split('/')[-1]}")
        plt.tight_layout()
        
        img_bytes = io.BytesIO()
        plt.savefig(img_bytes, format='png', dpi=100, bbox_inches='tight')
        img_bytes.seek(0)
        plt.close()
        
        img_base64 = base64.b64encode(img_bytes.getvalue()).decode()
        
        return jsonify(image=f"data:image/png;base64,{img_base64}"), 200
        
    except Exception as e:
        return jsonify(error=f"Visualization error: {str(e)}"), 400
    
    
# ============================================================================
# SECTION 14: PAGE ROUTES
# ============================================================================

@app.route("/admin", methods=["GET"])
@login_required
@role_required("Admin")
def admin_page():
    return render_template("admin.html")

@app.route("/chat", methods=["GET"])
@login_required
def chat_page():
    return render_template("chat.html", user_role=getattr(current_user, "role", "User"))

@app.route("/admin/files", methods=["GET"])
@login_required
@role_required("Admin")
def admin_files_list():
    """Get all files (admin + all user files)"""
    import pathlib
    base = pathlib.Path(os.path.dirname(__file__)) / "datasets"
    base.mkdir(exist_ok=True)
    
    all_files = []
    
    # Get admin files (root level)
    for f in base.iterdir():
        if f.is_file():
            all_files.append({
                "name": f.name,
                "path": f.name,
                "owner": "Admin",
                "user_id": None,
                "size": f.stat().st_size,
                "type": "admin"
            })
    
    # Get user files (from user folders)
    for user_folder in base.iterdir():
        if user_folder.is_dir() and user_folder.name.startswith("user_") and user_folder.name.endswith("_files"):
            # Extract user ID from folder name
            try:
                user_id = int(user_folder.name.replace("user_", "").replace("_files", ""))
            except:
                continue
            
            # Get user name from database
            cur = mysql.connection.cursor()
            cur.execute("SELECT name FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            cur.close()
            
            user_name = row[0] if row else f"User {user_id}"
            
            # Get all files in this user's folder
            for f in user_folder.iterdir():
                if f.is_file():
                    all_files.append({
                        "name": f.name,
                        "path": f"{user_folder.name}/{f.name}",
                        "owner": user_name,
                        "user_id": user_id,
                        "size": f.stat().st_size,
                        "type": "user"
                    })
    
    # Sort by owner, then by name
    all_files.sort(key=lambda x: (x["owner"], x["name"]))
    
    return jsonify(files=all_files)

@app.route("/admin/files/<path:filepath>", methods=["DELETE"])
@login_required
@role_required("Admin")
def admin_files_delete(filepath):
    """Delete any file (admin or user file)"""
    import pathlib
    base = pathlib.Path(os.path.dirname(__file__)) / "datasets"
    file_path = base / filepath

    if not file_path.exists() or not file_path.is_file():
        return jsonify(error="File not found"), 404

    # Remove from FTS index FIRST
    try:
        fts_clear_file(filepath)
        print(f"✅ Cleared from FTS index: {filepath}")
    except Exception as e:
        print(f"⚠️ FTS clear error for {filepath}: {str(e)}")

    # Delete the actual file
    try:
        file_path.unlink()
        print(f"✅ Deleted file: {file_path}")
        
        # If it was a user file, check if folder is now empty and delete folder
        if "/" in filepath:
            folder_path = file_path.parent
            if folder_path.exists() and not any(folder_path.iterdir()):
                folder_path.rmdir()
                print(f"✅ Deleted empty folder: {folder_path}")
        
        return jsonify(status="deleted", file=filepath)
    except Exception as e:
        print(f"❌ File delete error: {str(e)}")
        return jsonify(error=str(e)), 500
    
# ============================================================================
# SECTION 15: RUN THE APP
# ============================================================================

if __name__ == '__main__':
    app.run(debug=True)
    
    
           
