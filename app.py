from flask import Flask, request, jsonify, g, send_file
import os
import threading
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import mysql.connector
from mysql.connector import Error
from flask_bcrypt import Bcrypt

app = Flask(__name__)
bcrypt = Bcrypt(app)

# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION CONFIGURATION
# Set these environment variables before running the server:
#   SMTP_USER  — the Gmail address that sends alerts (e.g. yourname@gmail.com)
#   SMTP_PASS  — the Gmail App Password (NOT your regular password).
#               Generate one at: Google Account → Security → App Passwords
#   SMTP_HOST  — defaults to smtp.gmail.com
#   SMTP_PORT  — defaults to 587
# If SMTP_USER / SMTP_PASS are not set, emails are skipped and a log
# message is printed instead.
# ──────────────────────────────────────────────────────────────────────
ADMIN_EMAIL       = 'add your email'
SMTP_HOST         = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT         = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER         = os.environ.get('SMTP_USER', '')   # sender Gmail address
SMTP_PASS         = os.environ.get('SMTP_PASS', '')   # Gmail App Password
LOW_STOCK_QTY     = 50   # matches the frontend threshold (qty < 50 = low stock)
NOTIFY_INTERVAL   = 3600 # seconds between automatic checks (default: 1 hour)

# In-memory sets so the same alert is not spammed on every cycle.
# Cleared on server restart (acceptable — a fresh check will re-notify if needed).
_notified_checks    = set()   # invoice IDs whose overdue email was already sent
_notified_low_stock = set()   # variety names whose low-stock email was already sent
_scheduler_started  = False
_scheduler_lock     = threading.Lock()

app.config.update(
    DB_HOST=os.environ.get("DB_HOST", "localhost"),
    DB_USER=os.environ.get("DB_USER", "root"),
    DB_PASSWORD=os.environ.get("DB_PASSWORD", "1234"), # Leave blank if you have no MySQL password
    DB_NAME=os.environ.get("DB_NAME", "amm_farm_db")
)

def get_db_connection():
    if "db_conn" not in g:
        g.db_conn = mysql.connector.connect(
            host=app.config["DB_HOST"],
            user=app.config["DB_USER"],
            password=app.config["DB_PASSWORD"],
            database=app.config["DB_NAME"]
        )
    return g.db_conn

