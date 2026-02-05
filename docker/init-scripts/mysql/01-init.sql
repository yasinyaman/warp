-- MySQL Test Database Initialization
-- This script creates sample tables for testing the Auto CRUD API

-- ===================
-- Users Table
-- ===================
CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    uuid CHAR(36) DEFAULT (UUID()) UNIQUE NOT NULL,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    status ENUM('active', 'inactive', 'banned') DEFAULT 'active',
    role ENUM('admin', 'user', 'moderator') DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_users_status (status),
    INDEX idx_users_role (role),
    INDEX idx_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ===================
-- Categories Table
-- ===================
CREATE TABLE categories (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    parent_id INT,
    sort_order INT DEFAULT 0,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE SET NULL,
    INDEX idx_categories_parent (parent_id),
    INDEX idx_categories_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ===================
-- Products Table
-- ===================
CREATE TABLE products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    sku VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    price DECIMAL(10, 2) NOT NULL,
    cost_price DECIMAL(10, 2),
    quantity INT DEFAULT 0,
    category_id INT,
    status ENUM('draft', 'active', 'archived') DEFAULT 'draft',
    is_featured BOOLEAN DEFAULT false,
    metadata JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL,
    INDEX idx_products_category (category_id),
    INDEX idx_products_status (status),
    INDEX idx_products_sku (sku),
    INDEX idx_products_price (price),
    CHECK (price >= 0),
    CHECK (cost_price >= 0 OR cost_price IS NULL),
    CHECK (quantity >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ===================
-- Orders Table
-- ===================
CREATE TABLE orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_number VARCHAR(50) UNIQUE NOT NULL,
    user_id INT,
    status ENUM('pending', 'confirmed', 'processing', 'shipped', 'delivered', 'cancelled') DEFAULT 'pending',
    subtotal DECIMAL(10, 2) NOT NULL,
    tax DECIMAL(10, 2) DEFAULT 0,
    shipping_cost DECIMAL(10, 2) DEFAULT 0,
    total DECIMAL(10, 2) NOT NULL,
    shipping_address JSON,
    billing_address JSON,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    INDEX idx_orders_user (user_id),
    INDEX idx_orders_status (status),
    INDEX idx_orders_number (order_number),
    INDEX idx_orders_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ===================
-- Order Items Table
-- ===================
CREATE TABLE order_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    product_id INT,
    product_name VARCHAR(255) NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(10, 2) NOT NULL,
    total_price DECIMAL(10, 2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL,
    INDEX idx_order_items_order (order_id),
    INDEX idx_order_items_product (product_id),
    CHECK (quantity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ===================
-- Insert Sample Data
-- ===================

-- Users
INSERT INTO users (username, email, password_hash, first_name, last_name, status, role) VALUES
('admin', 'admin@example.com', '$2b$12$hash1', 'Admin', 'User', 'active', 'admin'),
('john_doe', 'john@example.com', '$2b$12$hash2', 'John', 'Doe', 'active', 'user'),
('jane_smith', 'jane@example.com', '$2b$12$hash3', 'Jane', 'Smith', 'active', 'user'),
('bob_wilson', 'bob@example.com', '$2b$12$hash4', 'Bob', 'Wilson', 'inactive', 'user'),
('alice_brown', 'alice@example.com', '$2b$12$hash5', 'Alice', 'Brown', 'active', 'moderator');

-- Categories
INSERT INTO categories (name, slug, description, parent_id, sort_order) VALUES
('Electronics', 'electronics', 'Electronic devices and accessories', NULL, 1),
('Computers', 'computers', 'Desktop and laptop computers', 1, 1),
('Smartphones', 'smartphones', 'Mobile phones and tablets', 1, 2),
('Clothing', 'clothing', 'Apparel and accessories', NULL, 2),
('Men', 'men', 'Men\'s clothing', 4, 1),
('Women', 'women', 'Women\'s clothing', 4, 2);

-- Products
INSERT INTO products (sku, name, description, price, cost_price, quantity, category_id, status, is_featured, metadata) VALUES
('LAPTOP-001', 'Pro Laptop 15"', 'High-performance laptop with 15" display', 1299.99, 900.00, 50, 2, 'active', true, '{"brand": "TechBrand", "warranty": "2 years"}'),
('LAPTOP-002', 'Budget Laptop 14"', 'Affordable laptop for everyday use', 499.99, 350.00, 100, 2, 'active', false, '{"brand": "ValueTech", "warranty": "1 year"}'),
('PHONE-001', 'SmartPhone X', 'Latest flagship smartphone', 999.99, 700.00, 200, 3, 'active', true, '{"brand": "PhoneCo", "storage": "256GB"}'),
('PHONE-002', 'SmartPhone Lite', 'Budget-friendly smartphone', 299.99, 180.00, 300, 3, 'active', false, '{"brand": "PhoneCo", "storage": "64GB"}'),
('SHIRT-001', 'Classic T-Shirt', 'Cotton t-shirt for men', 29.99, 10.00, 500, 5, 'active', false, '{"size": ["S", "M", "L", "XL"], "color": ["white", "black", "blue"]}'),
('DRESS-001', 'Summer Dress', 'Light summer dress for women', 79.99, 35.00, 150, 6, 'active', true, '{"size": ["XS", "S", "M", "L"], "color": ["red", "blue", "green"]}');

-- Orders
INSERT INTO orders (order_number, user_id, status, subtotal, tax, shipping_cost, total, shipping_address, notes) VALUES
('ORD-2024-001', 2, 'delivered', 1299.99, 117.00, 15.00, 1431.99, '{"street": "123 Main St", "city": "New York", "zip": "10001"}', 'Gift wrap please'),
('ORD-2024-002', 3, 'processing', 999.99, 90.00, 0.00, 1089.99, '{"street": "456 Oak Ave", "city": "Los Angeles", "zip": "90001"}', NULL),
('ORD-2024-003', 2, 'pending', 329.98, 29.70, 10.00, 369.68, '{"street": "123 Main St", "city": "New York", "zip": "10001"}', NULL);

-- Order Items
INSERT INTO order_items (order_id, product_id, product_name, quantity, unit_price, total_price) VALUES
(1, 1, 'Pro Laptop 15"', 1, 1299.99, 1299.99),
(2, 3, 'SmartPhone X', 1, 999.99, 999.99),
(3, 4, 'SmartPhone Lite', 1, 299.99, 299.99),
(3, 5, 'Classic T-Shirt', 1, 29.99, 29.99);
