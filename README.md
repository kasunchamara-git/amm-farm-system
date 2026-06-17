-- Create and use the database
CREATE DATABASE IF NOT EXISTS amm_farm_db;
USE amm_farm_db;

-- 1. Customers Table
CREATE TABLE customers (
    id VARCHAR(50) NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(20) DEFAULT NULL,
    email VARCHAR(100) DEFAULT NULL,
    address TEXT DEFAULT NULL
);

-- 2. Damages & Returns Table
CREATE TABLE damages_returns (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    variety_name VARCHAR(100) DEFAULT NULL,
    type ENUM('Damage','Return') DEFAULT NULL,
    qty INT DEFAULT NULL,
    resale_price DECIMAL(10,2) DEFAULT NULL,
    loss_amount DECIMAL(10,2) DEFAULT NULL,
    lorry_no VARCHAR(50) DEFAULT NULL,
    customer_name VARCHAR(100) DEFAULT NULL,
    shop_name VARCHAR(100) DEFAULT NULL
);

-- 3. Egg Varieties (Main Inventory) Table
CREATE TABLE egg_varieties (
    batch_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    id VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    buy DECIMAL(10,2) NOT NULL,
    sell DECIMAL(10,2) NOT NULL,
    qty INT NOT NULL,
    date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- 4. Invoice Items Table
CREATE TABLE invoice_items (
    item_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    invoice_id VARCHAR(50) NOT NULL,
    varietyId VARCHAR(50) NOT NULL,
    varietyName VARCHAR(100) NOT NULL,
    qty INT NOT NULL,
    rate DECIMAL(10,2) NOT NULL,
    total DECIMAL(10,2) NOT NULL,
    KEY fk_invoice (invoice_id)
);

-- 5. Invoices Table
CREATE TABLE invoices (
    id VARCHAR(50) NOT NULL PRIMARY KEY,
    shop VARCHAR(100) NOT NULL,
    date VARCHAR(20) NOT NULL,
    time VARCHAR(20) DEFAULT NULL,
    total DECIMAL(10,2) NOT NULL,
    paidAmount DECIMAL(10,2) NOT NULL,
    balance DECIMAL(10,2) NOT NULL,
    paymentStatus VARCHAR(50) DEFAULT 'Unpaid',
    createdByUser VARCHAR(50) DEFAULT NULL,
    vehicle VARCHAR(50) DEFAULT NULL,
    driver VARCHAR(50) DEFAULT NULL,
    helper VARCHAR(100) DEFAULT NULL,
    timestamp BIGINT DEFAULT NULL,
    payment_method VARCHAR(20) DEFAULT 'Cash',
    check_due_date DATE DEFAULT NULL,
    check_status VARCHAR(20) DEFAULT NULL,
    check_clearance_date DATE DEFAULT NULL
);

-- 6. Lorry Inventory Table
CREATE TABLE lorry_inventory (
    user_id INT NOT NULL,
    item_id INT NOT NULL,
    quantity INT DEFAULT 0,
    loaded_at DATETIME DEFAULT NULL,
    PRIMARY KEY (user_id, item_id)
);

-- 7. Lorry Stock Table
CREATE TABLE lorry_stock (
    stock_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    shift_id VARCHAR(100) DEFAULT NULL,
    lorry_no VARCHAR(50) DEFAULT NULL,
    batch_id INT DEFAULT NULL,
    qty INT NOT NULL,
    KEY fk_batch (batch_id)
);

-- 8. User Sessions Table
CREATE TABLE user_sessions (
    username VARCHAR(50) NOT NULL PRIMARY KEY,
    driver VARCHAR(100) NOT NULL DEFAULT '',
    vehicle VARCHAR(50) NOT NULL DEFAULT '',
    helper VARCHAR(100) NOT NULL DEFAULT '',
    started_at BIGINT NOT NULL
);

-- 9. Users Table
CREATE TABLE users (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('admin','user') NOT NULL DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    current_vehicle VARCHAR(50) DEFAULT NULL,
    current_driver VARCHAR(50) DEFAULT NULL,
    current_helper VARCHAR(100) DEFAULT NULL,
    shift_timestamp BIGINT DEFAULT NULL
);
