-- PostgreSQL Test Database Initialization
-- This script creates sample tables for testing the Auto CRUD API

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ===================
-- Users Table
-- ===================
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    uuid UUID DEFAULT uuid_generate_v4() UNIQUE NOT NULL,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'banned')),
    role VARCHAR(20) DEFAULT 'user' CHECK (role IN ('admin', 'user', 'moderator')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for common queries
CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_users_email ON users(email);

-- ===================
-- Categories Table
-- ===================
CREATE TABLE categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    sort_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_categories_parent ON categories(parent_id);
CREATE INDEX idx_categories_slug ON categories(slug);

-- ===================
-- Products Table
-- ===================
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    sku VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    price DECIMAL(10, 2) NOT NULL CHECK (price >= 0),
    cost_price DECIMAL(10, 2) CHECK (cost_price >= 0),
    quantity INTEGER DEFAULT 0 CHECK (quantity >= 0),
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    status VARCHAR(20) DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'archived')),
    is_featured BOOLEAN DEFAULT false,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_products_status ON products(status);
CREATE INDEX idx_products_sku ON products(sku);
CREATE INDEX idx_products_price ON products(price);

-- ===================
-- Orders Table
-- ===================
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    order_number VARCHAR(50) UNIQUE NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status VARCHAR(30) DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'processing', 'shipped', 'delivered', 'cancelled')),
    subtotal DECIMAL(10, 2) NOT NULL,
    tax DECIMAL(10, 2) DEFAULT 0,
    shipping_cost DECIMAL(10, 2) DEFAULT 0,
    total DECIMAL(10, 2) NOT NULL,
    shipping_address JSONB,
    billing_address JSONB,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_number ON orders(order_number);
CREATE INDEX idx_orders_created ON orders(created_at);

-- ===================
-- Order Items Table
-- ===================
CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name VARCHAR(255) NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price DECIMAL(10, 2) NOT NULL,
    total_price DECIMAL(10, 2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);

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
('Men', 'men', 'Men''s clothing', 4, 1),
('Women', 'women', 'Women''s clothing', 4, 2);

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

-- Create updated_at trigger function
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to tables with updated_at column
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_products_updated_at BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_orders_updated_at BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Grant permissions (for non-superuser connections)
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO app_user;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO app_user;