def ensure_schema():
    """Create all tables if they don't exist, then apply any missing column migrations."""
    try:
        conn = mysql.connector.connect(
            host=app.config["DB_HOST"],
            user=app.config["DB_USER"],
            password=app.config["DB_PASSWORD"],
            database=app.config["DB_NAME"]
        )
        with conn.cursor() as cursor:

            # ── 1. CREATE TABLES (safe on every startup — IF NOT EXISTS) ──────────
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    username      VARCHAR(50)  NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    role          ENUM('admin','user') NOT NULL DEFAULT 'user',
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id      VARCHAR(50)  NOT NULL PRIMARY KEY,
                    name    VARCHAR(100) NOT NULL,
                    phone   VARCHAR(20),
                    email   VARCHAR(100),
                    address TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS egg_varieties (
                    batch_id     INT AUTO_INCREMENT PRIMARY KEY,
                    id           VARCHAR(50)    NOT NULL,
                    name         VARCHAR(100)   NOT NULL,
                    buy          DECIMAL(10,2)  NOT NULL,
                    sell         DECIMAL(10,2)  NOT NULL,
                    qty          INT            NOT NULL,
                    date_added   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id               VARCHAR(50)   NOT NULL PRIMARY KEY,
                    shop             VARCHAR(100)  NOT NULL,
                    date             VARCHAR(20)   NOT NULL,
                    time             VARCHAR(20),
                    total            DECIMAL(10,2) NOT NULL,
                    paidAmount       DECIMAL(10,2) NOT NULL,
                    balance          DECIMAL(10,2) NOT NULL,
                    paymentStatus    VARCHAR(50)   NOT NULL DEFAULT 'Unpaid',
                    createdByUser    VARCHAR(50),
                    vehicle          VARCHAR(50),
                    driver           VARCHAR(50),
                    helper           VARCHAR(100),
                    timestamp        BIGINT,
                    payment_method   VARCHAR(20)   DEFAULT 'Cash',
                    check_due_date   DATE,
                    check_status     VARCHAR(20),
                    check_clearance_date DATE
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invoice_items (
                    item_id      INT AUTO_INCREMENT PRIMARY KEY,
                    invoice_id   VARCHAR(50)   NOT NULL,
                    varietyId    VARCHAR(50)   NOT NULL,
                    varietyName  VARCHAR(100)  NOT NULL,
                    qty          INT           NOT NULL,
                    rate         DECIMAL(10,2) NOT NULL,
                    total        DECIMAL(10,2) NOT NULL,
                    KEY fk_invoice (invoice_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lorry_stock (
                    stock_id INT AUTO_INCREMENT PRIMARY KEY,
                    shift_id VARCHAR(100),
                    lorry_no VARCHAR(50),
                    batch_id INT,
                    qty      INT NOT NULL,
                    KEY fk_batch (batch_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS damages_returns (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    date         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    variety_name VARCHAR(100),
                    type         ENUM('Damage','Return'),
                    qty          INT,
                    resale_price DECIMAL(10,2),
                    loss_amount  DECIMAL(10,2),
                    lorry_no     VARCHAR(50),
                    shop_name    VARCHAR(100)
                )
            """)

            conn.commit()
            print("✓ All tables verified / created.")

            # ── 2. COLUMN MIGRATIONS (for databases upgraded from older versions) ──
            migrations = [
                # (check_table, check_column, alter_sql)
                ("egg_varieties", "last_updated",
                 "ALTER TABLE egg_varieties ADD COLUMN last_updated TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
                ("invoices", "payment_method",
                 "ALTER TABLE invoices ADD COLUMN payment_method VARCHAR(20) NULL DEFAULT NULL"),
                ("invoices", "check_due_date",
                 "ALTER TABLE invoices ADD COLUMN check_due_date DATE NULL"),
                ("invoices", "check_clearance_date",
                 "ALTER TABLE invoices ADD COLUMN check_clearance_date DATE NULL"),
                ("damages_returns", "lorry_no",
                 "ALTER TABLE damages_returns ADD COLUMN lorry_no VARCHAR(50) NULL"),
                ("damages_returns", "shop_name",
                 "ALTER TABLE damages_returns ADD COLUMN shop_name VARCHAR(100) NULL"),
                 ("users", "current_vehicle", "ALTER TABLE users ADD COLUMN current_vehicle VARCHAR(50) NULL"),
                ("users", "current_driver", "ALTER TABLE users ADD COLUMN current_driver VARCHAR(50) NULL"),
                ("users", "current_helper", "ALTER TABLE users ADD COLUMN current_helper VARCHAR(100) NULL"),
                ("users", "shift_timestamp", "ALTER TABLE users ADD COLUMN shift_timestamp BIGINT NULL"),
                ("users", "current_vehicle", "ALTER TABLE users ADD COLUMN current_vehicle VARCHAR(50) NULL"),
                ("users", "current_driver", "ALTER TABLE users ADD COLUMN current_driver VARCHAR(50) NULL"),
            ]

            for tbl, col, sql in migrations:
                cursor.execute("""
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME   = %s
                      AND COLUMN_NAME  = %s
                """, (tbl, col))
                if cursor.fetchone()[0] == 0:
                    cursor.execute(sql)
                    conn.commit()
                    print(f"✓ Migration applied: added {col} to {tbl}")

        conn.close()
        print("✓ Schema ready.")
    except Error as e:
        print(f"Schema migration error: {e}")

@app.teardown_appcontext
def close_db_connection(exception):
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()

def json_error(message, status=400):
    return jsonify({"status": "error", "message": message}), status

# --- 1. AUTHENTICATION ---
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True)
    username = data.get('username')
    password = data.get('password')

    try:
        conn = get_db_connection()
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()

        if user and user.get('password_hash') and bcrypt.check_password_hash(user['password_hash'], password):
            
            # Extract shift info if it exists
            shift_info = None
            if user.get('shift_timestamp'):
                shift_info = {
                    'vehicle': user.get('current_vehicle'),
                    'driver': user.get('current_driver'),
                    'helper': user.get('current_helper'),
                    'timestamp': user.get('shift_timestamp')
                }
                
            return jsonify({
                'status': 'success', 
                'user': {
                    'id': user.get('id'), 
                    'username': user.get('username'), 
                    'role': user.get('role', 'user'),
                    'shift': shift_info
                }
            })

        # Bootstrap: if no users row found, allow default admin credentials
        if not user:
            if username == 'admin' and password == 'admin123':
                return jsonify({'status': 'success', 'user': {'username': 'admin', 'role': 'admin'}})
            return json_error('Invalid credentials.', 401)

        return json_error('Invalid credentials.', 401)

    except Error as err:
        return json_error(str(err), 500)
    
@app.route('/api/shift', methods=['POST'])
def update_shift():
    data = request.get_json(silent=True)
    if not data:
        return json_error("Invalid or missing JSON payload.", 400)
    username = data.get('username')
    vehicle = data.get('vehicle')
    driver = data.get('driver')
    helper = data.get('helper')
    timestamp = data.get('timestamp')

    conn = get_db_connection()
    try:
        with conn.cursor(dictionary=True) as cursor:
            # Automatically return any leftover stock for this vehicle back to the warehouse
            if vehicle:
                cursor.execute(
                    'SELECT stock_id, batch_id, qty FROM lorry_stock WHERE lorry_no = %s AND qty > 0',
                    (vehicle,)
                )
                existing_stock = cursor.fetchall()
                for row in existing_stock:
                    # Return stock to main warehouse
                    cursor.execute(
                        'UPDATE egg_varieties SET qty = qty + %s, last_updated = NOW() WHERE batch_id = %s',
                        (row['qty'], row['batch_id'])
                    )
                    # Clear stock from lorry
                    cursor.execute(
                        'DELETE FROM lorry_stock WHERE stock_id = %s',
                        (row['stock_id'],)
                    )

            # Then update the user's shift details
            cursor.execute("""
                UPDATE users
                SET current_vehicle = %s, current_driver = %s, current_helper = %s, shift_timestamp = %s
                WHERE username = %s
            """, (vehicle, driver, helper, timestamp, username))
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

@app.route('/api/shift/<username>', methods=['GET'])
def get_shift(username):
    """Fetches the user's active shift session directly from the database."""
    conn = get_db_connection()
    try:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT current_vehicle, current_driver, current_helper, shift_timestamp 
                FROM users 
                WHERE username = %s
            """, (username,))
            user = cursor.fetchone()
            
            # If the user has a shift registered, return it instantly
            if user and user.get('shift_timestamp'):
                return jsonify({
                    'status': 'success',
                    'shift': {
                        'vehicle': user.get('current_vehicle'),
                        'driver': user.get('current_driver'),
                        'helper': user.get('current_helper'),
                        'timestamp': user.get('shift_timestamp')
                    }
                })
            return jsonify({'status': 'success', 'shift': None})
    except Error as err:
        return json_error(str(err), 500)

@app.route('/api/admin/update-user', methods=['POST'])
def update_user_credentials():
    data = request.get_json(silent=True)
    username = data.get('username')
    new_password = data.get('password')
    hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET password_hash = %s WHERE username = %s", (hashed_password, username))
            conn.commit()
            if cursor.rowcount == 0:
                return json_error('User not found.', 404)
        return jsonify({'status': 'success', 'message': 'Password updated.'})
    except Error as err:
        return json_error(str(err), 500)


@app.route('/api/users', methods=['GET', 'POST'])
def manage_users():
    conn = get_db_connection()
    if request.method == 'POST':
        data = request.get_json(silent=True)
        username = data.get('username')
        password = data.get('password')
        role = data.get('role', 'user')
        if not username or not password:
            return json_error('Username and password are required.', 400)
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        try:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)", (username, hashed, role))
                conn.commit()
            return jsonify({'status': 'success'})
        except Error as err:
            return json_error(str(err), 500)
    else:
        try:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("SELECT id, username, role FROM users")
                users = cursor.fetchall()
            return jsonify({'status': 'success', 'users': users})
        except Error as err:
            return json_error(str(err), 500)


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

# --- 2. CUSTOMERS ---
@app.route('/api/customers', methods=['GET', 'POST'])
def handle_customers():
    conn = get_db_connection()
    if request.method == 'POST':
        data = request.get_json(silent=True)
        try:
            # Use dictionary cursor so fetchone()/fetchall() return dicts
            with conn.cursor(dictionary=True) as cursor:
                # REPLACE acts like an INSERT or UPDATE if the ID already exists
                cursor.execute(
                    "REPLACE INTO customers (id, name, phone, email, address) VALUES (%s, %s, %s, %s, %s)",
                    (data.get('id'), data.get('name'), data.get('phone'), data.get('email'), data.get('address'))
                )
                conn.commit()
            return jsonify({'status': 'success'})
        except Error as err:
            return json_error(str(err), 500)
    else:
        try:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("SELECT * FROM customers")
                return jsonify({'status': 'success', 'customers': cursor.fetchall()})
        except Error as err:
            return json_error(str(err), 500)

@app.route('/api/customers/<id>', methods=['DELETE'])
def delete_customer(id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM customers WHERE id = %s", (id,))
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

# --- 3. INVENTORY (EGG VARIETIES) ---
@app.route('/api/inventory', methods=['GET', 'POST'])
def handle_inventory():
    conn = get_db_connection()
    if request.method == 'POST':
        data = request.get_json(silent=True)
        try:
            with conn.cursor() as cursor:
                # INSERT ... ON DUPLICATE KEY UPDATE preserves the original date_added
                # and lets MySQL's ON UPDATE CURRENT_TIMESTAMP handle last_updated automatically
                cursor.execute("""
                    INSERT INTO egg_varieties (id, name, buy, sell, qty)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name         = VALUES(name),
                        buy          = VALUES(buy),
                        sell         = VALUES(sell),
                        qty          = VALUES(qty),
                        last_updated = NOW()
                """, (data.get('id'), data.get('name'), data.get('buy'), data.get('sell'), data.get('qty')))
                conn.commit()
            return jsonify({'status': 'success'})
        except Error as err:
            return json_error(str(err), 500)
    else:
        try:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("SELECT * FROM egg_varieties")
                rows = cursor.fetchall()
                # Explicitly serialize datetime objects so JSON output is consistent
                for row in rows:
                    for key in ('date_added', 'last_updated'):
                        val = row.get(key)
                        if val and hasattr(val, 'strftime'):
                            row[key] = val.strftime('%Y-%m-%d %H:%M:%S')
                return jsonify({'status': 'success', 'inventory': rows})
        except Error as err:
            return json_error(str(err), 500)

@app.route('/api/inventory/<id>', methods=['DELETE'])
def delete_inventory(id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Temporarily disable foreign key constraints for this connection 
            # to safely delete items bound to invoices/lorry stocks
            cursor.execute("SET FOREIGN_KEY_CHECKS=0;")
            cursor.execute("DELETE FROM egg_varieties WHERE id = %s LIMIT 1", (id,))
            cursor.execute("SET FOREIGN_KEY_CHECKS=1;")
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

@app.route('/api/inventory/<id>/qty', methods=['PATCH'])
def update_inventory_quantity(id):
    data = request.get_json(silent=True) or {}
    delta = int(data.get('delta', 0))
    if delta == 0:
        return jsonify({'status': 'success'})

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE egg_varieties SET qty = qty + %s, last_updated = NOW() WHERE id = %s",
                (delta, id)
            )
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

@app.route('/api/inventory/deduct', methods=['POST'])
def deduct_inventory_items():
    data = request.get_json(silent=True) or {}
    items = data.get('items')
    if not isinstance(items, list) or len(items) == 0:
        return json_error('No inventory items provided for deduction.', 400)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            for item in items:
                item_id = item.get('id') or item.get('varietyId')
                qty = int(item.get('qty', 0))
                if not item_id or qty <= 0:
                    continue
                cursor.execute(
                    "UPDATE egg_varieties SET qty = GREATEST(0, qty - %s), last_updated = NOW() WHERE id = %s",
                    (qty, item_id)
                )
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

# --- DAMAGES & RETURNS ---
@app.route('/api/damages', methods=['GET', 'POST'])
def handle_damages():
    conn = get_db_connection()
    if request.method == 'POST':
        data = request.get_json(silent=True)
        try:
            with conn.cursor(dictionary=True) as cursor:
                # 1. Record the damage/return in the log
                cursor.execute("""
                    INSERT INTO damages_returns (variety_name, type, qty, resale_price, loss_amount, lorry_no, shop_name) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (data['variety_name'], data['type'], data['qty'], data.get('resale_price', 0), data['loss_amount'], data.get('lorry_no'), data.get('customer_name')))
                
                # 2. Manage stock deduction
                variety_name = data['variety_name']
                qty = int(data['qty'])
                lorry_no = data.get('lorry_no')
                
                # Fetch ID & Batch ID to accurately locate the stock item
                cursor.execute("SELECT id, batch_id FROM egg_varieties WHERE name = %s LIMIT 1", (variety_name,))
                ev_row = cursor.fetchone()
                
                if ev_row:
                    variety_id = ev_row['id']
                    batch_id = ev_row['batch_id'] or variety_id
                    
                    if lorry_no:
                        # User logged in with active shift -> deduct from lorry_stock
                        cursor.execute("""
                            UPDATE lorry_stock 
                            SET qty = GREATEST(0, qty - %s) 
                            WHERE lorry_no = %s AND batch_id = %s
                        """, (qty, lorry_no, batch_id))
                        
                        # Fallback just in case lorry_stock used variety_id instead of batch_id
                        if cursor.rowcount == 0:
                            cursor.execute("""
                                UPDATE lorry_stock 
                                SET qty = GREATEST(0, qty - %s) 
                                WHERE lorry_no = %s AND batch_id = %s
                            """, (qty, lorry_no, variety_id))
                    else:
                        # Admin -> deduct from main warehouse
                        cursor.execute("""
                            UPDATE egg_varieties 
                            SET qty = GREATEST(0, qty - %s) 
                            WHERE id = %s
                        """, (qty, variety_id))
                
                conn.commit()
            return jsonify({'status': 'success'})
        except Error as err:
            return json_error(str(err), 500)
    else: # GET request for Report Center
        try:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("SELECT * FROM damages_returns ORDER BY date DESC")
                return jsonify({'status': 'success', 'damages': cursor.fetchall()})
        except Error as err:
            return json_error(str(err), 500)

@app.route('/api/damages/<int:damage_id>', methods=['DELETE'])
def delete_damage(damage_id):
    """Permanently delete a damage/return log entry by its ID."""
    try:
        conn = get_db_connection()
        with conn.cursor(dictionary=True) as cursor:
            # Verify it exists first
            cursor.execute("SELECT id FROM damages_returns WHERE id = %s", (damage_id,))
            row = cursor.fetchone()
            if not row:
                return json_error('Damage record not found.', 404)
            cursor.execute("DELETE FROM damages_returns WHERE id = %s", (damage_id,))
            conn.commit()
        return jsonify({'status': 'success', 'message': 'Damage record deleted.'})
    except Error as err:
        return json_error(str(err), 500)

# --- 4. BILLS / INVOICES ---
@app.route('/api/bills', methods=['GET', 'POST'])
def handle_bills():
    conn = get_db_connection()
    if request.method == 'POST':
        data = request.get_json(silent=True)
        try:
            with conn.cursor() as cursor:
                # 1. Insert Invoice
                cursor.execute("""
                    INSERT INTO invoices (id, shop, date, time, total, paidAmount, balance, paymentStatus, createdByUser, vehicle, driver, helper, timestamp) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (data.get('id'), data.get('shop'), data.get('date'), data.get('time'), data.get('total'), data.get('paidAmount'), data.get('balance'), data.get('paymentStatus'), data.get('createdByUser'), data.get('vehicle'), data.get('driver'), data.get('helper'), data.get('timestamp')))
                
                # 2. Insert Items & Deduct Inventory conditionally
                for item in data.get('items', []):
                    cursor.execute("""
                        INSERT INTO invoice_items (invoice_id, varietyId, varietyName, qty, rate, total)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (data.get('id'), item.get('varietyId'), item.get('varietyName'), item.get('qty'), item.get('rate'), item.get('total')))
                    
                    if item.get('isReturn'):
                        cursor.execute("""
                            INSERT INTO damages_returns (variety_name, type, qty, resale_price, loss_amount, lorry_no, shop_name) 
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (item.get('varietyName'), 'Return', item.get('qty'), item.get('resalePrice', 0), item.get('lossAmount', 0), data.get('vehicle'), data.get('shop')))
                        
                        vehicle = data.get('vehicle')
                        if vehicle:
                            cursor.execute("SELECT batch_id FROM egg_varieties WHERE id = %s", (item.get('varietyId'),))
                            results = cursor.fetchall()
                            batch_res = results[0] if results else None
                            if batch_res and batch_res[0]:
                                cursor.execute("""
                                    UPDATE lorry_stock 
                                    SET qty = GREATEST(0, qty - %s) 
                                    WHERE lorry_no = %s AND batch_id = %s
                                """, (item.get('qty'), vehicle, batch_res[0]))
                                
                        continue
                        
                    vehicle = data.get('vehicle')
                    if vehicle:
                        # Deduct from lorry_stock using batch_id mapping
                        cursor.execute("SELECT batch_id FROM egg_varieties WHERE id = %s", (item.get('varietyId'),))
                        results = cursor.fetchall()
                        batch_res = results[0] if results else None
                        
                        if batch_res and batch_res[0]:
                            cursor.execute("""
                                UPDATE lorry_stock 
                                SET qty = GREATEST(0, qty - %s) 
                                WHERE lorry_no = %s AND batch_id = %s
                            """, (item.get('qty'), vehicle, batch_res[0]))
                        else:
                            # Fallback: if no batch_id mapping exists, attempt to deduct from main stock
                            cursor.execute("""
                                UPDATE egg_varieties 
                                SET qty = GREATEST(0, qty - %s) 
                                WHERE id = %s
                            """, (item.get('qty'), item.get('varietyId')))
                    else:
                        # Deduct directly from main stock if no vehicle is active
                        cursor.execute("""
                            UPDATE egg_varieties 
                            SET qty = GREATEST(0, qty - %s) 
                            WHERE id = %s
                        """, (item.get('qty'), item.get('varietyId')))
                
                conn.commit()
            return jsonify({'status': 'success'})
        except Error as err:
            return json_error(str(err), 500)
    else:
        try:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute("SELECT * FROM invoices ORDER BY timestamp ASC")
                bills = cursor.fetchall()
                # Attach items to each bill and serialize date fields
                for bill in bills:
                    cursor.execute("SELECT * FROM invoice_items WHERE invoice_id = %s", (bill['id'],))
                    bill['items'] = cursor.fetchall()
                    # Serialize check_due_date for JSON
                    if bill.get('check_due_date') and hasattr(bill['check_due_date'], 'strftime'):
                        bill['check_due_date'] = bill['check_due_date'].strftime('%Y-%m-%d')
                return jsonify({'status': 'success', 'bills': bills})
        except Error as err:
            return json_error(str(err), 500)

@app.route('/api/bills/<id>', methods=['DELETE'])
def delete_bill(id):
    try:
        conn = get_db_connection()
        with conn.cursor(dictionary=True) as cursor:
            # 1. Check status and vehicle association
            cursor.execute("SELECT paymentStatus, vehicle FROM invoices WHERE id = %s", (id,))
            bill_info = cursor.fetchone()
            
            # Only return stock if the bill was entirely unpaid
            if bill_info and bill_info['paymentStatus'] == 'Unpaid':
                cursor.execute("SELECT varietyId, qty, varietyName FROM invoice_items WHERE invoice_id = %s", (id,))
                items = cursor.fetchall()
                vehicle = bill_info.get('vehicle')
                
                for item in items:
                    # Do not restore stock for items explicitly marked as Damages or Returns on the bill
                    if "(Damage)" in item['varietyName'] or "(Return)" in item['varietyName']:
                        continue
                        
                    if vehicle:
                        cursor.execute("""
                            UPDATE lorry_stock 
                            SET qty = qty + %s 
                            WHERE lorry_no = %s AND (batch_id = %s OR batch_id = (SELECT batch_id FROM egg_varieties WHERE id = %s))
                        """, (item['qty'], vehicle, item['varietyId'], item['varietyId']))
                        
                        if cursor.rowcount == 0:
                            cursor.execute("UPDATE egg_varieties SET qty = qty + %s WHERE id = %s", (item['qty'], item['varietyId']))
                    else:
                        cursor.execute("UPDATE egg_varieties SET qty = qty + %s WHERE id = %s", (item['qty'], item['varietyId']))
            
            # 2. Delete items then invoice
            cursor.execute("DELETE FROM invoice_items WHERE invoice_id = %s", (id,))
            cursor.execute("DELETE FROM invoices WHERE id = %s", (id,))
            conn.commit()
            
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

# --- 5. ACCOUNT UPDATES ---
@app.route('/api/bills/<id>/pay', methods=['POST'])
def mark_bill_paid(id):
    data = request.get_json(silent=True) or {}
    method = data.get('method', 'Cash')  # Cash, Bank Deposit, or Check
    clearance_days = int(data.get('clearance_days', 0)) # Used for Check payments
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if method == 'Check':
                # Check payment: set to pending, calculate future date
                cursor.execute("""
                    UPDATE invoices
                    SET paymentStatus = 'Check Pending',
                        payment_method = 'Check',
                        check_due_date = DATE_ADD(CURDATE(), INTERVAL %s DAY)
                    WHERE id = %s
                """, (clearance_days, id))
            else:
                # Cash or Bank Deposit: mark as fully paid immediately
                cursor.execute("""
                    UPDATE invoices
                    SET paymentStatus = 'Paid',
                        paidAmount = total,
                        balance = 0,
                        payment_method = %s,
                        check_due_date = NULL
                    WHERE id = %s
                """, (method, id))
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

@app.route('/api/bills/<id>/check-action', methods=['POST'])
def check_action(id):
    """Accept or reject a check-pending bill."""
    data = request.get_json(silent=True) or {}
    action = data.get('action')  # 'accept' or 'reject'
    if action not in ('accept', 'reject'):
        return json_error('Invalid action. Must be accept or reject.', 400)
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if action == 'accept':
                cursor.execute("""
                    UPDATE invoices
                    SET paymentStatus = 'Paid',
                        paidAmount = total,
                        balance = 0
                    WHERE id = %s
                """, (id,))
            else:
                # Reject: Move it to the Rejected status
                cursor.execute("""
                    UPDATE invoices
                    SET paymentStatus = 'Rejected'
                    WHERE id = %s
                """, (id,))
            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)

@app.route('/api/bills/expired-checks', methods=['GET'])
def get_expired_checks():
    """Fetch checks that have passed their clearance date to trigger frontend alerts."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, shop, total, check_due_date 
            FROM invoices 
            WHERE paymentStatus = 'Check Pending' AND check_due_date <= CURDATE()
        """)
        expired = cursor.fetchall()
        return jsonify({'status': 'success', 'expired_checks': expired})
    except Error as err:
        return json_error(str(err), 500)

# --- LORRY STOCK MANAGEMENT ---
@app.route('/api/lorry/stock', methods=['GET'])
def get_lorry_stock():
    vehicle = request.args.get('lorry_no')
    if not vehicle:
        return json_error("Lorry number required", 400)
    try:
        conn = get_db_connection()
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT ls.stock_id, ls.qty, ev.id, ev.name, ev.sell, ev.buy, ev.batch_id
                FROM lorry_stock ls
                JOIN egg_varieties ev ON (ls.batch_id = ev.batch_id OR ls.batch_id = ev.id)
                WHERE ls.lorry_no = %s AND ls.qty > 0
            """, (vehicle,))
            stock = cursor.fetchall()
        return jsonify(stock)
    except Error as err:
        return json_error(str(err), 500)

@app.route('/api/lorry/load', methods=['POST'])
def load_lorry_stock():
    data = request.get_json(silent=True) or {}
    items = data.get('items', [])
    vehicle = data.get('vehicle')
    shift_id = data.get('shift_id', 'SHIFT-' + vehicle)

    if not vehicle or not items:
        return json_error("Vehicle and items required", 400)

    conn = get_db_connection()
    try:
        with conn.cursor(dictionary=True) as cursor:
            for item in items:
                variety_id = item.get('id')
                qty_to_load = int(item.get('qty', 0))
                
                if qty_to_load <= 0: continue

                cursor.execute("SELECT batch_id, qty FROM egg_varieties WHERE id = %s", (variety_id,))
                results = cursor.fetchall()
                ev = results[0] if results else None
                if not ev or ev['qty'] < qty_to_load:
                    return json_error(f"Insufficient main stock for {variety_id}", 400)
                
                # Fallback to variety_id if batch_id is NULL
                batch_id = ev['batch_id'] or variety_id 

                cursor.execute("UPDATE egg_varieties SET qty = qty - %s WHERE id = %s", (qty_to_load, variety_id))
                cursor.fetchall() 

                cursor.execute("SELECT stock_id, qty FROM lorry_stock WHERE lorry_no = %s AND batch_id = %s", (vehicle, batch_id))
                results = cursor.fetchall()
                existing = results[0] if results else None

                if existing:
                    cursor.execute("UPDATE lorry_stock SET qty = qty + %s WHERE stock_id = %s", (qty_to_load, existing['stock_id']))
                    cursor.fetchall() 
                else:
                    cursor.execute("INSERT INTO lorry_stock (shift_id, lorry_no, batch_id, qty) VALUES (%s, %s, %s, %s)", (shift_id, vehicle, batch_id, qty_to_load))
                    cursor.fetchall() 

            conn.commit()
        return jsonify({'status': 'success'})
    except Error as err:
        return json_error(str(err), 500)


# --- LORRY STOCK: update (add more qty) ---
@app.route('/api/lorry/stock/update', methods=['POST'])
def update_lorry_stock():
    """Add more quantity to an existing lorry stock entry (top-up from main warehouse)."""
    data = request.get_json(silent=True) or {}
    vehicle = data.get('vehicle')
    items = data.get('items', [])

    if not vehicle or not items:
        return json_error('Vehicle and items are required.', 400)

    conn = get_db_connection()
    try:
        with conn.cursor(dictionary=True) as cursor:
            for item in items:
                variety_id = item.get('id')
                qty_to_add = int(item.get('qty', 0))
                if qty_to_add <= 0:
                    continue

                # Get batch_id and check main stock
                cursor.execute('SELECT batch_id, qty FROM egg_varieties WHERE id = %s', (variety_id,))
                results = cursor.fetchall()
                ev = results[0] if results else None
                if not ev or ev['qty'] < qty_to_add:
                    return json_error(f'Insufficient main stock for variety {variety_id}', 400)

                batch_id = ev['batch_id']

                # Deduct from main stock
                cursor.execute(
                    'UPDATE egg_varieties SET qty = qty - %s, last_updated = NOW() WHERE id = %s',
                    (qty_to_add, variety_id)
                )
                cursor.fetchall()  # Consume the result

                # Add to lorry stock
                cursor.execute(
                    'SELECT stock_id FROM lorry_stock WHERE lorry_no = %s AND batch_id = %s',
                    (vehicle, batch_id)
                )
                results = cursor.fetchall()
                existing = results[0] if results else None
                if existing:
                    cursor.execute(
                        'UPDATE lorry_stock SET qty = qty + %s WHERE stock_id = %s',
                        (qty_to_add, existing['stock_id'])
                    )
                    cursor.fetchall()  # Consume the result
                else:
                    cursor.execute(
                        'INSERT INTO lorry_stock (shift_id, lorry_no, batch_id, qty) VALUES (%s, %s, %s, %s)',
                        ('SHIFT-' + vehicle, vehicle, batch_id, qty_to_add)
                    )
                    cursor.fetchall()  # Consume the result
            conn.commit()
        return jsonify({'status': 'success', 'message': 'Lorry stock updated successfully.'})
    except Error as err:
        return json_error(str(err), 500)


@app.route('/api/lorry/stock/return', methods=['POST'])
def return_lorry_stock():
    """Return stock from the lorry back to the main warehouse."""
    data = request.get_json(silent=True) or {}
    stock_id = data.get('stock_id')   # lorry_stock PK (preferred)
    batch_id  = data.get('batch_id')  # egg_varieties batch_id (fallback)
    vehicle   = data.get('vehicle')
    qty       = int(data.get('qty', 0))

    if qty <= 0:
        return json_error('Quantity must be greater than zero.', 400)
    if not vehicle:
        return json_error('Vehicle number is required.', 400)

    conn = get_db_connection()
    try:
        with conn.cursor(dictionary=True) as cursor:
            # 1. Find the lorry_stock record
            if stock_id:
                cursor.execute(
                    'SELECT stock_id, batch_id, qty FROM lorry_stock WHERE stock_id = %s AND lorry_no = %s',
                    (stock_id, vehicle)
                )
            else:
                cursor.execute(
                    'SELECT stock_id, batch_id, qty FROM lorry_stock WHERE lorry_no = %s AND batch_id = %s',
                    (vehicle, batch_id)
                )
            row = cursor.fetchone()

            if not row:
                return json_error('Lorry stock record not found.', 404)
            if qty > row['qty']:
                return json_error(f"Cannot return more than current lorry stock ({row['qty']} pcs).", 400)

            ls_batch_id = row['batch_id']
            new_lorry_qty = row['qty'] - qty

            # 2. Deduct from lorry_stock
            cursor.execute(
                'UPDATE lorry_stock SET qty = %s WHERE stock_id = %s',
                (new_lorry_qty, row['stock_id'])
            )

            # 3. Return to egg_varieties.
            # lorry_stock.batch_id is an INT FK that always references egg_varieties.batch_id
            # (the INT auto-increment PK). Matching on egg_varieties.id (VARCHAR) with an
            # integer value caused MySQL to convert every id like 'EGG-05' to DOUBLE,
            # triggering error 1292 "Truncated incorrect DOUBLE value". Use batch_id = %s
            # (INT-to-INT) to avoid any implicit type conversion.
            cursor.execute(
                'UPDATE egg_varieties SET qty = qty + %s, last_updated = NOW() WHERE batch_id = %s',
                (qty, ls_batch_id)
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return json_error('Could not find the corresponding warehouse stock entry.', 500)

            conn.commit()

        return jsonify({
            'status': 'success',
            'message': f'{qty} pcs returned to main warehouse.',
            'remaining_on_lorry': new_lorry_qty
        })
    except Error as err:
        return json_error(str(err), 500)

# --- ANALYTICS & REPORTS ---
@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    time_filter = request.args.get('filter', 'all') # daily, weekly, monthly, yearly, all
    
    # MySQL Date Filtering Logic
    where_invoice = "1=1"
    where_damage = "1=1"
    
    if time_filter == 'daily':
        where_invoice = "DATE(FROM_UNIXTIME(timestamp/1000)) = CURDATE()"
        where_damage = "DATE(date) = CURDATE()"
    elif time_filter == 'weekly':
        where_invoice = "YEARWEEK(FROM_UNIXTIME(timestamp/1000), 1) = YEARWEEK(CURDATE(), 1)"
        where_damage = "YEARWEEK(date, 1) = YEARWEEK(CURDATE(), 1)"
    elif time_filter == 'monthly':
        where_invoice = "MONTH(FROM_UNIXTIME(timestamp/1000)) = MONTH(CURDATE()) AND YEAR(FROM_UNIXTIME(timestamp/1000)) = YEAR(CURDATE())"
        where_damage = "MONTH(date) = MONTH(CURDATE()) AND YEAR(date) = YEAR(CURDATE())"
    elif time_filter == 'yearly':
        where_invoice = "YEAR(FROM_UNIXTIME(timestamp/1000)) = YEAR(CURDATE())"
        where_damage = "YEAR(date) = YEAR(CURDATE())"

    try:
        conn = get_db_connection()
        with conn.cursor(dictionary=True) as cursor:
            analytics_data = {}

            # 1. Total Damage Losses
            cursor.execute(f"SELECT COALESCE(SUM(loss_amount), 0.00) as total_loss FROM damages_returns WHERE {where_damage}")
            damage_data = cursor.fetchone()
            analytics_data['total_loss'] = damage_data['total_loss']

            # 2. Summary Metrics
            cursor.execute(f"""
                SELECT 
                    COALESCE(SUM(total), 0.00) AS gross_revenue,
                    COALESCE(SUM(balance), 0.00) AS total_outstanding
                FROM invoices WHERE {where_invoice}
            """)
            analytics_data['summary'] = cursor.fetchone()

            # 3. True Net Profit (Revenue - COGS - Damage Losses)
            cursor.execute(f"""
                SELECT 
                    COALESCE(SUM(i.total), 0.00) AS total_sales,
                    (COALESCE(SUM(i.total), 0.00) - COALESCE(SUM(i.qty * v.buy), 0.00) - %s) AS net_profit
                FROM invoice_items i
                LEFT JOIN egg_varieties v ON i.varietyId = v.id
                JOIN invoices inv ON i.invoice_id = inv.id
                WHERE {where_invoice.replace('timestamp', 'inv.timestamp')}
            """, (analytics_data['total_loss'],))
            analytics_data['profitability'] = cursor.fetchone()

            # 4. Variety Breakdown
            cursor.execute(f"""
                SELECT COALESCE(v.name, i.varietyId) AS name, SUM(i.qty) AS total_units_sold
                FROM invoice_items i
                LEFT JOIN egg_varieties v ON i.varietyId = v.id
                JOIN invoices inv ON i.invoice_id = inv.id
                WHERE {where_invoice.replace('timestamp', 'inv.timestamp')}
                GROUP BY i.varietyId, v.name ORDER BY total_units_sold DESC
            """)
            analytics_data['variety_distribution'] = cursor.fetchall()

            # 5. Sales Timeline
            cursor.execute(f"""
                SELECT date AS calendar_date, COALESCE(SUM(total), 0.00) AS daily_revenue
                FROM invoices WHERE {where_invoice}
                GROUP BY date ORDER BY MAX(timestamp) ASC LIMIT 30
            """)
            analytics_data['sales_timeline'] = cursor.fetchall()

        return jsonify({'status': 'success', 'analytics': analytics_data})
    except Error as err:
        return jsonify({'status': 'error', 'message': str(err)}), 500
    
@app.route('/api/losses', methods=['POST'])
def add_loss():
    try:
        data = request.json
        variety_id = data.get('varietyId')
        qty = int(data.get('qty', 0))
        reason = data.get('reason', '')
        date_str = data.get('date')
        username = data.get('username', '')
        role = data.get('role', 'user')  # Expect 'admin' or 'user'

        if not variety_id or qty <= 0:
            return jsonify({'status': 'error', 'message': 'Invalid variety or quantity'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. Log the damage/return into the losses table
        cursor.execute("""
            INSERT INTO losses (varietyId, qty, reason, date)
            VALUES (%s, %s, %s, %s)
        """, (variety_id, qty, reason, date_str))

        # 2. Conditionally update stock based on role
        if role == 'admin':
            # Admin role: Reduce from the Main Inventory Stock
            cursor.execute("""
                UPDATE egg_varieties 
                SET stock = stock - %s 
                WHERE id = %s
            """, (qty, variety_id))
        else:
            # User role: Reduce from the driver's active Lorry Allocation stock
            if not username:
                return jsonify({'status': 'error', 'message': 'Username is required for user role'}), 400
                
            cursor.execute("""
                UPDATE lorry_allocations 
                SET current_stock = current_stock - %s 
                WHERE username = %s AND varietyId = %s AND status = 'active'
            """, (qty, username, variety_id))
            
            # Double check if any active shift was found and modified
            if cursor.rowcount == 0:
                return jsonify({
                    'status': 'error', 
                    'message': f'No active lorry allocation found for user "{username}" to deduct stock.'
                }), 400

        conn.commit()
        return jsonify({'status': 'success', 'message': 'Loss logged and inventory updated successfully.'})

    except Error as err:
        return jsonify({'status': 'error', 'message': str(err)}), 500
    


# ======================================================================
#  EMAIL NOTIFICATION ENGINE
# ======================================================================

def _get_db_for_notifier():
    """Open a fresh MySQL connection for background-thread use (no Flask g context)."""
    return mysql.connector.connect(
        host=app.config['DB_HOST'],
        user=app.config['DB_USER'],
        password=app.config['DB_PASSWORD'],
        database=app.config['DB_NAME']
    )


def send_email(subject, html_body):
    """Send an HTML email to ADMIN_EMAIL.
    If SMTP_USER / SMTP_PASS are not set, the send is skipped and a
    console message is printed so nothing breaks.
    """
    if not SMTP_USER or not SMTP_PASS:
        print(f"[Email] SMTP not configured - skipping: '{subject}'")
        print("[Email] Set SMTP_USER and SMTP_PASS environment variables to enable emails.")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f"AMM Farm System <{SMTP_USER}>"
        msg['To']      = ADMIN_EMAIL
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ADMIN_EMAIL, msg.as_string())

        print(f"[Email] Sent: '{subject}' to {ADMIN_EMAIL}")
        return True
    except Exception as exc:
        print(f"[Email] Failed '{subject}': {exc}")
        return False


def _email_wrapper(alert_bg, icon_text, heading, subheading, body_html):
    """Render a branded HTML email shell for AMM Farm notifications."""
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body "
        "style=\"margin:0;padding:20px;background:#f9f5f0;font-family:Arial,sans-serif;\">"
        "<div style=\"max-width:620px;margin:0 auto;background:#fff;border-radius:12px;"
        "overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1);\">"

        # brand header
        "<div style=\"background:#3c1a00;padding:22px 28px;color:#fff;\">"
        "<div style=\"font-size:1.2rem;font-weight:800;\">AMM Farm Management</div>"
        "<div style=\"font-size:.78rem;opacity:.75;margin-top:3px;\">Automated Notification System</div>"
        "</div>"

        # alert banner
        f"<div style=\"background:{alert_bg};padding:16px 24px;\">"
        f"<div style=\"font-size:1rem;font-weight:700;color:#1a0a00;\">{icon_text} {heading}</div>"
        f"<div style=\"font-size:.85rem;color:#3c1a00;margin-top:5px;\">{subheading}</div>"
        "</div>"

        # body
        f"<div style=\"padding:24px 28px;\">{body_html}"
        "<p style=\"color:#9ca3af;font-size:.78rem;margin-top:24px;border-top:1px solid #f0ebe3;"
        "padding-top:14px;\">Please log in to the AMM Farm Management System to take action.</p>"
        "</div>"

        # footer
        "<div style=\"background:#f9f5f0;padding:12px 28px;color:#9ca3af;font-size:.74rem;text-align:center;\">"
        "AMM Farm Management System - Automated Alert - Do not reply.</div>"
        "</div></body></html>"
    )


# ── Expired / Overdue Check Payments ──────────────────────────────────────

def check_and_notify_expired_checks():
    """Query for Check Pending bills past their due date and email admin once per bill."""
    global _notified_checks
    try:
        conn = _get_db_for_notifier()
        with conn.cursor(dictionary=True) as cur:
            cur.execute("""
                SELECT id, shop, total, check_due_date
                FROM   invoices
                WHERE  paymentStatus = 'Check Pending'
                  AND  check_due_date <= CURDATE()
                ORDER  BY check_due_date ASC
            """)
            expired = cur.fetchall()
        conn.close()
    except Exception as exc:
        print(f"[Notify] DB error (expired checks): {exc}")
        return

    new_expired = [b for b in expired if b['id'] not in _notified_checks]
    if not new_expired:
        return

    for b in new_expired:
        if b.get('check_due_date') and hasattr(b['check_due_date'], 'strftime'):
            b['check_due_date'] = b['check_due_date'].strftime('%Y-%m-%d')

    th_style  = "padding:9px 12px;text-align:left;color:#78350f;font-weight:700;border-bottom:2px solid #d97706;"
    td_style  = "padding:9px 12px;border-bottom:1px solid #f0ebe3;"

    rows = "".join(
        f"<tr>"
        f"<td style=\"{td_style}font-family:monospace;font-size:.82rem;color:#5c3317;\">{b['id']}</td>"
        f"<td style=\"{td_style}font-weight:600;\">{b['shop']}</td>"
        f"<td style=\"{td_style}text-align:right;font-family:monospace;color:#d97706;font-weight:700;\">"
        f"LKR {float(b['total']):,.2f}</td>"
        f"<td style=\"{td_style}color:#dc2626;font-weight:600;\">{b['check_due_date']}</td>"
        f"</tr>"
        for b in new_expired
    )

    table = (
        "<table style=\"width:100%;border-collapse:collapse;font-size:.88rem;\">"
        "<thead><tr style=\"background:#fef3c7;\">"
        f"<th style=\"{th_style}\">Invoice ID</th>"
        f"<th style=\"{th_style}\">Shop / Customer</th>"
        f"<th style=\"{th_style}text-align:right;\">Amount</th>"
        f"<th style=\"{th_style}\">Due Date</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )

    n       = len(new_expired)
    subject = f"Check Overdue Alert: {n} payment{'s' if n>1 else ''} past due - AMM Farm"
    html    = _email_wrapper(
        alert_bg   = '#fef3c7',
        icon_text  = '[!]',
        heading    = f"{n} Check Payment{'s' if n>1 else ''} Past Due Date",
        subheading = (f"{n} check{'s have' if n>1 else ' has'} passed the clearance date. "
                      "Please accept or reject them in the Accounts tab."),
        body_html  = table
    )

    if send_email(subject, html):
        for b in new_expired:
            _notified_checks.add(b['id'])


# ── Low Stock ─────────────────────────────────────────────────────────────

def check_and_notify_low_stock():
    """Query egg_varieties for items below LOW_STOCK_QTY and email admin once per variety."""
    global _notified_low_stock
    try:
        conn = _get_db_for_notifier()
        with conn.cursor(dictionary=True) as cur:
            cur.execute("""
                SELECT id, name, qty
                FROM   egg_varieties
                WHERE  qty < %s AND qty >= 0
                ORDER  BY qty ASC
            """, (LOW_STOCK_QTY,))
            low_items = cur.fetchall()
        conn.close()
    except Exception as exc:
        print(f"[Notify] DB error (low stock): {exc}")
        return

    # Reset notifications for varieties that recovered above the threshold
    current_low = {item['name'] for item in low_items}
    _notified_low_stock.intersection_update(current_low)

    new_low = [item for item in low_items if item['name'] not in _notified_low_stock]
    if not new_low:
        return

    th_style = "padding:9px 12px;text-align:left;color:#7f1d1d;font-weight:700;border-bottom:2px solid #dc2626;"
    td_style = "padding:9px 12px;border-bottom:1px solid #f0ebe3;"

    def _badge(qty):
        if qty == 0:
            return ('#fee2e2','#dc2626','OUT OF STOCK')
        if qty < 20:
            return ('#fee2e2','#dc2626',f'{qty} pcs')
        return ('#fef3c7','#d97706',f'{qty} pcs')

    rows = "".join(
        f"<tr>"
        f"<td style=\"{td_style}font-family:monospace;font-size:.82rem;color:#5c3317;\">{item['id']}</td>"
        f"<td style=\"{td_style}font-weight:600;\">{item['name']}</td>"
        f"<td style=\"{td_style}text-align:right;\">"
        f"<span style=\"background:{_badge(item['qty'])[0]};color:{_badge(item['qty'])[1]};"
        f"padding:3px 10px;border-radius:20px;font-weight:700;font-family:monospace;font-size:.84rem;\">"
        f"{_badge(item['qty'])[2]}</span></td>"
        f"<td style=\"{td_style}color:#6b7280;font-size:.82rem;\">min: {LOW_STOCK_QTY} pcs</td>"
        f"</tr>"
        for item in new_low
    )

    note = (f"<p style=\"color:#6b7280;font-size:.82rem;margin-top:12px;\">"
            f"You will receive another alert if additional varieties drop below "
            f"<strong>{LOW_STOCK_QTY} pcs</strong>. Recovered varieties reset automatically.</p>")

    table = (
        "<table style=\"width:100%;border-collapse:collapse;font-size:.88rem;\">"
        "<thead><tr style=\"background:#fee2e2;\">"
        f"<th style=\"{th_style}\">Variety ID</th>"
        f"<th style=\"{th_style}\">Name</th>"
        f"<th style=\"{th_style}text-align:right;\">Current Qty</th>"
        f"<th style=\"{th_style}\">Threshold</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>{note}"
    )

    n       = len(new_low)
    subject = f"Low Stock Alert: {n} {'variety' if n==1 else 'varieties'} below {LOW_STOCK_QTY} pcs - AMM Farm"
    html    = _email_wrapper(
        alert_bg   = '#fee2e2',
        icon_text  = '[LOW]',
        heading    = f"Low Stock: {n} Egg {'Variety' if n==1 else 'Varieties'} Below Threshold",
        subheading = (f"{n} egg {'variety is' if n==1 else 'varieties are'} below "
                      f"the minimum stock level of {LOW_STOCK_QTY} pcs. Please restock."),
        body_html  = table
    )

    if send_email(subject, html):
        for item in new_low:
            _notified_low_stock.add(item['name'])


# ── Background Scheduler ──────────────────────────────────────────────────

def _notification_tick():
    """Run both notification checks, then reschedule for the next interval."""
    today = date.today().isoformat()
    print(f"[Notify] Running checks ({today})...")
    try:
        check_and_notify_expired_checks()
    except Exception as exc:
        print(f"[Notify] check_expired_checks error: {exc}")
    try:
        check_and_notify_low_stock()
    except Exception as exc:
        print(f"[Notify] check_low_stock error: {exc}")

    t = threading.Timer(NOTIFY_INTERVAL, _notification_tick)
    t.daemon = True
    t.start()


def start_notification_scheduler():
    """Start the background notification scheduler.
    Safe to call multiple times - only one scheduler thread will run.
    First check fires 30 seconds after startup.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    t = threading.Timer(30, _notification_tick)
    t.daemon = True
    t.start()
    interval_min = NOTIFY_INTERVAL // 60
    print(f"[Notify] Scheduler started - checks every {interval_min} minute(s). Admin email: {ADMIN_EMAIL}")


# ── Manual Trigger Route (useful for testing email config) ─────────────────

@app.route('/api/notifications/trigger', methods=['POST'])
def trigger_notifications():
    """Immediately run all notification checks (bypasses the hourly schedule).
    POST /api/notifications/trigger
    Check server console for results and any email errors.
    """
    def _run():
        check_and_notify_expired_checks()
        check_and_notify_low_stock()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'success', 'message': 'Notification checks triggered. See server console for details.'})

@app.route('/')
def index():
    return send_file('amm_farm_new.html')

# Run ensure_schema at module load — works with both `python app.py` and `gunicorn app:app`
ensure_schema()

# Start background email notification scheduler
start_notification_scheduler()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
